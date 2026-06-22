"""
FastAPI dashboard for TEXTING AUDIT AUTOMATION.

Routes:
    GET  /                      - Renders index.html with all agents + latest audit scores
    GET  /api/agents            - JSON list of all agents with latest scores
    POST /api/run               - Start a background audit subprocess for one agent
    GET  /api/status            - Dict of running/done subprocess states
    GET  /api/agent/<agent_id>  - Full per-conversation details for one agent
    GET  /api/agent/<agent_id>/conversations - Conversations + AI analysis for one agent
    DELETE /api/reset-all       - Clear all extractions, scores, and audited chats
    DELETE /api/agent/<agent_id>/reset - Clear one agent's data
    POST /api/agents/add        - Add a new agent to the database
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timedelta
import pytz
from pathlib import Path

import asyncpg
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.httpx_client import AsyncOAuth2Client
from pydantic import BaseModel

from config.settings import (
    DATABASE_URL, get_now,
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, APP_BASE_URL,
    SESSION_SECRET_KEY, TOOL_ACCESS_SEED_EMAILS,
)
from config.rate_limiter import get_rate_limiter, route_bucket

# ── Route-level rate-limit config ─────────────────────────────────────────────
# Each entry: (route_prefix, capacity, rate_per_second)
# More specific prefixes must come first (they are matched top-to-bottom).
_ROUTE_LIMITS: list[tuple[str, float, float]] = [
    ("/api/run",             20,  2.0),   # 20 burst, 2 req/s  — allow launching all bots at once
    ("/api/rate-limit",      20,  5.0),   # very relaxed — status monitoring only
    ("/api/ai",              10,  1.0),   # moderate — AI pool status
    ("/api/",                60,  6.0),   # default for all other /api/ routes
]
_dashboard_rl = get_rate_limiter()

# â"€â"€ Project root so we can locate the DB and run main.py â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
PROJECT_ROOT = Path(__file__).parent.parent
SCHEMA_PATH  = PROJECT_ROOT / "database" / "schema.sql"
MAIN_PY      = str(PROJECT_ROOT / "main.py")

# ── Persistent embedding service ──────────────────────────────────────────────
# The dashboard hosts the embedding model (see ai/prefilter/embedding_service.py)
# and tells every audit subprocess where to reach it, so subprocesses skip the
# ~15-20s model load. Honors $PORT (Railway) and falls back to local port 5000.
EMBEDDING_SERVICE_URL = f"http://127.0.0.1:{os.getenv('PORT', '5000')}"
RUN_STATUS_DIR = PROJECT_ROOT / "logs" / "run_status"

# â"€â"€ App setup â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€


async def _scheduled_reset_all(pool):
    """Background task to trigger the reset-all logic at 11:00 PM EST daily."""
    est = pytz.timezone("US/Eastern")
    while True:
        try:
            now = datetime.now(est)
            # Target is 11:00 PM (23:00) today
            target = now.replace(hour=23, minute=0, second=0, microsecond=0)
            
            # If already past 11:00 PM EST today, schedule for tomorrow
            if now >= target:
                target += timedelta(days=1)
                
            seconds_to_wait = (target - now).total_seconds()
            logger.info(f"Schedule: Next automated 'Reset All' at {target.strftime('%Y-%m-%d %H:%M:%S')} EST (in {seconds_to_wait/3600:.1f}h)")
            
            await asyncio.sleep(seconds_to_wait)
            
            # Execute Reset All Logic
            logger.info("Schedule: Triggering 11:00 PM automated Reset All...")
            async with pool.acquire() as conn:
                count_row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM accounts")
                count = count_row["cnt"] if count_row else 0
                await conn.execute("DELETE FROM audit_scores")
                await conn.execute("UPDATE conversations SET is_archived = TRUE")
            
            global _snapshotted
            _snapshotted.clear()
            logger.info(f"Schedule: Automated reset complete for {count} accounts.")
            
            # Sleep briefly to ensure we don't re-trigger in the same second
            await asyncio.sleep(60)
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Schedule Error: automated reset loop failed: {e}", exc_info=True)
            await asyncio.sleep(300) # Wait 5m before retry if something crashed


@asynccontextmanager
async def lifespan(app):
    """Create asyncpg connection pool, ensure all tables exist, and load roster."""
    # Mask password for safe logging
    from urllib.parse import urlparse
    u = urlparse(DATABASE_URL)
    masked_url = f"{u.scheme}://{u.username}:****@{u.hostname}:{u.port}{u.path}"
    logger.info(f"Connecting to database: {masked_url}")

    # ── Wipe stale run_status files from the previous container lifetime ──────
    # If a Railway redeploy kills mid-run processes, their JSON files are left on
    # disk with state="running". On next startup those files make the UI show
    # agents permanently stuck on "Logging in". Delete them all at boot time.
    try:
        if RUN_STATUS_DIR.exists():
            stale = list(RUN_STATUS_DIR.glob("*.json")) + list(RUN_STATUS_DIR.glob("*.json.tmp"))
            for f in stale:
                try:
                    f.unlink()
                except Exception as _e:
                    logger.debug("swallowed: %r", _e)
            if stale:
                logger.info(f"Startup: removed {len(stale)} stale run_status file(s) from previous container")
    except Exception as e:
        logger.warning(f"Startup: could not clean stale run_status files: {e}")

    # Retry logic for cloud startup
    max_retries = 5
    for attempt in range(1, max_retries + 1):
        try:
            app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
            async with app.state.pool.acquire() as conn:
                schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
                await conn.execute(schema_sql)
                # Fix sequence out-of-sync issues that cause UniqueViolationError
                # (only applies when tables use SERIAL; IDENTITY columns have no separate sequence)
                for seq_sql in [
                    "SELECT setval('accounts_id_seq', COALESCE((SELECT MAX(id) FROM accounts), 1))",
                    "SELECT setval('account_assignments_id_seq', COALESCE((SELECT MAX(id) FROM account_assignments), 1))",
                ]:
                    try:
                        await conn.execute(seq_sql)
                    except Exception as seq_err:
                        logger.debug(f"Sequence sync skipped (likely IDENTITY column): {seq_err}")
            break
        except Exception as e:
            if attempt == max_retries:
                logger.error(f"Failed to connect to DB after {max_retries} attempts: {e}")
                raise
            logger.warning(f"DB connection attempt {attempt} failed, retrying in 5s... ({e})")
            await asyncio.sleep(5)
    # Load texter roster from DB into memory
    await _load_agent_roster_from_db()
    logger.info(f"Loaded {len(AGENT_ROSTER)} texters from database")

    # Seed tool_access allowlist from env var (runs once when table is empty)
    await _seed_tool_access(app.state.pool)

    # Start scheduled reset task
    reset_task = asyncio.create_task(_scheduled_reset_all(app.state.pool))

    # Warm the embedding model in the background so the persistent embedding
    # service is ready before the first audit subprocess asks for a vector.
    # Daemon thread → never blocks startup or shutdown.
    try:
        from ai.prefilter.embedding_service import warmup as _warmup_embeddings
        threading.Thread(
            target=_warmup_embeddings, name="embed-warmup", daemon=True
        ).start()
        logger.info("Embedding model warmup started in background")
    except Exception as e:
        logger.warning(f"Could not start embedding warmup: {e}")

    yield

    # Cleanup
    reset_task.cancel()
    try:
        await reset_task
    except asyncio.CancelledError:
        pass
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)

# Mount the persistent embedding service endpoints (/internal/embed*).
try:
    from ai.prefilter.embedding_service import router as embedding_router
    app.include_router(embedding_router)
except Exception as _e:
    logging.getLogger(__name__).warning(f"Embedding service router not mounted: {_e}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s")
logger = logging.getLogger(__name__)

_SILENT_ROUTES = {"/api/status", "/api/agents", "/api/flags/realtime"}


class _SilencePollingRoutes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(route in msg for route in _SILENT_ROUTES)


logging.getLogger("uvicorn.access").addFilter(_SilencePollingRoutes())

# Owner email — permanent, cannot be removed via API
OWNER_EMAIL = "mohamedibrahimpayonner@gmail.com"

# ── Seed helper ────────────────────────────────────────────────────────────────
async def _seed_tool_access(pool) -> None:
    """Always ensure the owner email exists. Seed other bootstrap emails only once."""
    async with pool.acquire() as conn:
        # Owner is always upserted — can never be lost
        await conn.execute(
            "INSERT INTO tool_access (email, added_by) VALUES ($1, 'owner') "
            "ON CONFLICT (email) DO UPDATE SET is_active = TRUE, added_by = 'owner'",
            OWNER_EMAIL,
        )
        # Other seed emails only inserted if table had just the owner (first run)
        if TOOL_ACCESS_SEED_EMAILS:
            count = await conn.fetchval("SELECT COUNT(*) FROM tool_access")
            if count <= 1:
                for email in TOOL_ACCESS_SEED_EMAILS:
                    if email != OWNER_EMAIL:
                        await conn.execute(
                            "INSERT INTO tool_access (email, added_by) VALUES ($1, 'system') ON CONFLICT DO NOTHING",
                            email,
                        )
    logger.info(f"tool_access: owner '{OWNER_EMAIL}' ensured")


# ── Middleware ─────────────────────────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket rate limiter applied to all /api/* routes.
    Non-API paths (/, /static/…, HTML pages) are never touched.
    Rejected requests receive HTTP 429 with a Retry-After header — instantly,
    with no queuing and no waiting.
    """

    async def dispatch(self, request: StarletteRequest, call_next):
        path = request.url.path

        # Only gate API routes
        if path.startswith("/api/"):
            ip = request.client.host if request.client else "unknown"

            # Match most-specific prefix first
            for prefix, capacity, rate in _ROUTE_LIMITS:
                if path.startswith(prefix):
                    bucket_key = route_bucket(ip, prefix)
                    allowed, retry_after = _dashboard_rl.check(bucket_key, capacity, rate)
                    if not allowed:
                        return StarletteJSONResponse(
                            status_code=429,
                            content={
                                "error": "Too Many Requests",
                                "detail": f"Rate limit exceeded for {prefix}. Try again in {retry_after:.1f}s.",
                                "retry_after": round(retry_after, 1),
                            },
                            headers={"Retry-After": str(int(retry_after) + 1)},
                        )
                    break

        return await call_next(request)


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """
    Enforce session authentication on / and /api/* routes.
    Open paths: /login, /auth/*, /static/*
    API paths get 401 JSON; HTML paths get a redirect to /login.
    """
    _OPEN = {"/login", "/auth/google", "/auth/callback", "/auth/logout"}

    # Localhost-only IPs allowed to reach /internal/* (subprocess embedding traffic).
    _LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}

    async def dispatch(self, request: StarletteRequest, call_next):
        path = request.url.path
        if path in self._OPEN or path.startswith("/auth/") or path.startswith("/static/"):
            return await call_next(request)
        # Internal embedding service: open to local subprocess traffic only.
        if path.startswith("/internal/"):
            client_host = request.client.host if request.client else ""
            if client_host in self._LOCAL_HOSTS:
                return await call_next(request)
            return StarletteJSONResponse(
                {"success": False, "error": "Forbidden"}, status_code=403
            )
        user = request.session.get("user_email")
        if not user:
            if path.startswith("/api/"):
                return StarletteJSONResponse(
                    {"success": False, "error": "Not authenticated"}, status_code=401
                )
            # Only show "session expired" if user had a cookie before (was previously logged in).
            # Fresh visitors with no cookie go straight to /login with no error message.
            had_cookie = "vos_session" in request.cookies
            location = "/login?error=session_expired" if had_cookie else "/login"
            return StarletteJSONResponse(
                status_code=302,
                content={},
                headers={"Location": location},
            )
        return await call_next(request)


# Middleware add order is REVERSE of execution order in Starlette.
# Request flow: SessionMiddleware → RateLimitMiddleware → SessionAuthMiddleware → routes
# So: SessionAuthMiddleware added first (innermost), SessionMiddleware added last (outermost).
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SessionAuthMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET_KEY,
    session_cookie="vos_session",
    max_age=60 * 60 * 24 * 7,   # 7 days
    same_site="lax",
    https_only=APP_BASE_URL.startswith("https://"),
)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


# ── Admin auth (env-gated) ────────────────────────────────────────────────────
# When ADMIN_TOKEN is unset the gate is disabled (preserves Railway / local-dev
# behavior). When ADMIN_TOKEN is set, every mutating route requires the same
# value in the `X-Admin-Token` header. Set the env var on Railway to lock down
# state-changing endpoints without breaking GET traffic.
_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()


def require_admin(request: Request) -> None:
    if not _ADMIN_TOKEN:
        return  # gate disabled — auth not configured
    provided = request.headers.get("x-admin-token", "") or request.headers.get("X-Admin-Token", "")
    if provided != _ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing X-Admin-Token")

