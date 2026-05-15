"""
Persistent embedding service — Solution B.

Hosts the sentence-transformer model resident inside a long-lived process
(the dashboard) so audit subprocesses can fetch embeddings over HTTP instead
of each paying the ~15-20s model load on first score.

Subprocesses opt in via the EMBEDDING_SERVICE_URL env var (set by the
dashboard when it spawns them — see dashboard/app.py). If the service is
down, embedder.py transparently falls back to loading the model in-process.

Endpoints are deliberately mounted under /internal — they are meant for
local subprocess traffic, not public callers.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from . import embedder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["embedding-service"])


class EmbedRequest(BaseModel):
    text: str


class EmbedBatchRequest(BaseModel):
    texts: list[str]


@router.get("/embed/health")
def embed_health() -> dict:
    """Report whether the embedding model is loaded and resident."""
    return {"ready": embedder._model is not None}


@router.post("/embed")
def embed_one(body: EmbedRequest) -> dict:
    """Embed a single string. Sync def → FastAPI runs it in its threadpool."""
    return {"vector": embedder._embed_local(body.text)}


@router.post("/embed_batch")
def embed_batch(body: EmbedBatchRequest) -> dict:
    """Embed many strings at once. Sync def → runs in FastAPI's threadpool."""
    return {"vectors": embedder._embed_batch_local(body.texts)}


def warmup() -> None:
    """
    Force the embedding model + prefilter artifacts to load now.

    Call this once at dashboard startup (in a background thread) so the first
    scoring request served by the service is instant instead of paying the
    cold-load cost. Never raises — warmup failure just means the first real
    request loads lazily.
    """
    try:
        logger.info("[EmbeddingService] Warming up embedding model...")
        embedder.get_model()
        # Warm the FAISS index and classifier too — cheap loads, but this
        # avoids a first-request stall on Tier 2 / Tier 3.
        try:
            from . import tier2_embedding
            tier2_embedding._load_index()
        except Exception as e:
            logger.debug(f"[EmbeddingService] T2 index warmup skipped: {e}")
        try:
            from . import tier3_classifier
            tier3_classifier._load_classifier()
        except Exception as e:
            logger.debug(f"[EmbeddingService] T3 classifier warmup skipped: {e}")
        logger.info("[EmbeddingService] Warmup complete — model resident.")
    except Exception as e:
        logger.error(f"[EmbeddingService] Warmup failed (will load lazily): {e}")
