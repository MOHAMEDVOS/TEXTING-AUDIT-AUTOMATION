# -*- coding: utf-8 -*-
"""
Fetch 50 conversations with messages for manual baseline evaluation.
Normalizes sender to 'agent'/'contact' using the account name.
"""
import json
import sys

sys.path.insert(0, r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION")
import psycopg2
import psycopg2.extras
from config.settings import DATABASE_URL

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# Get 50 conversations across different accounts, ordered by most recent
cur.execute("""
    SELECT 
        c.id AS conversation_id,
        ct.name AS contact_name,
        c.texter_name,
        c.assigned_labels,
        c.agent_id,
        ac.funnel_tier,
        ac.guidelines,
        ac.name AS account_name,
        (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count
    FROM conversations c
    JOIN contacts ct ON ct.id = c.contact_id
    LEFT JOIN accounts ac ON ac.id = c.agent_id
    WHERE EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id)
    ORDER BY c.extracted_at DESC
    LIMIT 50
""")
convos = cur.fetchall()

results = []
for conv in convos:
    cid = conv["conversation_id"]
    cur.execute("""
        SELECT sender, body, sent_at
        FROM messages 
        WHERE conversation_id = %s 
        ORDER BY sent_at NULLS LAST, id
    """, (cid,))
    msgs = cur.fetchall()
    
    account_name = conv["account_name"] or ""
    texter_name = conv["texter_name"] or ""
    messages = []
    for m in msgs:
        sender_raw = (m["sender"] or "").strip()
        # Agent messages: sender matches the account name (e.g. "Resva1014", "Noah")
        # Contact messages: sender is "Contact" or the contact's name
        if sender_raw.lower() == account_name.lower() or sender_raw.lower() == "agent":
            role = "agent"
        elif sender_raw.lower() == "contact":
            role = "contact"
        elif sender_raw.lower() == "system":
            role = "system"
        else:
            role = "contact"
        messages.append({
            "sender": role,
            "body": m["body"] or "",
            "sent_at": str(m["sent_at"]) if m["sent_at"] else None,
        })
    
    results.append({
        "conversation_id": cid,
        "contact_name": conv["contact_name"],
        "texter_name": texter_name,
        "account_name": account_name,
        "assigned_labels": conv["assigned_labels"],
        "funnel_tier": conv["funnel_tier"],
        "guidelines": conv["guidelines"],
        "agent_id": conv["agent_id"],
        "msg_count": conv["msg_count"],
        "messages": messages,
    })

cur.close()
conn.close()

# Write to file
output_path = r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION\scripts\eval_50_conversations.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False, default=str)

print(f"Fetched {len(results)} conversations")
print(f"Saved to {output_path}")

# Print summary
for i, r in enumerate(results):
    agent_msgs = sum(1 for m in r["messages"] if m["sender"] == "agent")
    contact_msgs = sum(1 for m in r["messages"] if m["sender"] == "contact")
    labels = r["assigned_labels"] or "none"
    tier = r["funnel_tier"] or "?"
    print(f"  {i+1:2d}. [{r['conversation_id']:5d}] {r['contact_name'][:30]:30s} | {r['account_name'][:12]:12s} | {tier:3s} | A:{agent_msgs:2d} C:{contact_msgs:2d} | labels={labels}")
