"""
Offline: build the FAISS kNN index used by Tier 2.

Reads every (conversation, conversation_score) pair from Postgres, embeds
the conversation, normalizes the embedding (cosine via inner-product),
and writes a FAISS IndexFlatIP to disk along with a JSON metadata file.

Usage:
    python -m ai.prefilter.index_builder
    python -m ai.prefilter.index_builder --rebuild      # discard cache, re-embed all
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

from config import settings

from . import embedder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("index_builder")


def _connect():
    return psycopg2.connect(settings.DATABASE_URL)


def fetch_training_rows(conn) -> list[dict]:
    """
    Pull every conversation that has a Groq score (not prefilter-sourced).

    Filters applied:
    - Always excludes conversations marked invalid in validation_log
    - When PREFILTER_REQUIRE_VALIDATION=true, also requires an explicit
      'valid' entry in validation_log (opt-in mode for trusted data only)

    Returns rows with a `funnel_tier` field (WF/MF/NF) and a
    `conversation_text` field that is already prefixed with "[WF] " etc.
    so the embedding is funnel-aware.
    """
    base_sql = """
    SELECT
        c.id                AS conversation_id,
        COALESCE(ac.funnel_tier, 'NF') AS funnel_tier,
        cs.compliance_score,
        cs.sentiment_score,
        cs.professionalism_score,
        cs.script_adherence_score,
        cs.red_flags,
        STRING_AGG(
            CASE WHEN LOWER(m.sender) = LOWER(COALESCE(ac.name,'agent'))
                      OR LOWER(m.sender) = 'agent'
                 THEN 'AGENT: ' || m.body
                 ELSE 'CONTACT: ' || m.body
            END,
            E'\n' ORDER BY m.sent_at NULLS LAST, m.id
        ) AS conversation_text
    FROM conversations c
    JOIN conversation_scores cs ON cs.conversation_id = c.id
    JOIN contacts ct            ON ct.id = c.contact_id
    LEFT JOIN accounts ac       ON ac.id = c.agent_id
    LEFT JOIN messages m        ON m.conversation_id = c.id
    WHERE
        cs.model_used IS NOT NULL
        AND cs.model_used <> ''
        AND COALESCE(cs.source, 'groq') NOT IN ('prefilter_t1','prefilter_t2','prefilter_t3')
        -- Also include T4 results (deterministic, high-quality)
        -- OR cs.source = 'prefilter_t4'
        -- Never train on conversations the manager marked invalid
        AND NOT EXISTS (
            SELECT 1 FROM validation_log vl
            WHERE vl.agent_id = c.agent_id
              AND LOWER(vl.contact_name) = LOWER(ct.name)
              AND vl.status = 'invalid'
        )
    """

    if settings.PREFILTER_REQUIRE_VALIDATION:
        # Require explicit manager confirmation before including in index
        base_sql += """
        AND EXISTS (
            SELECT 1 FROM validation_log vl
            WHERE vl.agent_id = c.agent_id
              AND LOWER(vl.contact_name) = LOWER(ct.name)
              AND vl.status = 'valid'
        )
        """

    base_sql += """
    GROUP BY c.id, ac.funnel_tier, ac.name,
             cs.compliance_score, cs.sentiment_score,
             cs.professionalism_score, cs.script_adherence_score, cs.red_flags
    HAVING STRING_AGG(m.body, '') IS NOT NULL
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(base_sql)
        rows = list(cur.fetchall())

    # Prefix each conversation text with its funnel tier tag so the embedding
    # is funnel-aware. Inference time must apply the same prefix (see tier2_embedding.py).
    for row in rows:
        ft = (row.get("funnel_tier") or "NF").upper().strip()
        if ft not in ("WF", "MF", "NF"):
            ft = "NF"
        row["funnel_tier"] = ft
        row["conversation_text"] = f"[{ft}]\n{row['conversation_text']}"

    return rows