# â"€â"€ In-memory process registry â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
# { "Noah": <Popen> | "done" }
running_processes: dict[str, "subprocess.Popen | str"] = {}
# { "Noah": "gsk_..."}
running_pinned_keys: dict[str, str] = {}
# { "Noah": Path("logs/run_status/...json") }
running_status_files: dict[str, Path] = {}
running_status_details: dict[str, dict] = {}


# â"€â"€ Async DB helpers â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

async def _fetch_agents_with_scores() -> list[dict]:
    """
    Return every agent joined with their latest audit score.
    red_flags and conversations_analyzed are aggregated across ALL audit runs.
    Agents that have never been scored still appear (scores will be None).
    """
    # Latest score row per agent (for score numbers and label accuracy)
    sql = """
        SELECT
            a.id,
            a.name,
            a.email,
            a.funnel_tier,
            a.guidelines,
            a.created_at,
            s.audit_date,
            s.overall_score,
            s.compliance_score,
            s.sentiment_score,
            s.professionalism_score,
            s.script_adherence_score,
            s.red_flags,
            s.details
        FROM accounts a
        LEFT JOIN audit_scores s
            ON s.id = (
                SELECT id FROM audit_scores
                WHERE agent_id = a.id
                ORDER BY audit_date DESC, id DESC
                LIMIT 1
            )
        ORDER BY a.name
    """
    # All audit_scores rows — aggregate per_conversation across every run per agent.
    # ORDER BY id ASC so that later rows overwrite earlier ones for the same contact.
    sql_all = "SELECT agent_id, details FROM audit_scores ORDER BY id ASC"

    async with app.state.pool.acquire() as conn:
        rows     = await conn.fetch(sql)
        all_rows = await conn.fetch(sql_all)

    # Per agent: deduplicated contact map — latest run’s entry wins for same contact
    agg: dict[int, dict[str, dict]] = {}
    for ar in all_rows:
        aid = ar["agent_id"]
        if aid not in agg:
            agg[aid] = {}
        try:
            d = ar["details"] or {}
            if isinstance(d, str):
                try: d = json.loads(d)
                except Exception: d = {}
            for pc in d.get("per_conversation", []):
                key = (pc.get("contact") or "").lower().strip()
                if key:
                    agg[aid][key] = pc
        except (json.JSONDecodeError, TypeError):
            pass

    # ── Batched per-agent stats: fixed query count regardless of agent count ──
    # (agent_id, latest audit_date) pairs for agents that have been scored
    pairs = [(row["id"], row["audit_date"]) for row in rows if row["audit_date"]]
    flags_by_agent: dict[int, list[str]] = {}
    convos_by_agent: dict[int, int] = {}

    async with app.state.pool.acquire() as conn:
        if pairs:
            agent_ids   = [p[0] for p in pairs]
            audit_dates = [p[1] for p in pairs]

            flagged_rows = await conn.fetch(
                """SELECT DISTINCT c.agent_id, ct.name
                   FROM conversation_scores cs
                   JOIN conversations c ON c.id = cs.conversation_id
                   JOIN contacts ct ON ct.id = c.contact_id
                   JOIN unnest($1::int[], $2::date[]) AS t(agent_id, audit_date)
                     ON t.agent_id = c.agent_id AND t.audit_date = c.audit_date
                   WHERE c.is_archived = FALSE
                     AND cs.id = (
                       SELECT MAX(cs2.id) FROM conversation_scores cs2
                       WHERE cs2.conversation_id = c.id
                     )
                     AND (
                       (cs.red_flags IS NOT NULL AND cs.red_flags::text NOT IN ('[]','null'))
                       OR (cs.label_correct = false AND cs.label_assigned IS DISTINCT FROM cs.label_should_be)
                     )""",
                agent_ids,
                audit_dates,
            )
            for fr in flagged_rows:
                flags_by_agent.setdefault(fr["agent_id"], []).append(fr["name"])

            count_rows = await conn.fetch(
                """SELECT c.agent_id, COUNT(DISTINCT LOWER(TRIM(ct.name))) AS n
                   FROM conversations c
                   JOIN contacts ct ON ct.id = c.contact_id
                   JOIN unnest($1::int[], $2::date[]) AS t(agent_id, audit_date)
                     ON t.agent_id = c.agent_id AND t.audit_date = c.audit_date
                   WHERE c.is_archived = FALSE
                   GROUP BY c.agent_id""",
                agent_ids,
                audit_dates,
            )
            convos_by_agent = {cr["agent_id"]: cr["n"] for cr in count_rows}

        review_stats = await _compute_review_stats_bulk(conn)

    result = []
    for row in rows:
        r = dict(row)
        agent_id = r["id"]

        all_flags  = flags_by_agent.get(agent_id, [])
        all_convos = convos_by_agent.get(agent_id, 0)
        needs_review, flagged_total = review_stats.get(agent_id, (0, 0))

        r["red_flags"]              = all_flags
        r["conversations_analyzed"] = all_convos or 0
        r["needs_review_count"]     = needs_review
        r["flagged_count"]          = flagged_total
        # label_accuracy and unread from latest run only
        details_raw = r.pop("details", None)
        details = {}
        if details_raw:
            if isinstance(details_raw, dict):
                details = details_raw
            else:
                try:
                    details = json.loads(details_raw)
                except (json.JSONDecodeError, TypeError):
                    details = {}
        r["label_accuracy"]        = details.get("label_accuracy")
        r["wrong_label_count"]     = details.get("wrong_label_count", 0)
        r["unread_messages_left"]  = details.get("unread_messages_left")

        result.append(r)

    return result



@app.get("/api/flags/realtime")
async def api_flags_realtime():
    """
    Return the number of flagged conversations per agent for the current day.
    Used by the dashboard for the total flag counter in the header.
    """
    # EST date — conversations.audit_date is EST; naive date.today() is UTC on
    # Railway and drifts a day ahead after ~8 PM EST.
    today = get_now().date()
    sql = """
        SELECT c.agent_id, COUNT(DISTINCT c.contact_id) as flagged
        FROM conversation_scores cs
        JOIN conversations c ON c.id = cs.conversation_id
        WHERE c.audit_date = $1
          AND c.is_archived = FALSE
          AND cs.id = (
            SELECT MAX(cs2.id) FROM conversation_scores cs2
            WHERE cs2.conversation_id = c.id
          )
          AND (
            (cs.red_flags IS NOT NULL AND cs.red_flags::text NOT IN ('[]','null'))
            OR (cs.label_correct = false AND cs.label_assigned IS DISTINCT FROM cs.label_should_be)
          )
        GROUP BY c.agent_id
    """
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(sql, today)
        return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"Error in /api/flags/realtime: {e}")
        return []


async def _fetch_agent_detail(agent_id: int) -> dict | None:

    """
    Return the latest audit_scores row for one agent, with full details parsed.
    Returns None if the agent does not exist.
    """
    async with app.state.pool.acquire() as conn:
        agent_row = await conn.fetchrow(
            "SELECT id, name, email, created_at FROM accounts WHERE id = $1", agent_id
        )
        if not agent_row:
            return None
        agent = dict(agent_row)

        score_row = await conn.fetchrow(
            """SELECT * FROM audit_scores
               WHERE agent_id = $1
               ORDER BY audit_date DESC, id DESC LIMIT 1""",
            agent_id,
        )

    if not score_row:
        return {"agent": agent, "scores": None, "details": None}

    score = dict(score_row)

    # JSONB columns come back as Python objects directly
    red_flags = score.get("red_flags") or []
    if isinstance(red_flags, str):
        try: red_flags = json.loads(red_flags)
        except Exception: red_flags = []
    score["red_flags"] = red_flags

    details = score.pop("details", None) or {}
    if isinstance(details, str):
        try: details = json.loads(details)
        except Exception: details = {}

    return {"agent": agent, "scores": score, "details": details}


def _is_wrong_label_flag(flag: str) -> bool:
    return (flag or "").strip().lower().startswith("wrong label:")


def _parse_json_list(val) -> list:
    if not val:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


def _score_row_to_analysis(score_row) -> dict:
    if not score_row:
        return {}
    raw = dict(score_row)
    for field in ("pillars_gathered", "rebuttals_used", "red_flags"):
        raw[field] = _parse_json_list(raw.get(field))
    return {
        "compliance": raw.get("compliance_score"),
        "sentiment": raw.get("sentiment_score"),
        "professionalism": raw.get("professionalism_score"),
        "script_adherence": raw.get("script_adherence_score"),
        "funnel_stage_reached": raw.get("funnel_stage"),
        "pillars_gathered": raw.get("pillars_gathered", []),
        "rebuttals_used": raw.get("rebuttals_used", []),
        "label_assigned": raw.get("label_assigned"),
        "label_correct": raw.get("label_correct"),
        "label_should_be": raw.get("label_should_be"),
        "label_reason": raw.get("label_reason"),
        "red_flags": raw.get("red_flags", []),
        "summary": raw.get("summary", ""),
        "model_used": raw.get("model_used"),
        "source": raw.get("source"),
    }


def _wrong_label_flag_text(analysis: dict) -> str | None:
    if not analysis or analysis.get("label_correct") is not False:
        return None
    wrong = (analysis.get("label_assigned") or "").strip()
    should = (analysis.get("label_should_be") or "").strip()
    if not wrong or not should or wrong == should:
        return None
    return f"Wrong label: assigned '{wrong}' but should be '{should}'"


def _is_label_wrong_dismissed(analysis: dict, invalidated: set[str]) -> bool:
    inv_lower = {f.strip().lower() for f in invalidated}
    for f in inv_lower:
        if f.startswith("wrong label:"):
            return True
    expected = _wrong_label_flag_text(analysis)
    return bool(expected and expected.lower() in inv_lower)


def _label_wrong_active(analysis: dict, invalidated: set[str]) -> bool:
    if not analysis or analysis.get("label_correct") is not False:
        return False
    if (analysis.get("label_assigned") or "") == (analysis.get("label_should_be") or ""):
        return False
    return not _is_label_wrong_dismissed(analysis, invalidated)


def _conv_issue_count(analysis: dict, invalidated: set[str]) -> int:
    inv_lower = {f.strip().lower() for f in invalidated}
    active_flags = [
        f for f in (analysis.get("red_flags") or [])
        if f.strip().lower() not in inv_lower and not _is_wrong_label_flag(f)
    ]
    label_wrong = 1 if _label_wrong_active(analysis, invalidated) else 0
    return len(active_flags) + label_wrong


def _is_flagged_convo_reviewed(
    analysis: dict,
    invalidated: set[str],
    is_flag_reviewed: bool,
) -> bool:
    issues = _conv_issue_count(analysis, invalidated)
    if issues == 0:
        return True
    if is_flag_reviewed:
        return True
    inv = {f.strip() for f in invalidated}
    red_flags = [
        f.strip() for f in (analysis.get("red_flags") or [])
        if not _is_wrong_label_flag(f)
    ]
    if (
        red_flags
        and all(f in inv for f in red_flags)
        and not _label_wrong_active(analysis, invalidated)
    ):
        return True
    return False


async def _compute_review_stats_bulk(conn) -> dict[int, tuple[int, int]]:
    """
    Return {agent_id: (needs_review_count, flagged_count)} for ALL agents,
    mirroring the convo list UI. Uses a fixed number of set-based queries
    instead of one query per conversation — critical on Railway where every
    round trip costs network latency.
    """
    conv_rows = await conn.fetch(
        """SELECT c.agent_id, c.id, ct.name AS contact_name
           FROM conversations c
           JOIN contacts ct ON ct.id = c.contact_id
           WHERE c.is_archived = FALSE
           ORDER BY c.extracted_at DESC, c.id DESC"""
    )

    # Per agent: keep only the most recent convo per contact (rows are newest-first)
    seen: dict[int, set[str]] = {}
    unique_convos: list = []
    for row in conv_rows:
        aid = row["agent_id"]
        key = (row["contact_name"] or "").lower().strip()
        agent_seen = seen.setdefault(aid, set())
        if key and key not in agent_seen:
            agent_seen.add(key)
            unique_convos.append(row)

    fb_rows = await conn.fetch(
        "SELECT agent_id, contact_name, red_flag FROM flag_feedback"
    )
    invalidated_map: dict[tuple[int, str], set[str]] = {}
    for fb in fb_rows:
        key = (fb["agent_id"], (fb["contact_name"] or "").lower().strip())
        invalidated_map.setdefault(key, set()).add(fb["red_flag"])

    try:
        fr_rows = await conn.fetch(
            "SELECT agent_id, contact_name FROM flagged_conversation_reviews"
        )
    except Exception:
        fr_rows = []
    reviewed_set = {
        (fr["agent_id"], (fr["contact_name"] or "").lower().strip()) for fr in fr_rows
    }

    # Latest score row per conversation — single batched query
    conv_ids = [c["id"] for c in unique_convos]
    score_by_conv: dict[int, dict] = {}
    if conv_ids:
        score_rows = await conn.fetch(
            """SELECT DISTINCT ON (conversation_id)
                      conversation_id,
                      compliance_score, sentiment_score, professionalism_score,
                      script_adherence_score, funnel_stage, pillars_gathered,
                      rebuttals_used, label_assigned, label_correct,
                      label_should_be, label_reason, red_flags, summary, model_used,
                      COALESCE(source, 'groq') AS source
               FROM conversation_scores
               WHERE conversation_id = ANY($1::int[])
               ORDER BY conversation_id, id DESC""",
            conv_ids,
        )
        score_by_conv = {r["conversation_id"]: r for r in score_rows}

    stats: dict[int, tuple[int, int]] = {}
    for conv in unique_convos:
        score_row = score_by_conv.get(conv["id"])
        if not score_row:
            continue

        aid = conv["agent_id"]
        contact_key = (conv["contact_name"] or "").lower().strip()
        invalidated = invalidated_map.get((aid, contact_key), set())
        analysis = _score_row_to_analysis(score_row)
        issues = _conv_issue_count(analysis, invalidated)
        if issues == 0:
            continue
        needs_review, flagged = stats.get(aid, (0, 0))
        flagged += 1
        if not _is_flagged_convo_reviewed(
            analysis,
            invalidated,
            (aid, contact_key) in reviewed_set,
        ):
            needs_review += 1
        stats[aid] = (needs_review, flagged)

    return stats


