"""
Diagnostic: check if Noah's conversations are genuinely audited in DB.
Run: python check_audits.py
"""
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()
DB = os.getenv("DATABASE_URL")
if not DB:
    raise SystemExit("DATABASE_URL is not set — define it in .env or the environment")

async def check():
    conn = await asyncpg.connect(DB)

    # Find Noah
    agent = await conn.fetchrow(
        "SELECT id, name FROM accounts WHERE LOWER(name) = 'noah'"
    )
    if not agent:
        print("Agent 'Noah' not found in DB")
        await conn.close()
        return
    print(f"Agent: {agent['name']} (id={agent['id']})")

    # Conversation counts
    total  = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE agent_id = $1", agent["id"])
    archived = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE agent_id = $1 AND is_archived = TRUE", agent["id"])
    active   = await conn.fetchval("SELECT COUNT(*) FROM conversations WHERE agent_id = $1 AND is_archived = FALSE", agent["id"])
    print(f"Conversations: total={total}  active={active}  archived={archived}")

    # Scored conversations
    scored = await conn.fetchval("""
        SELECT COUNT(DISTINCT c.id)
        FROM conversations c
        JOIN conversation_scores cs ON cs.conversation_id = c.id
        WHERE c.agent_id = $1
    """, agent["id"])
    print(f"Conversations with AI scores: {scored}")

    # Latest audit_scores row
    latest = await conn.fetchrow(
        "SELECT overall_score, audit_date FROM audit_scores WHERE agent_id = $1 ORDER BY audit_date DESC LIMIT 1",
        agent["id"]
    )
    if latest:
        print(f"Latest audit_score: overall={latest['overall_score']}  date={latest['audit_date']}")
    else:
        print("No audit_scores entry for Noah — not yet scored!")

    # Show active contacts + whether they have a conversation_score
    print(f"\n{'Contact':<30} {'Archived':<9} {'Extracted':<22} {'AuditDate':<12} {'HasScore'}")
    print("-" * 85)
    rows = await conn.fetch("""
        SELECT ct.name, c.is_archived, c.extracted_at, c.audit_date,
               EXISTS(SELECT 1 FROM conversation_scores cs WHERE cs.conversation_id = c.id) AS has_score
        FROM conversations c
        JOIN contacts ct ON ct.id = c.contact_id
        WHERE c.agent_id = $1 AND c.is_archived = FALSE
        ORDER BY c.extracted_at DESC
        LIMIT 50
    """, agent["id"])

    no_score = 0
    for r in rows:
        flag = "" if r["has_score"] else " ← NO SCORE"
        if not r["has_score"]:
            no_score += 1
        print(f"{str(r['name']):<30} {str(r['is_archived']):<9} {str(r['extracted_at']):<22} {str(r['audit_date']):<12} {str(r['has_score'])}{flag}")

    print(f"\nActive conversations WITHOUT a score: {no_score}")

    await conn.close()

asyncio.run(check())
