"""
Sentence-transformer embedding helper used by Tier 2 (kNN) and Tier 3 (classifier).

Uses `all-MiniLM-L6-v2` by default — 22 MB on disk, 384-dim embeddings, runs
on CPU at ~3000 sentences/sec. Imports are lazy so the prefilter package
can be imported even when sentence-transformers isn't installed yet.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()


def get_model():
    """
    Lazily load the sentence-transformer model. Thread-safe and process-local.
    Returns None if sentence-transformers is not installed.
    """
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer  # heavy import
        except ImportError:
            logger.warning(
                "[Prefilter] sentence-transformers not installed — Tier 2/3 disabled. "
                "Install with: pip install sentence-transformers"
            )
            return None
        logger.info(
            f"[Prefilter] Loading embedding model: {settings.PREFILTER_EMBEDDING_MODEL}"
        )
        _model = SentenceTransformer(settings.PREFILTER_EMBEDDING_MODEL)
        logger.info("[Prefilter] Embedding model loaded.")
        return _model


def conversation_to_text(messages: list[dict], agent_name: str = "Agent") -> str:
    """
    Flatten a conversation into the form we embed. Same format used both at
    index-build time and at inference time so the embedding distribution matches.
    """
    parts: list[str] = []
    for m in messages:
        sender = (m.get("sender") or "").lower()
        role = "AGENT" if sender == "agent" else "CONTACT"
        # Handle both "body" (scraper/test) and "message" (production DB alias)
        body = (m.get("body") or m.get("message") or "").strip()
        if not body:
            continue
        parts.append(f"{role}: {body}")
    return "\n".join(parts)


def text_hash(text: str) -> str:
    """SHA-256 of the embedded text — used as the cache key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embed(text: str) -> Optional[list[float]]:
    """
    Embed a single string. Returns None if the model isn't available.
    """
    model = get_model()
    if model is None:
        return None
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def embed_batch(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed many strings at once. Much faster than calling embed() in a loop."""
    if not texts:
        return []
    model = get_model()
    if model is None:
        return None
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]