async def _upsert_flag_review(
    conn,
    agent_id: int,
    contact_name: str,
    conversation_id: int | None = None,
) -> None:
    """Record that a flagged conversation was reviewed by a manager."""
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS flagged_conversation_reviews (
               id              SERIAL PRIMARY KEY,
               agent_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
               contact_name    TEXT NOT NULL,
               conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
               reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
               UNIQUE(agent_id, contact_name)
           )"""
    )
    normalized = contact_name.strip()
    await conn.execute(
        """DELETE FROM flagged_conversation_reviews
           WHERE agent_id = $1 AND LOWER(TRIM(contact_name)) = LOWER(TRIM($2))""",
        agent_id,
        normalized,
    )
    await conn.execute(
        """INSERT INTO flagged_conversation_reviews
               (agent_id, contact_name, conversation_id)
           VALUES ($1, $2, $3)
           ON CONFLICT (agent_id, contact_name) DO UPDATE
               SET reviewed_at = NOW(),
                   conversation_id = COALESCE(
                       EXCLUDED.conversation_id,
                       flagged_conversation_reviews.conversation_id
                   )""",
        agent_id,
        contact_name.strip(),
        conversation_id,
    )


async def _fetch_agent_conversations(agent_id: int) -> dict | None:
    """
    Return conversations with parsed messages + per-conversation AI analysis
    for the given agent, sourced from the normalized conversations/messages/conversation_scores tables.
    """
    async with app.state.pool.acquire() as conn:
        agent_row = await conn.fetchrow(
            "SELECT id, name, email FROM accounts WHERE id = $1", agent_id
        )
        if not agent_row:
            return None
        agent = dict(agent_row)

        # Load all conversations for this agent, newest first
        conv_rows = await conn.fetch(
            """SELECT c.id, c.extracted_at, c.audit_date, c.convo_date, c.assigned_labels, ct.name AS contact_name
               FROM conversations c
               JOIN contacts ct ON ct.id = c.contact_id
               WHERE c.agent_id = $1 AND c.is_archived = FALSE
               ORDER BY c.extracted_at DESC, c.id DESC""",
            agent_id,
        )

        # Deduplicate by contact_name — keep only the most recent per contact
        seen: set[str] = set()
        unique_convos = []
        for row in conv_rows:
            key = (row["contact_name"] or "").lower().strip()
            if key not in seen:
                seen.add(key)
                unique_convos.append(row)

        # Load invalidated flags for this agent (for all contacts)
        fb_rows = await conn.fetch(
            "SELECT contact_name, red_flag FROM flag_feedback WHERE agent_id = $1",
            agent_id,
        )

        # Load flagged-conversation review status
        try:
            fr_rows = await conn.fetch(
                "SELECT contact_name FROM flagged_conversation_reviews WHERE agent_id = $1",
                agent_id,
            )
        except Exception as exc:
            logger.warning("flagged_conversation_reviews query failed: %s", exc)
            fr_rows = []

    invalidated_map: dict[str, set] = {}
    for fb in fb_rows:
        key = (fb["contact_name"] or "").lower().strip()
        invalidated_map.setdefault(key, set()).add(fb["red_flag"])

    reviewed_set: set[str] = set()
    for fr in fr_rows:
        reviewed_set.add((fr["contact_name"] or "").lower().strip())

    merged = []
    conv_ids = [conv["id"] for conv in unique_convos]
    msgs_by_conv: dict[int, list[dict]] = {}
    score_by_conv: dict[int, dict] = {}
    if conv_ids:
        async with app.state.pool.acquire() as conn2:
            # All messages for all conversations — one batched query
            msg_rows = await conn2.fetch(
                """SELECT conversation_id, sender, body AS message,
                          sent_at AS time, sc_date_label
                   FROM messages
                   WHERE conversation_id = ANY($1::int[])
                   ORDER BY conversation_id, seq ASC, id ASC""",
                conv_ids,
            )
            for m in msg_rows:
                d = dict(m)
                cid = d.pop("conversation_id")
                msgs_by_conv.setdefault(cid, []).append(d)

            # Latest AI analysis per conversation — one batched query
            score_rows = await conn2.fetch(
                """SELECT DISTINCT ON (conversation_id)
                          conversation_id,
                          compliance_score, sentiment_score, professionalism_score,
                          script_adherence_score, funnel_stage, pillars_gathered,
                          rebuttals_used, label_assigned, label_correct,
                          label_should_be, label_reason, red_flags, summary, model_used,
                          COALESCE(source, 'groq') AS source
                   FROM conversation_scores
                   WHERE conversation_id = ANY($1::int[])
                   ORDER BY conversation_id, id DESC""",
                conv_ids,
            )
            score_by_conv = {r["conversation_id"]: r for r in score_rows}

    for conv in unique_convos:
        conv_id = conv["id"]
        contact = conv["contact_name"] or "Contact"
        contact_key = contact.lower().strip()

        parsed_messages = msgs_by_conv.get(conv_id, [])
        score_row = score_by_conv.get(conv_id)
        analysis = {}
        if score_row:
            raw = dict(score_row)
            # Normalize JSONB fields
            for field in ("pillars_gathered", "rebuttals_used", "red_flags"):
                val = raw.get(field) or []
                if isinstance(val, str):
                    try:
                        import json as _json
                        val = _json.loads(val)
                    except Exception:
                        val = []
                raw[field] = val
            # Remap DB column names → frontend field names expected by renderAiAnalysis
            analysis = {
                "compliance":          raw.get("compliance_score"),
                "sentiment":           raw.get("sentiment_score"),
                "professionalism":     raw.get("professionalism_score"),
                "script_adherence":    raw.get("script_adherence_score"),
                "funnel_stage_reached": raw.get("funnel_stage"),
                "pillars_gathered":    raw.get("pillars_gathered", []),
                "rebuttals_used":      raw.get("rebuttals_used", []),
                "label_assigned":      raw.get("label_assigned"),
                "label_correct":       raw.get("label_correct"),
                "label_should_be":     raw.get("label_should_be"),
                "label_reason":        raw.get("label_reason"),
                "red_flags":           raw.get("red_flags", []),
                "summary":             raw.get("summary", ""),
                "model_used":          raw.get("model_used"),
                "source":              raw.get("source"),
            }

        merged.append({
            "contact_name":      contact,
            "audit_date":        str(conv["audit_date"]) if conv["audit_date"] else None,
            "convo_date":        (conv["convo_date"] or None),
            "parsed_messages":   parsed_messages,
            "assigned_labels":   list(conv["assigned_labels"] or []),
            "analysis":          analysis,
            "invalidated_flags": list(invalidated_map.get(contact_key, set())),
            "is_flag_reviewed":  contact_key in reviewed_set,
            "conversation_id":   conv_id,
        })

    return {"agent": agent, "conversations": merged}


# Tracks when each agent's run was first seen as "running" (for stale timeout)
_run_started_at: dict[str, datetime] = {}

# Max minutes a process may stay in "running" state before being auto-expired
_MAX_RUN_MINUTES = 45


def _cleanup_finished():
    """Mark processes that have completed as 'done' or 'failed'.

    Also auto-expires any process that has been "running" longer than
    _MAX_RUN_MINUTES — this catches Railway-killed processes whose Popen
    handle is gone but the in-memory dict was never cleared.
    """
    now = get_now()
    for name, proc in list(running_processes.items()):
        if proc in {"done", "failed"}:
            _run_started_at.pop(name, None)
            continue

        # Track when this process was first seen running
        if name not in _run_started_at:
            _run_started_at[name] = now

        # Auto-expire if the process handle is dead
        if proc.poll() is not None:
            detail = _read_run_status_detail(name)
            state = detail.get("state")
            running_processes[name] = state if state in {"done", "failed"} else ("done" if proc.returncode == 0 else "failed")
            running_pinned_keys.pop(name, None)
            _run_started_at.pop(name, None)
            continue

        # Auto-expire if stuck in running state too long (Railway crash / orphaned process)
        elapsed = (now - _run_started_at[name]).total_seconds() / 60
        if elapsed > _MAX_RUN_MINUTES:
            logger.warning(
                f"[Cleanup] '{name}' has been running for {elapsed:.0f} min — "
                f"auto-expiring as 'failed' (likely a crashed/killed process)"
            )
            try:
                proc.kill()
            except Exception as _e:
                logger.debug("swallowed: %r", _e)
            running_processes[name] = "failed"
            running_pinned_keys.pop(name, None)
            _run_started_at.pop(name, None)


def _agent_status(name: str) -> str:
    entry = running_processes.get(name)
    if entry is None:
        return "idle"
    if entry in {"done", "failed"}:
        return entry
    if entry.poll() is None:
        detail = _read_run_status_detail(name)
        if detail.get("state") == "failed":
            running_processes[name] = "failed"
            running_pinned_keys.pop(name, None)
            return "failed"
        return "running"
    detail = _read_run_status_detail(name)
    state = detail.get("state")
    running_processes[name] = state if state in {"done", "failed"} else ("done" if entry.returncode == 0 else "failed")
    running_pinned_keys.pop(name, None)
    return running_processes[name]


def _read_run_status_detail(agent_name: str) -> dict:
    """Read the latest subprocess status handoff for one agent."""
    path = running_status_files.get(agent_name)
    if not path or not path.exists():
        detail = running_status_details.get(agent_name, {})
        state = running_processes.get(agent_name)
        if state in {"done", "failed"}:
            return {"state": state, **detail}
        return detail

    try:
        detail = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(detail, dict):
            running_status_details[agent_name] = detail
            return detail
    except Exception as exc:
        logger.debug(f"Failed to read status file for {agent_name}: {exc}")
    return running_status_details.get(agent_name, {})


def _new_run_status_path(agent_name: str) -> Path:
    safe = "".join(ch if ch.isalnum() else "_" for ch in agent_name).strip("_") or "agent"
    return RUN_STATUS_DIR / f"{safe}_{get_now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid.uuid4().hex[:8]}.json"


def _pick_unique_run_key(agent_name: str):
    """
    Pick one Groq key for this run that is not currently assigned
    to another running agent in this dashboard process.
    """
    from ai.analyzer import _pool

    _pool.ensure_loaded()
    with _pool._lock:
        used_keys = set(running_pinned_keys.values())
        candidates = [
            pk for pk in _pool._groq_pool
            if (not pk.quota_exhausted) and (pk.key not in used_keys)
        ]
        if not candidates:
            return None
        # LRU among currently unassigned keys
        chosen = min(candidates, key=lambda k: k.last_used_at)
        return chosen


# â"€â"€ Pydantic request models â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

class RunRequest(BaseModel):
    agent_name: str = ""
    date_filter: str = "today"
    sample_size: int = 10
    date_start: str = ""   # "YYYY-MM-DD" for custom range
    date_end: str = ""     # "YYYY-MM-DD" for custom range
    labels: str = ""       # Comma-separated labels to filter


class ClearStuckRequest(BaseModel):
    agent_name: str = ""   # empty = clear all stuck agents


# Whitelist for the --date-filter argv flag forwarded to main.py.
# Keeps subprocess input strictly bounded — any other value is rejected.
_ALLOWED_DATE_FILTERS = {
    "today", "yesterday", "last_week", "this_month", "last_month",
    "last_30_days", "last_year", "all_time", "custom",
}
# ISO date: YYYY-MM-DD. Used to validate custom-range args before subprocess.
import re as _re
_ISO_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALL_LABEL_FILTER_VALUES = {"all", "all label", "all labels", "all lable", "all lables"}


def _normalize_label_filter(labels: str | None) -> str:
    if not labels:
        return ""

    requested = [label.strip() for label in labels.split(",") if label.strip()]
    if not requested:
        return ""

    def is_all_labels(value: str) -> bool:
        normalized = _re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
        return normalized in _ALL_LABEL_FILTER_VALUES

    requested = [label for label in requested if not is_all_labels(label)]
    if not requested:
        return ""

    return ",".join(requested)


class CustomLabelRequest(BaseModel):
    name: str = ""


class BlacklistLabelRequest(BaseModel):
    name: str = ""
    skip_mode: str = "any"  # 'any' | 'only'


class AddAgentRequest(BaseModel):
    name:       str = ""
    email:      str = ""
    password:   str = ""
    funnel_tier: str | None = None
    guidelines:  str | None = None


class EditAgentRequest(BaseModel):
    name:        str = ""
    email:       str = ""
    password:    str = ""
    funnel_tier: str | None = None
    guidelines:  str | None = None


class EditAgentKeyRequest(BaseModel):
    provider: str = ""   # "groq"
    key:      str = ""   # the API key value (empty string = remove key)


class RedFlagFeedbackRequest(BaseModel):
    agent_id:        int
    agent_name:      str
    contact_name:    str
    red_flag:        str
    evidence:        str = ""
    reason:          str = ""
    category:        str = ""
    conversation_id: int | None = None


class FlagReviewRequest(BaseModel):
    agent_id:        int
    contact_name:    str
    conversation_id: int | None = None


class AssignmentRequest(BaseModel):
    account_email: str
    agent_name:    str
    assigned_date: str  # "YYYY-MM-DD"
    groq_key_id:   int | None = None


class AddTexterRequest(BaseModel):
    name: str


# ── Trend snapshot dedup guard (in-memory, reset on server restart) ────────────────
# Stores (agent_name, audit_date) tuples that have already been snapshotted this
# server session to prevent duplicate rows from rapid /api/status polls.
_snapshotted: set[tuple[str, str]] = set()

# ── Agent Roster - loaded from database at startup ────────────────────────────
# In-memory cache; refreshed from DB on add/delete.
AGENT_ROSTER: list[str] = []

async def _load_agent_roster_from_db() -> list[str]:
    """Load the texter roster from the texters table."""
    global AGENT_ROSTER
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("SELECT name FROM texters ORDER BY id")
    AGENT_ROSTER = [r["name"] for r in rows]
    return AGENT_ROSTER


async def _save_trend_snapshot(agent_name: str) -> None:
    """
    Persist a trend snapshot for the given agent after their audit completes.
    Pulls scores from the latest audit_scores row, and account_email from
    today's account_assignments entry (if any).
    """
    from datetime import date as _date
    today     = get_now().date()
    today_str = today.isoformat()
    key = (agent_name, today_str)
    if key in _snapshotted:
        return
    _snapshotted.add(key)

    try:
        async with app.state.pool.acquire() as conn:
            # Find agent row
            agent_row = await conn.fetchrow(
                "SELECT id, email FROM accounts WHERE LOWER(name) = LOWER($1)", agent_name
            )
            if not agent_row:
                logger.warning(f"_save_trend_snapshot: agent '{agent_name}' not found in DB")
                return
            agent_id    = agent_row["id"]
            agent_email = agent_row["email"]

            # Latest audit score
            score_row = await conn.fetchrow(
                """SELECT overall_score, compliance_score, sentiment_score,
                          professionalism_score, script_adherence_score,
                          red_flags, details, audit_date
                   FROM audit_scores
                   WHERE agent_id = $1
                   ORDER BY audit_date DESC, id DESC LIMIT 1""",
                agent_id,
            )
            if not score_row:
                logger.info(f"_save_trend_snapshot: no scores yet for '{agent_name}', skipping")
                # Keep the key in _snapshotted so we do not retry snapshotting on every status poll.
                # If a new run is started today, the key will be discarded there to allow retry.
                return

            # Count total issues from red_flags (JSONB list)
            total_issues = 0
            try:
                flags_raw = score_row["red_flags"] or []
                if isinstance(flags_raw, str):
                    import json as _json
                    flags_raw = _json.loads(flags_raw)
                total_issues = len(flags_raw)
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

            conversations_analyzed = 0
            try:
                details = score_row["details"] or {}
                if isinstance(details, str):
                    import json as _json
                    details = _json.loads(details)
                pc = details.get("per_conversation", [])
                conversations_analyzed = len(pc)
                if total_issues == 0:
                    total_issues = sum(1 for c in pc if c.get("red_flags"))
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

            # Look up today's assignment to resolve the texter name
            assign_row = await conn.fetchrow(
                """SELECT agent_name AS texter_name, account_email
                   FROM account_assignments
                   WHERE LOWER(account_email) = LOWER($1) AND assigned_date = $2""",
                agent_email, today,   # pass date object, not string
            )
            snapshot_agent_name = assign_row["texter_name"] if assign_row else agent_name
            snapshot_account_email = assign_row["account_email"] if assign_row else agent_email

            audit_date_val = score_row["audit_date"] if score_row["audit_date"] else today
            now_ts = get_now()   # asyncpg needs a datetime object, not a string
            await conn.execute(
                """INSERT INTO trend_snapshots
                   (agent_name, audit_date, audit_timestamp, account_email,
                    total_issues, overall_score, compliance_score, sentiment_score,
                    professionalism_score, script_adherence_score, conversations_analyzed)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                   ON CONFLICT (agent_name, audit_date, account_email) 
                   DO UPDATE SET
                       audit_timestamp = EXCLUDED.audit_timestamp,
                       total_issues = EXCLUDED.total_issues,
                       overall_score = EXCLUDED.overall_score,
                       compliance_score = EXCLUDED.compliance_score,
                       sentiment_score = EXCLUDED.sentiment_score,
                       professionalism_score = EXCLUDED.professionalism_score,
                       script_adherence_score = EXCLUDED.script_adherence_score,
                       conversations_analyzed = EXCLUDED.conversations_analyzed""",
                snapshot_agent_name,
                audit_date_val,
                now_ts,
                snapshot_account_email,
                total_issues,
                score_row["overall_score"],
                score_row["compliance_score"],
                score_row["sentiment_score"],
                score_row["professionalism_score"],
                score_row["script_adherence_score"],
                conversations_analyzed,
            )
            logger.info(f"Trend snapshot saved for '{snapshot_agent_name}' (account: {agent_name}) on {today}")
    except Exception as exc:
        logger.exception(f"_save_trend_snapshot failed for '{agent_name}': {exc}")
        _snapshotted.discard(key)  # allow retry next poll


# ── Routes ──────────────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page():
    return FileResponse(str(Path(__file__).parent / "static" / "login.html"))


@app.get("/auth/google")
async def auth_google(request: Request):
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="OAuth not configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET")
    redirect_uri = f"{APP_BASE_URL}/auth/callback"
    async with AsyncOAuth2Client(client_id=GOOGLE_CLIENT_ID, redirect_uri=redirect_uri) as client:
        uri, state = client.create_authorization_url(
            "https://accounts.google.com/o/oauth2/v2/auth",
            scope="openid email profile",
        )
    request.session["oauth_state"] = state
    return RedirectResponse(uri)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(f"/login?error={error}")

    expected = request.session.pop("oauth_state", None)
    if not expected or state != expected:
        return RedirectResponse("/login?error=state_mismatch")

    redirect_uri = f"{APP_BASE_URL}/auth/callback"
    try:
        async with AsyncOAuth2Client(
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            redirect_uri=redirect_uri,
        ) as client:
            await client.fetch_token("https://oauth2.googleapis.com/token", code=code)
            resp = await client.get("https://www.googleapis.com/oauth2/v3/userinfo")
        email = (resp.json().get("email") or "").lower().strip()
    except Exception as exc:
        logger.warning(f"OAuth callback error: {exc}")
        return RedirectResponse("/login?error=oauth_failed")

    if not email:
        return RedirectResponse("/login?error=no_email")

    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM tool_access WHERE LOWER(email) = $1 AND is_active = TRUE", email
        )
    if not row:
        logger.warning(f"Unauthorized login attempt: {email}")
        return RedirectResponse("/login?error=unauthorized")

    request.session["user_email"] = email
    logger.info(f"Login: {email}")
    return RedirectResponse("/")


@app.get("/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


@app.get("/api/me")
async def api_me(request: Request):
    """Return the current session user's email."""
    email = request.session.get("user_email", "")
    return {"email": email}


