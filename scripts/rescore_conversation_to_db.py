from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai.analyzer import analyze_conversation
from config.settings import DATABASE_URL


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python scripts/rescore_conversation_to_db.py <conversation_id>")
        raise SystemExit(2)

    conversation_id = int(sys.argv[1])

    con = psycopg2.connect(DATABASE_URL)
    cur = con.cursor()

    cur.execute(
        """
        SELECT
            c.id,
            c.texter_name,
            c.assigned_labels,
            ct.name AS contact_name
        FROM conversations c
        JOIN contacts ct ON ct.id = c.contact_id
        WHERE c.id = %s
        """,
        (conversation_id,),
    )
    conv = cur.fetchone()
    if not conv:
        con.close()
        raise SystemExit(f"Conversation {conversation_id} not found")

    _, texter_name, assigned_labels, contact_name = conv
    assigned_labels = assigned_labels or []

    cur.execute(
        """
        SELECT sender, body, sent_at
        FROM messages
        WHERE conversation_id = %s
        ORDER BY sent_at ASC NULLS LAST, id ASC
        """,
        (conversation_id,),
    )
    rows = cur.fetchall()
    messages = [
        {"sender": sender, "message": body or "", "date": "", "time": ""}
        for (sender, body, _sent_at) in rows
    ]

    result = analyze_conversation(
        messages=messages,
        agent_name=texter_name or "Unknown",
        contact_name=contact_name or "Contact",
        assigned_labels=list(assigned_labels),
        conversation_id=conversation_id,
    )

    # Persist a new conversation_scores row (dashboard reads latest by id desc)
    cur.execute(
        """
        INSERT INTO conversation_scores
            (conversation_id, compliance_score, sentiment_score,
             professionalism_score, script_adherence_score,
             funnel_stage, pillars_gathered, rebuttals_used,
             label_assigned, label_correct, label_should_be, label_reason,
             red_flags, actions_triggered, summary, model_used)
        VALUES
            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
        RETURNING id, scored_at
        """,
        (
            conversation_id,
            result.get("compliance_score"),
            result.get("sentiment_score"),
            result.get("professionalism_score"),
            result.get("script_adherence_score"),
            result.get("funnel_stage_reached"),
            result.get("pillars_gathered") or [],
            result.get("rebuttals_used") or [],
            result.get("label_assigned"),
            result.get("label_correct"),
            result.get("label_should_be"),
            result.get("label_reason"),
            json.dumps(result.get("red_flags") or []),
            result.get("actions_triggered") or [],
            result.get("summary"),
            result.get("model_used"),
        ),
    )
    score_id, scored_at = cur.fetchone()
    con.commit()
    con.close()

    print(f"rescored conversation_id={conversation_id} contact={contact_name} texter={texter_name}")
    print(f"new_score_id={score_id} scored_at={scored_at}")
    print(f"red_flags={result.get('red_flags')}")
    print(f"summary={result.get('summary')}")


if __name__ == "__main__":
    main()

