"""
One-shot migration: add cross-process reservation columns to api_keys.

Adds:
  - reserved_until   TIMESTAMPTZ   (15s lease for active Groq calls)
  - cool_until       TIMESTAMPTZ   (post-429 cooldown, shared across processes)
  - last_used_at_db  TIMESTAMPTZ   (persisted LRU ordering across processes)

Plus a partial index on the shared Groq pool for fast reservation selects.

Idempotent — safe to run multiple times.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from config.settings import DATABASE_URL


DDL = """
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS reserved_until  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cool_until      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_used_at_db TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS api_keys_groq_shared_idx
    ON api_keys (provider, agent_name, reserved_until, cool_until)
    WHERE provider = 'groq' AND agent_name IS NULL;
"""


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(DDL)
        print("[migration] api_keys reservation columns + index ensured")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