@app.delete("/api/reset-dedup-cache")
async def api_reset_dedup_cache(request: Request):
    """Owner-only: clear the dedup cache so all conversations can be re-audited."""
    requester = (request.session.get("user_email") or "").lower()
    if requester != OWNER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute("DELETE FROM audited_chats")
        logger.info(f"reset-dedup-cache: audited_chats cleared by {requester}")
        return {"success": True}
    except Exception as exc:
        logger.exception("Error in /api/reset-dedup-cache")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/reset-history")
async def api_reset_history(request: Request):
    """Owner-only: wipe all conversation history, keeping ML data, trends, accounts/credentials/keys/labels."""
    requester = (request.session.get("user_email") or "").lower()
    if requester != OWNER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute("DELETE FROM flagged_conversation_reviews")
            await conn.execute("DELETE FROM conversation_scores")
            await conn.execute("DELETE FROM messages")
            await conn.execute("DELETE FROM conversations")
            await conn.execute("DELETE FROM contacts")
            await conn.execute("DELETE FROM audit_scores")
            await conn.execute("DELETE FROM extractions")
            await conn.execute("DELETE FROM audited_chats")
            await conn.execute("DELETE FROM session_events")
        _snapshotted.clear()
        logger.info(f"reset-history: conversation history wiped by {requester}")
        return {"success": True}
    except Exception as exc:
        logger.exception("Error in /api/reset-history")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Tool Access API ───────────────────────────────────────────────────────────

class ToolAccessRequest(BaseModel):
    email: str


_OWNER_EMAILS = {e.lower() for e in TOOL_ACCESS_SEED_EMAILS}

def _mask_added_by(added_by: str) -> str:
    """Never expose the owner's email — show 'Owner' instead."""
    if not added_by:
        return "Owner"
    if added_by.lower() in _OWNER_EMAILS or added_by in ("system", "Owner"):
        return "Owner"
    return added_by


@app.get("/api/tool-access")
async def api_tool_access_list():
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT email, added_by, added_at, is_active FROM tool_access ORDER BY added_at"
            )
        return {"success": True, "data": [
            {
                "email": r["email"],
                "added_by": _mask_added_by(r["added_by"] or ""),
                "added_at": r["added_at"].isoformat() if r["added_at"] else None,
                "is_active": r["is_active"],
            }
            for r in rows
        ]}
    except Exception as exc:
        logger.exception("Error in GET /api/tool-access")
        return {"success": False, "error": str(exc)}


@app.post("/api/tool-access", dependencies=[Depends(require_admin)])
async def api_tool_access_add(body: ToolAccessRequest, request: Request):
    email = body.email.strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    requester = (request.session.get("user_email") or "").lower()
    added_by = "Owner" if requester in _OWNER_EMAILS else requester
    try:
        async with app.state.pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT id FROM tool_access WHERE LOWER(email) = $1", email
            )
            if exists:
                raise HTTPException(status_code=409, detail="Email already in tool_access")
            await conn.execute(
                "INSERT INTO tool_access (email, added_by) VALUES ($1, $2)", email, added_by
            )
        logger.info(f"tool_access add: {email} by {requester}")
        return {"success": True, "data": {"email": email, "added_by": added_by}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in POST /api/tool-access")
        return {"success": False, "error": str(exc)}


@app.delete("/api/tool-access/{email:path}", dependencies=[Depends(require_admin)])
async def api_tool_access_remove(email: str, request: Request):
    email = email.strip().lower()
    requester = request.session.get("user_email", "")
    if email == OWNER_EMAIL:
        raise HTTPException(status_code=403, detail="Owner account cannot be removed")
    if email == requester:
        raise HTTPException(status_code=400, detail="Cannot remove your own access")
    try:
        async with app.state.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM tool_access WHERE LOWER(email) = $1", email
            )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Email not found in tool_access")
        logger.info(f"tool_access remove: {email} by {requester}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in DELETE /api/tool-access")
        return {"success": False, "error": str(exc)}


