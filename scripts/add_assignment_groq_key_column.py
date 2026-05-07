"""
One-shot migration: add manual Groq key assignment column to account_assignments.

Adds:
  - groq_key_id INTEGER

Idempotent — safe to run multiple times.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from config.settings import DATABASE_URL


DDL = """
ALTER TABLE account_assignments
    ADD COLUMN IF NOT EXISTS groq_key_id INTEGER;
"""


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
        print("[migration] account_assignments.groq_key_id ensured")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
