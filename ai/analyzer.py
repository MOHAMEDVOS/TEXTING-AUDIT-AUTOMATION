"""
ML-only conversation analyzer.

No LLM calls — every conversation is scored by the local ML pre-filter
(ai/prefilter). Tiers 1-3 short-circuit confidently-clean conversations;
anything that reaches the end of the tier stack is finalized by the
deterministic Tier 4 flag generator.

Public API:
    analyze_conversation(...)  → dict
"""
import logging

logger = logging.getLogger(__name__)

# ── 30-day rolling window for conversation auditing ──────────────────────────
# Only messages within the last 30 days from the newest message are audited.
# This prevents stale history (months/years old) from skewing current scores.
_AUDIT_WINDOW_DAYS = 30


def _parse_message_date(date_str: str):
    """Parse a SmarterContact date string like 'Thursday, March 26, 2026' into a date object.

    Returns None if the string can't be parsed.
    """
    from datetime import datetime as _dt
    if not date_str or not date_str.strip():
        return None
    s = date_str.strip()
    # Format: "Thursday, March 26, 2026" → "%A, %B %d, %Y"
    for fmt in ("%A, %B %d, %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return _dt.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def filter_recent_messages(
    messages: list[dict],
    window_days: int = _AUDIT_WINDOW_DAYS,
) -> list[dict]:
    """Return only messages within `window_days` of the latest message date.

    Messages without a parseable date are assigned the most recently seen date
    (same-day messages at the top of the conversation have date="").

    If no dates can be parsed at all, returns the original list unchanged.
    """
    from datetime import timedelta

    if not messages:
        return messages

    # Pass 1: parse all dates, propagate to dateless messages
    dated: list[tuple[dict, "date | None"]] = []
    last_known_date = None
    for msg in messages:
        d = _parse_message_date(msg.get("date") or "")
        if d is not None:
            last_known_date = d
        dated.append((msg, d if d is not None else last_known_date))

    # Find the latest date across all messages
    all_dates = [d for _, d in dated if d is not None]
    if not all_dates:
        return messages  # no parseable dates → audit everything

    latest_date = max(all_dates)
    cutoff = latest_date - timedelta(days=window_days)

    # Pass 2: keep only messages on or after the cutoff
    filtered = [msg for msg, d in dated if d is not None and d >= cutoff]

    if not filtered:
        return messages  # safety: never return empty if input had messages

    if len(filtered) < len(messages):
        dropped = len(messages) - len(filtered)
        logger.info(
            f"[Analyzer] 30-day window: kept {len(filtered)}/{len(messages)} messages "
            f"(dropped {dropped} older than {cutoff.isoformat()})"
        )

    return filtered


# ── Shared guard imports (authoritative source: ai.prefilter._guards) ────────
# All deterministic guard logic lives in _guards.py and is shared with Tier 4.
from ai.prefilter._guards import (
    agent_continued_after_opt_out as _agent_continued_after_opt_out,
    agent_continued_pitch_after_wn as _agent_continued_pitch_after_wn,
    last_message_from_contact as _last_message_from_contact,
    agent_replied_after_first_soft_no as _agent_replied_after_first_soft_no,
    apply_label_guards as _apply_label_guards,
)


def _apply_deterministic_guards(result: dict, messages: list[dict]) -> None:
    """Drop flags that require a follow-up condition the transcript doesn't support."""
    flags = list(result.get("red_flags") or [])
    if "Continued texting after explicit opt-out." in flags and not _agent_continued_after_opt_out(messages):
        flags = [f for f in flags if f != "Continued texting after explicit opt-out."]
    if "Gave up after first no with zero rebuttal." in flags and _agent_replied_after_first_soft_no(messages):
        flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
    if _last_message_from_contact(messages) and "Gave up after first no with zero rebuttal." in flags:
        flags = [f for f in flags if f != "Gave up after first no with zero rebuttal."]
    if "Continued original pitch after wrong number." in flags and not _agent_continued_pitch_after_wn(messages):
        flags = [f for f in flags if f != "Continued original pitch after wrong number."]
    result["red_flags"] = flags
    _apply_label_guards(result, messages)


# ── Main public function ──────────────────────────────────────────────────────

def analyze_conversation(
    messages: list[dict],
    agent_name: str,
    contact_name: str = "Contact",
    assigned_labels: list[str] | None = None,
    *,
    funnel_tier: str | None = None,
    conversation_id: int | None = None,
    db_pool=None,
) -> dict:
    """
    Analyze a single parsed conversation using the local ML pipeline.

    `funnel_tier` is a per-account override injected into the pre-filter's
    tier logic. Pass None to default to "NF".

    `conversation_id` + `db_pool` are optional — when both are provided the
    ML pre-filter records its tier decision for later evaluation.

    Returns dict with audit scores or {scores=None, error=...} on failure.
    """
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    # ── 30-day rolling window: drop messages older than 30 days ──────
    messages = filter_recent_messages(messages)

    # ── ML pre-filter (Tier 1/2/3) — may short-circuit ────────────────
    try:
        from ai.prefilter import run_prefilter
        prefilter_result = run_prefilter(
            messages,
            agent_name,
            contact_name,
            conversation_id=conversation_id,
            funnel_tier=funnel_tier or "NF",
            assigned_labels=assigned_labels or [],
            db_pool=db_pool,
        )
        if prefilter_result is not None:
            if isinstance(prefilter_result, dict):
                _apply_deterministic_guards(prefilter_result, messages)
            if conversation_id is not None:
                prefilter_result.setdefault("conversation_id", conversation_id)
            return prefilter_result
    except Exception as e:
        logger.warning(f"[Analyzer] Prefilter failed for {contact_name}: {e}")

    # run_prefilter already returns a terminal T4 result in the common case;
    # reaching here means an edge case (no messages, or T4 errored). Run the
    # deterministic Tier 4 generator directly as a last resort.
    result = _ml_only_fallback(messages, agent_name, contact_name, assigned_labels)
    if conversation_id is not None:
        result.setdefault("conversation_id", conversation_id)
    return result


def _ml_only_fallback(
    messages: list[dict],
    agent_name: str,
    contact_name: str,
    assigned_labels: list[str] | None,
) -> dict:
    """Terminal result when the pre-filter pipeline didn't produce one. Runs the
    deterministic Tier 4 generator directly; if that fails, returns an empty
    (skipped) result."""
    try:
        from ai.prefilter import tier4_flag_generator
        result = tier4_flag_generator.generate(
            messages, agent_name, contact_name, assigned_labels=assigned_labels or [],
        )
        if isinstance(result, dict):
            result.setdefault("model_used", "prefilter_t4")
            result.setdefault("contact_name", contact_name)
            return result
    except Exception as e:
        logger.warning(f"[Analyzer] ML-only T4 fallback failed for {contact_name}: {e}")
    return _empty_result("ML-only mode: no deterministic result available", contact_name)


# ── Empty result ──────────────────────────────────────────────────────────────

def _empty_result(reason: str, contact_name: str = "Contact") -> dict:
    return {
        "compliance_score": None,
        "sentiment_score": None,
        "professionalism_score": None,
        "script_adherence_score": None,
        "red_flags": [],
        "summary": f"Analysis skipped: {reason}",
        "model_used": None,
        "contact_name": contact_name,
        "error": reason,
    }
