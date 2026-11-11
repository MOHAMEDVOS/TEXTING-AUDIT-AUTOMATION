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

# ── 7-day rolling window for conversation auditing ────────────────────────────
# Only messages within the last 7 days from the newest message are audited.
# This prevents stale history (weeks/months/years old) from skewing current scores.
_AUDIT_WINDOW_DAYS = 7


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


def _is_contact_msg(msg: dict) -> bool:
    return (msg.get("sender") or "").strip().lower() in ("contact", "lead")


def filter_recent_messages(
    messages: list[dict],
    window_days: int = _AUDIT_WINDOW_DAYS,
) -> list[dict]:
    """Return only messages within `window_days` of the latest message date.

    Messages without a parseable date are assigned the most recently seen date
    (same-day messages at the top of the conversation have date="").

    If no dates can be parsed at all, returns the original list unchanged.

    Exception: the contact's most recent reply is always kept, even when it
    falls outside the window. Without it a thread whose only engagement is
    older than `window_days` reads as "contact never replied" and gets
    mislabelled Stopped Responding — e.g. a lead who said "not now, maybe
    fall/winter" 9 days ago is a Maybe Later, not a silent contact.
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

    # Pass 3: rescue the contact's last reply if the window dropped every one.
    # `dated` is chronological, so the last match is the most recent reply and
    # prepending it preserves message order.
    if not any(_is_contact_msg(m) for m in filtered):
        rescued = next(
            (m for m, d in reversed(dated)
             if _is_contact_msg(m) and d is not None and d < cutoff),
            None,
        )
        if rescued is not None:
            # Tagged (on a copy, so the original dict used elsewhere — e.g. the
            # chat UI — is untouched) so ai.response_time.check_response_time
            # knows this message is a stitched-in historical artifact, not a
            # fresh in-window reply, and won't open a response-time clock on
            # it. Without this, a months-old rescued message paired with
            # today's agent reply reads as one giant unanswered wait — the
            # exact defect this rolling window (deep review F28) was meant to
            # prevent, reintroduced through this rescue path.
            rescued = {**rescued, "_stale_rescue": True}
            filtered = [rescued] + filtered
            logger.info(
                f"[Analyzer] {window_days}-day window: kept contact's last reply "
                f"from outside the window so the thread isn't read as silent"
            )

    if len(filtered) < len(messages):
        dropped = len(messages) - len(filtered)
        logger.info(
            f"[Analyzer] {window_days}-day window: kept {len(filtered)}/{len(messages)} messages "
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


def _mark_label_wrong(
    result: dict, assigned_labels: list[str] | None, hygiene_flags: list[str]
) -> None:
    """Make the label verdict agree with an F18/F19 flag. A label another rule
    already judged wrong keeps its own, more specific, 'should be'."""
    from ai.prefilter.label_combination import (
        CONFLICTING_DISPO_FLAG,
        status_label_keys,
    )

    if result.get("label_correct") is False:
        return
    if CONFLICTING_DISPO_FLAG in hygiene_flags:
        left = status_label_keys(assigned_labels) - {"new lead"}
        should = next(iter(left)).title() if len(left) == 1 else "a single status"
        reason = "More than one status is on the account — clear the one that no longer applies."
    else:
        should = "a real status (not New Lead)"
        reason = "The contact replied, so the account should no longer be on New Lead."
    result["label_assigned"] = result.get("label_assigned") or ", ".join(assigned_labels or [])
    result["label_correct"] = False
    result["label_should_be"] = should
    result["label_reason"] = reason
    result["_dispo_hygiene"] = True


def _apply_dispo_hygiene_flags(
    result: dict, messages: list[dict], assigned_labels: list[str] | None
) -> None:
    """Flag an account the texter left on the wrong number of statuses.

    Lives here rather than in the T4 generator because Tier 1 short-circuits on
    trivial conversations (a bare "wrong number" reply never reaches T4), and a
    stale disposition is exactly as wrong on those. Every tier's result passes
    through _apply_deterministic_guards, so this is the one place that sees all
    of them.

    These are state flags, not message flags: nobody said anything wrong, the
    account was simply left untidy, so the holder at the last activity owns it
    — the same rule the wrong-label flag uses.

    The label itself is also marked wrong (label_correct=False), so the panel
    never shows "Correct" next to a flag saying the dispositions are wrong, and
    the scorer adds the usual "Wrong label" flag.
    """
    try:
        from ai.prefilter.label_combination import check_label_combination
        from ai.prefilter._guards import canon_flag_text
        from ai.prefilter.tier4_flag_generator import _culprit_ref
    except Exception as e:  # pragma: no cover - import guard
        logger.debug(f"[Analyzer] dispo hygiene unavailable: {e!r}")
        return

    new_flags = check_label_combination(assigned_labels, messages)
    if not new_flags:
        return
    _mark_label_wrong(result, assigned_labels, new_flags)

    flags = list(result.get("red_flags") or [])
    existing = {canon_flag_text(f) for f in flags if isinstance(f, str)}
    culprits = result.get("flag_culprits")
    if not isinstance(culprits, dict):
        culprits = {}

    for flag in new_flags:
        key = canon_flag_text(flag)
        if key in existing:
            continue
        flags.append(flag)
        existing.add(key)
        culprits.setdefault(
            key,
            _culprit_ref(
                messages[-1] if messages else None,
                len(messages) - 1 if messages else None,
                "label",
            ),
        )

    result["red_flags"] = flags
    if result.get("_dispo_hygiene"):
        culprits.setdefault("__label__", _culprit_ref(
            messages[-1] if messages else None,
            len(messages) - 1 if messages else None,
            "label",
        ))
    if culprits:
        result["flag_culprits"] = culprits


def _apply_deterministic_guards(
    result: dict, messages: list[dict], assigned_labels: list[str] | None = None
) -> None:
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
    _apply_dispo_hygiene_flags(result, messages, assigned_labels)


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

    Returns the audit result dict, or one with model_used=None and error=... on failure.
    """
    if not messages:
        return _empty_result("No messages to analyze", contact_name)

    # ── 7-day rolling window: drop messages older than 7 days ──────
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
                _apply_deterministic_guards(
                    prefilter_result, messages, assigned_labels
                )
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
        "red_flags": [],
        "summary": f"Analysis skipped: {reason}",
        "model_used": None,
        "contact_name": contact_name,
        "error": reason,
    }
