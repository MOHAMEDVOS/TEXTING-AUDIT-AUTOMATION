"""Disposition hygiene — an account must end up with ONE coherent status.

SmarterContact lets a texter stack labels, and the CRM seeds every contact with
a campaign/list tag plus "New Lead". The texter's job at the end of a
conversation is to set the real status AND clear the one that no longer
applies. Leaving both on is the defect this module catches:

    New Lead, Wrong Number      -> two unrelated statuses, New Lead never cleared
    Do Not Call, New lead       -> same
    New Lead  (contact replied) -> status never set at all

Three things are deliberately NOT flagged:

1. Campaign / list tags. Every conversation carries one ("Fishnet list" is on
   289 of 292 rows in the dev snapshot; "Denza_Sniper_FINAL" on the rest).
   They are not statuses, so they never count toward the limit. Anything
   outside STATUS_LABELS is treated as one of these.

2. Statuses that mean the same thing. "Verified" and "Decision Maker" are team
   synonyms for Not Interested, so "Decision Maker, Not interested" is one
   answer said twice. label_validator._label_key already collapses these, so
   this module counts canonical keys rather than raw strings and the valid
   pairs fall out for free.

3. Genuine multi-part statuses — the push stack ("Lead, Pushed to client", the
   wide-funnel hand raise) and the drip stack ("FU1, WL drip" = follow-up 1 on
   the warm-lead track). Both are single dispositions that the team spells with
   two labels.

The "New Lead alone" case is gated on the contact having actually replied, and
replied recently. A drip firing into silence is correctly still a New Lead, and
so is one whose only reply predates the 7-day audit window — the texter on
shift never saw it. That is the same reasoning as
_guards.contact_evidence_is_stale_only().
"""
from __future__ import annotations

from ai.prefilter.label_validator import _label_key, _norm

# ── Flag text ────────────────────────────────────────────────────────────────
# Fixed strings: dashboard/views/index.html FLAG_SHORT maps flags by exact text,
# and database/db.py keys attribution off canon_flag_text(), so these must not
# be built by interpolation.
CONFLICTING_DISPO_FLAG = "Left conflicting dispositions on the account."
STALE_NEW_LEAD_FLAG = "Left New Lead after the contact replied."

# ── Status vocabulary ────────────────────────────────────────────────────────
# Every status the team uses, as dashboard/static/labels-guide.html lists them,
# plus the synonyms only the code knows ("Decision Maker", "DNC", "Undefined").
# A label that is not in here is a CRM list/campaign tag and is ignored.
STATUS_LABELS: frozenset[str] = frozenset({
    # outcome
    "new lead", "not interested", "verified", "decision maker",
    "maybe later", "wrong number", "wrong #", "do not call", "dnc",
    "do not call (dnc)", "sold", "listed", "potential", "disqualified",
    "duplicate", "deal closed", "hot", "warm", "investor",
    "abv mv", "above market value", "bluffer",
    # silence
    "stopped responding", "stop responding", "missed call", "undefined",
    # push
    "lead", "lead pushed", "pushed to client", "waiting to be pushed",
    # drip
    "fu1", "fu2", "fu3", "wl drip", "ap drip", "hl drip", "reason fu",
})

# Status groups whose members describe ONE disposition between them, so holding
# several at once is correct rather than contradictory.
_PUSH_STACK: frozenset[str] = frozenset({
    "lead", "lead pushed", "pushed to client", "waiting to be pushed",
})
_DRIP_STACK: frozenset[str] = frozenset({
    "fu1", "fu2", "fu3", "wl drip", "ap drip", "hl drip", "reason fu",
})
_COHERENT_STACKS: tuple[frozenset[str], ...] = (_PUSH_STACK, _DRIP_STACK)


def status_label_keys(assigned_labels: list[str] | None) -> set[str]:
    """Canonical status keys on this account, campaign tags dropped.

    Synonyms collapse: {"Not interested", "Decision Maker"} -> {"not interested"}.
    """
    keys: set[str] = set()
    for raw in assigned_labels or []:
        if not isinstance(raw, str):
            continue
        if _norm(raw) in STATUS_LABELS:
            keys.add(_label_key(raw))
    return keys


def _has_fresh_contact_reply(messages: list[dict] | None) -> bool:
    """True when the contact said something inside the audit window.

    False both when the contact never replied and when their only reply was
    stitched in from outside the window (tagged `_stale_rescue` by
    ai.analyzer.filter_recent_messages) — in neither case did the texter on
    shift have a reply in front of them to re-disposition.
    """
    for m in messages or []:
        if (m.get("sender") or "").strip().lower() not in ("contact", "lead"):
            continue
        if m.get("_stale_rescue"):
            continue
        if (m.get("body") or m.get("message") or "").strip():
            return True
    return False


def check_label_combination(
    assigned_labels: list[str] | None,
    messages: list[dict] | None,
) -> list[str]:
    """Return the disposition-hygiene flags this account has earned."""
    keys = status_label_keys(assigned_labels)
    flags: list[str] = []

    if len(keys) > 1:
        # Legal only when every status belongs to one multi-part disposition.
        if not any(keys <= stack for stack in _COHERENT_STACKS):
            flags.append(CONFLICTING_DISPO_FLAG)
    elif keys == {"new lead"} and _has_fresh_contact_reply(messages):
        flags.append(STALE_NEW_LEAD_FLAG)

    return flags
