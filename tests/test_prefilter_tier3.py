"""Tests for Tier 3 classifier prefilter."""
import numpy as np
from unittest.mock import patch, MagicMock

import pytest

import ai.prefilter.tier3_classifier as t3
from ai.prefilter._pipeline_types import PipelineResult
from config import settings


def _msgs(*pairs):
    return [{"sender": s, "body": b, "sent_at": None} for s, b in pairs]


def _make_bundle(flag_prob: float, scores: list[float]) -> dict:
    """Build a fake joblib bundle with controllable outputs."""
    flag_clf = MagicMock()
    flag_clf.predict_proba.return_value = np.array([[1.0 - flag_prob, flag_prob]])

    score_reg = MagicMock()
    score_reg.predict.return_value = np.array([scores])

    return {
        "flag_clf": flag_clf,
        "score_reg": score_reg,
        "feature_dim": 384,
        "model_name": "all-MiniLM-L6-v2",
        "trained_at": "2026-04-24",
    }


# ── evaluate() — bundle not loaded ─────────────────────────────────────────

def test_evaluate_returns_none_when_bundle_not_loaded():
    msgs = _msgs(("agent", "Hi"), ("contact", "Sure"))
    with patch.object(t3, "_load_bundle", return_value=False):
        result = t3.evaluate(msgs, agent_name="Agent", contact_name="Bob")
    assert result is None


# ── decision logic — flag_prob low + scores high → short_circuit ───────────

def test_evaluate_short_circuits_when_flag_prob_low_and_scores_high():
    """flag_prob below threshold AND all scores above min → short_circuit."""
    # Use values safely below PREFILTER_T3_MAX_FLAG_PROB (0.15) and above
    # PREFILTER_T3_MIN_SCORE (75).
    flag_prob = 0.05
    scores = [95.0, 90.0, 88.0, 85.0]  # comp, sent, prof, script

    bundle = _make_bundle(flag_prob, scores)

    msgs = _msgs(("agent", "Hi"), ("contact", "Sure"))

    with (
        patch.object(t3, "_load_bundle", return_value=True),
        patch("ai.prefilter.tier3_classifier.embedder") as mock_emb,
    ):
        mock_emb.conversation_to_text.return_value = "Hi Sure"
        mock_emb.embed.return_value = [0.1] * 384

        t3._bundle = bundle
        t3._load_failed = False

        result = t3.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    assert result is not None
    assert result.decision == "short_circuit"
    assert result.tier_hit == 3
    assert result.result is not None
    assert result.result["red_flags"] == []


# ── decision logic — flag_prob high → escalate ─────────────────────────────

def test_evaluate_escalates_when_flag_prob_high():
    """flag_prob above threshold → escalate even if scores are fine."""
    flag_prob = 0.80  # well above 0.15
    scores = [95.0, 90.0, 88.0, 85.0]

    bundle = _make_bundle(flag_prob, scores)
    msgs = _msgs(("agent", "Hi"), ("contact", "Not interested"))

    with (
        patch.object(t3, "_load_bundle", return_value=True),
        patch("ai.prefilter.tier3_classifier.embedder") as mock_emb,
    ):
        mock_emb.conversation_to_text.return_value = "Hi Not interested"
        mock_emb.embed.return_value = [0.1] * 384

        t3._bundle = bundle
        t3._load_failed = False

        result = t3.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    assert result is not None
    assert result.decision == "escalate"
    assert result.tier_hit == 3


# ── decision logic — score below threshold → escalate ──────────────────────

def test_evaluate_escalates_when_any_score_below_threshold():
    """Even low flag_prob → escalate if any predicted score is below min (75)."""
    flag_prob = 0.05  # safe
    # compliance is below PREFILTER_T3_MIN_SCORE (75)
    scores = [60.0, 90.0, 88.0, 85.0]

    bundle = _make_bundle(flag_prob, scores)
    msgs = _msgs(("agent", "Hi"), ("contact", "Sure"))

    with (
        patch.object(t3, "_load_bundle", return_value=True),
        patch("ai.prefilter.tier3_classifier.embedder") as mock_emb,
    ):
        mock_emb.conversation_to_text.return_value = "Hi Sure"
        mock_emb.embed.return_value = [0.1] * 384

        t3._bundle = bundle
        t3._load_failed = False

        result = t3.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    assert result is not None
    assert result.decision == "escalate"
    assert result.tier_hit == 3


# ── edge case — empty text → None ──────────────────────────────────────────

def test_evaluate_returns_none_for_empty_text():
    msgs = _msgs(("agent", "   "), ("contact", "  "))

    with (
        patch.object(t3, "_load_bundle", return_value=True),
        patch("ai.prefilter.tier3_classifier.embedder") as mock_emb,
    ):
        mock_emb.conversation_to_text.return_value = "   "
        mock_emb.embed.return_value = None  # shouldn't matter since text.strip() is empty

        t3._bundle = _make_bundle(0.05, [95.0, 90.0, 88.0, 85.0])
        t3._load_failed = False

        result = t3.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    # strip() is empty → returns None before inference
    assert result is None


# ── edge case — embed returns None → None ──────────────────────────────────

def test_evaluate_returns_none_when_embed_fails():
    msgs = _msgs(("agent", "Hi"), ("contact", "Sure"))

    with (
        patch.object(t3, "_load_bundle", return_value=True),
        patch("ai.prefilter.tier3_classifier.embedder") as mock_emb,
    ):
        mock_emb.conversation_to_text.return_value = "Hi Sure"
        mock_emb.embed.return_value = None  # embedder failed

        t3._bundle = _make_bundle(0.05, [95.0, 90.0, 88.0, 85.0])
        t3._load_failed = False

        result = t3.evaluate(msgs, agent_name="Agent", contact_name="Bob")

    assert result is None
