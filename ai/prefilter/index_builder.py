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
    """
    base_sql = """
    SELECT
        c.id                AS conversation_id,
        cs.compliance_score,
        cs.sentiment_score,
        cs.professionalism_score,
        cs.script_adherence_score,
        cs.red_flags,
        STRING_AGG(
            CASE WHEN LOWER(m.sender) = 'agent' THEN 'AGENT: ' || m.body
                 ELSE 'CONTACT: ' || m.body
            END,
            E'\n' ORDER BY m.sent_at NULLS LAST, m.id
        ) AS conversation_text
    FROM conversations c
    JOIN conversation_scores cs ON cs.conversation_id = c.id
    JOIN contacts ct            ON ct.id = c.contact_id
    LEFT JOIN messages m        ON m.conversation_id = c.id
    WHERE
        cs.model_used IS NOT NULL
        AND cs.model_used <> ''
        AND COALESCE(cs.source, 'groq') NOT IN ('prefilter_t1','prefilter_t2','prefilter_t3')
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
    GROUP BY c.id, cs.compliance_score, cs.sentiment_score,
             cs.professionalism_score, cs.script_adherence_score, cs.red_flags
    HAVING STRING_AGG(m.body, '') IS NOT NULL
    """

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(base_sql)
        return list(cur.fetchall())


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
        meta.append({
            "conversation_id": int(r["conversation_id"]),
            "is_clean": is_clean(r["red_flags"], invalid_patterns),
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
        except Exception:
            pass
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build Tier 2 kNN index.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Regenerate even if index exists.")
    args = parser.parse_args()
    build(rebuild=args.rebuild)
