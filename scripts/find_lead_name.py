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
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
        ORDER BY table_name
        """
    )
    tables = [r[0] for r in cur.fetchall()]
    print("tables:", tables)

    # Latest scored conversation with contact id
    cur.execute(
        """
        SELECT cs.conversation_id, c.contact_id, c.texter_name
        FROM conversation_scores cs
        JOIN conversations c ON c.id = cs.conversation_id
        ORDER BY cs.scored_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    print("latest_conversation:", row)

    contact_id = row[1] if row else None
    if contact_id and "contacts" in tables:
        cur.execute(
            """
            SELECT *
            FROM contacts
            WHERE id = %s
            LIMIT 1
            """,
            (contact_id,),
        )
        contact_row = cur.fetchone()
        print("contact_row:", contact_row)
    else:
        print("No contacts table or no contact_id available.")

    con.close()


if __name__ == "__main__":
    main()
