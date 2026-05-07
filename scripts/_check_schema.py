import sys
sys.path.insert(0, r"c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION")
import psycopg2
from config.settings import DATABASE_URL

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

for tbl in ['accounts', 'account_assignments', 'texters']:
    print(f"=== {tbl} ===")
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (tbl,))
    for r in cur.fetchall():
        print(f"  {r[0]}")

# Check how conversations link to agent accounts
print("\n=== Sample agent_ids in conversations ===")
cur.execute("SELECT DISTINCT agent_id FROM conversations ORDER BY agent_id")
for r in cur.fetchall():
    print(f"  agent_id={r[0]}")

# Check accounts
print("\n=== accounts sample ===")
cur.execute("SELECT * FROM accounts LIMIT 3")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    print(dict(zip(cols, row)))

# Check account_assignments
print("\n=== account_assignments sample ===")
cur.execute("SELECT * FROM account_assignments LIMIT 3")
cols = [d[0] for d in cur.description]
for row in cur.fetchall():
    print(dict(zip(cols, row)))

cur.close()
conn.close()