def fetch_promoted_candidates(conn) -> list[dict]:
    """
    Fetch promoted semantic candidates to include in the training set.
    These are high-quality conversations captured by the auto-learner.
    """
    sql = """
    SELECT
        sc.conversation_id,
        'NF' AS funnel_tier,
        sc.compliance_score,
        sc.sentiment_score,
        sc.professionalism_score,
        sc.script_adherence_score,
        '[]'::jsonb AS red_flags,
        STRING_AGG(
            CASE WHEN LOWER(m.sender) = LOWER(COALESCE(ac.name,'agent'))
                      OR LOWER(m.sender) = 'agent'
                 THEN 'AGENT: ' || m.body
                 ELSE 'CONTACT: ' || m.body
            END,
            E'\n' ORDER BY m.sent_at NULLS LAST, m.id
        ) AS conversation_text
    FROM semantic_candidates sc
    JOIN conversations c     ON c.id = sc.conversation_id
    LEFT JOIN accounts ac    ON ac.id = c.agent_id
    LEFT JOIN messages m     ON m.conversation_id = c.id
    WHERE sc.promoted = TRUE
      AND sc.rejected = FALSE
      AND sc.is_clean = TRUE
    GROUP BY sc.conversation_id, sc.compliance_score, sc.sentiment_score,
             sc.professionalism_score, sc.script_adherence_score, ac.name
    HAVING STRING_AGG(m.body, '') IS NOT NULL
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = list(cur.fetchall())

    for row in rows:
        ft = (row.get("funnel_tier") or "NF").upper().strip()
        row["funnel_tier"] = ft
        row["conversation_text"] = f"[{ft}]\n{row['conversation_text']}"

    return rows


def fetch_negative_example_ids(conn) -> set[int]:
    """
    Return conversation_ids where every Groq flag was marked invalid by a human.
    These become hard negatives (sample_weight=2.0) during classifier training.

    "All rejected" = conversation_scores.red_flags is now empty AND at least one
    flag_feedback row points at this conversation with status='invalid'.
    """
    sql = """
    SELECT DISTINCT ff.conversation_id
    FROM flag_feedback ff
    JOIN conversation_scores cs ON cs.conversation_id = ff.conversation_id
    WHERE ff.conversation_id IS NOT NULL
      AND ff.status = 'invalid'
      AND (cs.red_flags IS NULL OR cs.red_flags::text = '[]' OR cs.red_flags::text = 'null')
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return {row[0] for row in cur.fetchall() if row[0] is not None}


