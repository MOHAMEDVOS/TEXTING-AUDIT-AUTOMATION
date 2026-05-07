"""Tests for the prefilter pipeline orchestrator."""
from unittest.mock import patch, MagicMock

import pytest

from ai.prefilter import pipeline
from ai.prefilter._pipeline_types import PipelineResult


def _msgs(*pairs):
    return [{"sender": s, "body": b, "sent_at": None} for s, b in pairs]


MSGS = _msgs(("agent", "Hi there"), ("contact", "Hello"))

FAKE_RESULT_DICT = {
    "compliance_score": 95,
    "sentiment_score": 88,
    "professionalism_score": 90,
    "script_adherence_score": 85,
    "red_flags": [],
    "model_used": "prefilter_t1",
    "contact_name": "Bob",
}


# ── PREFILTER_ENABLED=False → always None ───────────────────────────────────

def test_run_prefilter_disabled_returns_none():
    with patch("ai.prefilter.pipeline.settings") as mock_settings:
        mock_settings.PREFILTER_ENABLED = False
        mock_settings.PREFILTER_SHADOW_MODE = False
        mock_settings.PREFILTER_T1_LIVE = True
        mock_settings.PREFILTER_T2_LIVE = False
        mock_settings.PREFILTER_T3_LIVE = False

        result = pipeline.run_prefilter(MSGS, "Agent", "Bob")

    assert result is None


# ── Shadow mode → always None even when a tier short-circuits ───────────────

def test_run_prefilter_shadow_mode_always_returns_none():
    """In shadow mode, even a short-circuit tier returns None so Groq still runs."""
    t1_decision = PipelineResult(
        tier_hit=1,
        decision="short_circuit",
        confidence=0.95,
        result=FAKE_RESULT_DICT,
    )

    with (
        patch("ai.prefilter.pipeline.settings") as mock_settings,
        patch.object(pipeline.tier1_phrases, "evaluate", return_value=t1_decision),
    ):
        mock_settings.PREFILTER_ENABLED = True
        mock_settings.PREFILTER_SHADOW_MODE = True
        mock_settings.PREFILTER_T1_LIVE = True
        mock_settings.PREFILTER_T2_LIVE = False
        mock_settings.PREFILTER_T3_LIVE = False

        result = pipeline.run_prefilter(MSGS, "Agent", "Bob")

    assert result is None


# ── Live mode: Tier 1 short-circuits → returns result dict ──────────────────

def test_run_prefilter_live_tier1_short_circuit_returns_dict():
    """T1 short-circuits + live mode → caller gets the result dict, not None."""
    t1_decision = PipelineResult(
        tier_hit=1,
        decision="short_circuit",
        confidence=0.95,
        result=FAKE_RESULT_DICT,
    )

    with (
        patch("ai.prefilter.pipeline.settings") as mock_settings,
        patch.object(pipeline.tier1_phrases, "evaluate", return_value=t1_decision),
    ):
        mock_settings.PREFILTER_ENABLED = True
        mock_settings.PREFILTER_SHADOW_MODE = False
        mock_settings.PREFILTER_T1_LIVE = True
        mock_settings.PREFILTER_T2_LIVE = False
        mock_settings.PREFILTER_T3_LIVE = False

        result = pipeline.run_prefilter(MSGS, "Agent", "Bob")

    assert result is FAKE_RESULT_DICT


# ── No tier reaches a decision → returns None ───────────────────────────────

def test_run_prefilter_no_tier_decision_returns_none():
    """All tiers pass through (return None) → escalate to Groq → None."""
    with (
        patch("ai.prefilter.pipeline.settings") as mock_settings,
        patch.object(pipeline.tier1_phrases, "evaluate", return_value=None),
    ):
        # T2 and T3 disabled in live mode and not in shadow → won't be called
        mock_settings.PREFILTER_ENABLED = True
        mock_settings.PREFILTER_SHADOW_MODE = False
        mock_settings.PREFILTER_T1_LIVE = True
        mock_settings.PREFILTER_T2_LIVE = False
        mock_settings.PREFILTER_T3_LIVE = False

        result = pipeline.run_prefilter(MSGS, "Agent", "Bob")

    assert result is None


# ── Empty messages → always None ────────────────────────────────────────────

def test_run_prefilter_empty_messages_returns_none():
    with patch("ai.prefilter.pipeline.settings") as mock_settings:
        mock_settings.PREFILTER_ENABLED = True
        mock_settings.PREFILTER_SHADOW_MODE = False
        mock_settings.PREFILTER_T1_LIVE = True
        mock_settings.PREFILTER_T2_LIVE = False
        mock_settings.PREFILTER_T3_LIVE = False

        result = pipeline.run_prefilter([], "Agent", "Bob")

    assert result is None


# ── T1 live kill-switch off → short_circuit result is suppressed ────────────

def test_run_prefilter_t1_live_false_suppresses_short_circuit():
    """T1 short-circuits but T1_LIVE=False → still returns None."""
    t1_decision = PipelineResult(
        tier_hit=1,
        decision="short_circuit",
        confidence=0.95,
        result=FAKE_RESULT_DICT,
    )

    with (
        patch("ai.prefilter.pipeline.settings") as mock_settings,
        patch.object(pipeline.tier1_phrases, "evaluate", return_value=t1_decision),
    ):
        mock_settings.PREFILTER_ENABLED = True
        mock_settings.PREFILTER_SHADOW_MODE = False
        mock_settings.PREFILTER_T1_LIVE = False  # kill-switch OFF
        mock_settings.PREFILTER_T2_LIVE = False
        mock_settings.PREFILTER_T3_LIVE = False

        result = pipeline.run_prefilter(MSGS, "Agent", "Bob")

    assert result is None


# ── T1 escalates → pipeline escalates even if other tiers are live ──────────

def test_run_prefilter_t1_escalate_propagates():
    """T1 found a suspicious phrase → escalate decision returned (result=None from caller)."""
    t1_decision = PipelineResult(
        tier_hit=1,
        decision="escalate",
        confidence=1.0,
    )

    with (
        patch("ai.prefilter.pipeline.settings") as mock_settings,
        patch.object(pipeline.tier1_phrases, "evaluate", return_value=t1_decision),
    ):
        mock_settings.PREFILTER_ENABLED = True
        mock_settings.PREFILTER_SHADOW_MODE = False
        mock_settings.PREFILTER_T1_LIVE = True
        mock_settings.PREFILTER_T2_LIVE = True
        mock_settings.PREFILTER_T3_LIVE = True

        result = pipeline.run_prefilter(MSGS, "Agent", "Bob")

    # escalate decision → caller gets None (goes to Groq)
    assert result is None
