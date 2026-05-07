"""
Quick diagnostics for shared Groq key pool in api_keys table.

Shows:
- total shared Groq keys
- available vs cooling vs reserved-right-now
- last-used order and key suffixes
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg2
from config.settings import DATABASE_URL


def _mask(api_key: str) -> str:
    if not api_key:
        return "(empty)"
    return f"...{api_key[-6:]}"


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  COUNT(*)                                                   AS total,
                  COUNT(*) FILTER (
                    WHERE (cool_until IS NULL OR cool_until < now())
                      AND (reserved_until IS NULL OR reserved_until < now())
                  )                                                          AS available_now,
                  COUNT(*) FILTER (
                    WHERE cool_until IS NOT NULL AND cool_until >= now()
                  )                                                          AS cooling_now,
                  COUNT(*) FILTER (
                    WHERE reserved_until IS NOT NULL AND reserved_until >= now()
                  )                                                          AS reserved_now
                FROM api_keys
                WHERE provider = 'groq' AND agent_name IS NULL
                """
            )
            total, available, cooling, reserved = cur.fetchone()

            print("=== Groq Shared Pool Diagnostics ===")
            print(f"total_keys      : {total}")
            print(f"available_now   : {available}")
            print(f"cooling_now     : {cooling}")
            print(f"reserved_now    : {reserved}")
            print("")

            cur.execute(
                """
                SELECT
                  id,
                  api_key,
                  last_used_at_db,
                  cool_until,
                  reserved_until,
                  CASE
                    WHEN cool_until IS NOT NULL AND cool_until >= now() THEN 'cooling'
                    WHEN reserved_until IS NOT NULL AND reserved_until >= now() THEN 'reserved'
                    ELSE 'available'
                  END AS state
                FROM api_keys
                WHERE provider = 'groq' AND agent_name IS NULL
                ORDER BY last_used_at_db DESC NULLS LAST, id
                """
            )
            rows = cur.fetchall()

            if not rows:
                print("No shared Groq keys found in api_keys.")
                return

            print("Most recently used keys:")
            for key_id, api_key, last_used_at, cool_until, reserved_until, state in rows:
                print(
                    f"- id={key_id:<3} key={_mask(api_key):<10} "
                    f"state={state:<9} last_used={last_used_at} "
                    f"cool_until={cool_until} reserved_until={reserved_until}"
                )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
