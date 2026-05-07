-- Migration 003: validation_log
--
-- Adds a table for managers to record whether a Groq audit result was
-- correct ("valid") or wrong ("invalid"). This is the positive-signal
-- counterpart to flag_feedback, which only records the negative signal.
--
-- Relationship to flag_feedback:
--   - flag_feedback:  per-flag "Not Valid" clicks (existing)
--   - validation_log: per-conversation "Valid" OR "Invalid" clicks (new)
--
-- index_builder.py reads validation_log when PREFILTER_REQUIRE_VALIDATION=true
-- to restrict the ML training set to manager-confirmed conversations only.

CREATE TABLE IF NOT EXISTS validation_log (
    id              SERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL,
    agent_name      TEXT NOT NULL,
    contact_name    TEXT NOT NULL,
    conversation_id BIGINT REFERENCES conversations(id) ON DELETE SET NULL,
    score_id        INTEGER REFERENCES conversation_scores(id) ON DELETE SET NULL,
    status          TEXT NOT NULL CHECK (status IN ('valid', 'invalid')),
    validated_by    TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_id, contact_name)
);

CREATE INDEX IF NOT EXISTS idx_validation_log_status     ON validation_log(status);
CREATE INDEX IF NOT EXISTS idx_validation_log_agent      ON validation_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_validation_log_created_at ON validation_log(created_at DESC);
