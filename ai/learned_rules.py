"""
Learned Rules Manager — load, save, deduplicate, and inject dynamic correction rules.

Rules are stored in ai/learned_rules.json and are derived from human feedback via
the dream worker. At module load time, this file caches rules in memory (with mtime
invalidation) to avoid file I/O on every LLM prompt call.
"""
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path

from config.settings import LEARNED_RULES_PATH

logger = logging.getLogger(__name__)

# ── Module-level cache (mtime invalidation) ───────────────────────────────────
_cache_mtime: float = 0.0
_cache_rules: list[dict] = []


def load_rules() -> list[dict]:
    """
    Load active rules from learned_rules.json.
    Returns only rules where active=True. Returns [] if file doesn't exist (normal on first run).
    Never raises — logs warning on error and returns [].

    Uses module-level cache with mtime invalidation to avoid file I/O on every call.
    """
    global _cache_mtime, _cache_rules

    try:
        if not LEARNED_RULES_PATH.exists():
            return []

        # Fast path: if file hasn't changed, use cached list
        mtime = LEARNED_RULES_PATH.stat().st_mtime
        if mtime == _cache_mtime:
            return _cache_rules

        # File was updated — reload from disk
        data = json.loads(LEARNED_RULES_PATH.read_text(encoding="utf-8"))
        _cache_rules = [r for r in data.get("rules", []) if r.get("active", True)]
        _cache_mtime = mtime

        logger.debug(f"[LearnedRules] Loaded {len(_cache_rules)} active rules from {LEARNED_RULES_PATH}")
        return _cache_rules

    except Exception as e:
        logger.warning(f"[LearnedRules] Could not load rules: {e}")
        return []


def save_rules(rules: list[dict]) -> None:
    """
    Write the complete rules list to learned_rules.json.
    Performs atomic write via temp file + rename to avoid corruption on interruption.
    """
    try:
        LEARNED_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": 1,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "rules": rules,
        }

        # Atomic write: temp file + rename
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=LEARNED_RULES_PATH.parent,
            delete=False,
            suffix=".json",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name

        # Rename to target (atomic on POSIX, mostly on Windows)
        Path(tmp_path).replace(LEARNED_RULES_PATH)

        # Invalidate cache so next load() reads from disk
        global _cache_mtime, _cache_rules
        _cache_mtime = 0.0
        _cache_rules = []

        logger.info(f"[LearnedRules] Saved {len(rules)} rules to {LEARNED_RULES_PATH}")

    except Exception as e:
        logger.error(f"[LearnedRules] Could not save rules: {e}")


def append_rules(new_rules: list[dict]) -> int:
    """
    Merge new rules into the existing file. Deduplicates by case-insensitive rule_text
    (after stripping). Assigns sequential IDs (lr_001, lr_002, ...).
    Returns the count of rules actually added (0 if all were duplicates).
    """
    try:
        existing = load_rules()

        # Normalize existing rule_texts for dedup check
        existing_texts_lower = {r["rule_text"].strip().lower() for r in existing}

        added = 0
        for new_rule in new_rules:
            text_lower = new_rule.get("rule_text", "").strip().lower()

            # Skip if this rule_text already exists (case-insensitive)
            if text_lower in existing_texts_lower:
                logger.debug(f"[LearnedRules] Dedup: skipping rule (already exists)")
                continue

            # Assign ID: next sequence
            next_id_num = len(existing) + 1
            new_rule["id"] = f"lr_{next_id_num:03d}"
            new_rule["created_at"] = datetime.utcnow().isoformat() + "Z"
            new_rule["active"] = new_rule.get("active", True)
            new_rule["times_applied"] = new_rule.get("times_applied", 0)

            existing.append(new_rule)
            existing_texts_lower.add(text_lower)
            added += 1

        if added > 0:
            save_rules(existing)
            logger.info(f"[LearnedRules] Added {added} new rule(s)")
        else:
            logger.debug(f"[LearnedRules] append_rules: no new rules after dedup")

        return added

    except Exception as e:
        logger.error(f"[LearnedRules] append_rules failed: {e}")
        return 0


def inject_into_prompt(base_prompt: str) -> str:
    """
    Load active rules and append them to the base prompt as PART 14.
    If no rules exist, returns base_prompt unchanged (zero-cost path).

    This function is called before every LLM API call, so it's performance-critical.
    The module-level cache ensures we don't re-read the file on every call.
    """
    rules = load_rules()

    if not rules:
        return base_prompt

    # Build LEARNED_RULES block
    rules_block = "\n\n<LEARNED_RULES>\n"
    rules_block += "These rules were derived from real reviewer feedback and take precedence over all other logic.\n\n"

    for i, rule in enumerate(rules, 1):
        rule_id = rule.get("id", f"lr_{i:03d}")
        category = rule.get("category", "uncategorized")
        text = rule.get("rule_text", "")
        rules_block += f"RULE {rule_id} — {category}\n  {text}\n\n"

    rules_block += "</LEARNED_RULES>\n"

    logger.debug(f"[LearnedRules] Injecting {len(rules)} learned rule(s) into prompt")

    return base_prompt + rules_block


def get_rules_summary() -> dict:
    """
    Return a summary of loaded rules for dashboard status endpoint.
    """
    rules = load_rules()
    categories = list(set(r.get("category", "uncategorized") for r in rules))

    return {
        "total": len(rules),
        "active": len([r for r in rules if r.get("active", True)]),
        "categories": sorted(categories),
    }
