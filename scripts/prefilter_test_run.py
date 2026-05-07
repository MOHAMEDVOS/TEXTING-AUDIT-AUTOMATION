"""
Final prefilter-only test with full comparison and accuracy assessment.
"""
import json
import logging
import sys

import psycopg2
from config.settings import DATABASE_URL

import config.settings as settings
settings.PREFILTER_ENABLED = True
settings.PREFILTER_SHADOW_MODE = False
settings.PREFILTER_T1_LIVE = True
settings.PREFILTER_FLAG_ROUTING_ENABLED = True

from ai.prefilter.pipeline import run_prefilter

logging.basicConfig(level=logging.WARNING, format="%(message)s",
    handlers=[logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))])

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
    SELECT c.id, ct.name, c.assigned_labels
    FROM conversations c
    JOIN contacts ct ON ct.id = c.contact_id
    WHERE c.agent_id = 6
    ORDER BY c.extracted_at DESC LIMIT 20
""")
conv_rows = cur.fetchall()

print(f"{'='*110}")
print(f"  PREFILTER-ONLY TEST — Resva1014 ({len(conv_rows)} conversations)")
print(f"  Zero Groq tokens used. All decisions are local ML/regex only.")
print(f"{'='*110}\n")

results = []
for conv_id, contact_name, labels in conv_rows:
    cur.execute("SELECT sender, body FROM messages WHERE conversation_id = %s ORDER BY id", (conv_id,))
    msg_rows = cur.fetchall()
    if not msg_rows:
        continue
    messages = [{"sender": "agent" if s.lower() == "resva1014" else "contact", "body": b or ""} for s, b in msg_rows]
    agent_msgs = [m for m in messages if m["sender"] == "agent"]
    contact_msgs = [m for m in messages if m["sender"] == "contact"]

    pf = run_prefilter(messages, "Resva1014", contact_name)

    # Get Groq comparison
    cur.execute("""
        SELECT compliance_score, sentiment_score, professionalism_score,
               script_adherence_score, funnel_stage, label_assigned,
               label_correct, red_flags, summary, model_used
        FROM conversation_scores WHERE conversation_id = %s
        ORDER BY id DESC LIMIT 1
    """, (conv_id,))
    groq = cur.fetchone()

    results.append({
        "conv_id": conv_id, "contact": contact_name, "labels": labels or [],
        "msg_count": len(messages), "agent_count": len(agent_msgs),
        "contact_count": len(contact_msgs), "messages": messages,
        "pf_result": pf, "groq": groq,
    })

cur.close()
conn.close()

# Print results
handled = [r for r in results if r["pf_result"] is not None]
needs_groq = [r for r in results if r["pf_result"] is None]

for r in results:
    pf = r["pf_result"]
    labels_str = ", ".join(r["labels"]) if r["labels"] else "none"
    groq = r["groq"]
    groq_comp = groq[0] if groq else None
    groq_label = (groq[5] or "").strip() if groq else "?"
    groq_flags = json.loads(groq[7]) if groq and groq[7] else []

    if pf:
        pf_comp = pf.get("compliance_score")
        pf_label = (pf.get("label_assigned") or "").strip()
        pf_flags = pf.get("red_flags", [])
        pf_model = pf.get("model_used", "?")

        print(f"  [OK] {r['contact']:25s} | assigned=[{labels_str:22s}] | a:{r['agent_count']:2d} c:{r['contact_count']:2d} | "
              f"PF_label={pf_label:20s} comp={pf_comp} flags={len(pf_flags)} | "
              f"Groq_label={groq_label:20s} comp={groq_comp} flags={len(groq_flags)}")
    else:
        print(f"  [??] {r['contact']:25s} | assigned=[{labels_str:22s}] | a:{r['agent_count']:2d} c:{r['contact_count']:2d} | "
              f"-> NEEDS GROQ | "
              f"Groq_label={groq_label:20s} comp={groq_comp} flags={len(groq_flags)}")

# Summary
print(f"\n{'='*110}")
print(f"  SUMMARY")
print(f"{'='*110}")
print(f"  Total:              {len(results)}")
print(f"  Prefilter handled:  {len(handled)} ({len(handled)/len(results)*100:.0f}%)")
print(f"  Needs Groq:         {len(needs_groq)} ({len(needs_groq)/len(results)*100:.0f}%)")

# Accuracy check for handled conversations
print(f"\n{'='*110}")
print(f"  ACCURACY AUDIT — Verifying prefilter decisions against conversation transcripts")
print(f"{'='*110}")

issues = []
for r in handled:
    pf = r["pf_result"]
    pf_label = (pf.get("label_assigned") or "").strip().lower()
    assigned_labels = [l.lower().strip() for l in (r["labels"] or [])]
    contact_texts = [m["body"] for m in r["messages"] if m["sender"] == "contact"]

    # Manual verification logic
    problem = None
    if pf_label == "wrong number" and not any("wrong" in t.lower() or "not " in t.lower() for t in contact_texts):
        problem = f"labeled 'Wrong Number' but no wrong-number language in contact text"
    if pf_label == "not interested" and any("wrong" in t.lower() for t in contact_texts):
        problem = f"labeled 'Not Interested' but contact said 'wrong'"

    groq = r["groq"]
    if groq:
        groq_comp = groq[0]
        pf_comp = pf.get("compliance_score")
        if groq_comp is not None and pf_comp is not None and abs(pf_comp - groq_comp) > 15:
            problem = f"compliance diff: PF={pf_comp} vs Groq={groq_comp}"

    if problem:
        issues.append((r["contact"], problem))
        print(f"  [!!]  {r['contact']}: {problem}")
    else:
        print(f"  [OK] {r['contact']}: CORRECT ({pf_label})")

if issues:
    print(f"\n  Result: {len(handled)-len(issues)}/{len(handled)} correct ({(len(handled)-len(issues))/len(handled)*100:.0f}%)")
    print(f"  Issues found: {len(issues)}")
else:
    print(f"\n  Result: {len(handled)}/{len(handled)} (100%) — ALL CORRECT ✅")

# Show what Groq will still need to handle
print(f"\n{'='*110}")
print(f"  GROQ-REQUIRED CONVERSATIONS (can't be prefiltered)")
print(f"{'='*110}")
for r in needs_groq:
    contact_texts = [m["body"][:80] for m in r["messages"] if m["sender"] == "contact"]
    labels_str = ", ".join(r["labels"]) if r["labels"] else "none"
    print(f"  🔶 {r['contact']:25s} | [{labels_str:22s}] | a:{r['agent_count']:2d} c:{r['contact_count']:2d} | contact: {contact_texts[:2]}")
