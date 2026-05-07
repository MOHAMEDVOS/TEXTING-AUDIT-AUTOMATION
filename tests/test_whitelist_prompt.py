"""Structural tests for the whitelist-only SYSTEM_PROMPT."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.prompts import SYSTEM_PROMPT, BATCH_SYSTEM_PROMPT


# ── Whitelist constraint must be declared TWICE ───────────────────────────────


def test_whitelist_constraint_declared_at_top():
    """Top-of-prompt 'ABSOLUTE LAW' block must state the whitelist rule."""
    top = SYSTEM_PROMPT[:1500]
    assert "ABSOLUTE LAW" in top
    assert "RED FLAGS WHITELIST" in top
    assert "ONLY contain flags from the 12" in top or "may ONLY contain" in top


def test_whitelist_constraint_repeated_in_part_8():
    """PART 8 must restate the constraint and list 12 numbered flags."""
    assert "## PART 8 — RED FLAGS" in SYSTEM_PROMPT
    part_8 = SYSTEM_PROMPT.split("## PART 8")[1].split("## PART 9")[0]
    assert "WHITELIST" in part_8
    for i in range(1, 13):
        assert f"FLAG {i} —" in part_8, f"FLAG {i} missing from PART 8"


# ── Each of the 12 flags has TRIGGER, DO NOT FIRE, OUTPUT ─────────────────────


def test_each_flag_has_trigger_and_output():
    part_8 = SYSTEM_PROMPT.split("## PART 8")[1].split("## PART 9")[0]
    for i in range(1, 13):
        if i < 12:
            block = part_8.split(f"FLAG {i} —")[1].split("FLAG ")[0]
        else:
            block = part_8.split(f"FLAG {i} —")[1].split("═══ END")[0]
        assert "TRIGGER:" in block, f"FLAG {i} missing TRIGGER"
        assert "DO NOT FIRE FOR:" in block, f"FLAG {i} missing DO NOT FIRE FOR"
        assert "OUTPUT:" in block, f"FLAG {i} missing OUTPUT"


# ── Critical "never flag" carve-outs ──────────────────────────────────────────


def test_referral_close_carveout_present():
    """The $1,000 referral must be explicitly excluded from Flag 3."""
    assert "$1,000 referral" in SYSTEM_PROMPT or "$1k referral" in SYSTEM_PROMPT
    assert "referral" in SYSTEM_PROMPT.lower()


def test_soft_no_not_optout_carveout():
    """Soft 'no' must be explicitly stated as NOT an opt-out."""
    assert "Soft" in SYSTEM_PROMPT and "opt-out" in SYSTEM_PROMPT
    assert 'NOT an opt-out' in SYSTEM_PROMPT or 'NOT opt-outs' in SYSTEM_PROMPT or 'none are opt-outs' in SYSTEM_PROMPT


def test_flag_pair_dedup_rules_present():
    """Dedup rules for Flag 9+10 and Flag 4+11 must be in the prompt."""
    assert "Flag 9 and Flag 10" in SYSTEM_PROMPT or "Flag 10 only" in SYSTEM_PROMPT
    assert "Flag 4 and Flag 11" in SYSTEM_PROMPT or "Flag 11 only" in SYSTEM_PROMPT


def test_emoji_only_reaction_not_forced_to_potential():
    """Emoji-only replies must not be treated as real engagement."""
    assert "Emoji/reaction-only responses" in SYSTEM_PROMPT
    assert "Never force \"Potential\" from emoji-only reactions" in SYSTEM_PROMPT


def test_potential_requires_three_clear_pillars():
    """Potential should only be valid at 3+ clear pillars."""
    assert "\"Potential\" is valid ONLY when the lead gave at least 3 clear pillars" in SYSTEM_PROMPT
    assert "If fewer than 3 clear pillars are gathered, do NOT suggest or force \"Potential\"" in SYSTEM_PROMPT


# ── Scoring must be flag-driven ───────────────────────────────────────────────


def test_script_adherence_is_flag_driven():
    """script_adherence_score must reference the formula tied to flag count."""
    assert (
        "flags_fired" in SYSTEM_PROMPT
        or "flag count" in SYSTEM_PROMPT.lower()
        or "flags × 20" in SYSTEM_PROMPT
        or "flags_fired × 20" in SYSTEM_PROMPT
    )


# ── Output JSON shape preserved ───────────────────────────────────────────────


def test_output_json_has_all_required_fields():
    required = [
        "compliance_score",
        "sentiment_score",
        "professionalism_score",
        "script_adherence_score",
        "funnel_stage_reached",
        "pillars_gathered",
        "rebuttals_used",
        "label_assigned",
        "label_correct",
        "label_should_be",
        "label_reason",
        "red_flags",
        "actions_triggered",
        "summary",
    ]
    for field in required:
        assert f'"{field}"' in SYSTEM_PROMPT, f"Output JSON field '{field}' missing"


def test_batch_prompt_inherits_whitelist_constraint():
    """The BATCH version must also have the whitelist constraint."""
    assert "ABSOLUTE LAW" in BATCH_SYSTEM_PROMPT
    assert "RED FLAGS WHITELIST" in BATCH_SYSTEM_PROMPT
    for i in range(1, 13):
        assert f"FLAG {i} —" in BATCH_SYSTEM_PROMPT


# ── Token economy: prompt must be substantially smaller ────────────────────────


def test_prompt_length_reduced():
    """Confirm the rewrite achieved its size goal (<= ~26000 bytes)."""
    size_bytes = len(SYSTEM_PROMPT.encode("utf-8"))
    assert size_bytes < 26000, f"SYSTEM_PROMPT is {size_bytes} bytes — too large (target <26000)"


# ── Old defensive prose should be GONE ────────────────────────────────────────


def test_old_never_flag_wall_removed():
    """The old PART 8 'NEVER RED FLAGS' block had many defensive bullets."""
    bullet_count = SYSTEM_PROMPT.count("✗ Agent ")
    assert bullet_count < 10, f"Old defensive prose still present: {bullet_count} lines starting with '✗ Agent '"
