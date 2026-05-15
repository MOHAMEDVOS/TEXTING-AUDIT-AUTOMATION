"""
Sentence-transformer embedding helper used by Tier 2 (kNN) and Tier 3 (classifier).

Uses `all-MiniLM-L6-v2` by default — 22 MB on disk, 384-dim embeddings, runs
on CPU at ~3000 sentences/sec. Imports are lazy so the prefilter package
can be imported even when sentence-transformers isn't installed yet.

Two embedding paths:
  • Service path  — when settings.EMBEDDING_SERVICE_URL is set, vectors are
    fetched over HTTP from a long-lived process (the dashboard) that keeps
    the model resident. Audit subprocesses use this so they never pay the
    ~15-20s model load.
  • Local path    — fallback: load the model in-process. Used by CLI runs,
    or transparently if the service is unreachable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.request
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()

# Latched True once the embedding service is found unreachable, so we don't
# retry a dead service on every conversation — we fall back to local for the
# rest of the process lifetime.
_service_unavailable = False


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


# ── Persistent embedding service client ──────────────────────────────────────

def _service_url() -> str:
    """The configured embedding-service base URL, trailing slash stripped."""
    return (getattr(settings, "EMBEDDING_SERVICE_URL", "") or "").rstrip("/")


def _embed_via_service(texts: list[str]) -> Optional[list[list[float]]]:
    """
    POST texts to the persistent embedding service.

    Returns the list of vectors on success, or None to signal that the caller
    should fall back to the local model. Failure is latched: once the service
    is unreachable we stop trying for the rest of the process.
    """
    global _service_unavailable
    url = _service_url()
    if not url or _service_unavailable:
        return None
    try:
        payload = json.dumps({"texts": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{url}/internal/embed_batch",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # 60s allows for a cold service whose first request triggers the load.
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vectors = data.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("malformed response from embedding service")
        return vectors
    except Exception as e:
        logger.warning(
            f"[Prefilter] Embedding service unreachable at {url} ({e}); "
            f"falling back to in-process model for the rest of this run."
        )
        _service_unavailable = True
        return None


# ── Local (in-process) model encoding ────────────────────────────────────────

def _embed_local(text: str) -> Optional[list[float]]:
    """Embed a single string with the in-process model. None if unavailable."""
    model = get_model()
    if model is None:
        return None
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def _embed_batch_local(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed many strings with the in-process model. None if unavailable."""
    if not texts:
        return []
    model = get_model()
    if model is None:
        return None
    vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [v.tolist() for v in vecs]


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
    Embed a single string. Uses the persistent service when configured,
    otherwise the in-process model. Returns None if neither is available.
    """
    via = _embed_via_service([text])
    if via is not None:
        return via[0]
    return _embed_local(text)


def embed_batch(texts: list[str]) -> Optional[list[list[float]]]:
    """Embed many strings at once. Much faster than calling embed() in a loop."""
    if not texts:
        return []
    via = _embed_via_service(texts)
    if via is not None:
        return via
    return _embed_batch_local(texts)
