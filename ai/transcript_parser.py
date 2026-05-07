"""
Transcript Parser — Phase 2 entry point.

Parses the raw inner_text() of the SmarterContact messages panel into
clean, structured message dicts ready for AI analysis.

ACTUAL FORMAT (confirmed from live page):
  Messages are displayed NEWEST first (top → bottom = recent → old).

  Recent messages (same-day, top section):
      [message text]          ← one or more lines
      [HH:MM AM/PM]           ← timestamp CLOSES the block above

  Historical messages (older days, below date dividers):
      [HH:MM AM/PM]           ← timestamp OPENS the block below
      [message text]

  Campaign tag (always follows the last line of an agent message):
      Sent from campaign: <name>

  Date dividers mark day boundaries as you scroll back in time:
      "Thursday, March 26, 2026"

SENDER DETERMINATION:
  - "Sent from campaign: ..." present → Agent (campaign-sent)
  - Message ends with "-<AgentName>" signature → Agent (manually sent)
  - Everything else → Contact
  (The AI analysis phase refines ambiguous cases semantically.)
"""

import re
from typing import Optional

_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*(AM|PM)$", re.IGNORECASE)
_DATE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+\w+\s+\d{1,2},\s+\d{4}$",
    re.IGNORECASE,
)
_CAMPAIGN_RE = re.compile(r"^Sent from campaign:", re.IGNORECASE)
_MISSED_CALL_RE = re.compile(r"^\s*missed call\s*$", re.IGNORECASE)

# UI chrome lines that leak into inner_text() — strip these out
_UI_NOISE = {
    "send", "messagesinfo", "messagesinfomessages", "messagesinfosend",
    "messagesinfomessagessend", "messagesinfomatesnotes", "messagesinfomatesnotes",
    "loading...", "messagesinfotabs", "no messages yet",
}

_UI_NOISE_PATTERNS = [
    re.compile(r"^Messenger\s+Contacts\s+Campaigns", re.IGNORECASE),
    re.compile(r"^Messages\s+Info", re.IGNORECASE),
    re.compile(r"^Newest\s+labels?\s+Date:", re.IGNORECASE),
    re.compile(r"^Export\s+Name\s+Conversations\s+Date:", re.IGNORECASE),
    re.compile(r"^Sent\s+from\s+campaigns?:", re.IGNORECASE),
    re.compile(r"^campaigns?\s+inbox\b", re.IGNORECASE),
]


def _is_ui_noise_line(line: str) -> bool:
    """Heuristic filter for UI chrome text leaked into panel inner_text()."""
    s = (line or "").strip()
    if not s:
        return True
    if s.lower() in _UI_NOISE:
        return True
    return any(p.search(s) for p in _UI_NOISE_PATTERNS)


def _is_agent_signature(line: str, agent_name: str) -> bool:
    """Check if a line is an agent sign-off like '-Jack' or '-Jack w/LHB'."""
    stripped = line.strip()
    if not stripped:
        return False
    
    # 1. Exact match with agent name
    no_dash = stripped.lstrip("-").strip()
    first_word = no_dash.split()[0].lower() if no_dash else ""
    if first_word and agent_name.lower().startswith(first_word) and len(first_word) >= 3:
        return True
        
    # 2. General signature fallback (e.g. "- Adam", "- Jessica w/ LHB")
    # Usually at the end of a message, or on a newline.
    if "\n" in stripped:
        last_line = stripped.split("\n")[-1].strip()
    else:
        last_line = stripped
        
    import re
    # Matches "- Adam", "-Adam", "- Adam w/ ABC", etc. at the end of the text
    if re.search(r"-\s*[A-Z][a-z]{2,15}(\s+w/\s*[A-Z0-9]+)?\s*$", last_line):
        return True
        
    return False


