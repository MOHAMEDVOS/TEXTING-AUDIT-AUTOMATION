"""
SmarterContact GraphQL HTTP Client.

Wraps all GQL operations used by the audit — no browser required.
All query strings are the REAL queries captured from live browser traffic.
"""
import asyncio
import calendar
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from scraper.firebase_auth import AuthSession

logger = logging.getLogger(__name__)

GQL_URL = "https://api.smartercontact.com/gql"

# ── Captured GQL queries (verbatim from browser network sniffer) ──────────────

Q_FIND_CONVERSATIONS = """
query FindConversations($filter: ConversationFilter, $pagination: Pagination, $order: ConversationOrder) {
  findConversations(filter: $filter, pagination: $pagination, order: $order) {
    items {
      id
      name
      isRead
      unreadMessages
      lastMessageAt
      createdAt
      labels { id title color __typename }
      lastMessage { id direction content contentType __typename }
      __typename
    }
    hasNext
    nextId
    nextCreatedAt
    __typename
  }
}
"""

Q_FIND_MESSAGES = """
query FindConversationMessages($contactId: String!, $pagination: Pagination, $order: MessageOrder) {
  findConversationMessages(contactId: $contactId, pagination: $pagination, order: $order) {
    items {
      id
      direction
      type
      createdAt
      content
      contentType
      campaign { id name __typename }
      __typename
    }
    total
    hasNext
    nextId
    nextCreatedAt
    __typename
  }
}
"""

Q_UNREAD_COUNT = """
query GetUnreadConversationsCounters {
  getUnreadConversationsCounters {
    withUnreadMessages
    withMissedCalls
    __typename
  }
}
"""

Q_FIND_LABELS = """
query FindLabels($pagination: CursorPagination) {
  findLabels(pagination: $pagination) {
    items { id title color default scopes readOnly createdAt __typename }
    total
    nextPageToken
    __typename
  }
}
"""


