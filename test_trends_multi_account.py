"""
Test: Abdellatif assigned to 3 accounts on the same day -> 3 trend snapshots appear in /api/trends

Steps:
1. Pick 3 existing accounts (Noah, Resva1017, Resva1010)
2. Insert today's assignments for Abdellatif on all 3
3. Insert a fake audit_scores row for each account
4. Call _save_trend_snapshot() for each account name (as the poller does)
5. Query /api/trends and assert 3 rows come back for Abdellatif today
6. Clean up test data
"""

import asyncio
import sys
import os
from pathlib import Path
from datetime import date

# Make sure we can import from dashboard/app.py
sys.path.insert(0, str(Path(__file__).parent / "dashboard"))

import aiosqlite

DB_PATH = str(Path(__file__).parent / "database" / "audit_data.db")
TODAY = date.today().isoformat()
TEXTER = "Abdellatif Omar Osama Mohamed Ahmed"

# 3 accounts to test with
TEST_ACCOUNTS = [
    {"name": "Noah",      "email": "Noah@goccs.net",      "id": 1},
    {"name": "Resva1017", "email": "Resva1017@gmail.com",  "id": 10},
    {"name": "Resva1010", "email": "Resva1010@gmail.com",  "id": 3},
]

FAKE_SCORES = [
    # (overall, compliance, sentiment, professionalism, script_adherence, red_flags_json, details_json)
    (88.5, 100.0, 82.0, 91.0, 71.0, '[{"flag":"Late response"}]',
     '{"per_conversation":[{"red_flags":["Late response"]},{}]}'),
    (74.2, 90.0,  68.0, 78.0, 61.0, '[{"flag":"Script skip"},{"flag":"Rude tone"}]',
     '{"per_conversation":[{"red_flags":["Script skip"]},{"red_flags":["Rude tone"]},{}]}'),
    (92.0, 100.0, 95.0, 93.0, 80.0, '[]',
     '{"per_conversation":[{},{},{}]}'),
]


async def setup(db):
    """Insert assignments and audit scores for today."""
    print(f"\n[SETUP] date={TODAY}, texter={TEXTER}")

    for i, acc in enumerate(TEST_ACCOUNTS):
        # Assign account to Abdellatif today
        await db.execute(
            """INSERT INTO account_assignments (account_email, agent_name, assigned_date)
               VALUES (?, ?, ?)
               ON CONFLICT(account_email, assigned_date) DO UPDATE SET agent_name=excluded.agent_name""",
            (acc["email"], TEXTER, TODAY),
        )

        s = FAKE_SCORES[i]
        # Insert audit score
        await db.execute(
            """INSERT INTO audit_scores
               (agent_id, overall_score, compliance_score, sentiment_score,
                professionalism_score, script_adherence_score, red_flags, details, audit_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (acc["id"], s[0], s[1], s[2], s[3], s[4], s[5], s[6], TODAY),
        )
        print(f"  OK Assigned {acc['email']} -> {TEXTER}, inserted score {s[0]}")

    await db.commit()


async def run_snapshots():
    """Import and call _save_trend_snapshot for each account, mimicking the poller."""
    # Import here after path is set
    import importlib
    import dashboard.app as app_module

    # Reset the in-memory dedup guard so today's keys don't block us
    app_module._snapshotted.clear()

    print(f"\n[SNAPSHOTS] Calling _save_trend_snapshot for each account...")
    for acc in TEST_ACCOUNTS:
        await app_module._save_trend_snapshot(acc["name"])
        print(f"  OK _save_trend_snapshot('{acc['name']}') done")


async def verify(db):
    """Query trend_snapshots and assert 3 rows for Abdellatif today."""
    print(f"\n[VERIFY] Querying trend_snapshots for {TEXTER} on {TODAY}...")
    cursor = await db.execute(
        """SELECT agent_name, account_email, overall_score, total_issues, conversations_analyzed
           FROM trend_snapshots
           WHERE agent_name = ? AND audit_date = ?
           ORDER BY account_email""",
        (TEXTER, TODAY),
    )
    rows = await cursor.fetchall()

    print(f"  Found {len(rows)} snapshot(s):")
    for r in rows:
        print(f"    account={r[1]}  score={r[2]}  issues={r[3]}  convos={r[4]}")

    assert len(rows) == 3, f"Expected 3 snapshots, got {len(rows)}"

    emails = {r[1] for r in rows}
    for acc in TEST_ACCOUNTS:
        assert acc["email"] in emails, f"Missing snapshot for {acc['email']}"

    print(f"\n  PASS — 3 snapshots found for {TEXTER} on {TODAY}")
    return rows


async def cleanup(db):
    """Remove test data inserted by this script."""
    print(f"\n[CLEANUP] Removing test assignments and scores...")
    for acc in TEST_ACCOUNTS:
        await db.execute(
            "DELETE FROM account_assignments WHERE account_email=? AND assigned_date=?",
            (acc["email"], TODAY),
        )
        await db.execute(
            "DELETE FROM audit_scores WHERE agent_id=? AND audit_date=?",
            (acc["id"], TODAY),
        )
        await db.execute(
            "DELETE FROM trend_snapshots WHERE agent_name=? AND audit_date=? AND account_email=?",
            (TEXTER, TODAY, acc["email"]),
        )
    await db.commit()
    print("  OK Cleanup done")


async def main():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await setup(db)
        except Exception as e:
            print(f"[SETUP ERROR] {e}")
            raise

    # Snapshots use their own DB connection internally
    try:
        await run_snapshots()
    except Exception as e:
        print(f"[SNAPSHOT ERROR] {e}")
        import traceback; traceback.print_exc()
        raise

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        try:
            await verify(db)
        except AssertionError as e:
            print(f"\n  FAIL — {e}")
            sys.exit(1)
        finally:
            await cleanup(db)

    print("\n[DONE] All assertions passed.\n")


if __name__ == "__main__":
    asyncio.run(main())
