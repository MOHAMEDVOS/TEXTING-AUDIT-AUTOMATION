"""
Dream Worker — Periodic reflection on flag feedback to generate correction rules.

After human reviewers mark AI flags as invalid, the dream worker:
1. Checks if enough time and new sessions have passed
2. Loads recent flag_feedback entries
3. Clusters them by category + semantic similarity
4. Calls Groq to generate new correction rules
5. Appends rules to ai/learned_rules.json

Intentionally synchronous (not async) so it can be called via run_in_executor()
from scorer.py without blocking the event loop.
"""
import json
import logging
import psycopg2
import psycopg2.extras
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import DATABASE_URL, DREAM_STATE_PATH, DREAM_WORKER_MIN_HOURS, DREAM_WORKER_MIN_SESSIONS
from ai.learned_rules import append_rules

logger = logging.getLogger(__name__)

# ── Reflection system prompt (used for Groq call only) ──────────────────────
REFLECTION_SYSTEM_PROMPT = """You are a meta-auditor reviewing patterns of incorrect AI audit flags.
Human reviewers consistently marked these flag patterns as wrong.
Your job is to extract concise correction rules from these patterns.

For each cluster of invalid flags, write one concise rule (≤ 3 sentences) that would prevent
this mistake in future audits. The rule should be actionable and specific.

Return ONLY valid JSON in this format:
{"rules": [{"rule_text": "...", "category": "...", "source_flags": [...]}, ...]}
"""


def should_run(db_path: str | None = None) -> bool:
    """
    Check if both conditions for running the dream worker are met:
    1. At least DREAM_WORKER_MIN_HOURS have passed since the last run
    2. At least DREAM_WORKER_MIN_SESSIONS new session_events exist since the last run

    Returns False (with no error) if conditions aren't met.
    Never raises — logs warnings on errors and returns False.
    """
    try:
        # Load dream_state.json to check last_run_at
        if not DREAM_STATE_PATH.exists():
            # First run — conditions are always met
            return True

        state = json.loads(DREAM_STATE_PATH.read_text(encoding="utf-8"))
        last_run_at = state.get("last_run_at")

        if not last_run_at:
            return True  # Never run before

        # Check hours elapsed
        last_run_dt = datetime.fromisoformat(last_run_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        hours_elapsed = (now_dt - last_run_dt).total_seconds() / 3600

        if hours_elapsed < DREAM_WORKER_MIN_HOURS:
            logger.debug(
                f"[DreamWorker] Not enough time elapsed: {hours_elapsed:.1f}h < {DREAM_WORKER_MIN_HOURS}h"
            )
            return False

        # Check new sessions
        dsn = db_path or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM session_events WHERE run_timestamp > %s",
                    (last_run_at,),
                )
                new_sessions = cur.fetchone()[0]

        if new_sessions < DREAM_WORKER_MIN_SESSIONS:
            logger.debug(
                f"[DreamWorker] Not enough new sessions: {new_sessions} < {DREAM_WORKER_MIN_SESSIONS}"
            )
            return False

        logger.info(
            f"[DreamWorker] Conditions met: {hours_elapsed:.1f}h elapsed, "
            f"{new_sessions} new session(s)"
        )
        return True

    except Exception as e:
        logger.warning(f"[DreamWorker] should_run check failed: {e}")
        return False


