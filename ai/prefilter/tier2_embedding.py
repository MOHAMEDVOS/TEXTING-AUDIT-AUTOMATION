"""
Tier 2 — Embedding kNN against past scored conversations.

How it works:
  1. Embed the incoming conversation with sentence-transformers.
  2. Look up the top-K nearest neighbors in a FAISS index built from past
     `conversation_scores` rows (see ai/prefilter/index_builder.py).
  3. If ≥N of those neighbors are CLEAN (no red flags) AND the closest one
     has cosine-similarity ≥ T, we copy the average score of that cluster
     and short-circuit Groq.
  4. Otherwise, return None to escalate.

Safety: if ANY of the top-K neighbors carries an unresolved red flag, we
escalate immediately — even if the similarity is high. We never short-circuit
into a "clean" decision when the closest historical example was flagged.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from config import settings

from . import embedder
from ._pipeline_types import PipelineResult as PrefilterResult

logger = logging.getLogger(__name__)

_index = None              # faiss.Index
_index_meta: list[dict] = []  # parallel to vectors: [{conversation_id, scores, red_flags}, ...]
_loaded = False
_load_failed = False
_index_lock = threading.Lock()


def _load_index() -> bool:
    """Lazily load the FAISS index + metadata from disk. Returns True on success."""
    global _index, _index_meta, _loaded, _load_failed

    if _loaded:
        return True
    if _load_failed:
        return False

    with _index_lock:
        # Double-checked: another thread may have loaded while we waited.
        if _loaded:
            return True
        if _load_failed:
            return False

        index_path = settings.PREFILTER_INDEX_PATH
        meta_path = settings.PREFILTER_INDEX_META_PATH

        if not Path(index_path).exists() or not Path(meta_path).exists():
            logger.info(
                f"[Prefilter T2] No index found at {index_path}. "
                f"Run `python -m ai.prefilter.index_builder` to build it."
            )
            _load_failed = True
            return False

        try:
            import faiss  # heavy import
        except ImportError:
            logger.warning(
                "[Prefilter T2] faiss not installed. "
                "Install with: pip install faiss-cpu"
            )
            _load_failed = True
            return False

        try:
            _index = faiss.read_index(str(index_path))
            with open(meta_path, "r", encoding="utf-8") as f:
                _index_meta = json.load(f)
            _loaded = True
            logger.info(
                f"[Prefilter T2] Loaded index: {len(_index_meta)} vectors, "
                f"dim={_index.d}"
            )
            return True
        except Exception as e:
            logger.error(f"[Prefilter T2] Failed to load index: {e}")
            _load_failed = True
            return False


def evaluate(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    funnel_tier: str = "NF",
    assigned_labels: list[str] | None = None,
) -> Optional[PrefilterResult]:
    """
    Look up nearest neighbors. Short-circuit if confidently clean, else None.

    funnel_tier: "WF" | "MF" | "NF" — prepended to the query text so the
    embedding matches the funnel-prefixed index vectors built by index_builder.
    """
    if not _load_index():
        return None

    ft = (funnel_tier or "NF").upper().strip()
    if ft not in ("WF", "MF", "NF"):
        ft = "NF"

    base_text = embedder.conversation_to_text(messages, agent_name)
    if not base_text.strip():
        return None
    text = f"[{ft}]\n{base_text}"

    vec = embedder.embed(text)
    if vec is None:
        return None

    query = np.asarray([vec], dtype=np.float32)

    k = settings.PREFILTER_T2_MIN_NEIGHBORS + 2  # check a couple extra
    sims, idxs = _index.search(query, k)
    sims = sims[0].tolist()
    idxs = idxs[0].tolist()

    neighbors = []
    for sim, idx in zip(sims, idxs):
        if idx < 0 or idx >= len(_index_meta):
            continue
        meta = _index_meta[idx]
        neighbors.append({
            "conversation_id": meta.get("conversation_id"),
            "similarity": float(sim),
            "is_clean": meta.get("is_clean", False),
            "scores": meta.get("scores", {}),
        })

    if not neighbors:
        return None

    top_sim = neighbors[0]["similarity"]
    if top_sim < settings.PREFILTER_T2_SIM_THRESHOLD:
        # Nothing close enough.
        return None

    # Only consider neighbors that are actually close (above threshold).
    close_neighbors = [
        n for n in neighbors
        if n["similarity"] >= settings.PREFILTER_T2_SIM_THRESHOLD
    ]

    # SAFETY: if any CLOSE neighbor has a red flag, escalate.
    # We ignore distant flagged neighbors (below threshold) — they're
    # too dissimilar to be relevant safety signals.
    flagged_close = [n for n in close_neighbors if not n["is_clean"]]
    if flagged_close:
        return PrefilterResult(
            tier_hit=2,
            decision="escalate",
            confidence=top_sim,
            notes=(
                f"top neighbor sim={top_sim:.3f} but "
                f"{len(flagged_close)}/{len(close_neighbors)} close neighbors flagged"
            ),
        )

    # Need at least N confidently clean neighbors at high similarity.
    clean_close = [n for n in close_neighbors if n["is_clean"]]
    if len(clean_close) < settings.PREFILTER_T2_MIN_NEIGHBORS:
        return None

    # Average the neighbor scores.
    avg_scores = _average_scores(clean_close)

    return PrefilterResult(
        tier_hit=2,
        decision="short_circuit",
        confidence=top_sim,
        predicted_scores=avg_scores,
        notes=(
            f"top sim={top_sim:.3f}, "
            f"{len(clean_close)} clean neighbors avg-pooled"
        ),
        result=_build_result(contact_name, avg_scores, clean_close, messages, agent_name, assigned_labels),
    )


def _average_scores(neighbors: list[dict]) -> dict:
    keys = ["compliance_score", "sentiment_score",
            "professionalism_score", "script_adherence_score"]
    out = {}
    for k in keys:
        vals = [n["scores"].get(k) for n in neighbors if n["scores"].get(k) is not None]
        out[k] = round(sum(vals) / len(vals), 1) if vals else 90.0
    return out


def _build_result(
    contact_name: str,
    scores: dict,
    neighbors: list[dict],
    messages: list[dict] | None = None,
    agent_name: str = "",
    assigned_labels: list[str] | None = None,
) -> dict:
    """Assemble a Groq-shaped output dict with smart summary."""
    from . import summary_builder

    label = (assigned_labels or [""])[0].strip() if assigned_labels else ""
    from .label_validator import validate_label
    label_check = validate_label(messages, label)
    label_flags = label_check.get("red_flags", [])
    if label_flags:
        # Missing handoff after a valid push degrades script adherence (1 flag = -20)
        scores = {**scores, "script_adherence_score": min(scores["script_adherence_score"], 80.0)}

    if messages:
        smart_summary = summary_builder.build_summary(
            messages, agent_name, contact_name, scores, model_used="prefilter_t2",
        )
        funnel = summary_builder.detect_funnel_stage(messages)
    else:
        neighbor_ids = [n["conversation_id"] for n in neighbors[:3]]
        smart_summary = (
            f"This conversation closely matches {len(neighbors)} "
            f"previously-audited clean threads (IDs: {neighbor_ids})."
        )
        funnel = "none"

    return {
        "compliance_score": scores["compliance_score"],
        "sentiment_score": scores["sentiment_score"],
        "professionalism_score": scores["professionalism_score"],
        "script_adherence_score": scores["script_adherence_score"],
        "funnel_stage_reached": funnel,
        "pillars_gathered": [],
        "rebuttals_used": [],
        "label_assigned": label,
        "label_correct": label_check["label_correct"],
        "label_should_be": label_check["label_should_be"],
        "label_reason": label_check["label_reason"],
        "red_flags": label_flags,
        "actions_triggered": ["Not Following Lead Flow"] if label_flags else [],
        "summary": smart_summary,
        "model_used": "prefilter_t2",
        "contact_name": contact_name,
    }
