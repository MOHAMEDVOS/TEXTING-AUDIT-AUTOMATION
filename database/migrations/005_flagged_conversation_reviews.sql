-- Migration 005: flagged_conversation_reviews
-- Tracks which flagged conversations a manager has opened/reviewed.

CREATE TABLE IF NOT EXISTS flagged_conversation_reviews (
    id              SERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    contact_name    TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(agent_id, contact_name)
);

CREATE INDEX IF NOT EXISTS idx_flagged_reviews_agent
    ON flagged_conversation_reviews(agent_id);
