"""
SmarterContact Audit Automation - Main Entry Point

Usage:
    python main.py --single "Agent 1" # Run for a single agent
    python main.py --agents "Noah,Charles" # Run for selected agents
    python main.py --test             # Test with first agent only
    python main.py --status           # Show last run status
"""
import asyncio
import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Set console window title on Windows
if sys.platform == "win32":
    title = os.environ.get("PYTITLE", "TEXTING Scraper")
    os.system(f"title {title}")

    # Silence the noisy "RuntimeError: Event loop is closed" raised by
    # asyncio's _ProactorBasePipeTransport.__del__ during interpreter shutdown.
    # This is a well-known cosmetic bug — the loop has already finished its
    # work; the GC just runs after asyncio.run() closed the loop. Suppressing
    # it stops one finishing subprocess from spamming errors that look like
    # a crash to the dashboard log tail.
    from asyncio.proactor_events import _ProactorBasePipeTransport  # type: ignore

    _orig_del = _ProactorBasePipeTransport.__del__

    def _silent_del(self, *args, **kwargs):
        try:
            _orig_del(self, *args, **kwargs)
        except (RuntimeError, AttributeError):
            pass

    _ProactorBasePipeTransport.__del__ = _silent_del  # type: ignore[assignment]

from config.settings import LOG_DIR, LOG_LEVEL, DATE_FILTER, DEFAULT_SAMPLE_SIZE, get_now

from scraper.queue_manager import QueueManager
from database.db import Database
from ai.scorer import score_agent_conversations