def _date_range_for_filter(date_filter: str, date_start: str = None, date_end: str = None):
    """
    Convert a date_filter string to (start_dt, end_dt) datetime objects (UTC, tz-naive).

    Supported values (same as browser_bot.py):
        today | last_week | this_month | last_month | last_30_days | last_year | all_time
        custom (uses date_start / date_end strings "YYYY-MM-DD")
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if date_filter == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif date_filter == "last_week":
        from datetime import timedelta
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0)
        end = now
    elif date_filter == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0)
        end = now
    elif date_filter == "last_month":
        m, y = (now.month - 1, now.year) if now.month > 1 else (12, now.year - 1)
        last_day = calendar.monthrange(y, m)[1]
        start = datetime(y, m, 1, 0, 0, 0)
        end = datetime(y, m, last_day, 23, 59, 59)
    elif date_filter == "last_30_days":
        from datetime import timedelta
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0)
        end = now
    elif date_filter == "last_year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0)
        end = now
    elif date_filter == "all_time":
        return None, None
    elif date_filter == "custom" and date_start and date_end:
        start = datetime.strptime(date_start, "%Y-%m-%d")
        end = datetime.strptime(date_end, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    else:
        logger.warning(f"Unknown date_filter '{date_filter}' — fetching all")
        return None, None

    return start, end


def _in_range(ts_str: str, start: Optional[datetime], end: Optional[datetime]) -> bool:
    """Check if an ISO timestamp string falls within [start, end]. None = no bound."""
    if start is None and end is None:
        return True
    if not ts_str:
        return False
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
        if start and dt < start:
            return False
        if end and dt > end:
            return False
        return True
    except Exception:
        return False


class SmarterContactGQL:
    """
    Async GraphQL client for api.smartercontact.com/gql.
    All calls require a live AuthSession.
    """

    def __init__(self, auth: AuthSession, client: httpx.AsyncClient):
        self.auth = auth
        self.client = client

    async def _post(self, operation: str, query: str, variables: dict) -> dict:
        token = await self.auth.ensure_fresh(self.client)
        resp = await self.client.post(
            GQL_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "operationName": operation,
                "query": query,
                "variables": variables,
            },
        )
        data = resp.json()
        if "errors" in data:
            msg = data["errors"][0].get("message", "GQL error")
            raise RuntimeError(f"[{operation}] {msg}")
        return data.get("data", {})

    async def get_unread_count(self) -> int:
        data = await self._post("GetUnreadConversationsCounters", Q_UNREAD_COUNT, {})
        return data.get("getUnreadConversationsCounters", {}).get("withUnreadMessages", 0)

    async def get_labels(self) -> list[str]:
        data = await self._post("FindLabels", Q_FIND_LABELS, {
            "pagination": {"limit": 100}
        })
        items = data.get("findLabels", {}).get("items", [])
        return [i["title"] for i in items]

    async def find_conversations(
        self,
        date_start: Optional[datetime] = None,
        date_end: Optional[datetime] = None,
        include_labels: Optional[set[str]] = None,
        blacklist_any: set[str] = frozenset(),
        blacklist_only: set[str] = frozenset(),
        limit: int = 20,
        batch_size: int = 50,
    ) -> list[dict]:
        """
        Paginate through the inbox and return up to `limit` eligible conversations.

        Eligible means:
          - lastMessageAt in [date_start, date_end]   (client-side, same as SC browser)
          - isRead == True AND unreadMessages == 0
          - has at least one label
          - if include_labels set: at least one label matches
          - not matching blacklist_any
          - not all labels in blacklist_only
        """
        eligible = []
        next_id, next_ts, page = None, None, 0

        while len(eligible) < limit * 2:
            pg = {"moveTo": "NEXT", "limit": batch_size}
            if next_id:
                pg["nextId"] = next_id
                pg["nextCreatedAt"] = next_ts

            data = await self._post("FindConversations", Q_FIND_CONVERSATIONS, {
                "filter": {"category": "ALL", "profileId": None},
                "pagination": pg,
                "order": {},
            })
            result = data.get("findConversations", {})
            items = result.get("items", [])
            page += 1

            if not items:
                break

            for c in items:
                if self._is_eligible(c, date_start, date_end,
                                     include_labels, blacklist_any, blacklist_only):
                    eligible.append(c)

            if date_start:
                oldest_ts = items[-1].get("lastMessageAt", "")
                if oldest_ts:
                    try:
                        oldest_dt = datetime.fromisoformat(oldest_ts.replace("Z", "+00:00")).replace(tzinfo=None)
                        if oldest_dt < date_start:
                            logger.debug(f"Reached date boundary at page {page}")
                            break
                    except Exception:
                        pass

            if not result.get("hasNext"):
                break

            next_id = result.get("nextId")
            next_ts = result.get("nextCreatedAt")

        logger.debug(f"find_conversations: {page} pages, {len(eligible)} eligible found")
        return eligible[:limit]

    async def find_messages(self, contact_id: str, batch_size: int = 200) -> list[dict]:
        """
        Fetch ALL messages for a conversation (handles pagination automatically).
        Returns list sorted oldest-first.
        """
        all_msgs = []
        next_id, next_ts = None, None

        while True:
            pg = {"moveTo": "NEXT", "limit": batch_size}
            if next_id:
                pg["nextId"] = next_id
                pg["nextCreatedAt"] = next_ts

            data = await self._post("FindConversationMessages", Q_FIND_MESSAGES, {
                "contactId": contact_id,
                "pagination": pg,
                "order": {"by": "CREATED_AT", "direction": "ASC"},
            })
            page_data = data.get("findConversationMessages", {})
            batch = page_data.get("items", [])
            all_msgs.extend(batch)

            if not page_data.get("hasNext"):
                break

            next_id = page_data.get("nextId")
            next_ts = page_data.get("nextCreatedAt")

        return all_msgs

    @staticmethod
    def _is_eligible(c: dict, date_start: Optional[datetime], date_end: Optional[datetime],
                     include_labels: Optional[set[str]], blacklist_any: set[str],
                     blacklist_only: set[str]) -> bool:
        labels = [l["title"] for l in (c.get("labels") or [])]
        labels_lower = {l.lower() for l in labels}

        ts = c.get("lastMessageAt") or c.get("createdAt") or ""
        if not _in_range(ts, date_start, date_end):
            return False

        if c.get("unreadMessages", 0) > 0 or not c.get("isRead", True):
            return False

        if not labels:
            return False

        if include_labels and not (labels_lower & include_labels):
            return False

        if labels_lower & blacklist_any:
            return False

        if blacklist_only and labels_lower and labels_lower.issubset(blacklist_only):
            return False

        return True
