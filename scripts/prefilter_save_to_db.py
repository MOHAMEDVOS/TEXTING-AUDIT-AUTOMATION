"""
Save prefilter-only results to the DB so they appear in the dashboard.
"""
import json
import logging
import sys
from datetime import datetime

import psycopg2
from config.settings import DATABASE_URL

import config.settings as settings
settings.PREFILTER_ENABLED = True
settings.PREFILTER_SHADOW_MODE = False
settings.PREFILTER_T1_LIVE = True
settings.PREFILTER_FLAG_ROUTING_ENABLED = True

from ai.prefilter.pipeline import run_prefilter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False))])
logger = logging.getLogger(__name__)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

# Get latest 20 conversations for Resva1014
cur.execute("""
    SELECT c.id, ct.name, c.assigned_labels
    FROM conversations c
    JOIN contacts ct ON ct.id = c.contact_id
    WHERE c.agent_id = 6
    ORDER BY c.extracted_at DESC LIMIT 20
""")
conv_rows = cur.fetchall()

logger.info(f"Found {len(conv_rows)} conversations for Resva1014")

# Delete any existing scores for these conversations (clean slate)
conv_ids = [r[0] for r in conv_rows]
cur.execute("DELETE FROM conversation_scores WHERE conversation_id = ANY(%s)", (conv_ids,))
deleted = cur.rowcount
if deleted:
    logger.info(f"Cleared {deleted} existing scores")
conn.commit()

saved = 0
for conv_id, contact_name, labels in conv_rows:
    cur.execute("SELECT sender, body FROM messages WHERE conversation_id = %s ORDER BY id", (conv_id,))
    msg_rows = cur.fetchall()
    if not msg_rows:
        continue

    messages = [{"sender": "agent" if s.lower() == "resva1014" else "contact", "body": b or ""} for s, b in msg_rows]

    # Run prefilter
    pf = run_prefilter(messages, "Resva1014", contact_name)

    if pf:
        # Prefilter handled it
        source = "prefilter_t1"
        model = pf.get("model_used", "prefilter_t1")
        result = pf
    else:
        # Prefilter escalated — create a placeholder result
        source = "prefilter_t1"  # Use prefilter_t1 since it was evaluated by T1
        model = "prefilter_escalated"
        result = {
            "compliance_score": None,
            "sentiment_score": None,
            "professionalism_score": None,
            "script_adherence_score": None,
            "funnel_stage_reached": "needs_groq",
            "pillars_gathered": [],
            "rebuttals_used": [],
            "label_assigned": "NEEDS GROQ",
            "label_correct": None,
            "label_should_be": None,
            "label_reason": "Prefilter escalated - requires Groq AI analysis",
            "red_flags": [],
            "actions_triggered": [],
            "summary": "This conversation requires full AI analysis (Groq). The prefilter determined it is too complex for local processing.",
        }

    # Write to conversation_scores
    cur.execute("""
        INSERT INTO conversation_scores (
            conversation_id, compliance_score, sentiment_score,
            professionalism_score, script_adherence_score,
            funnel_stage, pillars_gathered, rebuttals_used,
            label_assigned, label_correct, label_should_be, label_reason,
            red_flags, actions_triggered, summary,
            model_used, scored_at, source
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """, (
        conv_id,
        result.get("compliance_score"),
        result.get("sentiment_score"),
        result.get("professionalism_score"),
        result.get("script_adherence_score"),
        result.get("funnel_stage_reached") or result.get("funnel_stage"),
        result.get("pillars_gathered", []),
        result.get("rebuttals_used", []),
        result.get("label_assigned"),
        result.get("label_correct"),
        result.get("label_should_be"),
        result.get("label_reason"),
        json.dumps(result.get("red_flags", [])),
        result.get("actions_triggered", []),
        result.get("summary"),
        model,
        datetime.now(),
        source,
    ))
    saved += 1

    status = "HANDLED" if pf else "ESCALATED"
    label = result.get("label_assigned", "?")
    logger.info(f"  [{status:9s}] {contact_name:25s} -> label={label}")

conn.commit()
cur.close()
conn.close()

logger.info(f"\nSaved {saved} scores to database. Results should now appear in the dashboard!")
