"""
Prefilter unit tests — 20 scenarios covering all Check paths.
Run: python -m scripts.test_prefilter
"""
import sys, os
os.environ["PREFILTER_ENABLED"] = "true"
os.environ["PREFILTER_SHADOW_MODE"] = "false"
os.environ["PREFILTER_T1_LIVE"] = "true"

from ai.prefilter.tier1_phrases import evaluate as t1_evaluate

PASS = 0
FAIL = 0

def check(name, messages, expect_decision, expect_label=None):
    global PASS, FAIL
    result = t1_evaluate(messages, "TestAgent", "TestContact")
    decision = result.decision if result else "no_result"
    label = (result.result or {}).get("label_assigned", "") if result else ""
    label_lower = (label or "").lower().strip()
    expect_label_lower = (expect_label or "").lower().strip() if expect_label else None

    # no_result = T1 returned None = fall-through to Groq = effective escalation
    effective_decision = decision if decision != "no_result" else "escalate"

    ok = effective_decision == expect_decision
    if ok and expect_label_lower:
        ok = label_lower == expect_label_lower

    status = "[PASS]" if ok else "[FAIL]"
    if not ok:
        FAIL += 1
        print(f"  {status} {name}: got decision={effective_decision} label='{label}', expected decision={expect_decision} label='{expect_label}'")
    else:
        PASS += 1
        print(f"  {status} {name}")


def msg(sender, body):
    return {"sender": sender, "body": body}


print("=" * 80)
print("  PREFILTER UNIT TESTS")
print("=" * 80)

# ── Check 3: Silent contact (drip) ──────────────────────────────────
print("\n-- Check 3: Silent contact (drip campaigns) --")
check("Silent contact, 1 agent msg", [
    msg("agent", "Hi, interested in selling your property?"),
], "short_circuit")

check("Silent contact, 6 agent msgs (drip)", [
    msg("agent", "Hi, interested in selling your property?"),
    msg("agent", "Hey, it's Jack with LHB. If 123 Main St made sense in a cash range..."),
    msg("agent", "Hi, just checking if you'd consider something like 105k-140k?"),
    msg("agent", "Hey, it's Jack. I'm reaching out about 123 Main St."),
    msg("agent", "Hi, I came across 123 Main St and wondered if a cash price..."),
    msg("agent", "Hey, it's Jack with LHB. If 123 Main St ever made sense..."),
], "short_circuit")

# ── Check 4: Wrong Number ──────────────────────────────────────────
print("\n-- Check 4: Wrong Number --")
check("Wrong number, clean pivot", [
    msg("agent", "Hi, interested in selling 123 Main St?"),
    msg("contact", "Wrong number"),
    msg("agent", "I'm so sorry about that! Know anyone who wants to sell?"),
], "short_circuit", "Wrong Number")

check("Wrong person, clean pivot", [
    msg("agent", "Hi Thomas, interested in selling?"),
    msg("contact", "Wrong person"),
    msg("agent", "Sorry about that! Know someone who wants to sell?"),
], "short_circuit", "Wrong Number")

# ── Check 5: Not Interested ────────────────────────────────────────
print("\n-- Check 5: Not Interested --")
check("'No thanks' simple", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "No thanks"),
    msg("agent", "Not a problem! Do you think it could be for sale in 4 months?"),
], "short_circuit", "Not Interested")

check("'Not interested' explicit", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "Not interested"),
    msg("agent", "Not a problem at all."),
], "short_circuit", "Not Interested")

check("'No thank you' polite", [
    msg("agent", "Hi, interested in selling?"),
    msg("agent", "Just checking in about your property..."),
    msg("agent", "Hey, it's Jack with LHB..."),
    msg("contact", "No thank you"),
], "short_circuit", "Not Interested")

check("Bare 'No' response", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "No"),
    msg("agent", "Not a problem! Know anyone who wants to sell?"),
], "short_circuit", "Not Interested")

check("'Nope' response", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "Nope"),
], "short_circuit", "Not Interested")

check("'I'm not interested' lowercase", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "i'm not interested"),
    msg("agent", "Not a problem."),
], "short_circuit", "Not Interested")

check("'I'm okay' polite decline", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "I'm okay"),
], "short_circuit", "Not Interested")

check("Disliked reaction (thumbs down)", [
    msg("agent", "Hi, interested in selling?"),
    msg("agent", "Hey, it's Jack. Cash range like 61k-82k?"),
    msg("contact", 'Disliked "Hey, it\'s Jack. Cash range like 61k-82k?"'),
], "short_circuit", "Not Interested")

check("Drip + late 'No thanks' (16 agent msgs then 1 no)", [
    *[msg("agent", f"Hi, Jack with LHB. Outreach #{i}...") for i in range(14)],
    msg("contact", "No thank you"),
    msg("agent", "Not a problem at all."),
], "short_circuit", "Not Interested")

# ── Check 5: Not Interested SHOULD ESCALATE (agent over-persistent) ─
print("\n-- Check 5: Over-persistent agent (should escalate) --")
check("Contact said no, agent sent 3+ messages after", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "No thanks"),
    msg("agent", "Are you sure? The range is 105k-140k."),
    msg("agent", "Just checking in again about the property."),
    msg("agent", "Hey, still interested?"),
], "escalate")

# ── Check 9: Wrong Identity ────────────────────────────────────────
print("\n-- Check 9: Wrong Identity --")
check("'I'm not [Name]' capitalized", [
    msg("agent", "Hi Deborah, interested in selling?"),
    msg("contact", "I'm not Deborah"),
    msg("agent", "So sorry about that!"),
], "short_circuit", "Wrong Number")

check("'This is not Maria'", [
    msg("agent", "Hi Maria, interested in selling?"),
    msg("contact", "This is not Maria"),
    msg("agent", "Apologies!"),
], "short_circuit", "Wrong Number")

# ── ANTI-PATTERNS: should NOT trigger wrong identity ────────────────
print("\n-- Anti-patterns: should NOT trigger wrong identity --")
check("'I'm not interested' should NOT be wrong identity", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "I'm not interested"),
    msg("agent", "Not a problem."),
], "short_circuit", "Not Interested")

check("'I'm not ready' should NOT be wrong identity", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "I'm not ready to sell yet"),
    msg("agent", "Not a problem."),
], "short_circuit", "Not Interested")

check("'Im not selling' should NOT be wrong identity", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "Im not selling"),
    msg("agent", "Understood."),
], "short_circuit", "Not Interested")

# ── Active leads SHOULD escalate ────────────────────────────────────
print("\n-- Active leads (should escalate to Groq) --")
check("Contact says 'Sure' (interested)", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "Sure"),
    msg("agent", "Great! What sparks the initial interest?"),
], "escalate")

check("Contact gives price (active negotiation)", [
    msg("agent", "Hi, interested in selling?"),
    msg("contact", "175k cash"),
    msg("agent", "Got it! Let me run some numbers."),
], "escalate")

# ── Summary ─────────────────────────────────────────────────────────
print(f"\n{'='*80}")
print(f"  RESULTS: {PASS} passed, {FAIL} failed out of {PASS+FAIL} tests")
if FAIL == 0:
    print(f"  ALL TESTS PASSED")
else:
    print(f"  *** {FAIL} FAILURES ***")
print(f"{'='*80}")
sys.exit(1 if FAIL else 0)
