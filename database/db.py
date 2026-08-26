"""
Database module - PostgreSQL storage for audit data.
Stores extracted data, AI analysis results, and audit scores.
Uses asyncpg for async access; psycopg2 is used by synchronous callers (session_logger, dream_worker).
"""
import asyncpg
import json
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from config.settings import DATABASE_URL, get_now

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

def _parse_msg_datetime(msg: dict) -> datetime | None:
    """
    Combine the scraper's separate 'date' and 'time' fields into a datetime.

    Scraped format:
      msg["date"] = "Thursday, March 26, 2026"  (empty "" for today's messages)
      msg["time"] = "05:59 PM"

    If 'sent_at' or 'timestamp' already holds an ISO string, use that directly.
    Falls back to today's date when 'date' is empty (same-session messages).
    Returns None only when no usable time field exists.
    """
    # Already a full ISO datetime (e.g. from DB re-read or test fixtures)
    for key in ("sent_at", "timestamp"):
        raw = msg.get(key)
        if raw and isinstance(raw, datetime):
            return raw
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

    time_str = (msg.get("time") or "").strip()
    if not time_str:
        return None

    date_str = (msg.get("date") or "").strip()
    if not date_str:
        # No date divider anywhere — leave unresolved. Never assume "today":
        # scrapes run long after the conversation. Ordering uses `seq`, not time.
        return None

    # Parse "Thursday, March 26, 2026"
    try:
        parts = date_str.replace(",", "").split()
        # parts = ["Thursday", "March", "26", "2026"]
        month = _MONTH_MAP.get(parts[1].lower())
        day = int(parts[2])
        year = int(parts[3])
        msg_date = date(year, month, day)
    except Exception:
        return None

    try:
        t = datetime.strptime(time_str, "%I:%M %p")
        # Stamp UTC explicitly. api_bot renders these 'date'/'time' strings from
        # the UTC `createdAt`, so UTC is their true meaning — and leaving them
        # naive would make sent_at depend on the server's session timezone,
        # which silently shifts attribution once ownership is tracked by minute.
        return datetime(msg_date.year, msg_date.month, msg_date.day,
                        t.hour, t.minute, tzinfo=timezone.utc)
    except Exception:
        return None


def _is_outgoing(sender: str) -> bool:
    """Agent-sent messages are the only ones a texter can be accountable for."""
    return (sender or "").strip().lower() not in ("contact", "lead", "unknown", "")


async def attribute_flag_details(conn, conversation_id: int,
                                 flag_details: list[dict],
                                 culprits: dict | None) -> None:
    """
    Name the texter responsible for each flag.

    A shuffled account means one conversation can have two authors, so a flag
    belongs to whoever owned the account at the moment that decided it — the
    offending message for something the agent said, the lead's message for
    something the agent failed to say. Mutates `flag_details` in place.

    Shared by every write path so a flag can never be attributed two ways.
    """
    if not flag_details:
        return
    from ai.prefilter._guards import canon_flag_text

    row = await conn.fetchrow(
        """SELECT a.email AS account_email, c.texter_name
             FROM conversations c
             JOIN accounts a ON a.id = c.agent_id
            WHERE c.id = $1""",
        conversation_id,
    )
    if not row:
        return
    fallback = row["texter_name"]

    periods = [
        dict(r) for r in await conn.fetch(
            """SELECT texter_name, started_at, ended_at
                 FROM assignment_periods
                WHERE LOWER(account_email) = LOWER($1)
                ORDER BY started_at""",
            row["account_email"] or "",
        )
    ]

    for detail in flag_details:
        if not isinstance(detail, dict):
            continue
        text = detail.get("flag_text") or ""
        ref = (culprits or {}).get(canon_flag_text(text))
        if ref is None and str(text).lower().startswith("wrong label:"):
            ref = (culprits or {}).get("__label__")

        if not ref:
            # Scored before culprits were recorded, or by a tier that doesn't
            # emit them — keep the conversation owner but label it 'legacy'
            # rather than passing it off as a verified attribution.
            detail["texter_name"] = fallback
            detail["attribution"] = "legacy"
            continue

        when = _parse_msg_datetime({
            "timestamp": ref.get("at"),
            "date": ref.get("date"),
            "time": ref.get("time"),
        })
        texter, attribution = _resolve_texter(periods, when, fallback)
        detail["texter_name"]       = texter
        detail["attribution"]       = attribution
        detail["attribution_basis"] = ref.get("basis")
        if when is not None:
            detail["culprit_at"] = when.isoformat()


