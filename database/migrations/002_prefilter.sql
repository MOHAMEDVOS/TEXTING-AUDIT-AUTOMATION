-- database/migrations/002_prefilter.sql
-- Tables for the ML pre-filter pipeline.
-- Run once: psql -U postgres -d texting_audit -f database/migrations/002_prefilter.sql

CREATE TABLE IF NOT EXISTS prefilter_decisions (
    id              BIGSERIAL PRIMARY KEY,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    contact_name    TEXT,
    tier_hit        SMALLINT NOT NULL,         -- 1, 2, 3, 4
    short_circuited BOOLEAN NOT NULL DEFAULT FALSE,
    confidence      REAL,
    predicted       JSONB,                     -- prefilter's prediction
    groq_actual     JSONB,                     -- filled in shadow mode after Groq runs
    agreement       REAL,                      -- 0.0-1.0, computed offline
    shadow_mode     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prefilter_conv ON prefilter_decisions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_prefilter_tier ON prefilter_decisions(tier_hit);
CREATE INDEX IF NOT EXISTS idx_prefilter_created ON prefilter_decisions(created_at DESC);

CREATE TABLE IF NOT EXISTS conversation_embeddings (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    embedding       REAL[] NOT NULL,
    model_name      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_conv_emb_model ON conversation_embeddings(model_name);
