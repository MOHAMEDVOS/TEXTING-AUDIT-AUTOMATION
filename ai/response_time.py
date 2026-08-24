"""
Deterministic response-time audit (Flag F17).

Measures how long the AGENT took to reply to the LEAD/owner during business hours
(8:00 AM – 8:00 PM EST) and flags slow replies on live conversations:
  - Yellow Alert (> 10 min): Medium severity, -8 pts Script Adherence penalty
  - Red Alert (> 15 min): High severity, -15 pts Script Adherence penalty
  - Critical Delay (> 25 min): High severity, -25 pts Script Adherence penalty

Pure / deterministic: no ML or API calls, zero token cost.
"""
from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timedelta
import pytz

from database.db import _parse_msg_datetime

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

# Business hours limits (EST)
BUSINESS_START_HOUR = _env_int("BUSINESS_START_HOUR", 8)   # 8:00 AM
BUSINESS_END_HOUR = _env_int("BUSINESS_END_HOUR", 20)     # 8:00 PM

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
    """Contact/lead messages use sender == 'Contact'; everything else is the agent."""
    return (sender or "").strip().lower() != "contact"


def _business_minutes_between(dt1: datetime, dt2: datetime) -> float:
    """
    Calculate active business minutes between dt1 and dt2 during 8:00 AM - 8:00 PM EST.
    Overnight hours (8 PM to 8 AM next day) are excluded so overnight pauses don't skew stats,
    while long daytime delays (> 25 min) are strictly measured.
    """
    if not dt1 or not dt2 or dt2 <= dt1:
        return 0.0

    est = pytz.timezone("US/Eastern")
    if dt1.tzinfo is None:
        dt1 = est.localize(dt1)
    else:
        dt1 = dt1.astimezone(est)

    if dt2.tzinfo is None:
        dt2 = est.localize(dt2)
    else:
        dt2 = dt2.astimezone(est)

    total_seconds = 0.0
    curr_date = dt1.date()
    end_date = dt2.date()

    while curr_date <= end_date:
        b_start = est.localize(datetime(curr_date.year, curr_date.month, curr_date.day, BUSINESS_START_HOUR, 0, 0))
        b_end = est.localize(datetime(curr_date.year, curr_date.month, curr_date.day, BUSINESS_END_HOUR, 0, 0))

        t0 = max(dt1, b_start) if curr_date == dt1.date() else b_start
        t1 = min(dt2, b_end) if curr_date == end_date else b_end

        if t1 > t0:
            total_seconds += (t1 - t0).total_seconds()

        curr_date += timedelta(days=1)

    return total_seconds / 60.0


def check_response_time(parsed_messages, assigned_labels) -> dict | None:
    """
    Return a slow-response descriptor, or None when there's no violation or the
    conversation isn't in scope.

    On a hit:
        {
          "severity": "medium" | "high",
          "threshold_tag": "yellow" | "red" | "critical",
          "threshold_min": 10 | 15 | 25,
          "minutes": int,
          "evidence": [lead_msg, agent_msg],
          "script_penalty": int,
        }
    """
    if not _labels_match(assigned_labels):
        return None

    worst_minutes = -1.0
    worst_evidence = None

    pending_open = False   # is a lead burst awaiting a reply?
    pending_dt = None      # timestamp of the FIRST lead message in that burst
    pending_msg = None     # that lead message (for evidence)

    for msg in parsed_messages or []:
        dt = _parse_msg_datetime(msg)

        if _is_agent(msg.get("sender")):
            if pending_open and pending_dt is not None and dt is not None:
                gap = _business_minutes_between(pending_dt, dt)
                if gap > worst_minutes:
                    worst_minutes = gap
                    worst_evidence = [pending_msg, msg]
            pending_open = False
            pending_dt = None
            pending_msg = None
        else:
            if not pending_open:
                pending_open = True
                pending_dt = dt
                pending_msg = msg

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

    return {
        "severity": severity,
        "threshold_tag": threshold_tag,
        "threshold_min": threshold_min,
        "minutes": minutes,
        "evidence": worst_evidence,
        "script_penalty": penalty,
    }
