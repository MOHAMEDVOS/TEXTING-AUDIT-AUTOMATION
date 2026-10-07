"""
Deterministic response-time audit (Flag F17).

Measures how long the AGENT took to reply to the LEAD/owner *during the team's
staffed shift* (10:00 AM – 7:00 PM ET, Mon–Fri — see ai/shift.py) and flags slow
replies on live conversations. Off-shift time never counts, so an overnight or
weekend pause can't read as unresponsiveness.

The account's ownership timeline (`periods=`, its assignment_periods rows) is
required to confirm anyone was actually on the account during the gap: a wait
is only measured for the hours a texter is confirmed assigned. A gap with no
period coverage at all - no periods recorded for the account, or a stretch
outside every period that exists - counts as zero confirmed minutes and can't
raise this flag. We won't hold anyone accountable for time nobody can be shown
to have owned.

  - Yellow Alert (> 10 min): Medium severity, -8 pts Script Adherence penalty
  - Red Alert (> 15 min): High severity, -15 pts Script Adherence penalty
  - Critical Delay (> 25 min): High severity, -25 pts Script Adherence penalty

Pure / deterministic: no ML or API calls, zero token cost.
"""
from __future__ import annotations

import os
import re
import logging
from datetime import date, datetime

from ai.shift import shift_minutes_by_texter, shift_minutes_with_periods
from database.db import _parse_msg_datetime, _is_outgoing
from config.settings import TIMEZONE

logger = logging.getLogger(__name__)

# Canonical flag text — MUST match the F17 entry in ai/prefilter/_guards.py.
FLAG_TEXT = "Slow response time to an engaged lead."


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


# Thresholds (minutes) and Script-Adherence penalties — env-overridable.
YELLOW_MIN = _env_int("RESPONSE_TIME_YELLOW_MIN", 10)
RED_MIN = _env_int("RESPONSE_TIME_RED_MIN", 15)
CRITICAL_MIN = _env_int("RESPONSE_TIME_CRITICAL_MIN", 25)

SCRIPT_PENALTY_YELLOW = _env_int("RESPONSE_TIME_PENALTY_YELLOW", 8)
SCRIPT_PENALTY_RED = _env_int("RESPONSE_TIME_PENALTY_RED", 15)
SCRIPT_PENALTY_CRITICAL = _env_int("RESPONSE_TIME_PENALTY_CRITICAL", 25)

# The shift window itself lives in config/settings.py (SHIFT_START_HOUR /
# SHIFT_END_HOUR / SHIFT_DAYS) and is applied by ai.shift.shift_minutes_with_periods.

# Conversation labels that get response-time auditing.
# Includes the bare funnel labels, the WL/AP/HL Drip follow-up tracks, and the
# push-stage labels (ai/prefilter/label_validator.py's _LOCAL_PUSH_LABELS) —
# all still an "engaged lead awaiting reply", just later in the funnel.
TARGET_LABELS = {
    "lead", "potential", "hl", "wl", "ap", "undefined",
    "wl drip", "ap drip", "hl drip",
    "lead pushed", "pushed to client", "waiting to be pushed",
}

# Terminal/disqualifying labels — when one of these is also assigned (e.g.
# "FUI, WL Drip, Not Interested"), the lead is no longer actively engaged, so a
# slow reply shouldn't be penalized even though the base track label matches.
_EXCLUDE_LABELS = {
    "not interested", "no response", "dnc", "do not call", "wrong number",
    "sold", "under contract", "voicemail", "no answer", "stopped responding",
    "remove", "remove me", "unsubscribe",
}

_LABEL_SPLIT_RE = re.compile(r"[,;/|+]")


def _labels_match(assigned_labels) -> bool:
    """True if the assigned labels put this conversation in scope for
    response-time auditing (an active lead or WL/AP/HL Drip track), and none
    of them mark it as terminal/disqualified (opted out, DNC, sold, etc.)."""
    parts = set()
    for raw in assigned_labels or []:
        for part in _LABEL_SPLIT_RE.split(str(raw).lower()):
            p = part.strip()
            if p:
                parts.add(p)

    if parts & _EXCLUDE_LABELS:
        return False

    return bool(parts & TARGET_LABELS)


def _is_agent(sender: str | None) -> bool:
    """Delegates to the single shared predicate (deep review F10).

    The local version only excluded 'contact', so 'lead', 'unknown' and empty
    senders were counted as agent messages and could start a response-time clock.
    """
    return _is_outgoing(sender)


def _is_contact(sender: str | None) -> bool:
    """Only an explicitly identified lead/contact message starts the clock."""
    return (sender or "").strip().lower() in {"contact", "lead"}