def _resolve_texter(periods: list[dict], sent_at: datetime | None,
                    fallback: str | None) -> tuple[str | None, str]:
    """
    Map a message timestamp onto the ownership periods for its account.

    Returns (texter_name, attribution):
      exact      — a period covers this instant
      unassigned — a timestamp exists but nobody owned the account then
      inferred   — no usable timestamp, so the conversation-level owner is used
    """
    if sent_at is None:
        return fallback, "inferred"
    for p in periods:
        if p["started_at"] <= sent_at and (p["ended_at"] is None or sent_at < p["ended_at"]):
            return p["texter_name"], "exact"
    return None, "unassigned"


class Database:
    """Async PostgreSQL database for storing audit data."""

    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or DATABASE_URL
        self.pool: asyncpg.Pool | None = None
        # Whether migration 007 has installed
        # UNIQUE(agent_id, contact_id, audit_date) on conversations. Probed once at
        # initialize(). Lets save_extraction() upsert where the constraint exists
        # and fall back to a plain INSERT where it does not, so the code is safe to
        # deploy before or after the migration is applied (deep review F7).
        self._has_convo_unique: bool = False

    async def initialize(self):
        """Create connection pool and ensure all tables exist.

        Schema execution is bypassed if tables already exist to prevent relation lock
        deadlocks in concurrent agent runs, while connection pool limits are set to
        1-2 to avoid PostgreSQL client connection exhaustion.
        """
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=2)
        async with self.pool.acquire() as conn:
            # Check if database tables already exist
            tables_exist = await conn.fetchval(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'conversations')"
            )
            if not tables_exist:
                logger.info("Database schema not found. Initializing database schema...")
                schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
                # Acquire a session-level advisory lock so only one process runs
                # the schema at a time. Other processes block here (not deadlock).
                await conn.execute("SELECT pg_advisory_lock(987654321)")
                try:
                    await conn.execute(schema_sql)
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(987654321)")
            else:
                logger.debug("Database schema already initialized. Skipping schema DDL execution.")

            self._has_convo_unique = bool(await conn.fetchval(
                """SELECT EXISTS (
                       SELECT 1 FROM pg_constraint
                        WHERE conname = 'uq_conversations_agent_contact_day'
                   )"""
            ))
            if not self._has_convo_unique:
                logger.warning(
                    "Re-ingestion dedup constraint missing — every re-run will "
                    "duplicate conversations. Apply "
                    "database/migrations/007_data_integrity.sql (deep review F7)."
                )
        logger.info("Database initialized (PostgreSQL)")

    async def close(self):
        """Close the connection pool."""
        if self.pool:
            await self.pool.close()
            self.pool = None

    # ── Agents ────────────────────────────────────────────────────────────────

    async def upsert_agent(self, name: str, email: str) -> int:
        """Insert or find an agent. Returns agent_id."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM accounts WHERE email = $1", email
            )
            if row:
                return row["id"]
            row = await conn.fetchrow(
                "INSERT INTO accounts (name, email) VALUES ($1, $2) RETURNING id",
                name, email,
            )
            return row["id"]

    async def get_all_agents(self) -> list[dict]:
        """Get all registered agents."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM accounts ORDER BY name")
            return [dict(r) for r in rows]

    async def get_latest_extraction(self, agent_email: str) -> dict:
        """Get the most recent extraction metadata for an agent."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT e.*, a.name, a.email
                   FROM extractions e
                   JOIN accounts a ON e.agent_id = a.id
                   WHERE a.email = $1
                   ORDER BY e.extracted_at DESC LIMIT 1""",
                agent_email,
            )
            return dict(row) if row else {}

    # ── Contacts ──────────────────────────────────────────────────────────────

    async def _upsert_contact(self, conn: asyncpg.Connection, name: str) -> int:
        """Insert or find a contact by name. Returns contact_id.

        Matching is case- and whitespace-insensitive to agree with every reader,
        which normalises via LOWER(TRIM(...)). The old exact match meant
        "John Smith" and "john smith" were separate contacts at write time but
        the same contact at read time, and the dashboard silently dropped one of
        the two conversations (deep review F29).

        Still a check-then-act, but now race-safe: up to 20 audit subprocesses
        run concurrently, so two can pass the SELECT for the same new contact.
        Once migration 007 installs the unique index the loser gets a
        UniqueViolationError and re-reads the winner's row. Before the migration
        runs this behaves exactly as before, so it is safe to deploy either way.
        """
        _lookup = (
            "SELECT id FROM contacts "
            "WHERE LOWER(TRIM(name)) = LOWER(TRIM($1)) ORDER BY id LIMIT 1"
        )
        row = await conn.fetchrow(_lookup, name)
        if row:
            return row["id"]
        try:
            row = await conn.fetchrow(
                "INSERT INTO contacts (name) VALUES ($1) RETURNING id", name
            )
            return row["id"]
        except asyncpg.UniqueViolationError:
            # Lost the race — the concurrent writer's row is authoritative.
            row = await conn.fetchrow(_lookup, name)
            if row:
                return row["id"]
            raise

    # ── Extractions + Conversations + Messages ────────────────────────────────

    async def save_extraction(self, agent_id: int, result: dict) -> list[dict]:
        """
        Save extraction results to the database.

        Inserts:
          - One row into extractions (metadata)
          - One row per conversation into conversations + contacts
          - One row per message into messages

        Each conversation dict gets a 'conversation_id' key injected so the
        scorer can reference it when writing conversation_scores.

        Returns the conversations list with conversation_id injected.
        """
        conversations = result.get("conversations", [])
        extracted_at_str = result.get("started_at", get_now().isoformat())

        # Parse extracted_at — accept ISO strings with or without timezone
        try:
            if extracted_at_str.endswith("Z"):
                extracted_at_str = extracted_at_str[:-1] + "+00:00"
            extracted_at = datetime.fromisoformat(extracted_at_str)
        except Exception:
            extracted_at = get_now()

        audit_date = extracted_at.date() if hasattr(extracted_at, "date") else date.today()

        # Get the texter name for this agent from account_assignments.
        # Use the assignment that was active ON the audit date (assigned_date <= audit_date),
        # not the latest assignment overall — fixes the cross-date texter mismatch bug.
        # If no assignment exists on or before the audit date, fall back to the agent name.
        texter_name = result.get("agent_name", "Unknown")
        async with self.pool.acquire() as conn:
            account_email = await conn.fetchval(
                "SELECT email FROM accounts WHERE id = $1", agent_id
            )
            assignment = await conn.fetchrow(
                """SELECT agent_name FROM account_assignments
                   WHERE account_email = (SELECT email FROM accounts WHERE id = $1)
                     AND assigned_date <= $2
                   ORDER BY assigned_date DESC LIMIT 1""",
                agent_id,
                audit_date,
            )
            if assignment:
                texter_name = assignment["agent_name"]

            # Ownership timeline for this account — fetched once, then matched
            # in memory so a mid-day shuffle attributes each message to whoever
            # actually owned the account at the minute it was sent.
            periods = [
                dict(r) for r in await conn.fetch(
                    """SELECT texter_name, started_at, ended_at
                         FROM assignment_periods
                        WHERE LOWER(account_email) = LOWER($1)
                        ORDER BY started_at""",
                    account_email or "",
                )
            ]

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                reporting_data = result.get("reporting", {}) or {}
                errors_data    = result.get("errors", [])     or []
                # asyncpg requires JSON-serialized strings for JSONB columns
                await conn.execute(
                    """INSERT INTO extractions
                           (agent_id, extracted_at, status, reporting_data, page_text, errors)
                       VALUES ($1, $2, $3, $4::jsonb, $5, $6::jsonb)""",
                    agent_id,
                    extracted_at,
                    result.get("status", "unknown"),
                    json.dumps(reporting_data),
                    reporting_data.get("page_text", "") if isinstance(reporting_data, dict) else "",
                    json.dumps(errors_data),
                )

                # Insert each conversation + its messages
                for convo in conversations:
                    contact_name = convo.get("contact_name") or "Unknown"
                    contact_id = await self._upsert_contact(conn, contact_name)

                    labels = convo.get("assigned_labels") or []
                    convo_date = (convo.get("convo_date") or "").strip()

                    # Upsert, not a bare INSERT. Re-running an agent for the same
                    # day used to append a complete duplicate set of conversations,
                    # messages and scores every time (deep review F7). Migration 007
                    # adds the matching UNIQUE(agent_id, contact_id, audit_date);
                    # until it is applied the ON CONFLICT clause simply never fires,
                    # so this is safe to deploy before or after the migration.
                    conv_row = await conn.fetchrow(
                        """INSERT INTO conversations
                               (agent_id, contact_id, texter_name, assigned_labels, extracted_at, audit_date, convo_date)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           ON CONFLICT ON CONSTRAINT uq_conversations_agent_contact_day
                           DO UPDATE SET texter_name     = EXCLUDED.texter_name,
                                         assigned_labels = EXCLUDED.assigned_labels,
                                         extracted_at    = EXCLUDED.extracted_at,
                                         convo_date      = EXCLUDED.convo_date,
                                         is_archived     = FALSE
                           RETURNING id""",
                        agent_id,
                        contact_id,
                        texter_name,
                        labels,
                        extracted_at,
                        audit_date,
                        convo_date,
                    ) if self._has_convo_unique else await conn.fetchrow(
                        """INSERT INTO conversations
                               (agent_id, contact_id, texter_name, assigned_labels, extracted_at, audit_date, convo_date)
                           VALUES ($1, $2, $3, $4, $5, $6, $7)
                           RETURNING id""",
                        agent_id,
                        contact_id,
                        texter_name,
                        labels,
                        extracted_at,
                        audit_date,
                        convo_date,
                    )
                    conversation_id = conv_row["id"]
                    convo["conversation_id"] = conversation_id

                    # Re-scraping an existing conversation replaces its messages
                    # rather than appending them a second time.
                    if self._has_convo_unique:
                        await conn.execute(
                            "DELETE FROM messages WHERE conversation_id = $1",
                            conversation_id,
                        )

                    # Insert messages (seq preserves chronological scrape order)
                    parsed_messages = convo.get("parsed_messages") or []
                    for idx, msg in enumerate(parsed_messages):
                        sender = msg.get("sender", "unknown")
                        body = msg.get("message") or msg.get("text") or ""
                        sent_at = _parse_msg_datetime(msg)
                        sc_date_label = (msg.get("date") or "").strip()

                        # Inbound messages are not anyone's work product, so they
                        # carry no texter. Outbound ones resolve against the
                        # ownership timeline; the result is stored, not recomputed
                        # at read time, so an audit stays reproducible.
                        if _is_outgoing(sender):
                            msg_texter, attribution = _resolve_texter(
                                periods, sent_at, texter_name
                            )
                        else:
                            msg_texter, attribution = None, None

                        await conn.execute(
                            """INSERT INTO messages
                               (conversation_id, sender, body, sent_at, sc_date_label,
                                seq, texter_name, attribution)
                               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                            conversation_id, sender, body, sent_at, sc_date_label, idx,
                            msg_texter, attribution,
                        )

        logger.info(
            f"Saved extraction for agent_id={agent_id}: "
            f"{len(conversations)} conversations, texter='{texter_name}'"
        )
        return conversations

    async def save_results(self, results: list):
        """Save all extraction results from a run, injecting conversation_ids back."""
        for result in results:
            agent_id = await self.upsert_agent(
                result.get("agent_name", "Unknown"),
                result.get("email", "unknown@unknown.com"),
            )
            conversations = await self.save_extraction(agent_id, result)
            result["_all_conversations"] = conversations

    async def count_valid_scored_conversations(self, agent_id: int) -> int:
        """Count active conversations with a successful (non-null) score."""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """SELECT COUNT(DISTINCT c.id)
                   FROM conversations c
                   JOIN conversation_scores cs ON cs.conversation_id = c.id
                   WHERE c.agent_id = $1
                     AND c.is_archived = FALSE
                     AND cs.compliance_score IS NOT NULL""",
                agent_id,
            ) or 0

    # NOTE: save_conversation_score() was removed here (deep review F36). It had
    # zero callers — ai/scorer.py inlines its own INSERT — and would have failed
    # if called, binding a Python list to the red_flags JSONB column with no
    # ::jsonb cast.

    async def get_conversation_messages(self, conversation_id: int) -> list[dict]:
        """Return all messages for a conversation, ordered by sent_at."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT sender, body AS message, sent_at AS time, sc_date_label
                   FROM messages
                   WHERE conversation_id = $1
                   ORDER BY seq ASC, id ASC""",
                conversation_id,
            )
            return [dict(r) for r in rows]

    # ── Audited chats (dedup cache) ───────────────────────────────────────────

    async def is_chat_audited(self, agent_email: str, contact_name: str, message_preview: str) -> bool:
        """Check if a chat with this exact message preview was already audited."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id FROM audited_chats
                   WHERE agent_email = $1 AND contact_name = $2 AND message_preview = $3""",
                agent_email, contact_name, message_preview,
            )
            return bool(row)

    async def mark_chat_audited(self, agent_email: str, contact_name: str, message_preview: str):
        """Mark a chat as audited, updating the preview if the contact already exists."""
        async with self.pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO audited_chats (agent_email, contact_name, message_preview, audited_at)
                   VALUES ($1, $2, $3, NOW())
                   ON CONFLICT (agent_email, contact_name)
                   DO UPDATE SET message_preview = EXCLUDED.message_preview,
                                 audited_at = NOW()""",
                agent_email, contact_name, message_preview,
            )

    async def cleanup_failed_audits(self, agent_id: int | None = None) -> int:
        """
        Remove conversations that failed to score so the next run retries them.
        Covers two failure modes:
          1. Rate-limit skips  — summary = 'Analysis skipped: Could not score...' + NULL scores
          2. Ghost rows        — conversation exists but no score row was ever written

        When `agent_id` is provided, cleanup is scoped to that agent only — critical
        for parallel runs where multiple subprocesses each have in-flight conversations
        that haven't been scored yet. Without scoping, one subprocess finishing first
        will delete another's still-being-scored rows, causing FK violations.

        Deletes from audited_chats (dedup cache) and conversations (cascades to scores+messages).
        Also strips failed entries from audit_scores.details so UI counts are correct.
        Returns total count of conversations cleaned up.
        """
        import json as _json

        _PATTERN = "Analysis skipped: Could not score%"
        _EXCLUDE  = "%request stayed too large%"
        count = 0

        # Build agent scope clauses (parameterized via $3 if agent_id provided)
        agent_clause_cs   = " AND c.agent_id = $3" if agent_id is not None else ""
        agent_clause_conv = " AND agent_id = $3" if agent_id is not None else ""
        params_full = (_PATTERN, _EXCLUDE, agent_id) if agent_id is not None else (_PATTERN, _EXCLUDE)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # ── Case 1: rate-limit skips (have a score row, all scores NULL, skip summary) ──
                row = await conn.fetchrow(
                    f"""SELECT COUNT(*) AS n FROM conversation_scores cs
                        JOIN conversations c ON c.id = cs.conversation_id
                        WHERE cs.summary LIKE $1 AND cs.summary NOT LIKE $2
                          AND cs.compliance_score IS NULL AND cs.sentiment_score IS NULL
                          {agent_clause_cs}""",
                    *params_full,
                )
                count += row["n"] if row else 0

                await conn.execute(
                    f"""DELETE FROM audited_chats ac
                        WHERE EXISTS (
                            SELECT 1 FROM conversation_scores cs
                            JOIN conversations c ON c.id = cs.conversation_id
                            JOIN accounts a      ON a.id = c.agent_id
                            JOIN contacts co     ON co.id = c.contact_id
                            WHERE cs.summary LIKE $1 AND cs.summary NOT LIKE $2
                              AND cs.compliance_score IS NULL AND cs.sentiment_score IS NULL
                              AND ac.agent_email = a.email AND ac.contact_name = co.name
                              {agent_clause_cs}
                        )""",
                    *params_full,
                )
                await conn.execute(
                    f"""DELETE FROM conversations WHERE id IN (
                            SELECT cs.conversation_id FROM conversation_scores cs
                            JOIN conversations c ON c.id = cs.conversation_id
                            WHERE cs.summary LIKE $1 AND cs.summary NOT LIKE $2
                              AND cs.compliance_score IS NULL AND cs.sentiment_score IS NULL
                              {agent_clause_cs}
                        )""",
                    *params_full,
                )

                # ── Case 2: ghost rows — extracted but scoring never ran ──
                # CRITICAL: must be scoped to one agent during parallel runs, otherwise
                # this deletes another subprocess's not-yet-scored conversations.
                ghost_params = (agent_id,) if agent_id is not None else ()
                ghost_where_conv = f"WHERE is_archived = FALSE AND id NOT IN (SELECT conversation_id FROM conversation_scores){' AND agent_id = $1' if agent_id is not None else ''}"
                ghost_where_join = f"WHERE c.is_archived = FALSE AND c.id NOT IN (SELECT conversation_id FROM conversation_scores){' AND c.agent_id = $1' if agent_id is not None else ''}"

                ghost_row = await conn.fetchrow(
                    f"SELECT COUNT(*) AS n FROM conversations {ghost_where_conv}",
                    *ghost_params,
                )
                ghost_count = ghost_row["n"] if ghost_row else 0
                count += ghost_count

                if ghost_count > 0:
                    await conn.execute(
                        f"""DELETE FROM audited_chats ac
                            WHERE EXISTS (
                                SELECT 1 FROM conversations c
                                JOIN accounts a  ON a.id = c.agent_id
                                JOIN contacts co ON co.id = c.contact_id
                                {ghost_where_join}
                                  AND ac.agent_email = a.email AND ac.contact_name = co.name
                            )""",
                        *ghost_params,
                    )
                    await conn.execute(
                        f"DELETE FROM conversations {ghost_where_conv}",
                        *ghost_params,
                    )

        # ── Sync audit_scores.details to match live conversation_scores ──────────
        async with self.pool.acquire() as conn:
            # Build valid contact set per agent from live scored convos
            scored_rows = await conn.fetch(
                """SELECT c.agent_id, co.name AS contact_name
                   FROM conversations c
                   JOIN contacts co ON co.id = c.contact_id
                   JOIN conversation_scores cs ON cs.conversation_id = c.id
                   WHERE c.is_archived = FALSE AND cs.compliance_score IS NOT NULL"""
            )
            valid: dict[int, set] = {}
            for r in scored_rows:
                valid.setdefault(r["agent_id"], set()).add(r["contact_name"].lower().strip())

            # Delete audit_scores rows for agents with no live convos.
            # MUST be scoped the same way as the deletes above: unscoped, an agent
            # finishing its run wiped every other agent's summary row, and the
            # nightly archive reset made that fire for the whole roster
            # (deep review F11).
            if agent_id is not None:
                await conn.execute(
                    """DELETE FROM audit_scores s
                       WHERE s.agent_id = $1
                         AND NOT EXISTS (
                           SELECT 1 FROM conversations c
                           JOIN conversation_scores cs ON cs.conversation_id = c.id
                           WHERE c.agent_id = s.agent_id
                             AND c.is_archived = FALSE
                             AND cs.compliance_score IS NOT NULL
                       )""",
                    agent_id,
                )
            else:
                await conn.execute(
                    """DELETE FROM audit_scores s
                       WHERE NOT EXISTS (
                           SELECT 1 FROM conversations c
                           JOIN conversation_scores cs ON cs.conversation_id = c.id
                           WHERE c.agent_id = s.agent_id
                             AND c.is_archived = FALSE
                             AND cs.compliance_score IS NOT NULL
                       )"""
                )

            # Strip entries from details that no longer exist in conversation_scores.
            # Scoped for the same reason as the delete above.
            if agent_id is not None:
                as_rows = await conn.fetch(
                    "SELECT id, agent_id, details FROM audit_scores WHERE agent_id = $1",
                    agent_id,
                )
            else:
                as_rows = await conn.fetch("SELECT id, agent_id, details FROM audit_scores")
            for r in as_rows:
                d = r["details"] or {}
                if isinstance(d, str):
                    try: d = _json.loads(d)
                    except Exception: continue
                pc = d.get("per_conversation", [])
                valid_contacts = valid.get(r["agent_id"], set())
                clean = [
                    x for x in pc
                    if (x.get("contact") or "").lower().strip() in valid_contacts
                    and "Analysis skipped" not in (x.get("summary") or "")
                    and (x.get("compliance") is not None or x.get("compliance_score") is not None)
                ]
                if len(clean) != len(pc):
                    d["per_conversation"] = clean
                    await conn.execute(
                        "UPDATE audit_scores SET details = $1 WHERE id = $2",
                        _json.dumps(d), r["id"],
                    )

        return count

    # ── Per-account audit configuration ──────────────────────────────────────
    async def get_account_audit_config(self, account_email: str) -> dict:
        """
        Return {'funnel_tier': str|None, 'guidelines': str|None} for an account.
        Returns empty dict if the account is not found.
        Case-insensitive email match.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT funnel_tier, guidelines FROM accounts WHERE LOWER(email) = LOWER($1)",
                account_email,
            )
            if not row:
                return {}
            return {"funnel_tier": row["funnel_tier"], "guidelines": row["guidelines"]}

    async def set_account_funnel_tier(self, account_email: str, tier: str | None) -> bool:
        """Set or clear funnel_tier. tier must be NF/MF/WF or None. Returns True if account existed."""
        if tier is not None and tier not in ("NF", "MF", "WF"):
            raise ValueError(f"Invalid tier: {tier!r}. Must be NF, MF, WF, or None.")
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE accounts SET funnel_tier = $1 WHERE LOWER(email) = LOWER($2)",
                tier, account_email,
            )
            return result.endswith(" 1")

    async def set_account_guidelines(self, account_email: str, guidelines: str | None) -> bool:
        """Set or clear account guidelines. Returns True if account existed."""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """UPDATE accounts
                   SET guidelines = $1, guidelines_updated_at = NOW()
                   WHERE LOWER(email) = LOWER($2)""",
                guidelines, account_email,
            )
            return result.endswith(" 1")

    async def list_accounts_with_audit_config(self) -> list[dict]:
        """Return all accounts with their tier and guidelines for CLI listing."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT email, name, funnel_tier, guidelines, guidelines_updated_at
                   FROM accounts ORDER BY email"""
            )
            return [dict(r) for r in rows]
