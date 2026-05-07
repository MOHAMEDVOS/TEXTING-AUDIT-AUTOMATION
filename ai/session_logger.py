"""
Session Logger — record session events to database and JSONL file.

Called after each scoring run to log metadata about what was scored and how many
flags were generated. This data is used by the dream worker to decide whether to run.

Intentionally synchronous (not async) to avoid blocking the event loop in scorer.py.
Designed to be called via asyncio.to_thread() from async contexts.
"""
import json
import logging
import psycopg2
import psycopg2.extras
from datetime import datetime
from pathlib import Path

from config.settings import DATABASE_URL, LOG_DIR

logger = logging.getLogger(__name__)

SESSIONS_JSONL = LOG_DIR / "sessions.jsonl"


def log_session(
    agent_id: int,
    agent_name: str,
    conversations_scored: int,
    flags_generated: int,
    model_used: str | None = None,
    run_timestamp: str | None = None,
    db_path: str | None = None,
) -> None:
    """
    Log a session event to the database and JSONL file.

    Called after every scoring run. Writes one row to session_events table
    and appends one JSON line to logs/sessions.jsonl.

    Never raises — logs errors and returns silently to ensure the scorer
    is never blocked by logging failures.

    Args:
        agent_id: FK into agents table
        agent_name: Display name
        conversations_scored: Number of conversations analyzed
        flags_generated: Total red flags generated in this run
        model_used: AI model name used (e.g., 'llama-3.3-70b-versatile'), optional
        run_timestamp: ISO-8601 timestamp, defaults to utcnow()
        db_path: Path to database, defaults to settings.DB_PATH
    """
    try:
        if run_timestamp is None:
            run_timestamp = datetime.utcnow().isoformat() + "Z"

        # ── Write to database ───────────────────────────────────────────
        dsn = db_path or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO session_events
                        (agent_id, agent_name, conversations_scored, flags_generated, model_used, run_timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (agent_id, agent_name, conversations_scored, flags_generated, model_used, run_timestamp),
                )
            conn.commit()

        # ── Write to JSONL file ────────────────────────────────────────
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        event_line = json.dumps({
            "run_timestamp": run_timestamp,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "conversations_scored": conversations_scored,
            "flags_generated": flags_generated,
            "model_used": model_used,
        })
        with open(SESSIONS_JSONL, "a", encoding="utf-8") as f:
            f.write(event_line + "\n")

        logger.debug(
            f"[SessionLogger] Logged session: {agent_name} "
            f"({conversations_scored} convos, {flags_generated} flags)"
        )

    except Exception as e:
        logger.warning(f"[SessionLogger] Failed to log session (non-fatal): {e}")


def get_sessions_since(
    since_iso: str,
    db_path: str | None = None,
) -> list[dict]:
    """
    Return all session_events rows with run_timestamp > since_iso.

    Used by the dream worker to count how many new sessions have occurred
    since the last dream worker run.

    Returns empty list on error (never raises).

    Args:
        since_iso: ISO-8601 timestamp string (e.g., "2026-04-15T10:00:00Z")
        db_path: Path to database, defaults to settings.DB_PATH

    Returns:
        List of dicts with keys: agent_id, agent_name, conversations_scored, flags_generated, model_used, run_timestamp
    """
    try:
        dsn = db_path or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT agent_id, agent_name, conversations_scored, flags_generated, model_used, run_timestamp
                    FROM session_events
                    WHERE run_timestamp > %s
                    ORDER BY run_timestamp ASC
                    """,
                    (since_iso,),
                )
                rows = cur.fetchall()

        return [dict(row) for row in rows]

    except Exception as e:
        logger.warning(f"[SessionLogger] Failed to query sessions: {e}")
        return []
