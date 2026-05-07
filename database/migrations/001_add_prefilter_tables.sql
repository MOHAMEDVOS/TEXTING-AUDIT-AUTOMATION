-- Migration 001: ML pre-filter pipeline support
-- Adds tables for tier decisions, embeddings, and tracks which tier produced
-- each conversation_score so we can compare local vs. Groq output over time.

BEGIN;

-- 1. Audit trail: every conversation that went through the prefilter records
--    which tier handled it, the local prediction (if any), and (later) the
--    Groq result for shadow-mode agreement scoring.
CREATE TABLE IF NOT EXISTS prefilter_decisions (
    id               BIGSERIAL PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tier_hit         SMALLINT NOT NULL CHECK (tier_hit BETWEEN 1 AND 4),
    decision         TEXT NOT NULL CHECK (decision IN ('short_circuit', 'escalate')),
    confidence       REAL,
    predicted_scores JSONB,
    groq_scores      JSONB,
    agreement        REAL,
    shadow_mode      BOOLEAN NOT NULL DEFAULT TRUE,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prefilter_decisions_convo
    ON prefilter_decisions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_prefilter_decisions_tier
    ON prefilter_decisions(tier_hit);
CREATE INDEX IF NOT EXISTS idx_prefilter_decisions_created
    ON prefilter_decisions(created_at);

-- 2. Embedding cache: skip re-embedding the same conversation across runs.
--    Stored as REAL[] for portability (no pgvector dependency required).
--    For 1430 convos × 384 dims × 4 bytes = ~2.2 MB, well within Postgres limits.
CREATE TABLE IF NOT EXISTS conversation_embeddings (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    embedding       REAL[] NOT NULL,
    model_name      TEXT NOT NULL,
    text_hash       TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_embeddings_model
    ON conversation_embeddings(model_name);

-- 3. Track which tier produced each conversation_score so we can later
--    distinguish local predictions from Groq ground-truth in training data.
ALTER TABLE conversation_scores
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'groq'
    CHECK (source IN ('groq', 'nim', 'prefilter_t1', 'prefilter_t2', 'prefilter_t3'));

CREATE INDEX IF NOT EXISTS idx_conv_scores_source
    ON conversation_scores(source);

COMMIT;
