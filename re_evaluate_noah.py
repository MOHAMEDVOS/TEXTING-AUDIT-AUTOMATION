import sys
import os

# Add the project root to the python path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import psycopg2
from config.settings import DATABASE_URL
from ai.prefilter.tier1_phrases_v2 import evaluate
import json

def fix_noah():
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Get the record for Noah1010"SC"
            cur.execute("""
                SELECT id, contact_name, agent_name, raw_conversation, funnel_tier, label 
                FROM validation_log 
                WHERE contact_name = %s
                ORDER BY created_at DESC LIMIT 1
            """, ('Noah1010"SC"',))
            row = cur.fetchone()
            
            if not row:
                print("Could not find Noah1010\"SC\"")
                return

            record_id = row[0]
            contact_name = row[1]
            agent_name = row[2]
            messages = row[3]
            funnel_tier = row[4]
            label = row[5]

            print(f"Re-evaluating {contact_name} (ID: {record_id}), Label: {label}")
            
            # Re-evaluate
            result = evaluate(messages, funnel_tier, agent_name, contact_name, [label] if label else None)
            
            if result and result.result:
                r = result.result
                print("Result:")
                print(f"  Summary: {r.get('summary')}")
                print(f"  Label Assigned: {r.get('label_assigned')}")
                print(f"  Label Correct: {r.get('label_correct')}")
                print(f"  Label Reason: {r.get('label_reason')}")
                print(f"  Red Flags: {r.get('red_flags')}")
                
                # Update DB to clear the flags
                cur.execute("""
                    UPDATE validation_log 
                    SET 
                        summary = %s,
                        red_flags = %s,
                        label_correct = %s,
                        is_flagged = %s,
                        raw_response = %s
                    WHERE id = %s
                """, (
                    r.get("summary"),
                    json.dumps(r.get("red_flags", [])),
                    r.get("label_correct"),
                    bool(r.get("red_flags")),
                    json.dumps(r),
                    record_id
                ))
                conn.commit()
                print("Record updated successfully.")
            else:
                print("No result from evaluate() or no result.result")

if __name__ == "__main__":
    fix_noah()
