from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DATABASE_URL


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/query_contact_history.py \"Contact Name\"")
        raise SystemExit(2)

    name = sys.argv[1]
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()

    cur.execute(
        """
        SELECT id, name, created_at
        FROM contacts
        WHERE name = %s
        ORDER BY id DESC
        """,
        (name,),
    )
    contacts = cur.fetchall()
    print(f"contacts_found={len(contacts)}")
    for c in contacts[:10]:
        print("contact:", c)

    if not contacts:
        con.close()
        return

    contact_id = contacts[0][0]
    cur.execute(
        """
        SELECT
            c.id AS conversation_id,
            c.texter_name,
            c.assigned_labels,
            cs.id AS score_id,
            cs.scored_at,
            cs.model_used,
            cs.source,
            cs.red_flags,
            cs.summary
        FROM conversations c
        LEFT JOIN conversation_scores cs ON cs.conversation_id = c.id
        WHERE c.contact_id = %s
        ORDER BY cs.scored_at DESC NULLS LAST, c.extracted_at DESC
        LIMIT 25
        """,
        (contact_id,),
    )
    rows = cur.fetchall()
    con.close()

    print(f"rows={len(rows)}")
    for r in rows:
        (
            conversation_id,
            texter_name,
            assigned_labels,
            score_id,
            scored_at,
            model_used,
            source,
            red_flags,
            summary,
        ) = r
        print("---")
        print(
            f"conversation_id={conversation_id} texter={texter_name} assigned_labels={assigned_labels} "
            f"score_id={score_id} scored_at={scored_at} model={model_used} source={source}"
        )
        print(f"red_flags={red_flags}")
        s = (summary or "").replace("\n", " ")
        print(f"summary={s[:240]}")


if __name__ == "__main__":
    main()

