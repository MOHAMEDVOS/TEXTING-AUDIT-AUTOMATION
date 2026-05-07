"""Tests for Tier 2 embedding kNN prefilter."""
import types
from unittest.mock import patch, MagicMock

import pytest

import ai.prefilter.tier2_embedding as t2
from ai.prefilter._pipeline_types import PipelineResult


def _msgs(*pairs):
    return [{"sender": s, "body": b, "sent_at": None} for s, b in pairs]


# ── _average_scores (pure function) ────────────────────────────────────────

def test_average_scores_single_neighbor():
    neighbors = [{"scores": {
        "compliance_score": 90.0,
        "sentiment_score": 80.0,
        "professionalism_score": 85.0,
        "script_adherence_score": 75.0,
    }}]
    result = t2._average_scores(neighbors)
    assert result["compliance_score"] == 90.0
    assert result["sentiment_score"] == 80.0
    assert result["professionalism_score"] == 85.0
    assert result["script_adherence_score"] == 75.0


def test_average_scores_multiple_neighbors():
    neighbors = [
        {"scores": {"compliance_score": 80.0, "sentiment_score": 70.0,
                    "professionalism_score": 90.0, "script_adherence_score": 60.0}},
        {"scores": {"compliance_score": 100.0, "sentiment_score": 90.0,
                    "professionalism_score": 80.0, "script_adherence_score": 80.0}},
    ]
    result = t2._average_scores(neighbors)
    assert result["compliance_score"] == 90.0
    assert result["sentiment_score"] == 80.0
    assert result["professionalism_score"] == 85.0
    assert result["script_adherence_score"] == 70.0


def test_average_scores_missing_key_falls_back_to_90():
    """Keys absent from all neighbors default to 90.0."""
    neighbors = [{"scores": {}}]
    result = t2._average_scores(neighbors)
    assert result["compliance_score"] == 90.0
    assert result["sentiment_score"] == 90.0


def test_average_scores_partial_key_missing():
    """Key present in some neighbors → average only over present ones."""
    neighbors = [
        {"scores": {"compliance_score": 80.0}},
        {"scores": {}},
    ]
    result = t2._average_scores(neighbors)
    assert result["compliance_score"] == 80.0


# ── evaluate() — index not loaded ──────────────────────────────────────────

def test_evaluate_returns_none_when_index_not_loaded():
    """evaluate() → None when _load_index returns False."""
    msgs = _msgs(("agent", "Hi"), ("contact", "Hello"))
    with patch.object(t2, "_load_index", return_value=False):
        result = t2.evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is None


# ── evaluate() — escalate when any neighbor is flagged ─────────────────────

def _inject_loaded_state(index_mock, meta):
    """Patch module globals to simulate a loaded index."""
    return {
        "_loaded": True,
        "_load_failed": False,
        "_index": index_mock,
        "_index_meta": meta,
    }


def test_evaluate_escalates_when_flagged_neighbor():
    """Any flagged neighbor at high similarity → escalate."""
    import numpy as np

    meta = [
        {"conversation_id": 1, "is_clean": False, "scores": {"compliance_score": 50.0,
         "sentiment_score": 50.0, "professionalism_score": 50.0, "script_adherence_score": 50.0}},
        {"conversation_id": 2, "is_clean": True, "scores": {"compliance_score": 95.0,
         "sentiment_score": 90.0, "professionalism_score": 90.0, "script_adherence_score": 85.0}},
        {"conversation_id": 3, "is_clean": True, "scores": {"compliance_score": 92.0,
         "sentiment_score": 88.0, "professionalism_score": 91.0, "script_adherence_score": 80.0}},
    ]

    fake_index = MagicMock()
    # Return similarities above threshold for all k neighbors
    sims = np.array([[0.97, 0.95, 0.93, 0.91, 0.90]])
    idxs = np.array([[0, 1, 2, -1, -1]])
    fake_index.search.return_value = (sims, idxs)

    msgs = _msgs(("agent", "Hi"), ("contact", "Not interested"))

    with (
        patch.object(t2, "_load_index", return_value=True),
        patch.object(t2, "_index", fake_index, create=True),
        patch.object(t2, "_index_meta", meta, create=True),
        patch("ai.prefilter.tier2_embedding.embedder") as mock_embedder,
    ):
        mock_embedder.conversation_to_text.return_value = "Hi Not interested"
        mock_embedder.embed.return_value = [0.1] * 384

        # Replace module globals directly
        t2._index = fake_index
        t2._index_meta = meta
        t2._loaded = True
        t2._load_failed = False

        result = t2.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    assert result is not None
    assert result.decision == "escalate"
    assert result.tier_hit == 2


def test_evaluate_short_circuits_when_all_neighbors_clean():
    """All clean neighbors at high similarity → short_circuit."""
    import numpy as np

    meta = [
        {"conversation_id": 10, "is_clean": True, "scores": {"compliance_score": 95.0,
         "sentiment_score": 90.0, "professionalism_score": 93.0, "script_adherence_score": 88.0}},
        {"conversation_id": 11, "is_clean": True, "scores": {"compliance_score": 97.0,
         "sentiment_score": 88.0, "professionalism_score": 91.0, "script_adherence_score": 85.0}},
        {"conversation_id": 12, "is_clean": True, "scores": {"compliance_score": 93.0,
         "sentiment_score": 92.0, "professionalism_score": 89.0, "script_adherence_score": 87.0}},
    ]

    fake_index = MagicMock()
    # Three valid neighbors, all above threshold
    sims = np.array([[0.97, 0.96, 0.95, 0.90, 0.88]])
    idxs = np.array([[0, 1, 2, -1, -1]])
    fake_index.search.return_value = (sims, idxs)

    msgs = _msgs(("agent", "Hi"), ("contact", "Sure, tell me more"))

    with patch("ai.prefilter.tier2_embedding.embedder") as mock_embedder:
        mock_embedder.conversation_to_text.return_value = "Hi Sure tell me more"
        mock_embedder.embed.return_value = [0.1] * 384

        t2._index = fake_index
        t2._index_meta = meta
        t2._loaded = True
        t2._load_failed = False

        with patch.object(t2, "_load_index", return_value=True):
            result = t2.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    assert result is not None
    assert result.decision == "short_circuit"
    assert result.tier_hit == 2
    assert result.confidence >= 0.92
    assert result.predicted_scores is not None