def parse_transcript(
    raw: str,
    agent_name: str = "Agent",
    side_map: dict | None = None,
) -> list[dict]:
    """
    Parse the SmarterContact messages panel inner_text() into structured messages.

    Args:
        raw:        The full_transcript string from the scraper.
        agent_name: The agent's display name (e.g. "Noah Mallen").
        side_map:   Optional dict of {message_text: is_right_side} built from
                    DOM bounding-box positions during scraping.  When provided,
                    this is used as the primary sender signal (right = agent,
                    left = contact) — more reliable than text heuristics alone.

    Returns:
        List of message dicts in CHRONOLOGICAL order (oldest first):
            {
                "sender":  "<AgentFirstName>" | "Contact" | "System",
                "message": "the message text",
                "time":    "05:59 PM",   # "" if not found
                "date":    "Thursday, March 26, 2026",  # "" for same-day
            }
    """
    if not raw or not raw.strip():
        return []

    lines = [
        l.strip() for l in raw.split("\n")
        if not _is_ui_noise_line(l)
    ]

    blocks = []          # completed message blocks
    current_block: Optional[dict] = None
    current_date = ""

    def _push_block():
        nonlocal current_block
        if current_block and current_block["body_lines"]:
            blocks.append(current_block)
        current_block = None

    def _new_block(time: str = ""):
        nonlocal current_block
        return {
            "time": time,
            "date": current_date,
            "body_lines": [],
            "has_campaign": False,
            "has_agent_sig": False,
        }

    for line in lines:
        # ── Date divider ────────────────────────────────────────────────────
        if _DATE_RE.match(line):
            _push_block()
            current_date = line

        # ── Timestamp ───────────────────────────────────────────────────────
        elif _TIME_RE.match(line):
            if current_block and current_block["body_lines"]:
                # Pattern A (recent): text came first → timestamp closes it
                current_block["time"] = line
                _push_block()
            else:
                # Pattern B (historical): timestamp opens the next block
                _push_block()
                current_block = _new_block(time=line)

        # ── Campaign tag ─────────────────────────────────────────────────────
        elif _CAMPAIGN_RE.match(line):
            if current_block:
                current_block["has_campaign"] = True
            elif blocks:
                # Campaign tag after block was already closed (Pattern A case)
                blocks[-1]["has_campaign"] = True

        # ── Missed call event ────────────────────────────────────────────────
        elif _MISSED_CALL_RE.match(line):
            _push_block()
            blocks.append({
                "time": "",
                "date": current_date,
                "body_lines": ["Missed call"],
                "has_campaign": False,
                "has_agent_sig": False,
                "is_event": True,
            })

        # ── Regular text ─────────────────────────────────────────────────────
        else:
            if current_block is None:
                current_block = _new_block()
            current_block["body_lines"].append(line)
            # Check for agent signature line (e.g. "-Jack", "-Jack w/LHB")
            if line.startswith("-") and _is_agent_signature(line, agent_name):
                current_block["has_agent_sig"] = True

    _push_block()   # flush any remaining block

    # ── Determine sender and build final messages ────────────────────────────
    messages = []
    first_name = agent_name.split()[0] if agent_name else "Agent"

    for block in blocks:
        body = " ".join(block["body_lines"]).strip()
        if not body:
            continue

        if block.get("is_event"):
            sender = "System"
        else:
            # DOM side-map takes priority: right-side bubble = agent, left = contact.
            # Fall back to campaign/signature tags when the map has no entry.
            dom_is_agent = None
            if side_map:
                for line in block["body_lines"]:
                    if line in side_map:
                        dom_is_agent = side_map[line]
                        break

            if dom_is_agent is not None:
                sender = first_name if dom_is_agent else "Contact"
            elif block["has_campaign"] or block["has_agent_sig"]:
                sender = first_name
            else:
                sender = "Contact"

        messages.append({
            "sender":  sender,
            "message": body,
            "time":    block["time"],
            "date":    block["date"],
        })

    # Reverse so output is chronological (oldest first)
    messages.reverse()
    return messages


# ── WhatsApp-style formatter ─────────────────────────────────────────────────

def format_whatsapp(messages: list[dict], contact_name: str = "Contact") -> str:
    """
    Render parsed messages as a WhatsApp-style conversation string.

    Agent messages appear on the RIGHT (prefixed with spaces).
    Contact messages appear on the LEFT.
    """
    WIDTH = 60
    lines_out = []
    last_date = None

    for msg in messages:
        # Date divider
        if msg["date"] and msg["date"] != last_date:
            last_date = msg["date"]
            divider = f"─── {msg['date']} ───"
            lines_out.append(f"\n{divider.center(WIDTH)}\n")

        time_str = msg["time"] or ""
        sender = msg["sender"]
        text = msg["message"]

        if sender == "System":
            lines_out.append(f"{'  📞 ' + text:^{WIDTH}}")
            continue

        is_agent = sender != "Contact"

        # Word-wrap text to fit inside bubble
        max_width = WIDTH - 6
        words = text.split()
        wrapped, current = [], []
        current_len = 0
        for word in words:
            if current_len + len(word) + (1 if current else 0) > max_width:
                wrapped.append(" ".join(current))
                current = [word]
                current_len = len(word)
            else:
                current.append(word)
                current_len += len(word) + (1 if len(current) > 1 else 0)
        if current:
            wrapped.append(" ".join(current))

        bubble_width = min(max(len(l) for l in wrapped) + 4, WIDTH - 4)

        if is_agent:
            # Right-aligned (agent)
            header = f"{'📤 ' + sender + '  ' + time_str:>{WIDTH}}"
            top    = f"{'┌' + '─' * bubble_width + '┐':>{WIDTH}}"
            for wl in wrapped:
                row = f"│ {wl:<{bubble_width - 2}} │"
                lines_out.append(f"{row:>{WIDTH}}")
            lines_out.insert(-len(wrapped), header)
            lines_out.insert(-len(wrapped), top)
            lines_out.append(f"{'└' + '─' * bubble_width + '┘':>{WIDTH}}")
        else:
            # Left-aligned (contact)
            header = f"📥 {contact_name}  {time_str}"
            top    = f"┌{'─' * bubble_width}┐"
            lines_out.append(header)
            lines_out.append(top)
            for wl in wrapped:
                lines_out.append(f"│ {wl:<{bubble_width - 2}} │")
            lines_out.append(f"└{'─' * bubble_width}┘")

        lines_out.append("")   # spacing between bubbles

    return "\n".join(lines_out)