@app.get("/api/custom-labels")
async def api_custom_labels_list():
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, created_at FROM custom_labels ORDER BY name"
            )
        return {"success": True, "data": [
            {
                "name": r["name"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]}
    except Exception as exc:
        logger.exception("Error in GET /api/custom-labels")
        return {"success": False, "error": str(exc)}


@app.post("/api/custom-labels")
async def api_custom_labels_add(body: CustomLabelRequest):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Label name required")
    try:
        async with app.state.pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT id FROM custom_labels WHERE LOWER(name) = LOWER($1)", name
            )
            if exists:
                raise HTTPException(status_code=409, detail="Label already exists")
            await conn.execute(
                "INSERT INTO custom_labels (name) VALUES ($1)", name
            )
        logger.info(f"custom label added: {name}")
        return {"success": True, "data": {"name": name}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in POST /api/custom-labels")
        return {"success": False, "error": str(exc)}


@app.delete("/api/custom-labels/{name:path}")
async def api_custom_labels_remove(name: str):
    name = name.strip()
    try:
        async with app.state.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM custom_labels WHERE LOWER(name) = LOWER($1)", name
            )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Label not found")
        logger.info(f"custom label removed: {name}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in DELETE /api/custom-labels")
        return {"success": False, "error": str(exc)}


# ── Blacklist Labels ─────────────────────────────────────────────────────────

@app.get("/api/blacklist-labels")
async def api_blacklist_labels_list():
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name, skip_mode, created_at FROM blacklist_labels ORDER BY name"
            )
        return {"success": True, "data": [
            {
                "id": r["id"],
                "name": r["name"],
                "skip_mode": r["skip_mode"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]}
    except Exception as exc:
        logger.exception("Error in GET /api/blacklist-labels")
        return {"success": False, "error": str(exc)}


@app.post("/api/blacklist-labels")
async def api_blacklist_labels_add(body: BlacklistLabelRequest):
    name = body.name.strip()
    skip_mode = body.skip_mode.strip() if body.skip_mode.strip() in ("any", "only") else "any"
    if not name:
        raise HTTPException(status_code=400, detail="Label name required")
    try:
        async with app.state.pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT id FROM blacklist_labels WHERE LOWER(name) = LOWER($1)", name
            )
            if exists:
                raise HTTPException(status_code=409, detail="Label already in blacklist")
            row = await conn.fetchrow(
                "INSERT INTO blacklist_labels (name, skip_mode) VALUES ($1, $2) RETURNING id, name, skip_mode",
                name, skip_mode
            )
        logger.info(f"blacklist label added: {name} (mode={skip_mode})")
        return {"success": True, "data": {"id": row["id"], "name": row["name"], "skip_mode": row["skip_mode"]}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in POST /api/blacklist-labels")
        return {"success": False, "error": str(exc)}


@app.delete("/api/blacklist-labels/{label_id:int}")
async def api_blacklist_labels_remove(label_id: int):
    try:
        async with app.state.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM blacklist_labels WHERE id = $1", label_id
            )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Blacklist label not found")
        logger.info(f"blacklist label removed id={label_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in DELETE /api/blacklist-labels")
        return {"success": False, "error": str(exc)}


@app.get("/api/agents")
async def api_agents():
    """Return all agents with their latest audit scores."""
    try:
        agents = await _fetch_agents_with_scores()
        _cleanup_finished()
        for agent in agents:
            agent["process_status"] = _agent_status(agent["name"])

        # ── Backfill missing trend snapshots ──────────────────────────────────────────
        # If the server was restarted after an audit finished, the in-memory
        # running_processes dict is lost and _save_trend_snapshot never fired.
        # Detect agents whose latest audit_date is today but have no snapshot
        # for today, and create the snapshot now.
        from datetime import date as _date
        today = get_now().date().isoformat()
        for agent in agents:
            audit_date = agent.get("audit_date")
            if audit_date is not None:
                audit_date_str = audit_date.isoformat() if isinstance(audit_date, _date) else str(audit_date)
                if audit_date_str == today and agent.get("overall_score") is not None:
                    key = (agent["name"], today)
                    if key not in _snapshotted:
                        try:
                            logger.info(f"Backfill: attempting snapshot for '{agent['name']}' (audit_date={audit_date})")
                            await _save_trend_snapshot(agent["name"])
                            logger.info(f"Backfill: snapshot saved for '{agent['name']}'")
                        except Exception as exc:
                            logger.exception(f"Backfill: FAILED for '{agent['name']}': {exc}")

        return agents
    except Exception as exc:
        logger.exception("Error in /api/agents")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/run", dependencies=[Depends(require_admin)])
async def api_run(body: RunRequest):
    """
    Start a background audit for a single agent.

    Body: {"agent_name": "Noah"}
    Returns: {"status": "started"|"already_running", "agent": ...}
    """
    agent_name = body.agent_name.strip()

    if not agent_name:
        raise HTTPException(status_code=400, detail="agent_name is required")

    if body.date_filter not in _ALLOWED_DATE_FILTERS:
        raise HTTPException(
            status_code=400,
            detail=f"date_filter must be one of {sorted(_ALLOWED_DATE_FILTERS)}",
        )
    if body.date_start and not _ISO_DATE_RE.match(body.date_start):
        raise HTTPException(status_code=400, detail="date_start must be YYYY-MM-DD")
    if body.date_end and not _ISO_DATE_RE.match(body.date_end):
        raise HTTPException(status_code=400, detail="date_end must be YYYY-MM-DD")
    try:
        sample_size = int(body.sample_size)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="sample_size must be an integer")
    if not (1 <= sample_size <= 1000):
        raise HTTPException(status_code=400, detail="sample_size must be 1..1000")

    _cleanup_finished()

    # ── Single agent ──────────────────────────────────────────────────────────────────
    existing = running_processes.get(agent_name)
    if existing not in (None, "done", "failed") and existing.poll() is None:
        return {"status": "already_running", "agent": agent_name}

    # Clear today's snapshot block so a new run can attempt trend snapshotting
    _snapshotted.discard((agent_name, get_now().date().isoformat()))

    try:
        from datetime import date as _date

        async with app.state.pool.acquire() as conn:
            account_row = await conn.fetchrow(
                "SELECT email FROM accounts WHERE LOWER(name) = LOWER($1) LIMIT 1",
                agent_name,
            )
            if not account_row:
                raise HTTPException(status_code=404, detail=f"Account '{agent_name}' not found")

            today = get_now().date()
            assignment = await conn.fetchrow(
                """SELECT aa.agent_name, aa.groq_key_id, k.api_key
                   FROM account_assignments aa
                   LEFT JOIN api_keys k
                     ON k.id = aa.groq_key_id
                    AND k.provider = 'groq'
                    AND k.agent_name IS NULL
                   WHERE LOWER(aa.account_email) = LOWER($1)
                     AND aa.assigned_date = $2
                   LIMIT 1""",
                account_row["email"],
                today,
            )

        if not assignment or not assignment["agent_name"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No texter assigned for today. "
                    "Go to Settings → Daily Assignments."
                ),
            )
        if not assignment["groq_key_id"] or not assignment["api_key"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No Groq key assigned for today. "
                    "Go to Settings → Daily Assignments."
                ),
            )

        RUN_STATUS_DIR.mkdir(parents=True, exist_ok=True)
        status_path = _new_run_status_path(agent_name)
        running_status_files[agent_name] = status_path
        running_status_details[agent_name] = {
            "agent": agent_name,
            "state": "running",
            "stage": "starting",
            "message": "Starting audit",
            "updated_at": get_now().isoformat(),
        }

        extra_env = {
            "PYTITLE": f"TEXTING Scraper - {agent_name}",
            "GROQ_PINNED_KEY": assignment["api_key"],
            "GROQ_ASSIGNMENT_STRICT": "1",
            "AUDIT_STATUS_FILE": str(status_path),
            # Point the subprocess at the dashboard-hosted embedding service so
            # it skips the in-process sentence-transformer model load.
            "EMBEDDING_SERVICE_URL": EMBEDDING_SERVICE_URL,
        }

        cmd = [
            sys.executable, MAIN_PY, "--single", agent_name,
            "--date-filter", body.date_filter,
            "--limit", str(sample_size),
        ]
        # Append custom date range args if provided
        if body.date_start and body.date_end:
            cmd.extend(["--date-start", body.date_start, "--date-end", body.date_end])
        # Append custom labels if provided. Legacy "All labels" means leave
        # SmarterContact's label filter untouched, which is already all labels.
        label_filter = _normalize_label_filter(body.labels)
        if label_filter:
            cmd.extend(["--labels", label_filter])

        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
            env={**os.environ, **extra_env},
        )
        running_processes[agent_name] = proc
        running_pinned_keys[agent_name] = assignment["api_key"]
        logger.info(
            f"Started audit subprocess for '{agent_name}' (PID {proc.pid}) "
            f"pinned=...{assignment['api_key'][-6:]}"
        )
        return {"status": "started", "agent": agent_name}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Failed to start subprocess for '{agent_name}'")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/status")
async def api_status():
    """
    Return current process states for all known agents.

    Example response:
        {"Noah": "running", "Resva1006": "done", "Charles": "idle"}
    """
    _cleanup_finished()
    statuses = {name: _agent_status(name) for name in running_processes}
    status_details = {
        name: {
            **_read_run_status_detail(name),
            "state": statuses.get(name),
        }
        for name in running_processes
    }
    key_assignments = {
        name: (f"...{key[-6:]}" if isinstance(key, str) and len(key) >= 6 else key)
        for name, key in running_pinned_keys.items()
    }
    # Save a trend snapshot for each agent that just finished
    for name, status in statuses.items():
        if status == "done":
            await _save_trend_snapshot(name)
    return {
        "statuses": statuses,
        "status_details": status_details,
        "key_assignments": key_assignments,
    }


@app.post("/api/clear-stuck", dependencies=[Depends(require_admin)])
async def api_clear_stuck(body: ClearStuckRequest = ClearStuckRequest()):
    """
    Force-clear stuck 'Logging in' or 'Failed' badges for one or all agents.

    Body (optional JSON):
        {"agent_name": "Kev1040"}   — clear one specific agent
        {}                          — clear ALL stuck agents

    An agent is "stuck" if it is in running_processes but its process has
    already exited OR it has been running longer than _MAX_RUN_MINUTES.
    Failed entries are also cleared so the badge resets to idle.
    """
    target = body.agent_name.strip()
    cleared = []

    candidates = [target] if target else list(running_processes.keys())
    for name in candidates:
        proc = running_processes.get(name)
        if proc is None:
            continue
        # Clear if already marked done/failed, or process is dead, or it's just stuck
        is_terminal = proc in {"done", "failed"}
        is_dead     = (not is_terminal) and proc.poll() is not None
        started     = _run_started_at.get(name)
        is_overtime = started and (get_now() - started).total_seconds() / 60 > _MAX_RUN_MINUTES
        if is_terminal or is_dead or is_overtime:
            if not is_terminal:
                try:
                    proc.kill()
                except Exception as _e:
                    logger.debug("swallowed: %r", _e)
            running_processes.pop(name, None)
            running_pinned_keys.pop(name, None)
            _run_started_at.pop(name, None)
            sf = running_status_files.pop(name, None)
            if sf and sf.exists():
                try:
                    sf.unlink()
                except Exception as _e:
                    logger.debug("swallowed: %r", _e)
            running_status_details.pop(name, None)
            cleared.append(name)
            logger.info(f"[clear-stuck] Evicted '{name}' from process registry")

    return {"cleared": cleared, "count": len(cleared)}


@app.get("/api/ai/status")
async def api_ai_status():
    """
    Return real-time health of the multi-provider AI key pool.

    Response:
        {
          "success": true,
          "data": {
            "total_keys": 14,
            "available_keys": 13,
            "cooling_keys": 1,
            "providers": {
              "groq": {"total": 14, "available": 13, "model": "...", "success": 42, "failures": 1}
            }
          }
        }
    """
    try:
        from ai.analyzer import get_pool_status
        return {"success": True, "data": get_pool_status()}
    except Exception as exc:
        logger.exception("Error in /api/ai/status")
        return {"success": False, "error": str(exc)}


