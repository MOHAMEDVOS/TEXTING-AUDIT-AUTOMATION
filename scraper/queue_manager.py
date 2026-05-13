"""
Queue Manager - Orchestrates parallel browser workers across 65 accounts.
Uses asyncio semaphore to limit concurrent browser sessions.
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright

from config.settings import MAX_PARALLEL_WORKERS, MAX_RETRIES, DATABASE_URL, get_now
from scraper.browser_bot import SmarterContactBot

logger = logging.getLogger(__name__)


class QueueManager:
    """
    Manages a queue of SmarterContact accounts and processes them
    with a pool of parallel Playwright browser workers.

    Example:
        qm = QueueManager()
        results = await qm.run_all()
    """

    def __init__(self, max_workers: int = None, date_filter: str = "today", limit: int = 20,
                 date_start: str = None, date_end: str = None):
        self.max_workers = max_workers or MAX_PARALLEL_WORKERS
        self.date_filter = date_filter
        self.date_start = date_start
        self.date_end = date_end
        self.limit = limit
        self.agents = []
        self.results = []
        self.failed = []
        self.semaphore = asyncio.Semaphore(self.max_workers)

        # Progress tracking
        self.total = 0
        self.completed = 0
        self.errors = 0
        self.start_time = None

    def load_agents(self, filepath: Path = None) -> list:
        """
        Load agent credentials from the PostgreSQL accounts table.

        Returns list of dicts: [{"name": ..., "email": ..., "password": ...}, ...]
        """
        import psycopg2

        try:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("SELECT name, email, password FROM accounts ORDER BY name")
            rows = cur.fetchall()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Failed to load agents from database: {e}")
            return []

        self.agents = [
            {"name": row[0], "email": row[1], "password": row[2]}
            for row in rows
            if row[1] and row[2]  # skip agents with missing email or password
        ]

        logger.info(f"Loaded {len(self.agents)} agents from database")
        return self.agents

    def _create_template(self, filepath: Path):
        """Create a template agents.json file."""
        template = [
            {
                "name": "Agent 1",
                "email": "agent1@example.com",
                "password": "password123",
            },
            {
                "name": "Agent 2",
                "email": "agent2@example.com",
                "password": "password456",
            },
        ]
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(template, f, indent=2)
        logger.info(f"Template created at {filepath} — fill in your agent credentials")

    async def process_agent(self, agent: dict, worker_id: int) -> dict:
        """
        Process a single agent account:
        1. Acquire semaphore (limits concurrency)
        2. Launch browser
        3. Login → Extract → Logout
        4. Release semaphore

        Returns extraction result dict.
        """
        async with self.semaphore:
            bot = SmarterContactBot(
                agent_name=agent["name"],
                email=agent["email"],
                password=agent["password"],
                worker_id=worker_id,
                date_filter=self.date_filter,
                limit=self.limit,
                date_start=self.date_start,
                date_end=self.date_end,
            )

            result = {
                "agent_name": agent["name"],
                "email": agent["email"],
                "status": "pending",
                "started_at": get_now().isoformat(),
            }

            try:
                async with async_playwright() as playwright:
                    await bot.start_browser(playwright)
                    extraction = await bot.extract_all()
                    result.update(extraction)
                    await bot.close()

            except Exception as e:
                result["status"] = "error"
                result["errors"] = [str(e)]
                logger.error(f"[Worker-{worker_id}] Fatal error for {agent['name']}: {e}")

            finally:
                # Update progress
                self.completed += 1
                if result.get("status") != "success":
                    self.errors += 1
                    self.failed.append(agent["name"])

                self._print_progress()

            return result

    async def run_all(self) -> list:
        """
        Run extraction for all loaded agents using parallel workers.

        Returns list of all results.
        """
        if not self.agents:
            self.load_agents()

        if not self.agents:
            logger.error("No agents to process. Add credentials to the database first.")
            return []

        self.total = len(self.agents)
        self.completed = 0
        self.errors = 0
        self.failed = []
        self.start_time = time.time()
        self.results = []

        logger.info("=" * 60)
        logger.info(f"  STARTING EXTRACTION: {self.total} agents")
        logger.info(f"  Parallel workers: {self.max_workers}")
        logger.info(f"  Estimated time: ~{self._estimate_time()} minutes")
        logger.info("=" * 60)

        # Create tasks for all agents
        tasks = [
            self.process_agent(agent, i % self.max_workers)
            for i, agent in enumerate(self.agents)
        ]

        # Run all tasks concurrently (semaphore limits actual parallelism)
        self.results = await asyncio.gather(*tasks)

        # Print summary
        self._print_summary()

        return self.results

    async def run_single(self, agent_name: str) -> dict:
        """Run extraction for a single agent by name."""
        if not self.agents:
            self.load_agents()

        agent = next(
            (a for a in self.agents if a["name"].lower() == agent_name.lower()),
            None,
        )
        if not agent:
            logger.error(f"Agent '{agent_name}' not found in database")
            return {"error": f"Agent '{agent_name}' not found"}

        self.total = 1
        self.completed = 0
        self.start_time = time.time()

        result = await self.process_agent(agent, worker_id=0)
        return result

    def _estimate_time(self) -> int:
        """Estimate total processing time in minutes."""
        # ~30 seconds per agent, divided by parallel workers
        seconds_per_agent = 30
        total_seconds = (self.total * seconds_per_agent) / self.max_workers
        return max(1, int(total_seconds / 60))

    def _print_progress(self):
        """Print real-time progress update."""
        elapsed = time.time() - self.start_time
        remaining = self.total - self.completed
        rate = self.completed / max(elapsed, 1)
        eta_seconds = remaining / max(rate, 0.01)
        eta_min = int(eta_seconds / 60)

        success = self.completed - self.errors
        bar_len = 30
        filled = int(bar_len * self.completed / max(self.total, 1))
        bar = "█" * filled + "░" * (bar_len - filled)

        logger.info(
            f"  [{bar}] {self.completed}/{self.total} | "
            f"✓ {success} | ✗ {self.errors} | "
            f"ETA: {eta_min}m"
        )

    def _print_summary(self):
        """Print final extraction summary."""
        elapsed = time.time() - self.start_time
        success = self.completed - self.errors

        logger.info("")
        logger.info("=" * 60)
        logger.info("  EXTRACTION COMPLETE")
        logger.info("=" * 60)
        logger.info(f"  Total agents:    {self.total}")
        logger.info(f"  Successful:      {success}")
        logger.info(f"  Failed:          {self.errors}")
        logger.info(f"  Time elapsed:    {elapsed:.0f}s ({elapsed/60:.1f}m)")
        logger.info(f"  Avg per agent:   {elapsed/max(self.total,1):.1f}s")

        if self.failed:
            logger.warning(f"\n  Failed agents: {', '.join(self.failed)}")

        logger.info("=" * 60)

    def get_results_summary(self) -> dict:
        """Return a summary dict of the last run."""
        success = len([r for r in self.results if r.get("status") == "success"])
        failed = len([r for r in self.results if r.get("status") != "success"])

        return {
            "total": self.total,
            "success": success,
            "failed": failed,
            "failed_agents": self.failed,
            "timestamp": get_now().isoformat(),
        }
