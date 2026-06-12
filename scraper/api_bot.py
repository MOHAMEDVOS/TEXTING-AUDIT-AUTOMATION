"""
SmarterContact API Bot — drop-in replacement for browser_bot.SmarterContactBot.

Uses direct HTTP GraphQL requests instead of Playwright browser automation.
Same constructor signature, same extract_all() return shape, same filtering logic.
No Playwright, no Chromium, no DOM manipulation.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import DEFAULT_SAMPLE_SIZE, get_now
from scraper.firebase_auth import firebase_sign_in
from scraper.gql_client import SmarterContactGQL, _date_range_for_filter
import re

_ALL_LABEL_FILTER_VALUES = {"all labels", "all label", "all lable", "all", "all lables"}


def _is_all_labels_filter_value(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
    return normalized in _ALL_LABEL_FILTER_VALUES


def normalize_label_filter(labels: str | None) -> str | None:
    """
    Normalize the optional SmarterContact label filter.
    Copied locally to avoid importing browser_bot (which loads Playwright).
    """
    if not labels:
        return None
    requested = [label.strip() for label in labels.split(",") if label.strip()]
    if not requested:
        return None
    requested = [label for label in requested if not _is_all_labels_filter_value(label)]
    if not requested:
        return None
    return ",".join(requested)


logger = logging.getLogger(__name__)

# Concurrent message fetches per agent (safe for SC's rate limiter)
MSG_BATCH_SIZE = 10


def _build_transcript(messages: list[dict], agent_name: str) -> tuple[str, list[dict]]:
    """
    Convert GQL message objects → (full_transcript_str, parsed_messages_list).

    full_transcript_str  → passed to parse_transcript() (same as browser inner_text)
    parsed_messages_list → list of {sender, message, time, date}
    """
    parsed = []
    for msg in messages:                            # already sorted ASC by GQL
        direction = msg.get("direction", "")
        content   = (msg.get("content") or "").strip()
        created   = msg.get("createdAt", "")

        if not content:
            continue

        sender = (agent_name.split()[0] if agent_name else "Agent") \
            if direction == "OUTGOING" else "Contact"

        try:
            dt       = datetime.fromisoformat(created.replace("Z", "+00:00"))
            time_str = dt.strftime("%I:%M %p").lstrip("0") or "12:00 AM"
            date_str = dt.strftime("%A, %B %d, %Y").replace(" 0", " ")
        except Exception:
            time_str = date_str = ""

        parsed.append({
            "sender": sender,
            "message": content,
            "time": time_str,
            "date": date_str,
        })

    lines, last_date = [], None
    for m in parsed:
        if m["date"] and m["date"] != last_date:
            lines.append(m["date"])
            last_date = m["date"]
        if m["time"]:
            lines.append(m["time"])
        lines.append(m["message"])

    return "\n".join(lines), parsed


class SmarterContactAPIBot:
    """
    HTTP-based replacement for SmarterContactBot.

    Constructor accepts the EXACT same keyword arguments so queue_manager.py
    requires only a one-line import change.
    """

    def __init__(
        self,
        agent_name: str,
        email: str,
        password: str,
        worker_id: int = 0,
        date_filter: str = "today",
        limit: int = None,
        date_start: str = None,
        date_end: str = None,
        labels: str = None,
        blacklist_any: set = None,
        blacklist_only: set = None,
    ):
        self.agent_name = agent_name
        self.email = email
        self.password = password
        self.worker_id = worker_id
        self.date_filter = date_filter
        self.limit = limit or DEFAULT_SAMPLE_SIZE
        self.date_start = date_start
        self.date_end = date_end
        self.blacklist_any = {s.lower() for s in (blacklist_any or {"extra"})}
        self.blacklist_only = {s.lower() for s in (blacklist_only or {"new lead"})}

        raw_labels = normalize_label_filter(labels)
        if raw_labels:
            self.include_labels = {l.strip().lower() for l in raw_labels.split(",") if l.strip()}
        else:
            self.include_labels = None

    async def extract_all(self, db: "Database | None" = None) -> dict:
        """
        Full extraction pipeline:
          1. Firebase auth
          2. Get unread count
          3. Paginate conversations with all filters applied
          4. Fetch full message thread for each
          5. Build transcript + run parse_transcript
          6. Return result dict (same shape as browser_bot.extract_all)
        """
        started_at = get_now().isoformat()
        result = {
            "agent_name": self.agent_name,
            "email": self.email,
            "worker_id": self.worker_id,
            "started_at": started_at,
            "status": "error",
            "conversations": [],
            "unread_conversations": [],
            "unread_count": 0,
            "reporting": {},
            "errors": [],
        }

        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            # ---- 1. Firebase auth ----
            logger.info(f"[STEP] [{self.agent_name}] Authenticating with Firebase...")
            try:
                auth = await firebase_sign_in(self.email, self.password, http)
                logger.info(f"[STEP] [{self.agent_name}] Firebase auth OK")
            except Exception as e:
                logger.error(f"[STEP] [{self.agent_name}] Firebase auth FAILED: {e}")
                result["status"] = "login_failed"
                result["errors"].append(str(e))
                return result

            gql = SmarterContactGQL(auth=auth, client=http)

            # ---- 2. Unread count ----
            try:
                result["unread_count"] = await gql.get_unread_count()
                logger.info(f"[STEP] [{self.agent_name}] Unread count: {result['unread_count']}")
            except Exception as e:
                logger.warning(f"[STEP] [{self.agent_name}] Could not get unread count: {e}")

            # ---- 3. Paginate conversations (all filters applied) ----
            date_start_dt, date_end_dt = _date_range_for_filter(self.date_filter, self.date_start, self.date_end)
            date_range_str = f"{date_start_dt.date() if date_start_dt else 'any'} → {date_end_dt.date() if date_end_dt else 'any'}"
            logger.info(
                f"[STEP] [{self.agent_name}] Fetching conversations: "
                f"filter={self.date_filter!r} range={date_range_str} "
                f"limit={self.limit} labels={self.include_labels or 'all'}"
            )

            try:
                convos = await gql.find_conversations(
                    date_start=date_start_dt,
                    date_end=date_end_dt,
                    include_labels=self.include_labels,
                    blacklist_any=self.blacklist_any,
                    blacklist_only=self.blacklist_only,
                    limit=self.limit,
                )
            except Exception as e:
                logger.error(f"[STEP] [{self.agent_name}] Conversation fetch FAILED: {e}")
                result["errors"].append(str(e))
                return result

            if not convos:
                logger.warning(f"[STEP] [{self.agent_name}] 0 eligible conversations in range {date_range_str}")
                result["status"] = "success"
                return result

            logger.info(f"[STEP] [{self.agent_name}] Found {len(convos)} conversations to process")

            # ---- 4-5. Fetch full threads + build transcripts ----
            if db is None:
                raise ValueError("extract_all requires an initialized db (pass db= from the caller)")

            extracted = []
            errors = []

            async def process_one(convo: dict, idx: int) -> Optional[dict]:
                contact_id = convo["id"]
                contact_name = convo.get("name") or f"Contact_{idx}"
                labels = [l["title"] for l in (convo.get("labels") or [])]

                # dedup: skip already-audited chats (only for "today" runs;
                # historical date ranges should always re-audit)
                if self.date_filter == "today":
                    try:
                        last_msg_content = convo.get("lastMessage", {}).get("content") or ""
                        if await db.is_chat_audited(self.email, contact_name, last_msg_content):
                            logger.debug(f"  Skip (already audited): {contact_name}")
                            return None
                    except Exception as e:
                        logger.debug(f"  Dedup check failed for {contact_name}: {e}")

                # fetch full message thread
                try:
                    messages = await gql.find_messages(contact_id)
                except Exception as e:
                    logger.warning(f"  Message fetch failed for {contact_name}: {e}")
                    errors.append(f"{contact_name}: {e}")
                    return None

                full_transcript, parsed_messages = _build_transcript(messages, agent_name=self.agent_name)

                last_at = convo.get("lastMessageAt") or ""
                try:
                    convo_date = datetime.fromisoformat(last_at.replace("Z", "+00:00")).strftime("%m/%d/%Y") if last_at else ""
                except Exception:
                    convo_date = ""

                return {
                    "contact_name": contact_name,
                    "assigned_labels": labels,
                    "index": idx,
                    "extracted_at": datetime.now(timezone.utc).isoformat(),
                    "convo_date": convo_date,
                    "full_transcript": full_transcript,
                    "parsed_messages": parsed_messages,
                }

            for batch_start in range(0, len(convos), MSG_BATCH_SIZE):
                batch = convos[batch_start:batch_start + MSG_BATCH_SIZE]
                tasks = [process_one(c, batch_start + i) for i, c in enumerate(batch)]
                batch_r = await asyncio.gather(*tasks, return_exceptions=False)
                for r in batch_r:
                    if r is not None:
                        extracted.append(r)

            result["conversations"] = extracted
            result["errors"] = errors
            result["status"] = "success"

            logger.info(
                f"[Worker-{self.worker_id}] {self.agent_name}: extracted {len(extracted)} conversations "
                f"({'errors: ' + str(len(errors)) if errors else 'no errors'})"
            )

        return result