def fetch_invalid_flag_patterns(conn) -> set[str]:
    """flag_feedback rows with status='invalid' — used to mask false-positive flags."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT LOWER(red_flag) FROM flag_feedback WHERE status = 'invalid'"
        )
        return {row[0].strip() for row in cur.fetchall() if row[0]}


def is_clean(red_flags_json, invalid_patterns: set[str]) -> bool:
    """A conversation is 'clean' iff zero remaining flags after masking."""
    flags = red_flags_json or []
    if isinstance(flags, str):
        try:
            flags = json.loads(flags)
        except Exception:
            flags = []
    real_flags = [
        f for f in flags
        if isinstance(f, str) and f.strip()
        and f.strip().lower() not in invalid_patterns
    ]
    return len(real_flags) == 0


def build(rebuild: bool = False) -> None:
    settings.PREFILTER_DIR.mkdir(parents=True, exist_ok=True)

    if not rebuild and Path(settings.PREFILTER_INDEX_PATH).exists():
        logger.info(
            f"Index already exists at {settings.PREFILTER_INDEX_PATH}. "
            f"Use --rebuild to regenerate."
        )
        return

    try:
        import faiss
    except ImportError:
        logger.error("faiss-cpu not installed. Install with: pip install faiss-cpu")
        sys.exit(1)

    model = embedder.get_model()
    if model is None:
        logger.error("sentence-transformers not installed.")
        sys.exit(1)

    logger.info("Connecting to Postgres...")
    conn = _connect()
    try:
        invalid_patterns = fetch_invalid_flag_patterns(conn)
        logger.info(f"Loaded {len(invalid_patterns)} known-invalid flag patterns")

        rows = fetch_training_rows(conn)
        logger.info(f"Fetched {len(rows)} scored conversations from DB")

        # Merge promoted semantic candidates (auto-learned)
        promoted = fetch_promoted_candidates(conn)
        if promoted:
            # Deduplicate by conversation_id
            existing_ids = {r["conversation_id"] for r in rows}
            new_promoted = [p for p in promoted if p["conversation_id"] not in existing_ids]
            rows.extend(new_promoted)
            logger.info(f"Added {len(new_promoted)} promoted semantic candidates to training set")
    finally:
        conn.close()

    if not rows:
        logger.error("No training rows. Run a Groq audit first to populate conversation_scores.")
        sys.exit(1)

    texts = [r["conversation_text"] for r in rows]
    logger.info("Embedding conversations (this may take a minute)...")
    vectors = embedder.embed_batch(texts)
    if vectors is None:
        logger.error("Embedding failed.")
        sys.exit(1)

    arr = np.asarray(vectors, dtype=np.float32)
    dim = arr.shape[1]
    logger.info(f"Built embedding matrix: {arr.shape}")

    # Inner product on L2-normalized vectors == cosine similarity.
    index = faiss.IndexFlatIP(dim)
    index.add(arr)
    logger.info(f"Built FAISS index with {index.ntotal} vectors, dim={dim}")

    meta = []
    for r in rows:
        # Parse red_flags for metadata storage
        raw_flags = r.get("red_flags") or []
        if isinstance(raw_flags, str):
            try:
                raw_flags = json.loads(raw_flags)
            except Exception:
                raw_flags = []
        clean_flags = [
            f for f in (raw_flags or [])
            if isinstance(f, str) and f.strip()
            and f.strip().lower() not in invalid_patterns
        ]

        meta.append({
            "conversation_id": int(r["conversation_id"]),
            "funnel_tier": r.get("funnel_tier", "NF"),
            "is_clean": len(clean_flags) == 0,
            "red_flags": clean_flags,  # ← NEW: store actual flags for T3 multi-label
            "scores": {
                "compliance_score": r["compliance_score"],
                "sentiment_score": r["sentiment_score"],
                "professionalism_score": r["professionalism_score"],
                "script_adherence_score": r["script_adherence_score"],
            },
        })

    faiss.write_index(index, str(settings.PREFILTER_INDEX_PATH))
    with open(settings.PREFILTER_INDEX_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f)

    clean_count = sum(1 for m in meta if m["is_clean"])
    logger.info(
        f"Wrote index → {settings.PREFILTER_INDEX_PATH}\n"
        f"Wrote meta  → {settings.PREFILTER_INDEX_META_PATH}\n"
        f"Total: {len(meta)} convos | Clean: {clean_count} | "
        f"Flagged: {len(meta) - clean_count}"
    )

    # ── Write manifest.json (merge with existing if present) ─────────
    import datetime
    manifest_path = settings.PREFILTER_DIR / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as _e:
            logger.debug("swallowed: %r", _e)
    manifest["knn_index"] = {
        "built_at": datetime.datetime.utcnow().isoformat() + "Z",
        "n_vectors": len(meta),
        "n_clean": clean_count,
        "n_flagged": len(meta) - clean_count,
        "embedding_model": settings.PREFILTER_EMBEDDING_MODEL,
        "dimension": dim,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    logger.info(f"Updated manifest → {manifest_path}")


def main(rebuild: bool = True) -> None:
    """Programmatic entrypoint used by semantic_learner. Defaults to rebuild=True."""
    build(rebuild=rebuild)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Tier 2 kNN index.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Regenerate even if index exists.")
    args = parser.parse_args()
    build(rebuild=args.rebuild)