def run_dream_worker(db_path: str | None = None, dry_run: bool = False) -> dict:
    """
    Main entry point. Run the dream worker if conditions are met.

    Returns a summary dict:
    {
      "ran": bool,  # True if dream worker executed
      "new_rules": int,  # Number of rules appended
      "feedback_consumed": int,  # Number of flag_feedback entries processed
      "reason_skipped": str | None,  # Reason if not run
    }

    Never raises — all errors are logged and returned in the dict.
    """
    try:
        db_path = db_path or DATABASE_URL

        # Check if we should run
        if not should_run(db_path):
            return {
                "ran": False,
                "new_rules": 0,
                "feedback_consumed": 0,
                "reason_skipped": "Thresholds not met (hours or sessions)",
            }

        logger.info("[DreamWorker] ─ Starting reflection run")

        # Load last_run_at from dream_state
        last_run_at = "1970-01-01T00:00:00Z"  # Epoch default if first run
        if DREAM_STATE_PATH.exists():
            try:
                state = json.loads(DREAM_STATE_PATH.read_text(encoding="utf-8"))
                last_run_at = state.get("last_run_at", last_run_at)
            except Exception:
                pass

        # Load new feedback since last run
        feedback = _load_new_feedback(last_run_at, db_path)
        if not feedback:
            logger.info("[DreamWorker] No new flag_feedback entries to process")
            return {
                "ran": True,
                "new_rules": 0,
                "feedback_consumed": 0,
                "reason_skipped": "No new feedback",
            }

        logger.info(f"[DreamWorker] Loaded {len(feedback)} flag_feedback entries")

        # Cluster feedback
        clusters = _cluster_feedback(feedback)
        if not clusters:
            logger.info("[DreamWorker] No clusters formed from feedback")
            return {
                "ran": True,
                "new_rules": 0,
                "feedback_consumed": len(feedback),
                "reason_skipped": "No clusters",
            }

        logger.info(f"[DreamWorker] Formed {len(clusters)} cluster(s)")

        if dry_run:
            logger.info("[DreamWorker] DRY RUN — skipping Groq call")
            return {
                "ran": True,
                "new_rules": 0,
                "feedback_consumed": len(feedback),
                "reason_skipped": "Dry run mode",
            }

        # Call Groq to reflect on clusters
        try:
            rules = _call_groq_reflect(clusters)
        except Exception as e:
            logger.error(f"[DreamWorker] Groq reflection failed: {e}")
            return {
                "ran": True,
                "new_rules": 0,
                "feedback_consumed": len(feedback),
                "reason_skipped": f"Groq call failed: {e}",
            }

        if not rules:
            logger.info("[DreamWorker] No rules generated by Groq")
            return {
                "ran": True,
                "new_rules": 0,
                "feedback_consumed": len(feedback),
                "reason_skipped": "No rules generated",
            }

        logger.info(f"[DreamWorker] Groq generated {len(rules)} rule(s)")

        # Append to learned_rules.json
        added = append_rules(rules)

        # Update dream_state.json
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.isoformat().replace("+00:00", "Z")
        
        def _dt_to_iso(dt):
            if isinstance(dt, datetime):
                s = dt.isoformat()
                return s if "+" in s or s.endswith("Z") else s + "Z"
            return str(dt)
            
        max_dt = _dt_to_iso(max((f.get("created_at", "") for f in feedback), default=""))

        state = {
            "last_run_at": now_iso,
            "last_feedback_consumed_up_to": max_dt,
            "total_rules_generated": 0,
            "total_feedback_consumed": 0,
            "runs": [],
        }
        if DREAM_STATE_PATH.exists():
            try:
                state = json.loads(DREAM_STATE_PATH.read_text(encoding="utf-8"))
                state["last_run_at"] = now_iso
                state["last_feedback_consumed_up_to"] = max_dt
            except Exception:
                pass

        state.setdefault("runs", []).append({
            "ran_at": now_iso,
            "feedback_entries": len(feedback),
            "clusters": len(clusters),
            "new_rules": added,
        })
        state["total_rules_generated"] = state.get("total_rules_generated", 0) + added
        state["total_feedback_consumed"] = state.get("total_feedback_consumed", 0) + len(feedback)

        DREAM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        DREAM_STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")

        logger.info(f"[DreamWorker] ✓ Reflection complete: {added} new rule(s) added")

        return {
            "ran": True,
            "new_rules": added,
            "feedback_consumed": len(feedback),
            "reason_skipped": None,
        }

    except Exception as e:
        logger.error(f"[DreamWorker] Unexpected error in run_dream_worker: {e}")
        return {
            "ran": False,
            "new_rules": 0,
            "feedback_consumed": 0,
            "reason_skipped": str(e),
        }