# ─── Logging Setup ──────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)),
        logging.FileHandler(
            LOG_DIR / f"audit_{get_now().strftime('%Y%m%d_%H%M%S_%f')}_{os.getpid()}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)


RUN_STATUS_FILE = os.environ.get("AUDIT_STATUS_FILE")


def _write_run_status(
    agent_name: str,
    state: str,
    stage: str,
    message: str,
    code: str | None = None,
    errors: list | None = None,
) -> None:
    """Write a small status handoff file for the dashboard process."""
    if not RUN_STATUS_FILE:
        return

    try:
        path = Path(RUN_STATUS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent": agent_name,
            "state": state,
            "stage": stage,
            "code": code,
            "message": message,
            "errors": errors or [],
            "updated_at": get_now().isoformat(),
        }
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(path)
    except Exception:
        logger.debug("Failed to write audit status file", exc_info=True)


def _status_message(result: dict) -> tuple[str, str]:
    """Return a stable failure code and user-facing message for an extraction result."""
    status = result.get("status") or "error"
    errors = result.get("errors") or []
    first_error = str(errors[0]) if errors else ""

    messages = {
        "login_failed": "Failed logging in",
        "no_conversations": "No convos available",
        "account_not_found": "Account not found",
        "error": "Audit failed",
    }

    message = messages.get(status, f"Failed at {status.replace('_', ' ')}")
    if first_error and status not in {"login_failed", "no_conversations"}:
        message = f"{message}: {first_error}"
    return status, message



async def run_single_agent(agent_name: str, date_filter: str = "today", limit: int = 20,
                           date_start: str = None, date_end: str = None):
    """Run extraction for a single agent."""
    logger.info(f"Running single extraction for: {agent_name}")
    _write_run_status(agent_name, "running", "starting", "Starting audit")

    db = Database()
    current_stage = "database"
    agent_id: int | None = None
    try:
        _write_run_status(agent_name, "running", "database", "Connecting to database")
        await db.initialize()
        _write_run_status(agent_name, "running", "loading_account", "Loading account")
        current_stage = "loading_account"
        qm = QueueManager(date_filter=date_filter, limit=limit,
                          date_start=date_start, date_end=date_end)
        qm.load_agents()

        _write_run_status(agent_name, "running", "extracting_conversations", "Extracting conversations")
        current_stage = "scraping"
        result = await qm.run_single(agent_name)
        if result.get("error") and not result.get("status"):
            result["status"] = "account_not_found"
            result["errors"] = [result["error"]]

        if result.get("status") == "success":
            conversations = result.get("_all_conversations") or result.get("conversations", [])
            if not conversations:
                result["status"] = "no_conversations"
                result.setdefault("errors", []).append("No conversations matched the selected date/sample filter")
                code, message = _status_message(result)
                logger.warning(f"{message} for {agent_name}")
                _write_run_status(agent_name, "failed", "extracting_conversations", message, code, result.get("errors"))
                return result

            _write_run_status(agent_name, "running", "saving_results", "Saving extracted conversations")
            current_stage = "saving_results"
            await db.save_results([result])
            logger.info(f"Extraction complete for {agent_name}")
            agent_id = await db.upsert_agent(result["agent_name"], result["email"])

            # Pick up the pinned Groq key assigned by the dashboard for this run
            pinned_key = None
            pinned_key_value = os.environ.get("GROQ_PINNED_KEY")
            if pinned_key_value:
                from ai.analyzer import _pool, PooledKey
                _pool.ensure_loaded()
                with _pool._lock:
                    pinned_key = next(
                        (pk for pk in _pool._groq_pool if pk.key == pinned_key_value), None
                    )

            _write_run_status(agent_name, "running", "scoring", "Scoring conversations")
            current_stage = "scoring"
            await score_agent_conversations(
                agent_id=agent_id,
                agent_name=result["agent_name"],
                conversations=result.get("_all_conversations") or result.get("conversations", []),
                unread_count=result.get("unread_count", 0),
                pool=db.pool,
                pinned_key=pinned_key,
            )
            _write_run_status(agent_name, "done", "completed", "Done")
        else:
            code, message = _status_message(result)
            logger.error(f"Extraction failed for {agent_name}: {message}")
            _write_run_status(agent_name, "failed", current_stage, message, code, result.get("errors"))

        return result
    except Exception as exc:
        logger.exception(f"Audit failed for {agent_name} during {current_stage}")
        _write_run_status(
            agent_name,
            "failed",
            current_stage,
            f"Failed at {current_stage.replace('_', ' ')}: {exc}",
            "exception",
            [str(exc)],
        )
        return {"agent_name": agent_name, "status": "error", "errors": [str(exc)]}
    finally:
        if db.pool:
            cleaned = await db.cleanup_failed_audits(agent_id=agent_id)
            if cleaned:
                logger.info(f"[Cleanup] Removed {cleaned} failed conversation(s) for agent_id={agent_id} — will retry next run")
            await db.close()


async def run_test(date_filter: str = "today", limit: int = 20,
                   date_start: str = None, date_end: str = None):
    """Test extraction with the first agent only."""
    logger.info("=" * 60)
    logger.info("  TEST MODE - Processing first agent only")
    logger.info("=" * 60)

    db = Database()
    await db.initialize()
    last_agent_id: int | None = None
    try:
        qm = QueueManager(max_workers=1, date_filter=date_filter, limit=limit,
                          date_start=date_start, date_end=date_end)
        agents = qm.load_agents()

        if not agents:
            logger.error("No agents found. Add credentials to the database")
            return

        # Only process the first agent
        qm.agents = [agents[0]]
        results = await qm.run_all()

        if results:
            await db.save_results(results)
            for result in results:
                if result.get("status") == "success":
                    agent_id = await db.upsert_agent(result["agent_name"], result["email"])
                    last_agent_id = agent_id
                    await score_agent_conversations(
                        agent_id=agent_id,
                        agent_name=result["agent_name"],
                        conversations=result.get("_all_conversations") or result.get("conversations", []),
                        unread_count=result.get("unread_count", 0),
                        pool=db.pool,
                    )

        return results
    finally:
        cleaned = await db.cleanup_failed_audits(agent_id=last_agent_id)
        if cleaned:
            logger.info(f"[Cleanup] Removed {cleaned} failed conversation(s) for agent_id={last_agent_id} — will retry next run")
        await db.close()


async def show_status():
    """Show summary of the last extraction run."""
    db = Database()

    try:
        agents = await db.get_all_agents()
        if not agents:
            logger.info("No data yet. Run an extraction first.")
            return

        logger.info("=" * 60)
        logger.info("  LAST RUN STATUS")
        logger.info("=" * 60)
        logger.info(f"  Registered agents: {len(agents)}")

        for agent in agents:
            latest = await db.get_latest_extraction(agent["email"])
            status = latest.get("status", "no data")
            date = latest.get("extracted_at", "never")
            logger.info(f"  {agent['name']:20s} | {status:12s} | Last: {date}")

    except Exception as e:
        logger.error(f"Error getting status: {e}")


async def run_selected_agents(names: list[str], date_filter: str = "today", limit: int = 20,
                              date_start: str = None, date_end: str = None):
    """Run extraction for a specific list of agents sequentially."""
    logger.info("=" * 60)
    logger.info(f"  SELECTED RUN — {len(names)} agents")
    logger.info("=" * 60)
    for name in names:
        await run_single_agent(name, date_filter=date_filter, limit=limit,
                               date_start=date_start, date_end=date_end)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="SmarterContact Audit Automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --single "John D"              Run for one agent
  python main.py --agents "Noah,Charles"        Run for selected agents
  python main.py --test                         Test with first agent
  python main.py --status                       Show last run status
        """,
    )
    parser.add_argument(
        "--single",
        type=str,
        help="Run extraction for a single agent by name",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test mode: process first agent only",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show status of last extraction run",
    )
    parser.add_argument(
        "--agents",
        type=str,
        default=None,
        help="Comma-separated list of agent names to run (e.g. 'Noah,Charles')",
    )
    parser.add_argument(
        "--date-filter",
        type=str,
        default=None,
        help="Date filter for inbox: today, last_week, this_month, last_month, last_30_days, last_year (default: today)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Number of conversation samples to extract per agent (default: 20)",
    )
    parser.add_argument(
        "--date-start",
        type=str,
        default=None,
        help="Custom date range start (YYYY-MM-DD). Requires --date-end.",
    )
    parser.add_argument(
        "--date-end",
        type=str,
        default=None,
        help="Custom date range end (YYYY-MM-DD). Requires --date-start.",
    )

    args = parser.parse_args()

    # Resolve defaults from settings
    date_filter = args.date_filter or DATE_FILTER
    limit = args.limit or DEFAULT_SAMPLE_SIZE

    # Custom date range overrides preset filter
    date_start = getattr(args, 'date_start', None)
    date_end = getattr(args, 'date_end', None)
    if date_start and date_end:
        date_filter = "custom"

    if args.status:
        asyncio.run(show_status())
    elif args.test:
        asyncio.run(run_test(date_filter=date_filter, limit=limit,
                             date_start=date_start, date_end=date_end))
    elif args.single:
        result = asyncio.run(run_single_agent(args.single, date_filter=date_filter, limit=limit,
                                              date_start=date_start, date_end=date_end))
        if not result or result.get("status") != "success":
            sys.exit(1)
    elif args.agents:
        names = [n.strip() for n in args.agents.split(",") if n.strip()]
        asyncio.run(run_selected_agents(names, date_filter=date_filter, limit=limit,
                                        date_start=date_start, date_end=date_end))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
