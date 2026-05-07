-- Link each flag_feedback row to its source conversation so we can
-- (1) audit which conversation a rejected flag came from
-- (2) train the classifier on confirmed-clean conversations as hard negatives

ALTER TABLE flag_feedback
    ADD COLUMN IF NOT EXISTS conversation_id INTEGER
        REFERENCES conversations(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_flag_feedback_conversation
    ON flag_feedback(conversation_id);

-- Backfill from existing rows where we can match by agent_id + contact_name.
-- Latest conversation per (agent, contact) wins — same lookup the endpoint already does.
UPDATE flag_feedback ff
SET conversation_id = sub.conv_id
FROM (
    SELECT DISTINCT ON (c.agent_id, LOWER(ct.name))
        c.agent_id, LOWER(ct.name) AS contact_lower, c.id AS conv_id
    FROM conversations c
    JOIN contacts ct ON ct.id = c.contact_id
    ORDER BY c.agent_id, LOWER(ct.name), c.id DESC
) sub
WHERE ff.conversation_id IS NULL
  AND sub.agent_id      = ff.agent_id
  AND sub.contact_lower = LOWER(ff.contact_name);
