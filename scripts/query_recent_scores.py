from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DATABASE_URL


def main() -> None:
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    cur.execute(
        """
        SELECT
            cs.id,
            cs.conversation_id,
            c.texter_name,
            cs.scored_at,
            cs.compliance_score,
            cs.sentiment_score,
            cs.professionalism_score,
            cs.script_adherence_score,
            cs.red_flags,
            cs.summary
        FROM conversation_scores cs
        JOIN conversations c ON c.id = cs.conversation_id
        ORDER BY cs.scored_at DESC
        LIMIT 10
        """
    )
    rows = cur.fetchall()
    con.close()

    print(f"rows={len(rows)}")
    for r in rows:
        print("---")
        print(f"id={r[0]} conversation={r[1]} texter={r[2]} scored_at={r[3]}")
        print(f"scores c={r[4]} s={r[5]} p={r[6]} sa={r[7]}")
        print(f"red_flags={r[8]}")
        summary = (r[9] or "").replace("\n", " ")
        print(f"summary={summary[:220]}")


if __name__ == "__main__":
    main()