@app.get("/api/agent/{agent_id}")
async def api_agent_detail(agent_id: int):
    """Return full per-conversation details for one agent."""
    try:
        detail = await _fetch_agent_detail(agent_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return detail
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Error in /api/agent/{agent_id}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/agent/{agent_id}/conversations")
async def api_agent_conversations(agent_id: int):
    """Return conversations with parsed messages + AI analysis for one agent."""
    try:
        data = await _fetch_agent_conversations(agent_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        return data
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Error in /api/agent/{agent_id}/conversations")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/reset-all", dependencies=[Depends(require_admin)])
async def api_reset_all():
    """Clear audit score summaries for every agent so the next run starts fresh.
    Conversations, messages, and conversation_scores are preserved for Detailed Dashboard history.
    Trend snapshots are also preserved."""
    try:
        async with app.state.pool.acquire() as conn:
            count_row = await conn.fetchrow("SELECT COUNT(*) AS cnt FROM accounts")
            count = count_row["cnt"] if count_row else 0
            await conn.execute("DELETE FROM audit_scores")
            await conn.execute("UPDATE conversations SET is_archived = TRUE")
        _snapshotted.clear()
        logger.info(f"Reset-all: cleared audit_scores and archived all conversations for {count} agents")
        return {"status": "ok", "agents_cleared": count}
    except Exception as exc:
        logger.exception("Error in /api/reset-all")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/agent/{agent_id}/reset", dependencies=[Depends(require_admin)])
async def api_agent_reset(agent_id: int):
    """
    Clear audit score summary for one agent so the next run scores from scratch.
    Conversations are marked as archived so they disappear from the main dashboard,
    but they remain in the database for Detailed Dashboard history.
    Trend snapshots are preserved.
    """
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name FROM accounts WHERE id = $1", agent_id)
            if not row:
                raise HTTPException(status_code=404, detail="Agent not found")
            name = row["name"]
            await conn.execute("DELETE FROM audit_scores WHERE agent_id = $1", agent_id)
            await conn.execute("UPDATE conversations SET is_archived = TRUE WHERE agent_id = $1", agent_id)
        _snapshotted.discard((name, get_now().date().isoformat()))
        # ── Also evict from in-memory process registry so the UI badge clears immediately
        # Without this, the stuck "Logging in" badge persists until the next cleanup cycle.
        proc = running_processes.pop(name, None)
        if proc not in (None, "done", "failed"):
            try:
                proc.kill()
            except Exception as _e:
                logger.debug("swallowed: %r", _e)
        running_pinned_keys.pop(name, None)
        sf = running_status_files.pop(name, None)
        if sf and sf.exists():
            try:
                sf.unlink()
            except Exception as _e:
                logger.debug("swallowed: %r", _e)
        running_status_details.pop(name, None)
        logger.info(f"Reset agent_id={agent_id} ('{name}'): cleared scores, archived conversations, evicted process state.")
        return {"status": "ok", "agent_id": agent_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Error in /api/agent/{agent_id}/reset")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/agents/add", dependencies=[Depends(require_admin)])
async def api_add_agent(body: AddAgentRequest):
    """Add a new agent to the database."""
    name  = body.name.strip()
    email = body.email.strip()
    pwd   = body.password.strip()
    tier  = body.funnel_tier.upper() if body.funnel_tier else None
    guidelines = body.guidelines.strip() if body.guidelines else None

    if not name or not email or not pwd:
        raise HTTPException(status_code=400, detail="name, email and password are required")

    # Validate tier if provided
    if tier and tier not in ("NF", "MF", "WF"):
        raise HTTPException(status_code=400, detail="Funnel tier must be NF, MF, WF, or empty")

    try:
        async with app.state.pool.acquire() as conn:
            # Check for duplicate email
            existing = await conn.fetchrow(
                "SELECT id FROM accounts WHERE LOWER(email) = LOWER($1)", email
            )
            if existing:
                raise HTTPException(status_code=409, detail=f"An agent with email {email} already exists")
            await conn.execute(
                "INSERT INTO accounts (name, email, password, funnel_tier, guidelines) VALUES ($1, $2, $3, $4, $5)",
                name, email, pwd, tier, guidelines,
            )

        logger.info(f"Added new agent: {name} <{email}> (tier={tier})")
        return {"status": "ok", "agent": {"name": name, "email": email, "funnel_tier": tier}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error in /api/agents/add")
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def api_edit_agent(agent_id: int, body: EditAgentRequest):
    """Update an agent's name, email, and/or password in the database."""
    name       = body.name.strip()
    email      = body.email.strip()
    pwd        = body.password.strip()
    tier       = body.funnel_tier.upper() if body.funnel_tier else None
    guidelines = body.guidelines.strip() if body.guidelines else None

    if not name or not email:
        raise HTTPException(status_code=400, detail="name and email are required")

    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT email FROM accounts WHERE id = $1", agent_id)
            if not row:
                raise HTTPException(status_code=404, detail="Agent not found")
            old_email = row["email"]

            # Check new email is not taken by another agent
            conflict = await conn.fetchrow(
                "SELECT id FROM accounts WHERE LOWER(email) = LOWER($1) AND id != $2",
                email, agent_id,
            )
            if conflict:
                raise HTTPException(status_code=409, detail=f"Email {email} is already used by another agent")

            # Update name, email, funnel_tier, guidelines, and optionally password
            if pwd:
                await conn.execute(
                    "UPDATE accounts SET name = $1, email = $2, password = $3, funnel_tier = $4, guidelines = $5 WHERE id = $6",
                    name, email, pwd, tier, guidelines, agent_id,
                )
            else:
                await conn.execute(
                    "UPDATE accounts SET name = $1, email = $2, funnel_tier = $3, guidelines = $4 WHERE id = $5",
                    name, email, tier, guidelines, agent_id,
                )
            # Update audited_chats email reference if email changed
            if old_email.lower() != email.lower():
                await conn.execute(
                    "UPDATE audited_chats SET agent_email = $1 WHERE agent_email = $2",
                    email, old_email,
                )

        logger.info(f"Updated agent id={agent_id}: {name} <{email}>")
        return {"status": "ok", "agent": {"id": agent_id, "name": name, "email": email}}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Error in PUT /api/agents/{agent_id}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/agents/{agent_id}", dependencies=[Depends(require_admin)])
async def api_delete_agent(agent_id: int):
    """Remove an agent and all their data from the database."""
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name, email FROM accounts WHERE id = $1", agent_id)
            if not row:
                raise HTTPException(status_code=404, detail="Agent not found")
            name, email = row["name"], row["email"]

            await conn.execute("DELETE FROM audited_chats   WHERE agent_email = $1", email)
            await conn.execute("DELETE FROM session_events  WHERE agent_id   = $1", agent_id)
            await conn.execute("DELETE FROM flag_feedback   WHERE agent_id   = $1", agent_id)
            await conn.execute("DELETE FROM audit_scores    WHERE agent_id   = $1", agent_id)
            await conn.execute("DELETE FROM extractions     WHERE agent_id   = $1", agent_id)
            await conn.execute("DELETE FROM conversations   WHERE agent_id   = $1", agent_id)
            await conn.execute("DELETE FROM accounts        WHERE id         = $1", agent_id)

        logger.info(f"Deleted agent id={agent_id}: {name} <{email}>")
        return {"status": "ok", "agent_id": agent_id}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Error in DELETE /api/agents/{agent_id}")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/agents/{agent_id}/apikey")
async def api_get_agent_key(agent_id: int):
    """Return the API key info for one agent (key is masked except last 6 chars)."""
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name FROM accounts WHERE id = $1", agent_id)
            if not row:
                raise HTTPException(status_code=404, detail="Agent not found")
            agent_name = row["name"].lower()
            key_row = await conn.fetchrow(
                "SELECT provider, api_key FROM api_keys WHERE LOWER(agent_name) = LOWER($1) LIMIT 1",
                agent_name,
            )
        if not key_row:
            return {"has_key": False, "provider": None, "key_preview": None}
        key_val = key_row["api_key"] or ""
        preview = ("*" * max(0, len(key_val) - 6)) + key_val[-6:] if key_val else ""
        return {"has_key": bool(key_val), "provider": key_row["provider"], "key_preview": preview}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/api/agents/{agent_id}/apikey", dependencies=[Depends(require_admin)])
async def api_set_agent_key(agent_id: int, body: EditAgentKeyRequest):
    """Set or remove the API key for one agent in the database."""
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT name FROM accounts WHERE id = $1", agent_id)
            if not row:
                raise HTTPException(status_code=404, detail="Agent not found")
            agent_name = row["name"].lower()

            key_val  = body.key.strip()
            provider = body.provider.strip().lower()

            if not key_val:
                # Remove key
                await conn.execute(
                    "DELETE FROM api_keys WHERE LOWER(agent_name) = LOWER($1)",
                    agent_name,
                )
            else:
                if provider != "groq":
                    raise HTTPException(status_code=400, detail="provider must be 'groq'")
                # Upsert: delete old + insert new
                await conn.execute(
                    "DELETE FROM api_keys WHERE LOWER(agent_name) = LOWER($1)",
                    agent_name,
                )
                await conn.execute(
                    "INSERT INTO api_keys (provider, api_key, agent_name) VALUES ($1, $2, $3)",
                    provider, key_val, agent_name,
                )

        logger.info(f"Updated API key for agent '{agent_name}' (provider={provider or 'removed'})")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# â"€â"€ Red Flag Feedback â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

@app.post("/api/redflag/invalid", dependencies=[Depends(require_admin)])
async def api_redflag_invalid(body: RedFlagFeedbackRequest):
    """Mark an AI red flag as invalid and retroactively remove it from stored scores."""
    if not body.agent_id or not body.red_flag.strip():
        raise HTTPException(status_code=400, detail="agent_id and red_flag are required")
    flag_str = body.red_flag.strip()
    remaining_flags: int | None = None
    try:
        async with app.state.pool.acquire() as conn:
            # 1. Resolve conversation_id (use UI-provided or look it up)
            conv_id = body.conversation_id
            if conv_id is None:
                conv_id_row = await conn.fetchrow(
                    """SELECT c.id FROM conversations c
                       JOIN contacts ct ON ct.id = c.contact_id
                       WHERE c.agent_id = $1 AND LOWER(ct.name) = LOWER($2)
                       ORDER BY c.id DESC LIMIT 1""",
                    body.agent_id, body.contact_name,
                )
                if conv_id_row:
                    conv_id = conv_id_row["id"]

            # 2. Record the human feedback
            await conn.execute(
                """INSERT INTO flag_feedback
                   (agent_id, agent_name, contact_name, red_flag, evidence, reason, category, conversation_id)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                body.agent_id, body.agent_name, body.contact_name,
                flag_str, body.evidence.strip(),
                body.reason.strip(), body.category.strip(), conv_id,
            )

            # 3. Remove flag from conversation_scores for this agent+contact
            cs_row = await conn.fetchrow(
                """SELECT cs.id, cs.red_flags
                   FROM conversation_scores cs
                   JOIN conversations c ON c.id = cs.conversation_id
                   JOIN contacts ct ON ct.id = c.contact_id
                   WHERE c.agent_id = $1
                     AND LOWER(ct.name) = LOWER($2)
                   ORDER BY cs.id DESC
                   LIMIT 1""",
                body.agent_id, body.contact_name,
            )
            if cs_row:
                flags = cs_row["red_flags"] or []
                if isinstance(flags, str):
                    try: flags = json.loads(flags)
                    except Exception: flags = []
                updated_flags = [f for f in flags if f.lower() != flag_str.lower()]
                remaining_flags = len(updated_flags)
                await conn.execute(
                    "UPDATE conversation_scores SET red_flags = $1::jsonb WHERE id = $2",
                    json.dumps(updated_flags), cs_row["id"],
                )
                if _is_wrong_label_flag(flag_str):
                    await conn.execute(
                        "UPDATE conversation_scores SET label_correct = true WHERE id = $1",
                        cs_row["id"],
                    )
            else:
                logger.warning(
                    f"redflag/invalid: no conversation_scores found for "
                    f"agent_id={body.agent_id} contact='{body.contact_name}'"
                )

            if remaining_flags == 0 and body.contact_name.strip():
                await _upsert_flag_review(
                    conn, body.agent_id, body.contact_name.strip(), conv_id,
                )
            elif cs_row and _is_wrong_label_flag(flag_str):
                # Wrong-label was the only issue type but counter only tracks red_flags length
                score_check = await conn.fetchrow(
                    "SELECT label_correct, red_flags FROM conversation_scores WHERE id = $1",
                    cs_row["id"],
                )
                if score_check and score_check["label_correct"]:
                    flags_left = score_check["red_flags"] or []
                    if isinstance(flags_left, str):
                        try:
                            flags_left = json.loads(flags_left)
                        except Exception:
                            flags_left = []
                    if not flags_left:
                        await _upsert_flag_review(
                            conn, body.agent_id, body.contact_name.strip(), conv_id,
                        )

            # 4. Remove flag from audit_scores.details and recompute top-level red_flags
            as_row = await conn.fetchrow(
                """SELECT id, details FROM audit_scores
                   WHERE agent_id = $1
                   ORDER BY audit_date DESC, id DESC
                   LIMIT 1""",
                body.agent_id,
            )
            if as_row:
                details = as_row["details"] or {}
                if isinstance(details, str):
                    try: details = json.loads(details)
                    except Exception: details = {}
                pc_list = details.get("per_conversation", [])
                for pc in pc_list:
                    if (pc.get("contact") or "").lower().strip() == body.contact_name.lower().strip():
                        pc_flags = pc.get("red_flags") or []
                        pc["red_flags"] = [f for f in pc_flags if f.lower() != flag_str.lower()]
                        if _is_wrong_label_flag(flag_str):
                            pc["label_correct"] = True
                        break
                # Recompute top-level list (one entry per conversation that still has flags)
                top_flags = [pc.get("contact") for pc in pc_list if pc.get("red_flags")]
                await conn.execute(
                    "UPDATE audit_scores SET red_flags = $1::jsonb, details = $2::jsonb WHERE id = $3",
                    json.dumps(top_flags), json.dumps(details), as_row["id"],
                )

            # 4. Recompute total_issues from live conversation_scores for the latest snapshot.
            # Count conversations that still have at least one red flag — single source of truth.
            # texter_name in conversations matches agent_name in trend_snapshots directly.
            await conn.execute(
                """UPDATE trend_snapshots ts
                   SET total_issues = (
                       SELECT COUNT(*)
                       FROM conversations c
                       JOIN LATERAL (
                           SELECT red_flags FROM conversation_scores cs2
                           WHERE cs2.conversation_id = c.id
                           ORDER BY cs2.id DESC LIMIT 1
                       ) cs ON TRUE
                       WHERE LOWER(c.texter_name) = LOWER(ts.agent_name)
                         AND c.audit_date = ts.audit_date
                         AND jsonb_array_length(cs.red_flags::jsonb) > 0
                   )
                   WHERE ts.id = (
                       SELECT id FROM trend_snapshots
                       WHERE LOWER(agent_name) = LOWER($1)
                       ORDER BY audit_date DESC, id DESC
                       LIMIT 1
                   )""",
                body.agent_name,
            )

        logger.info(
            f"Flag marked invalid: agent={body.agent_name}, contact='{body.contact_name}', "
            f"flag='{flag_str[:60]}', remaining_flags={remaining_flags}"
        )
        return {"status": "ok", "remaining_flags": remaining_flags}
    except Exception as exc:
        logger.exception("Error in /api/redflag/invalid")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/conversation/flag-reviewed")
async def api_conversation_flag_reviewed(body: FlagReviewRequest):
    """Mark a flagged conversation as reviewed (manager opened / handled it)."""
    if not body.agent_id or not body.contact_name.strip():
        raise HTTPException(status_code=400, detail="agent_id and contact_name required")
    try:
        async with app.state.pool.acquire() as conn:
            conv_id = body.conversation_id
            if conv_id is None:
                cs_row = await conn.fetchrow(
                    """SELECT c.id AS conv_id
                       FROM conversations c
                       JOIN contacts ct ON ct.id = c.contact_id
                       WHERE c.agent_id = $1 AND LOWER(ct.name) = LOWER($2)
                       ORDER BY c.id DESC
                       LIMIT 1""",
                    body.agent_id, body.contact_name,
                )
                conv_id = cs_row["conv_id"] if cs_row else None

            await _upsert_flag_review(
                conn, body.agent_id, body.contact_name.strip(), conv_id,
            )

        logger.info(
            f"Flagged conversation reviewed: agent_id={body.agent_id}, "
            f"contact='{body.contact_name}'"
        )
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Error in /api/conversation/flag-reviewed")
        raise HTTPException(status_code=500, detail="internal error")


# â"€â"€ Account Assignments â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

@app.get("/api/groq-keys")
async def api_get_groq_keys():
    """
    Return shared Groq keys as UI-safe labeled options:
    [{id: 12, label: "Groq 1"}, ...]
    """
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, api_key
                   FROM api_keys
                   WHERE provider = 'groq' AND agent_name IS NULL
                   ORDER BY id"""
            )
        data = []
        for idx, row in enumerate(rows, start=1):
            data.append(
                {
                    "id": row["id"],
                    "label": f"Groq {idx}",
                }
            )
        return {"success": True, "data": data}
    except Exception as exc:
        logger.exception("Error in GET /api/groq-keys")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/assignments")
async def api_get_assignments(date: str = ""):
    """
    Return all account assignments for a given date (default: today).
    Unassigned accounts (from accounts table) are included with agent_name=null.
    """
    from datetime import date as _date
    if not date:
        date = get_now().date().isoformat()
    try:
        async with app.state.pool.acquire() as conn:
            # All SC accounts from accounts table
            agent_rows = await conn.fetch("SELECT name, email FROM accounts ORDER BY name")
            account_map = {r["email"]: r["name"] for r in agent_rows if r["email"]}

            from datetime import date as _date
            date_obj = _date.fromisoformat(date)
            rows = await conn.fetch(
                """SELECT aa.account_email, aa.agent_name, aa.groq_key_id,
                          aa.assigned_date, aa.assigned_at,
                          k.api_key
                   FROM account_assignments aa
                   LEFT JOIN api_keys k
                     ON k.id = aa.groq_key_id
                    AND k.provider = 'groq'
                    AND k.agent_name IS NULL
                   WHERE aa.assigned_date = $1""",
                date_obj,
            )
            shared_groq_rows = await conn.fetch(
                """SELECT id FROM api_keys
                   WHERE provider = 'groq' AND agent_name IS NULL
                   ORDER BY id"""
            )

        assigned_map = {r["account_email"]: dict(r) for r in rows}
        groq_rank = {r["id"]: i for i, r in enumerate(shared_groq_rows, start=1)}
        result = []
        for email, name in account_map.items():
            if email in assigned_map:
                row = assigned_map[email]
                key_id = row.get("groq_key_id")
                key_val = row.get("api_key") or ""
                row["account_name"] = name
                row["groq_key_label"] = f"Groq {groq_rank[key_id]}" if key_id in groq_rank else None
                row["groq_key_preview"] = f"...{key_val[-6:]}" if len(key_val) >= 6 else None
                row.pop("api_key", None)
                result.append(row)
            else:
                result.append(
                    {
                        "account_email": email,
                        "account_name": name,
                        "agent_name": None,
                        "groq_key_id": None,
                        "groq_key_label": None,
                        "groq_key_preview": None,
                        "assigned_date": date,
                        "assigned_at": None,
                    }
                )
        return {"success": True, "data": result, "date": date}
    except Exception as exc:
        logger.exception("Error in GET /api/assignments")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/assignments/dates")
async def api_assignments_dates():
    """Return distinct dates that have at least one assignment, newest first."""
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT assigned_date, COUNT(*) AS count
                   FROM account_assignments
                   GROUP BY assigned_date
                   ORDER BY assigned_date DESC"""
            )
        return {
            "success": True,
            "data": [{"date": r["assigned_date"].isoformat(), "count": r["count"]} for r in rows],
        }
    except Exception as exc:
        logger.exception("Error in GET /api/assignments/dates")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/assignments", dependencies=[Depends(require_admin)])
async def api_post_assignment(body: AssignmentRequest):
    """
    Assign an agent to an account for a given date (upserts - one assignment per account per day).
    Body: {account_email, agent_name, assigned_date, groq_key_id}
    """
    email      = body.account_email.strip()
    agent_name = body.agent_name.strip()
    date       = body.assigned_date.strip()
    groq_key_id = body.groq_key_id

    if not email or not date:
        raise HTTPException(status_code=400, detail="account_email and assigned_date are required")

    # If agent_name is empty, we treat this as an "unassign" action
    if not agent_name:
        async with app.state.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM account_assignments WHERE account_email = $1 AND assigned_date = $2::date",
                email, date
            )
        return {"status": "ok", "message": "Assignment removed"}

    if groq_key_id is None:
        raise HTTPException(status_code=400, detail="groq_key_id is required")

    if agent_name not in AGENT_ROSTER:
        raise HTTPException(status_code=400, detail=f"'{agent_name}' is not in the agent roster")

    # Validate date format
    try:
        from datetime import datetime as _dt
        _dt.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="assigned_date must be YYYY-MM-DD")

    # Validate account exists in accounts table + key belongs to shared groq pool.
    async with app.state.pool.acquire() as conn:
        acct = await conn.fetchrow(
            "SELECT id FROM accounts WHERE LOWER(email) = LOWER($1)", email
        )
        key_row = await conn.fetchrow(
            """SELECT id FROM api_keys
               WHERE id = $1
                 AND provider = 'groq'
                 AND agent_name IS NULL""",
            groq_key_id,
        )
    if not acct:
        raise HTTPException(status_code=404, detail=f"Account '{email}' not found in database")
    if not key_row:
        raise HTTPException(status_code=400, detail=f"groq_key_id '{groq_key_id}' is not a valid shared Groq key")

    try:
        async with app.state.pool.acquire() as conn:
            from datetime import date as _date
            date_obj = _date.fromisoformat(date)
            await conn.execute(
                """INSERT INTO account_assignments (account_email, agent_name, groq_key_id, assigned_date)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT(account_email, assigned_date) DO UPDATE SET
                       agent_name=EXCLUDED.agent_name,
                       groq_key_id=EXCLUDED.groq_key_id,
                       assigned_at=CURRENT_TIMESTAMP""",
                email, agent_name, groq_key_id, date_obj,
            )
        logger.info(f"Assignment saved: {email} -> {agent_name} (groq_key_id={groq_key_id}) on {date}")
        return {"success": True}
    except Exception as exc:
        logger.exception("Error in POST /api/assignments")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/assignments/copy-latest", dependencies=[Depends(require_admin)])
async def api_copy_latest_assignments(date: str = ""):
    """
    Find the most recent date with any assignments and copy them to the target date.
    """
    from datetime import date as _date
    if not date:
        date = get_now().date().isoformat()
    
    try:
        target_date = _date.fromisoformat(date)
        async with app.state.pool.acquire() as conn:
            # 1. Find the most recent date with assignments BEFORE target_date
            latest_date = await conn.fetchval(
                "SELECT MAX(assigned_date) FROM account_assignments WHERE assigned_date < $1",
                target_date
            )
            
            if not latest_date:
                return {"success": False, "error": "No previous assignments found to copy."}
            
            # 2. Copy those assignments to target_date
            # Use ON CONFLICT to avoid overwriting existing assignments if the user already made some for today
            result = await conn.execute(
                """
                INSERT INTO account_assignments (account_email, agent_name, groq_key_id, assigned_date)
                SELECT account_email, agent_name, groq_key_id, $1
                FROM account_assignments
                WHERE assigned_date = $2
                ON CONFLICT (account_email, assigned_date) DO NOTHING
                """,
                target_date, latest_date
            )
            
            count = int(result.split()[-1]) if "INSERT" in result else 0
            logger.info(f"Assignments copied from {latest_date} to {target_date} (count: {count})")
            return {"success": True, "from_date": str(latest_date), "count": count}
            
    except Exception as exc:
        logger.exception("Error in POST /api/assignments/copy-latest")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/assignments", dependencies=[Depends(require_admin)])
async def api_delete_assignments(date: str = ""):
    """Clear all account assignments for a given date."""
    from datetime import date as _date
    if not date:
        date = get_now().date().isoformat()
    try:
        date_obj = _date.fromisoformat(date)
        async with app.state.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM account_assignments WHERE assigned_date = $1",
                date_obj
            )
        logger.info(f"Assignments cleared for date: {date}")
        return {"success": True}
    except Exception as exc:
        logger.exception("Error in DELETE /api/assignments")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/assignments/history")
async def api_assignment_history(account: str = ""):
    """Return full assignment history for one account email, newest first."""
    if not account:
        raise HTTPException(status_code=400, detail="account query param is required")
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT aa.account_email, aa.agent_name, aa.groq_key_id,
                          aa.assigned_date, aa.assigned_at,
                          k.api_key
                   FROM account_assignments aa
                   LEFT JOIN api_keys k
                     ON k.id = aa.groq_key_id
                    AND k.provider = 'groq'
                    AND k.agent_name IS NULL
                   WHERE LOWER(aa.account_email) = LOWER($1)
                   ORDER BY aa.assigned_date DESC""",
                account,
            )
            shared_groq_rows = await conn.fetch(
                """SELECT id FROM api_keys
                   WHERE provider = 'groq' AND agent_name IS NULL
                   ORDER BY id"""
            )
        groq_rank = {r["id"]: i for i, r in enumerate(shared_groq_rows, start=1)}
        data = []
        for row in rows:
            rec = dict(row)
            key_id = rec.get("groq_key_id")
            key_val = rec.get("api_key") or ""
            rec["groq_key_label"] = f"Groq {groq_rank[key_id]}" if key_id in groq_rank else None
            rec["groq_key_preview"] = f"...{key_val[-6:]}" if len(key_val) >= 6 else None
            rec.pop("api_key", None)
            data.append(rec)
        return {"success": True, "data": data}
    except Exception as exc:
        logger.exception("Error in GET /api/assignments/history")
        raise HTTPException(status_code=500, detail=str(exc))