def _review_date(value) -> date | None:
    """Normalize a conversation's stored audit date for F17 calculations."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(value[:10], fmt).date()
            except ValueError:
                continue
    return None


def _message_local_date(dt: datetime | None) -> date | None:
    """Return the actual timestamp's calendar date in the team's timezone."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(TIMEZONE).date()


def check_response_time(parsed_messages, assigned_labels, *,
                        periods=None, audit_date=None) -> dict | None:
    """
    Return a slow-response descriptor, or None when there's no violation or the
    conversation isn't in scope.

    `periods` are the account's assignment_periods rows. The gap is measured
    only against the hours a texter is CONFIRMED to have owned the account -
    clipped to the global shift, never widened by it (see
    ai.shift.shift_minutes_with_periods). When `audit_date` is supplied, only
    messages whose actual timestamps fall on that local calendar date are
    considered; timestamps are never shifted to another date. Each contact
    message replaces the pending one, so a reply is paired with the immediately
    previous contact message. Without any periods covering the
    gap - including when the account has no periods at all - zero minutes are
    confirmed and the flag cannot fire, even if the raw elapsed time is huge.
    Keyword-only and defaulted so every existing positional call site is
    unaffected. Pass the rows in; this module never opens a connection.

    On a hit:
        {
          "severity": "medium" | "high",
          "threshold_tag": "yellow" | "red" | "critical",
          "threshold_min": 10 | 15 | 25,
          "minutes": int,
          "evidence": [lead_msg, agent_msg],
          "script_penalty": int,
          "started_at": datetime,       # when the clock started (lead's msg)
          "ended_at": datetime,         # when it stopped (agent's reply)
          "by_texter": {name: minutes}, # empty without periods
        }
    """
    audit_day = _review_date(audit_date)
    if not _labels_match(assigned_labels):
        return None

    worst_minutes = -1.0
    worst_evidence = None
    worst_span: tuple | None = None   # the winning gap's two instants

    pending_open = False   # is a lead burst awaiting a reply?
    pending_dt = None      # timestamp of the immediately previous lead message
    pending_msg = None     # that lead message (for evidence)

    for msg in parsed_messages or []:
        dt = _parse_msg_datetime(msg)

        # Never carry an open response clock across the selected audit date.
        # Use the real timestamp converted only for date comparison; the raw
        # instant below remains unchanged for shift-minute arithmetic.
        if audit_day is not None and _message_local_date(dt) != audit_day:
            pending_open = False
            pending_dt = None
            pending_msg = None
            continue

        if _is_agent(msg.get("sender")):
            if pending_open and pending_dt is not None and dt is not None:
                gap = shift_minutes_with_periods(pending_dt, dt, periods)
                if gap > worst_minutes:
                    worst_minutes = gap
                    worst_evidence = [pending_msg, msg]
                    worst_span = (pending_dt, dt)
            pending_open = False
            pending_dt = None
            pending_msg = None
        elif _is_contact(msg.get("sender")):
            # A message tagged _stale_rescue is historical context only and
            # must never start a response clock. For consecutive contact
            # messages, retain the immediately previous one for the next reply.
            if not msg.get("_stale_rescue"):
                pending_open = True
                pending_dt = dt
                pending_msg = msg
        else:
            # An unknown/system message breaks the adjacency: the next agent
            # reply must not be paired with an earlier contact message.
            pending_open = False
            pending_dt = None
            pending_msg = None

    if worst_evidence is None or worst_minutes <= YELLOW_MIN:
        return None

    minutes = int(round(worst_minutes))
    if worst_minutes > CRITICAL_MIN:
        severity = "high"
        penalty = SCRIPT_PENALTY_CRITICAL
        threshold_tag = "critical"
        threshold_min = CRITICAL_MIN
    elif worst_minutes > RED_MIN:
        severity = "high"
        penalty = SCRIPT_PENALTY_RED
        threshold_tag = "red"
        threshold_min = RED_MIN
    else:
        severity = "medium"
        penalty = SCRIPT_PENALTY_YELLOW
        threshold_tag = "yellow"
        threshold_min = YELLOW_MIN

    started_at, ended_at = worst_span if worst_span else (None, None)

    return {
        "severity": severity,
        "threshold_tag": threshold_tag,
        "threshold_min": threshold_min,
        "minutes": minutes,
        "evidence": worst_evidence,
        "script_penalty": penalty,
        # The interval itself, so the wait can be split across a handover
        # instead of landing entirely on whoever started the clock.
        "started_at": started_at,
        "ended_at": ended_at,
        "by_texter": shift_minutes_by_texter(started_at, ended_at, periods),
    }