def _load_new_feedback(since_iso: str, db_path: str) -> list[dict]:
    """
    Load all flag_feedback entries where created_at > since_iso.

    Returns list of dicts with keys: red_flag, reason, category, agent_name, contact_name, created_at
    """
    try:
        dsn = db_path or DATABASE_URL
        with psycopg2.connect(dsn) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT red_flag, reason, category, agent_name, contact_name, created_at
                    FROM flag_feedback
                    WHERE created_at > %s AND status = 'invalid'
                    ORDER BY created_at ASC
                    """,
                    (since_iso,),
                )
                rows = cur.fetchall()

        return [dict(row) for row in rows]

    except Exception as e:
        logger.error(f"[DreamWorker] Could not load feedback: {e}")
        return []


def _cluster_feedback(entries: list[dict]) -> list[dict]:
    """
    Cluster feedback entries by category (primary) and word overlap (secondary).

    Returns list of cluster dicts:
    {
      "category": str | None,
      "count": int,
      "representative_flag": str,
      "all_flags": list[str],
      "all_reasons": list[str],
      "confidence": "high" | "low"
    }
    """
    if not entries:
        return []

    clusters_by_category = {}

    # Primary clustering by category
    for entry in entries:
        cat = entry.get("category") or "uncategorized"
        if cat not in clusters_by_category:
            clusters_by_category[cat] = []
        clusters_by_category[cat].append(entry)

    # Convert to cluster dicts
    clusters = []
    for category, cluster_entries in clusters_by_category.items():
        flags = [e.get("red_flag", "") for e in cluster_entries if e.get("red_flag")]
        reasons = [e.get("reason", "") for e in cluster_entries if e.get("reason")]

        # Pick representative flag (longest or first)
        representative = max(flags, key=len) if flags else "Unknown flag"

        clusters.append({
            "category": category,
            "count": len(cluster_entries),
            "representative_flag": representative,
            "all_flags": flags,
            "all_reasons": reasons,
            "confidence": "high" if len(cluster_entries) >= 2 else "low",
        })

    return clusters


def _call_groq_reflect(clusters: list[dict]) -> list[dict]:
    """
    Call Groq with the reflection prompt to generate rules from clusters.

    Returns list of rule dicts with keys: rule_text, category, source_flags
    """
    try:
        from ai.analyzer import _pool

        # Get a Groq key from the pool
        groq_key = _pool._pick_groq_key()
        if groq_key is None:
            logger.error("[DreamWorker] No Groq keys available")
            return []

        # Build user message
        user_msg = _build_reflection_prompt(clusters)

        logger.debug(f"[DreamWorker] Calling Groq with {len(clusters)} cluster(s)")

        # Call Groq
        raw = groq_key.provider.generate(
            system_prompt=REFLECTION_SYSTEM_PROMPT,
            user_content=user_msg,
            max_tokens=600,
            temperature=0.2,  # Slightly creative but deterministic
        )

        # Parse response
        response = json.loads(raw)
        rules = response.get("rules", [])

        for rule in rules:
            rule["category"] = rule.get("category", "uncategorized")
            rule["source_flags"] = rule.get("source_flags", [])

        logger.info(f"[DreamWorker] Groq generated {len(rules)} rule(s)")
        _pool.mark_success(groq_key)
        return rules

    except json.JSONDecodeError as e:
        logger.error(f"[DreamWorker] JSON parse error from Groq: {e}")
        return []
    except Exception as e:
        logger.error(f"[DreamWorker] Groq call failed: {e}")
        return []


def _build_reflection_prompt(clusters: list[dict]) -> str:
    """Build the user message for the Groq reflection call."""
    lines = [
        "Human reviewers have marked the following AI-generated audit flags as INCORRECT.\n"
        "These are patterns of mistakes the AI system repeatedly makes.\n\n"
        "For each cluster, analyze the pattern and write ONE concise rule "
        "(≤3 sentences) to prevent this mistake in future audits.\n\n"
    ]

    for i, cluster in enumerate(clusters, 1):
        lines.append(f"─── CLUSTER {i}: {cluster['category']} ───")
        lines.append(f"Confidence: {cluster['confidence']}")
        lines.append(f"Count: {cluster['count']} flagged conversation(s)\n")
        lines.append("Example flags marked as WRONG:")
        for flag in cluster["all_flags"][:3]:  # Show first 3
            lines.append(f"  • {flag}")
        if len(cluster["all_flags"]) > 3:
            lines.append(f"  ... and {len(cluster['all_flags']) - 3} more\n")

        if cluster["all_reasons"]:
            lines.append("\nReviewer feedback on why these are wrong:")
            for reason in cluster["all_reasons"][:2]:  # Show first 2
                lines.append(f"  • {reason}")
            if len(cluster["all_reasons"]) > 2:
                lines.append(f"  ... and {len(cluster['all_reasons']) - 2} more")
        lines.append("\n")

    lines.append(
        "\nReturn ONLY valid JSON:\n"
        '{"rules": [{"rule_text": "...", "category": "...", "source_flags": [...]}, ...]}'
    )

    return "\n".join(lines)


if __name__ == "__main__":
    # CLI entry point: python -m ai.dream_worker
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
    )
    result = run_dream_worker()
    print(f"\n[Dream Worker Result]")
    print(f"  Ran: {result['ran']}")
    print(f"  New Rules: {result['new_rules']}")
    print(f"  Feedback Consumed: {result['feedback_consumed']}")
    if result['reason_skipped']:
        print(f"  Skipped: {result['reason_skipped']}")
    sys.exit(0 if result['ran'] else 1)