# â"€â"€ Trends â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

@app.get("/api/trends")
async def api_trends(start: str = "", end: str = "", agent: str = "all"):
    """
    Return trend snapshots filtered by date range and optional agent name.
    Query params: start (YYYY-MM-DD), end (YYYY-MM-DD), agent (name or 'all')
    """
    from datetime import date as _date, timedelta
    if not start:
        start = (get_now().date() - timedelta(days=30)).isoformat()
    if not end:
        end = get_now().date().isoformat()

    # asyncpg requires actual date objects, not strings
    try:
        start_d = _date.fromisoformat(start)
        end_d   = _date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    _UNIQUE_CONVOS_SQL = """
        SELECT COUNT(DISTINCT c.id)
        FROM conversations c
        JOIN LATERAL (
            SELECT 1 FROM conversation_scores cs2
            WHERE cs2.conversation_id = c.id
            LIMIT 1
        ) cs ON TRUE
        WHERE c.audit_date BETWEEN $1 AND $2
          {agent_clause}
    """

    try:
        async with app.state.pool.acquire() as conn:
            if agent.lower() == "all":
                rows = await conn.fetch(
                    """SELECT * FROM trend_snapshots
                       WHERE audit_date >= $1 AND audit_date <= $2
                       ORDER BY audit_date ASC, agent_name ASC""",
                    start_d, end_d,
                )
                unique_convos = await conn.fetchval(
                    _UNIQUE_CONVOS_SQL.format(agent_clause=""),
                    start_d,
                    end_d,
                )
            else:
                rows = await conn.fetch(
                    """SELECT * FROM trend_snapshots
                       WHERE audit_date >= $1 AND audit_date <= $2 AND LOWER(agent_name) = LOWER($3)
                       ORDER BY audit_date ASC""",
                    start_d, end_d, agent,
                )
                unique_convos = await conn.fetchval(
                    _UNIQUE_CONVOS_SQL.format(agent_clause="AND LOWER(c.texter_name) = LOWER($3)"),
                    start_d,
                    end_d,
                    agent,
                )
        snapshot_convos = sum((r["conversations_analyzed"] or 0) for r in rows)
        return {
            "success": True,
            "data": [dict(r) for r in rows],
            "summary": {
                "unique_conversations": int(unique_convos or 0),
                "snapshot_conversations_total": snapshot_convos,
            },
        }
    except Exception as exc:
        logger.exception("Error in GET /api/trends")
        raise HTTPException(status_code=500, detail=str(exc))




