"""
Detached post-audit reflection runner.

Spawned (fully detached) by ai/scorer.py once an agent's scoring completes.
Running the dream-worker rule learning and semantic kNN rebuild here — instead
of inside the audit subprocess — lets that subprocess exit immediately, so the
dashboard shows the result without waiting minutes for the FAISS rebuild and
classifier retrain.

A lockfile dedups concurrent runs: when several agents finish near the same
time, only the first reflection actually runs; the rest exit fast.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s"
)
logger = logging.getLogger("post_audit_reflection")

LOCK_PATH = PROJECT_ROOT / ".reflection.lock"
LOCK_STALE_SECONDS = 1800  # 30 min — longer than any reflection run can take


def _acquire_lock() -> bool:
    """Exclusive lockfile. Returns False if another reflection is already running."""
    try:
        if LOCK_PATH.exists():
            age = time.time() - LOCK_PATH.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                logger.info(f"Reflection already running (lock age {age:.0f}s) — exiting.")
                return False
            logger.warning(f"Stale reflection lock ({age:.0f}s old) — overriding.")
            LOCK_PATH.unlink(missing_ok=True)
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        logger.info("Reflection lock taken by a concurrent process — exiting.")
        return False
    except Exception as e:
        logger.warning(f"Lock handling failed ({e}) — proceeding without lock.")
        return True


def _release_lock() -> None:
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception as e:
        logger.debug(f"Lock release failed (non-fatal): {e}")


def main() -> None:
    if not _acquire_lock():
        return
    try:
        logger.info("[Reflection] Post-audit reflection started (detached).")

        # ── Dream worker: learn rules from flag feedback ────────────────────
        try:
            from ai.dream_worker import should_run, run_dream_worker
            if should_run():
                logger.info("[Reflection] Dream worker threshold met — running.")
                run_dream_worker()
            else:
                logger.info("[Reflection] Dream worker threshold not met — skipping.")
        except Exception as e:
            logger.warning(f"[Reflection] dream worker failed (non-fatal): {e}")

        # ── Semantic auto-promote: grow the kNN index ───────────────────────
        try:
            from config.settings import SEMANTIC_LEARNING_ENABLED
            if SEMANTIC_LEARNING_ENABLED:
                from ai.prefilter.semantic_learner import auto_promote
                auto_promote()
        except Exception as e:
            logger.warning(f"[Reflection] semantic auto-promote failed (non-fatal): {e}")

        logger.info("[Reflection] Post-audit reflection complete.")
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
