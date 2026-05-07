from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import DATABASE_URL


def cols(table: str) -> list[str]:
    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    )
    out = [r[0] for r in cur.fetchall()]
    con.close()
    return out


if __name__ == "__main__":
    for t in ("conversation_scores", "conversations", "messages"):
        print(t, cols(t))