@app.delete("/api/trends", dependencies=[Depends(require_admin)])
async def api_trends_reset(agent: str = "all"):
    """
    Delete trend snapshots - either all records or just one agent's.

    Query params:
        agent  - agent name to wipe, or 'all' (default) to wipe everything

    Returns: {"success": true, "deleted": row count}
    """
    global _snapshotted
    try:
        async with app.state.pool.acquire() as conn:
            if agent.lower() == "all":
                result = await conn.execute("DELETE FROM trend_snapshots")
                deleted = int(result.split()[-1]) if result else 0
                _snapshotted.clear()
                logger.info(f"Trend data reset: deleted all {deleted} snapshot rows")
            else:
                result = await conn.execute(
                    "DELETE FROM trend_snapshots WHERE LOWER(agent_name) = LOWER($1)",
                    agent,
                )
                deleted = int(result.split()[-1]) if result else 0
                keys_to_remove = {k for k in _snapshotted if k[0].lower() == agent.lower()}
                _snapshotted -= keys_to_remove
                logger.info(f"Trend data reset: deleted {deleted} rows for agent '{agent}'")
        return {"success": True, "deleted": deleted}
    except Exception as exc:
        logger.exception("Error in DELETE /api/trends")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Detailed Dashboard endpoints ───────────────────────────────────────────────

@app.get("/api/detailed-dashboard")
async def api_detailed_dashboard(
    texter_name: str = "",
    start_date: str = "",
    end_date: str = "",
    flagged_only: bool = False,
    contact_name: str = "",
):
    """
    Return conversations for a texter within a date range.

    Required query params: texter_name, start_date (YYYY-MM-DD), end_date (YYYY-MM-DD)
    Optional: flagged_only=true limits results to conversations with red flags or label issues.
    Optional: contact_name filters by owner/contact name (partial, case-insensitive).
    """
    if not texter_name or not start_date or not end_date:
        raise HTTPException(
            status_code=400,
            detail="texter_name, start_date, and end_date are all required",
        )
    _tn = texter_name.strip().lower()
    all_texters = _tn in {"all", "all texters", "__all__", "*"}
    # asyncpg requires actual date objects, not strings
    from datetime import date as _date
    try:
        start_d = _date.fromisoformat(start_date)
        end_d   = _date.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    flagged_clause = """
                  AND (
                    jsonb_array_length(cs.red_flags::jsonb) > 0
                    OR (cs.label_correct = false AND cs.label_assigned IS DISTINCT FROM cs.label_should_be)
                  )""" if flagged_only else ""

    _DETAILED_SQL = """
                SELECT
                    c.id             AS conversation_id,
                    ct.name          AS contact_name,
                    c.assigned_labels,
                    c.audit_date,
                    c.texter_name,
                    cs.compliance_score,
                    cs.sentiment_score,
                    cs.professionalism_score,
                    cs.script_adherence_score,
                    cs.red_flags,
                    cs.label_correct,
                    cs.label_assigned,
                    cs.label_should_be,
                    (
                      jsonb_array_length(cs.red_flags::jsonb)
                      + CASE WHEN cs.label_correct = false
                               AND cs.label_assigned IS DISTINCT FROM cs.label_should_be
                             THEN 1 ELSE 0 END
                    ) AS issue_count,
                    (
                        SELECT m.body FROM messages m
                        WHERE m.conversation_id = c.id
                          AND m.sender = 'agent'
                        ORDER BY m.seq ASC, m.id ASC
                        LIMIT 1
                    ) AS preview_snippet
                FROM conversations c
                JOIN contacts ct ON ct.id = c.contact_id
                JOIN LATERAL (
                    SELECT * FROM conversation_scores cs2
                    WHERE cs2.conversation_id = c.id
                    ORDER BY cs2.id DESC
                    LIMIT 1
                ) cs ON TRUE
                WHERE {texter_clause}
                  AND c.audit_date BETWEEN $1 AND $2
                  {contact_clause}
                  {flagged_clause}
                ORDER BY c.audit_date DESC, c.id DESC
                """

    contact_search = contact_name.strip()
    params: list = [start_d, end_d]
    texter_clause = "TRUE"
    if not all_texters:
        texter_clause = f"c.texter_name = ${len(params) + 1}"
        params.append(texter_name)

    contact_clause = ""
    if contact_search:
        contact_clause = f"AND ct.name ILIKE ${len(params) + 1}"
        params.append(f"%{contact_search}%")

    try:
        async with app.state.pool.acquire() as conn:
            sql = _DETAILED_SQL.format(
                texter_clause=texter_clause,
                contact_clause=contact_clause,
                flagged_clause=flagged_clause,
            )
            rows = await conn.fetch(sql, *params)

        result = []
        for row in rows:
            r = dict(row)
            # Normalize JSONB
            rf = r.get("red_flags") or []
            if isinstance(rf, str):
                try:
                    import json as _json
                    rf = _json.loads(rf)
                except Exception:
                    rf = []
            r["red_flags"] = rf
            r["assigned_labels"] = list(r.get("assigned_labels") or [])
            result.append(r)

        logger.info(
            "detailed-dashboard: texter=%r all_texters=%s flagged_only=%s contact=%r range=%s..%s rows=%d",
            texter_name, all_texters, flagged_only, contact_search or None, start_d, end_d, len(result),
        )
        return {"success": True, "data": result}
    except Exception as exc:
        logger.exception("Error in GET /api/detailed-dashboard")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/conversation/{conversation_id}/messages")
async def api_conversation_messages(conversation_id: int):
    """
    Return full conversation data for the Detailed Dashboard drill-down.

    Returns: contact_name, assigned_labels, texter_name, audit_date,
             parsed_messages (from messages table),
             analysis (from conversation_scores),
             invalidated_flags (from flag_feedback)
    """
    try:
        async with app.state.pool.acquire() as conn:
            # Basic conversation info
            conv_row = await conn.fetchrow(
                """SELECT c.id, ct.name AS contact_name, c.assigned_labels,
                          c.texter_name, c.audit_date,
                          a.id AS agent_id
                   FROM conversations c
                   JOIN contacts ct ON ct.id = c.contact_id
                   JOIN accounts a ON a.id = c.agent_id
                   WHERE c.id = $1""",
                conversation_id,
            )
            if not conv_row:
                raise HTTPException(status_code=404, detail="Conversation not found")

            agent_id = conv_row["agent_id"]

            # Messages
            msg_rows = await conn.fetch(
                """SELECT sender, body AS message, sent_at AS time, sc_date_label
                   FROM messages
                   WHERE conversation_id = $1
                   ORDER BY seq ASC, id ASC""",
                conversation_id,
            )

            # AI analysis
            score_row = await conn.fetchrow(
                """SELECT compliance_score, sentiment_score, professionalism_score,
                          script_adherence_score, funnel_stage, pillars_gathered,
                          rebuttals_used, label_assigned, label_correct,
                          label_should_be, label_reason, red_flags, summary, model_used,
                          COALESCE(source, 'groq') AS source
                   FROM conversation_scores
                   WHERE conversation_id = $1
                   ORDER BY id DESC LIMIT 1""",
                conversation_id,
            )

            # Invalidated flags for this contact + agent
            fb_rows = await conn.fetch(
                """SELECT red_flag FROM flag_feedback
                   WHERE agent_id = $1 AND contact_name = $2""",
                agent_id, conv_row["contact_name"],
            )

        analysis = {}
        if score_row:
            analysis = dict(score_row)
            for field in ("pillars_gathered", "rebuttals_used", "red_flags"):
                val = analysis.get(field) or []
                if isinstance(val, str):
                    try:
                        import json as _json
                        val = _json.loads(val)
                    except Exception:
                        val = []
                analysis[field] = val

        return {
            "success": True,
            "data": {
                "contact_name":      conv_row["contact_name"],
                "conversation_id":   conversation_id,
                "assigned_labels":   list(conv_row["assigned_labels"] or []),
                "texter_name":       conv_row["texter_name"],
                "audit_date":        str(conv_row["audit_date"]) if conv_row["audit_date"] else None,
                "parsed_messages":   [dict(m) for m in msg_rows],
                "analysis":          analysis,
                "invalidated_flags": [r["red_flag"] for r in fb_rows],
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Error in GET /api/conversation/{conversation_id}/messages")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Texter Roster endpoints ────────────────────────────────────────────────────




@app.get("/api/roster")
async def api_get_roster():
    """Return the current texter roster list from the database."""
    await _load_agent_roster_from_db()
    return AGENT_ROSTER


@app.post("/api/roster", dependencies=[Depends(require_admin)])
async def api_post_roster(body: AddTexterRequest):
    """Add a new texter to the database roster."""
    global AGENT_ROSTER
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO texters (name) VALUES ($1) ON CONFLICT (name) DO NOTHING",
                name,
            )
        await _load_agent_roster_from_db()
        logger.info(f"Roster: added '{name}' ({len(AGENT_ROSTER)} total)")
        return {"status": "ok", "roster": AGENT_ROSTER}
    except Exception as exc:
        logger.exception(f"Error adding texter '{name}'")
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/api/roster/{name:path}", dependencies=[Depends(require_admin)])
async def api_delete_roster(name: str):
    """Remove a texter from the database roster and wipe all their historical data."""
    global AGENT_ROSTER
    name = name.strip()
    if name not in AGENT_ROSTER:
        raise HTTPException(status_code=404, detail=f"'{name}' not found in roster")
    try:
        async with app.state.pool.acquire() as conn:
            await conn.execute("DELETE FROM texters WHERE name = $1", name)
            r1 = await conn.execute(
                "DELETE FROM trend_snapshots WHERE agent_name = $1", name
            )
            r2 = await conn.execute(
                "DELETE FROM account_assignments WHERE agent_name = $1", name
            )
        await _load_agent_roster_from_db()
        deleted_snapshots = int(r1.split()[-1]) if r1 else 0
        deleted_assignments = int(r2.split()[-1]) if r2 else 0
        logger.info(
            f"Roster: removed '{name}', wiped {deleted_snapshots} snapshots, "
            f"{deleted_assignments} assignments"
        )
        return {
            "status": "ok",
            "deleted_snapshots": deleted_snapshots,
            "deleted_assignments": deleted_assignments,
        }
    except Exception as exc:
        logger.exception(f"Error wiping data for '{name}'")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Rate Limiter Status endpoint ─────────────────────────────────────────────

@app.get("/api/rate-limit/status")
async def api_rate_limit_status():
    """
    Live snapshot of all active token buckets — dashboard routes + Groq keys.
    Polled every 5 s by the dashboard Rate Limiter widget.
    Returns fill %, tokens remaining, and all-time allowed/rejected counts.
    """
    from ai.analyzer import _rl as groq_rl

    # Dashboard route buckets
    combined = _dashboard_rl.status()

    # Merge Groq key buckets with a clear label prefix
    groq_status = groq_rl.status()
    for k, v in groq_status["buckets"].items():
        combined["buckets"][f"[groq] {k}"] = v

    combined["summary"]["groq_rejected"] = groq_status["summary"]["total_rejected_all_time"]

    return combined


# ── Entry point ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=5000, reload=True)
