"""
Deterministic response-time audit (Flag F17).

Measures how long the AGENT took to reply to the LEAD/owner and flags slow
replies — but only on "live" conversations (labels: Lead, Potential, HL, WL,
AP, Undefined). Pure / deterministic: no ML or API calls, zero token cost.

Locked decisions (see plan):
  - Wall-clock within a session, but gaps over MAX_GAP_MIN are ignored as
    session breaks (overnight / next-day), so a 12-hour overnight pause never
    flags as a "slow response".
  - RECENCY WINDOW: only replies on the current day (or up to WINDOW_DAYS
    before it, anchored to the real current date via get_now) are scored.
    Old conversations are out of scope — response-time coaching is about how
    fast agents reply *now*, not a weeks-old backlog.
  - Completed replies only — a trailing unanswered lead message is ignored.
  - One flag, dynamic severity: > YELLOW_MIN -> medium (yellow),
    > RED_MIN -> high (red). The worst gap within the cap wins.
  - Deducts from Script Adherence only.
"""
from __future__ import annotations

import os
import re
from datetime import timedelta

from config.settings import get_now
from database.db import _parse_msg_datetime

# Canonical flag text — MUST match the F17 entry in ai/prefilter/_guards.py.
FLAG_TEXT = "Slow response time to an engaged lead."


def _env_int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


# Thresholds (minutes) and Script-Adherence penalties — env-overridable.
YELLOW_MIN = _env_int("RESPONSE_TIME_YELLOW_MIN", 10)
RED_MIN = _env_int("RESPONSE_TIME_RED_MIN", 20)
SCRIPT_PENALTY_YELLOW = _env_int("RESPONSE_TIME_PENALTY_YELLOW", 8)
SCRIPT_PENALTY_RED = _env_int("RESPONSE_TIME_PENALTY_RED", 15)

# Session-break cap: gaps longer than this (minutes) are NOT a slow response —
# they're an overnight / next-day / lead-resurfaced-hours-later pause and are
# ignored. A real "slow reply" happens inside an active back-and-forth.
MAX_GAP_MIN = _env_int("RESPONSE_TIME_MAX_GAP_MIN", 60)

# Recency window: how many calendar days BEFORE today are still in scope.
#   0 = today only | 1 = today + yesterday (default) | 2 = today + 2 prior days
# Anchored to the real current date (get_now), so months-old conversations are
# never scored for response time.
WINDOW_DAYS = _env_int("RESPONSE_TIME_WINDOW_DAYS", 1)

# Conversation labels that get response-time auditing.
TARGET_LABELS = {"lead", "potential", "hl", "wl", "ap", "undefined"}

_LABEL_SPLIT_RE = re.compile(r"[,;/|+]")


def _labels_match(assigned_labels) -> bool:
    """True if any assigned label is one of the live/target categories."""
    for raw in assigned_labels or []:
        for part in _LABEL_SPLIT_RE.split(str(raw).lower()):
            if part.strip() in TARGET_LABELS:
                return True
    return False


def _is_agent(sender: str | None) -> bool:
    """Contact/lead messages use sender == 'Contact'; everything else is the
    agent (the scraper stores the agent's first name as the sender)."""
    return (sender or "").strip().lower() != "contact"


def check_response_time(parsed_messages, assigned_labels) -> dict | None:
    """
    Return a slow-response descriptor, or None when there's no violation or the
    conversation isn't in scope.

    On a hit:
        {
          "severity": "medium" | "high",   # yellow | red
          "minutes":  int,                  # worst completed gap, rounded
          "evidence": [lead_msg, agent_msg],
          "script_penalty": int,
        }
    """
    if not _labels_match(assigned_labels):
        return None

    # Recency window — only score replies on the current day or up to
    # WINDOW_DAYS before it. Compared on date() so a naive scraped datetime and
    # the tz-aware get_now() never clash.
    window_start = (get_now() - timedelta(days=WINDOW_DAYS)).date()

    worst_minutes = -1.0
    worst_evidence = None

    pending_open = False   # is a lead burst awaiting a reply?
    pending_dt = None      # timestamp of the FIRST lead message in that burst
    pending_msg = None     # that lead message (for evidence)

    for msg in parsed_messages or []:
        dt = _parse_msg_datetime(msg)

        if _is_agent(msg.get("sender")):
            # Agent reply closes any open lead gap. Skip replies that landed
            # before the recency window — old convos are out of scope.
            if (
                pending_open
                and pending_dt is not None
                and dt is not None
                and dt.date() >= window_start
            ):
                gap = (dt - pending_dt).total_seconds() / 60.0
                # Ignore session-break gaps (overnight / multi-day): they aren't
                # a response-time issue. Keep the worst gap within the cap.
                if 0 <= gap <= MAX_GAP_MIN and gap > worst_minutes:
                    worst_minutes = gap
                    worst_evidence = [pending_msg, msg]
            pending_open = False
            pending_dt = None
            pending_msg = None
        else:
            # Lead message: start the clock only on the FIRST of a burst, so a
            # run of consecutive lead messages reflects the true wait time.
            if not pending_open:
                pending_open = True
                pending_dt = dt
                pending_msg = msg
    # A trailing unanswered lead message is intentionally ignored.

    if worst_evidence is None or worst_minutes <= YELLOW_MIN:
        return None

    minutes = int(round(worst_minutes))
    if worst_minutes > RED_MIN:
        severity, penalty = "high", SCRIPT_PENALTY_RED
    else:
        severity, penalty = "medium", SCRIPT_PENALTY_YELLOW

    return {
        "severity": severity,
        "minutes": minutes,
        "evidence": worst_evidence,
        "script_penalty": penalty,
    }
