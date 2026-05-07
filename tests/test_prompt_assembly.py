"""Tests for prompt assembly: BATCH swap, funnel tier injection, account guidelines, learned rules."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.prompts import (
    SYSTEM_PROMPT,
    BATCH_SYSTEM_PROMPT,
    get_system_prompt,
)


def test_system_prompt_contains_part_12_output_format():
    assert "## PART 12 — OUTPUT FORMAT" in SYSTEM_PROMPT


def test_batch_system_prompt_replaces_part_12():
    """_swap_output_format must replace the single-mode PART 12 with the BATCH version."""
    assert "## PART 12 — OUTPUT FORMAT (BATCH MODE)" in BATCH_SYSTEM_PROMPT
    assert '"results"' in BATCH_SYSTEM_PROMPT


def test_batch_system_prompt_keeps_pre_part12_content():
    """Everything BEFORE PART 12 must survive the swap unchanged."""
    pre_12 = SYSTEM_PROMPT.split("## PART 12")[0]
    assert pre_12 in BATCH_SYSTEM_PROMPT


def test_get_system_prompt_default_returns_base():
    result = get_system_prompt(batch=False, funnel_tier=None, guidelines=None, include_learned_rules=False)
    assert result == SYSTEM_PROMPT


def test_get_system_prompt_batch_returns_batch_base():
    result = get_system_prompt(batch=True, funnel_tier=None, guidelines=None, include_learned_rules=False)
    assert result == BATCH_SYSTEM_PROMPT


def test_get_system_prompt_appends_funnel_tier_nf():
    result = get_system_prompt(batch=False, funnel_tier="NF", guidelines=None, include_learned_rules=False)
    assert "## PART 15 — ACCOUNT FUNNEL TIER: NARROW FUNNEL (NF)" in result


def test_get_system_prompt_appends_funnel_tier_wf():
    result = get_system_prompt(batch=False, funnel_tier="WF", guidelines=None, include_learned_rules=False)
    assert "## PART 15 — ACCOUNT FUNNEL TIER: WIDE FUNNEL (WF)" in result


def test_get_system_prompt_appends_account_guidelines():
    result = get_system_prompt(
        batch=False,
        funnel_tier=None,
        guidelines="condition\nasking price\nmotivation\nclosing timeline",
        include_learned_rules=False,
    )
    assert "## PART 16 — ACCOUNT-SPECIFIC GUIDELINES" in result


def test_get_system_prompt_skips_funnel_tier_when_unknown():
    result = get_system_prompt(batch=False, funnel_tier="XX", guidelines=None, include_learned_rules=False)
    assert "## PART 15" not in result
