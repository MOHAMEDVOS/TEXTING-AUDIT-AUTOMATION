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
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Set console window title on Windows
if sys.platform == "win32":
    title = os.environ.get("PYTITLE", "TEXTING Scraper")
    os.system(f"title {title}")

from config.settings import LOG_DIR, LOG_LEVEL, DATE_FILTER, DEFAULT_SAMPLE_SIZE
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
            LOG_DIR / f"audit_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)



async def run_single_agent(agent_name: str, date_filter: str = "today", limit: int = 20):
    """Run extraction for a single agent."""
    logger.info(f"Running single extraction for: {agent_name}")

    db = Database()
    await db.initialize()
    try:
        qm = QueueManager(date_filter=date_filter, limit=limit)
        qm.load_agents()

        result = await qm.run_single(agent_name)

        if result.get("status") == "success":
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

            await score_agent_conversations(
                agent_id=agent_id,
                agent_name=result["agent_name"],
                conversations=result.get("_all_conversations") or result.get("conversations", []),
                unread_count=result.get("unread_count", 0),
                pool=db.pool,
                pinned_key=pinned_key,
            )
        else:
            logger.error(f"Extraction failed for {agent_name}")

        return result
    finally:
        cleaned = await db.cleanup_failed_audits()
        if cleaned:
            logger.info(f"[Cleanup] Removed {cleaned} failed conversation(s) — will retry next run")
        await db.close()


async def run_test(date_filter: str = "today", limit: int = 20):
    """Test extraction with the first agent only."""
    logger.info("=" * 60)
    logger.info("  TEST MODE - Processing first agent only")
    logger.info("=" * 60)

    db = Database()
    await db.initialize()
    try:
        qm = QueueManager(max_workers=1, date_filter=date_filter, limit=limit)
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
                    await score_agent_conversations(
                        agent_id=agent_id,
                        agent_name=result["agent_name"],
                        conversations=result.get("_all_conversations") or result.get("conversations", []),
                        unread_count=result.get("unread_count", 0),
                        pool=db.pool,
                    )

        return results
    finally:
        cleaned = await db.cleanup_failed_audits()
        if cleaned:
            logger.info(f"[Cleanup] Removed {cleaned} failed conversation(s) — will retry next run")
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


async def run_selected_agents(names: list[str], date_filter: str = "today", limit: int = 20):
    """Run extraction for a specific list of agents sequentially."""
    logger.info("=" * 60)
    logger.info(f"  SELECTED RUN — {len(names)} agents")
    logger.info("=" * 60)
    for name in names:
        await run_single_agent(name, date_filter=date_filter, limit=limit)


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

    args = parser.parse_args()

    # Resolve defaults from settings
    date_filter = args.date_filter or DATE_FILTER
    limit = args.limit or DEFAULT_SAMPLE_SIZE

    if args.status:
        asyncio.run(show_status())
    elif args.test:
        asyncio.run(run_test(date_filter=date_filter, limit=limit))
    elif args.single:
        asyncio.run(run_single_agent(args.single, date_filter=date_filter, limit=limit))
    elif args.agents:
        names = [n.strip() for n in args.agents.split(",") if n.strip()]
        asyncio.run(run_selected_agents(names, date_filter=date_filter, limit=limit))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
