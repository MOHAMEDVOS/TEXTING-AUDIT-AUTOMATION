"""
Interactive tier promotion helper for the ML pre-filter.

Reads the current .env prefilter state, runs the eval harness,
checks gates for the next unpromoted tier, and offers to update .env.

Usage:
    python scripts/promote_prefilter.py
    python scripts/promote_prefilter.py --dry-run       # check only, don't modify .env
    python scripts/promote_prefilter.py --limit 500     # eval sample size
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import settings

ENV_PATH = PROJECT_ROOT / ".env"


def _read_env_flag(key: str) -> bool:
    """Read a boolean flag from .env file text."""
    text = ENV_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)}\s*=\s*(.+)$", text, re.MULTILINE)
    if not match:
        return False
    return match.group(1).strip().lower() == "true"


def _set_env_flag(key: str, value: str) -> None:
    """Update a key=value line in .env. Adds it if missing."""
    text = ENV_PATH.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}\s*=.*$"
    replacement = f"{key}={value}"
    if re.search(pattern, text, re.MULTILINE):
        text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
    else:
        text = text.rstrip() + f"\n{replacement}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def _detect_next_promotion() -> str | None:
    """
    Returns the next promotion action needed, or None if fully promoted.

    Promotion order:
      1. PREFILTER_SHADOW_MODE=false  (enable live mode)
      2. PREFILTER_T2_LIVE=true
      3. PREFILTER_T3_LIVE=true
    """
    shadow = _read_env_flag("PREFILTER_SHADOW_MODE")
    t2_live = _read_env_flag("PREFILTER_T2_LIVE")
    t3_live = _read_env_flag("PREFILTER_T3_LIVE")

    if shadow:
        return "shadow_off"  # First promotion: exit shadow mode (T1 goes live)
    if not t2_live:
        return "t2_live"
    if not t3_live:
        return "t3_live"
    return None  # All tiers promoted


def _describe_promotion(action: str) -> str:
    return {
        "shadow_off": "Exit shadow mode → Tier 1 goes LIVE (PREFILTER_SHADOW_MODE=false)",
        "t2_live": "Promote Tier 2 → LIVE (PREFILTER_T2_LIVE=true)",
        "t3_live": "Promote Tier 3 → LIVE (PREFILTER_T3_LIVE=true)",
    }[action]


def _apply_promotion(action: str) -> None:
    if action == "shadow_off":
        _set_env_flag("PREFILTER_SHADOW_MODE", "false")
    elif action == "t2_live":
        _set_env_flag("PREFILTER_T2_LIVE", "true")
    elif action == "t3_live":
        _set_env_flag("PREFILTER_T3_LIVE", "true")


def main():
    p = argparse.ArgumentParser(description="Promote the next prefilter tier.")
    p.add_argument("--dry-run", action="store_true",
                   help="Check gates only — don't modify .env.")
    p.add_argument("--limit", type=int, default=500,
                   help="Number of conversations to replay for eval.")
    p.add_argument("--since", default=None,
                   help="ISO date — only eval convos scored on/after this date.")
    args = p.parse_args()

    if not ENV_PATH.exists():
        print(f"ERROR: .env not found at {ENV_PATH}")
        sys.exit(1)

    # ── Detect what needs promoting ───────────────────────────────────
    action = _detect_next_promotion()
    if action is None:
        print("All tiers are already promoted to LIVE. Nothing to do.")
        print("  PREFILTER_SHADOW_MODE=false")
        print("  PREFILTER_T1_LIVE=true")
        print("  PREFILTER_T2_LIVE=true")
        print("  PREFILTER_T3_LIVE=true")
        sys.exit(0)

    print(f"\nNext promotion: {_describe_promotion(action)}")
    print(f"Running eval with --limit {args.limit}...\n")

    # ── Run eval ──────────────────────────────────────────────────────
    from scripts.eval_prefilter import evaluate
    gates_passed = evaluate(limit=args.limit, since=args.since)

    if not gates_passed:
        print("\n❌  Gates FAILED — promotion blocked.")
        print("    Tune thresholds or retrain before trying again.")
        print("    See docs/prefilter-runbook.md for guidance.")
        sys.exit(1)

    print(f"\n✅  Gates PASSED for: {_describe_promotion(action)}")

    if args.dry_run:
        print("    (dry-run mode — .env not modified)")
        sys.exit(0)

    # ── Apply promotion ───────────────────────────────────────────────
    _apply_promotion(action)
    print(f"    .env updated: {_describe_promotion(action)}")
    print("    Restart the dashboard/main process to pick up the change.")


if __name__ == "__main__":
    main()
