from __future__ import annotations

from pathlib import Path
import sys
import argparse
import time

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai.analyzer import analyze_conversation
from config.settings import DATABASE_URL


def get_columns(cur, table_name: str) -> list[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
        """,
        (table_name,),
    )
    return [r[0] for r in cur.fetchall()]


def pick_message_fk(message_cols: list[str], score_cols: list[str]) -> tuple[str, str]:
    candidates = ["contact_id", "conversation_id", "audit_id", "id"]
    for col in candidates:
        if col in message_cols and col in score_cols:
            return col, col
    if "contact_id" in message_cols:
        for score_col in ["id", "conversation_id", "audit_id"]:
            if score_col in score_cols:
                return "contact_id", score_col
    raise RuntimeError("Could not infer join key between conversation_scores and messages.")


def pick_time_col(cols: list[str]) -> str:
    for col in ["scored_at", "created_at", "updated_at", "date", "id"]:
        if col in cols:
            return col
    return cols[0]


def pick_sender_col(cols: list[str]) -> str:
    for col in ["sender", "from_name", "author", "role"]:
        if col in cols:
            return col
    raise RuntimeError("No sender column found in messages.")


def pick_message_col(cols: list[str]) -> str:
    for col in ["message", "text", "body", "content"]:
        if col in cols:
            return col
    raise RuntimeError("No message text column found in messages.")


def pick_sent_col(cols: list[str]) -> str | None:
    for col in ["sent_at", "created_at", "timestamp", "date"]:
        if col in cols:
            return col
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local prompt validation one conversation at a time.")
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="How many recent conversations to validate (default: 1).",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=10.0,
        help="Delay between conversations to avoid burst/rate limit (default: 10).",
    )
    args = parser.parse_args()

    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()

    score_cols = get_columns(cur, "conversation_scores")
    msg_cols = get_columns(cur, "messages")

    score_key_alias, msg_key_col = pick_message_fk(msg_cols, score_cols)
    score_time_col = pick_time_col(score_cols)
    sender_col = pick_sender_col(msg_cols)
    message_col = pick_message_col(msg_cols)
    sent_col = pick_sent_col(msg_cols)

    # Pick best key from scores table (prefer id variants)
    score_key_col = score_key_alias
    if score_key_col not in score_cols:
        for fallback in ["id", "conversation_id", "contact_id", "audit_id"]:
            if fallback in score_cols:
                score_key_col = fallback
                break

    cur.execute(
        f"""
        SELECT cs.{score_key_col}, COALESCE(c.texter_name, 'Unknown')
        FROM conversation_scores cs
        JOIN conversations c ON c.id = cs.conversation_id
        ORDER BY cs.{score_time_col} DESC
        LIMIT %s
        """
        ,
        (args.limit,),
    )
    rows = cur.fetchall()

    lines: list[str] = []
    for idx, (convo_key, agent_name) in enumerate(rows):
        if sent_col:
            cur.execute(
                f"""
                SELECT {sender_col}, {message_col}, {sent_col}
                FROM messages
                WHERE {msg_key_col} = %s
                ORDER BY {sent_col}
                """,
                (convo_key,),
            )
            data = cur.fetchall()
            msgs = [
                {
                    "sender": s,
                    "message": m,
                    "date": str(t.date()) if t else "",
                    "time": str(t.time())[:5] if t else "",
                }
                for s, m, t in data
            ]
        else:
            cur.execute(
                f"""
                SELECT {sender_col}, {message_col}
                FROM messages
                WHERE {msg_key_col} = %s
                """,
                (convo_key,),
            )
            data = cur.fetchall()
            msgs = [{"sender": s, "message": m, "date": "", "time": ""} for s, m in data]

        if not msgs:
            continue

        result = analyze_conversation(messages=msgs, agent_name=agent_name, contact_name="Lead")
        lines.append(f"=== {convo_key} / {agent_name} ===")
        lines.append(f"flags: {result.get('red_flags', [])}")
        lines.append(
            "scores: "
            f"c={result.get('compliance_score')} "
            f"s={result.get('sentiment_score')} "
            f"p={result.get('professionalism_score')} "
            f"sa={result.get('script_adherence_score')}"
        )
        lines.append(
            "label: "
            f"{result.get('label_assigned')} -> {result.get('label_should_be')} "
            f"(correct={result.get('label_correct')})"
        )
        lines.append(f"summary: {result.get('summary')}")
        lines.append("")

        # Avoid burst validation by spacing requests.
        if idx < len(rows) - 1 and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    con.close()

    out = Path("validation_run.txt")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
