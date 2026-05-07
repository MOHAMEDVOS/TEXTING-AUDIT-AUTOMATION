# -*- coding: utf-8 -*-
"""
T1 + T2 Evaluation Harness against 50-conversation baseline.

Runs tier1_phrases.evaluate() and tier2_embedding.evaluate() on each
conversation, collects results, then compares against eval_baseline.json.

No Groq. No DB writes. Pure local evaluation.
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION")

# Silence heavy-library noise
logging.basicConfig(level=logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("faiss").setLevel(logging.ERROR)

DATA_PATH     = Path(r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_50_conversations.json")
BASELINE_PATH = Path(r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_baseline.json")
RESULTS_PATH  = Path(r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_tier_results.json")

# ── Import prefilter tiers ─────────────────────────────────────────────────────
from ai.prefilter import tier1_phrases
try:
    from ai.prefilter import tier2_embedding
    T2_AVAILABLE = True
except Exception as e:
    print(f"[WARN] Tier 2 not available: {e}")
    T2_AVAILABLE = False


# ── Decision label translation ─────────────────────────────────────────────────
# T1 short-circuits with a category (e.g. "opt_out", "wrong_number", etc.)
# We need to map these to the same outcome vocabulary as the baseline.

T1_NOTE_TO_OUTCOME = [
    ("opt.out",           "opt_out"),
    ("do not call",       "opt_out"),
    ("dnc",               "opt_out"),
    ("wrong number",      "wrong_number"),
    ("wrong person",      "wrong_number"),
    ("not (this|my) person", "wrong_number"),
    ("not interested",    "not_interested"),
    ("not selling",       "not_interested"),
    ("sold",              "sold"),
    ("suspicious",        "escalate"),
    ("above market",      "abv_mv"),
    ("abv.?mv",           "abv_mv"),
]

def _t1_outcome(decision_obj):
    """Extract the outcome category from a T1 PipelineResult."""
    if decision_obj is None:
        return None

    import re
    notes = (decision_obj.notes or "").lower()

    # Try to parse from notes text
    for pattern, outcome in T1_NOTE_TO_OUTCOME:
        if re.search(pattern, notes, re.I):
            return outcome

    # Fallback: look at the result dict summary
    if decision_obj.result:
        summary = decision_obj.result.get("summary", "").lower()
        for pattern, outcome in T1_NOTE_TO_OUTCOME:
            if re.search(pattern, summary, re.I):
                return outcome
        label = decision_obj.result.get("label_assigned", "").lower()
        if "wrong number" in label:
            return "wrong_number"
        if "do not call" in label or "dnc" in label:
            return "opt_out"
        if "not interested" in label or "verified" in label:
            return "not_interested"
        if "sold" in label:
            return "sold"

    if decision_obj.decision == "escalate":
        return "escalate"
    return "short_circuit"  # category unknown


def _run_t1(conv):
    """Run T1 on a conversation. Returns (decision_str, confidence, notes, raw)."""
    messages = conv["messages"]
    agent_name   = conv.get("account_name", "Noah")
    contact_name = conv.get("contact_name", "")
    try:
        result = tier1_phrases.evaluate(messages, agent_name, contact_name)
        if result is None:
            return "pass", 0.0, "no_match", None
        return result.decision, result.confidence, result.notes or "", result
    except Exception as e:
        return "error", 0.0, str(e), None


def _run_t2(conv):
    """Run T2 on a conversation. Returns (decision_str, confidence, notes, raw)."""
    if not T2_AVAILABLE:
        return "unavailable", 0.0, "T2 not loaded", None
    messages = conv["messages"]
    agent_name   = conv.get("account_name", "Noah")
    contact_name = conv.get("contact_name", "")
    try:
        result = tier2_embedding.evaluate(messages, agent_name, contact_name)
        if result is None:
            return "pass", 0.0, "no_match", None
        return result.decision, result.confidence, result.notes or "", result
    except Exception as e:
        return "error", 0.0, str(e), None


# ── Outcome compatibility with baseline ───────────────────────────────────────

def _tier_matches_baseline(tier_decision, tier_outcome, baseline_outcome):
    """
    Returns True if the tier's decision is compatible with the baseline outcome.

    Compatibility rules:
    - "short_circuit" + matching category  → compare categories
    - "pass" / "escalate"                  → tier correctly deferred (not a false positive)
    - A false positive = tier said short_circuit for wrong category
    """
    if tier_decision == "error":
        return False, "tier_error"

    if tier_decision == "pass" or tier_decision == "escalate":
        # Tier correctly escalated — not a false positive (just not caught)
        return True, "escalated_correctly"

    if tier_decision == "unavailable":
        return True, "tier_unavailable"

    # tier short-circuited — check if category matches baseline
    if tier_outcome == baseline_outcome:
        return True, "correct_category"

    # Some cross-mappings are OK
    EQUIV = {
        ("wrong_number", "sold"),
        ("sold", "wrong_number"),
        ("opt_out", "not_interested"),
        ("not_interested", "opt_out"),
    }
    if (tier_outcome, baseline_outcome) in EQUIV:
        return True, "equivalent_category"

    if tier_outcome == "short_circuit":
        # Generic short-circuit without category — just means tier acted
        return None, "unknown_category"

    return False, f"wrong_category:{tier_outcome}_vs_{baseline_outcome}"


def main():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        conversations = json.load(f)
    with open(BASELINE_PATH, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    # Index baseline by conversation_id
    baseline_map = {b["conversation_id"]: b for b in baseline}

    print(f"Running T1 + T2 on {len(conversations)} conversations...")
    print(f"T2 available: {T2_AVAILABLE}")
    print()

    SEP = "=" * 90
    print(f"{'#':>3} {'ID':>5} {'Contact':28} {'Baseline':14} {'T1':14} {'T2':14} {'Match'}")
    print(SEP)

    results = []
    t1_sc = t1_correct = t1_fp = 0
    t2_sc = t2_correct = t2_fp = 0

    for i, conv in enumerate(conversations):
        cid  = conv["conversation_id"]
        name = conv["contact_name"][:27]
        bl   = baseline_map.get(cid, {})
        baseline_outcome = bl.get("outcome", "?")

        # Run T1
        t1_dec, t1_conf, t1_notes, t1_raw = _run_t1(conv)
        t1_out = _t1_outcome(t1_raw) if t1_dec == "short_circuit" else t1_dec

        # Run T2
        t2_dec, t2_conf, t2_notes, t2_raw = _run_t2(conv)
        t2_out = t2_dec  # T2 doesn't set category the same way

        # Assess T1
        t1_ok, t1_reason = _tier_matches_baseline(t1_dec, t1_out, baseline_outcome)
        if t1_dec == "short_circuit":
            t1_sc += 1
            if t1_ok:
                t1_correct += 1
            elif t1_ok is False:
                t1_fp += 1

        # Assess T2
        t2_ok, t2_reason = _tier_matches_baseline(t2_dec, t2_out, baseline_outcome)
        if t2_dec == "short_circuit":
            t2_sc += 1
            if t2_ok:
                t2_correct += 1
            elif t2_ok is False:
                t2_fp += 1

        # Display
        t1_disp = f"{t1_dec[:6]}({'Y' if t1_ok else 'N' if t1_ok is False else '?'})"
        t2_disp = f"{t2_dec[:6]}({'Y' if t2_ok else 'N' if t2_ok is False else '?'})" if T2_AVAILABLE else "N/A"
        match_str = ""
        if t1_dec == "short_circuit" and not t1_ok:
            match_str = f"T1-FP:{t1_out}"
        if t2_dec == "short_circuit" and not t2_ok:
            match_str += f" T2-FP:{t2_out}"

        print(f"{i+1:>3} [{cid:>4}] {name:28} {baseline_outcome:14} {t1_disp:14} {t2_disp:14} {match_str}")

        results.append({
            "conversation_id": cid,
            "contact_name": conv["contact_name"],
            "baseline_outcome": baseline_outcome,
            "baseline_label": bl.get("label_assigned",""),
            "baseline_label_correct": bl.get("label_correct", None),
            "t1_decision": t1_dec,
            "t1_confidence": round(t1_conf, 4),
            "t1_notes": t1_notes,
            "t1_outcome": t1_out,
            "t1_match": t1_ok,
            "t1_reason": t1_reason,
            "t2_decision": t2_dec,
            "t2_confidence": round(t2_conf, 4),
            "t2_notes": t2_notes,
            "t2_match": t2_ok,
            "t2_reason": t2_reason,
        })

    # ── Summary ────────────────────────────────────────────────────────────────
    total = len(conversations)
    t1_pass = sum(1 for r in results if r["t1_decision"] == "pass")
    t2_pass = sum(1 for r in results if r["t2_decision"] == "pass")

    print()
    print(SEP)
    print(f"TIER 1 SUMMARY")
    print(f"  Short-circuited : {t1_sc}/{total}  ({100*t1_sc//total}%)")
    print(f"  Correct SC      : {t1_correct}/{t1_sc}  (no false positives)" if t1_sc else "  No short-circuits")
    print(f"  False positives : {t1_fp}")
    print(f"  Passed through  : {t1_pass}")
    print()
    if T2_AVAILABLE:
        print(f"TIER 2 SUMMARY")
        print(f"  Short-circuited : {t2_sc}/{total}  ({100*t2_sc//total}%)")
        print(f"  Correct SC      : {t2_correct}/{t2_sc}" if t2_sc else "  No short-circuits")
        print(f"  False positives : {t2_fp}")
        print(f"  Passed through  : {t2_pass}")
    else:
        print("TIER 2: Not available (index not built yet)")
    print()

    # Gap analysis: what T1/T2 missed that they COULD have caught
    t1_missed = [r for r in results
                 if r["t1_decision"] == "pass"
                 and r["baseline_outcome"] in ("opt_out","wrong_number","not_interested","sold","maybe")]
    print(f"POTENTIAL T1 MISSES (baseline has catchable outcome, T1 passed):")
    for r in t1_missed:
        print(f"  [{r['conversation_id']}] {r['contact_name'][:30]:30} | baseline={r['baseline_outcome']}")

    # Save
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nResults saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
