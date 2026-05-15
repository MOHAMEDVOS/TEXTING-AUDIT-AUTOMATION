-- PostgreSQL schema for TEXTING AUDIT AUTOMATION
-- Run once against your texting_audit database
-- Requires pgvector extension

-- CREATE EXTENSION IF NOT EXISTS vector;
-- ── accounts (SmarterContact login credentials) ──────────────────────────────
CREATE TABLE IF NOT EXISTS accounts (
    id                    SERIAL PRIMARY KEY,
    name                  TEXT NOT NULL,
    email                 TEXT UNIQUE NOT NULL,
    password              TEXT,
    funnel_tier           TEXT CHECK (funnel_tier IN ('NF', 'MF', 'WF')),
    guidelines            TEXT,
    guidelines_updated_at TIMESTAMPTZ,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

-- For existing databases, run these ALTERs once:
-- ALTER TABLE accounts ADD COLUMN IF NOT EXISTS funnel_tier TEXT CHECK (funnel_tier IN ('NF', 'MF', 'WF'));
-- ALTER TABLE accounts ADD COLUMN IF NOT EXISTS guidelines TEXT;
-- ALTER TABLE accounts ADD COLUMN IF NOT EXISTS guidelines_updated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_accounts_funnel_tier
    ON accounts(funnel_tier) WHERE funnel_tier IS NOT NULL;

-- ── texters (agent roster) ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS texters (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── contacts (leads / prospects) ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS contacts (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    phone_number TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- ── conversations (one row per thread per audit run) ─────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id              SERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES accounts(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    texter_name     TEXT NOT NULL,
    assigned_labels TEXT[],
    extracted_at    TIMESTAMPTZ NOT NULL,
    audit_date      DATE NOT NULL,
    convo_date      TEXT NOT NULL DEFAULT '',
    is_archived     BOOLEAN DEFAULT FALSE
);

-- Migration: convo_date holds the SmarterContact inbox-row date (MM/DD/YYYY)
-- as scraped — shown on the conversation card next to the audit date.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS convo_date TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_conversations_agent   ON conversations(agent_id);
CREATE INDEX IF NOT EXISTS idx_conversations_texter  ON conversations(texter_name);
CREATE INDEX IF NOT EXISTS idx_conversations_date    ON conversations(audit_date);
CREATE INDEX IF NOT EXISTS idx_conversations_contact ON conversations(contact_id);

-- ── messages (normalized from JSON blobs) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id              SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender          TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         TIMESTAMPTZ,
    sc_date_label   TEXT NOT NULL DEFAULT '',
    seq             INTEGER NOT NULL DEFAULT 0
    -- embedding       VECTOR(1536)   -- pgvector column, NULL for now (future use)
);

-- Migration: add columns to existing tables
ALTER TABLE messages ADD COLUMN IF NOT EXISTS sc_date_label TEXT NOT NULL DEFAULT '';
ALTER TABLE messages ADD COLUMN IF NOT EXISTS seq INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);

-- ── extractions (run metadata — conversations moved to normalized tables) ─────
CREATE TABLE IF NOT EXISTS extractions (
    id             SERIAL PRIMARY KEY,
    agent_id       INTEGER NOT NULL REFERENCES accounts(id),
    extracted_at   TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL,
    reporting_data JSONB,
    page_text      TEXT,
    errors         JSONB
);

CREATE INDEX IF NOT EXISTS idx_extractions_agent ON extractions(agent_id);
CREATE INDEX IF NOT EXISTS idx_extractions_date  ON extractions(extracted_at);

-- ── audit_scores (aggregated per agent per audit date) ────────────────────────
CREATE TABLE IF NOT EXISTS audit_scores (
    id                     SERIAL PRIMARY KEY,
    agent_id               INTEGER NOT NULL REFERENCES accounts(id),
    audit_date             DATE NOT NULL,
    overall_score          DOUBLE PRECISION,
    compliance_score       DOUBLE PRECISION,
    sentiment_score        DOUBLE PRECISION,
    professionalism_score  DOUBLE PRECISION,
    response_time_score    DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    red_flags              JSONB,
    details                JSONB
);

CREATE INDEX IF NOT EXISTS idx_scores_agent ON audit_scores(agent_id);
CREATE INDEX IF NOT EXISTS idx_scores_date  ON audit_scores(audit_date);
-- idx_scores_agent_date unique index applied as a one-time migration (not recreated here)

-- ── conversation_scores (per-conversation AI analysis, permanent) ─────────────
CREATE TABLE IF NOT EXISTS conversation_scores (
    id                     SERIAL PRIMARY KEY,
    conversation_id        INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    compliance_score       DOUBLE PRECISION,
    sentiment_score        DOUBLE PRECISION,
    professionalism_score  DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    funnel_stage           TEXT,
    pillars_gathered       TEXT[],
    rebuttals_used         TEXT[],
    label_assigned         TEXT,
    label_correct          BOOLEAN,
    label_should_be        TEXT,
    label_reason           TEXT,
    red_flags              JSONB,
    actions_triggered      TEXT[],
    summary                TEXT,
    model_used             TEXT,
    scored_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conv_scores_conversation ON conversation_scores(conversation_id);
CREATE INDEX IF NOT EXISTS idx_conv_scores_red_flags    ON conversation_scores USING GIN(red_flags);

-- ── audited_chats (deduplication cache) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS audited_chats (
    id              SERIAL PRIMARY KEY,
    agent_email     TEXT NOT NULL,
    contact_name    TEXT NOT NULL,
    message_preview TEXT NOT NULL,
    audited_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(agent_email, contact_name)
);

-- ── flag_feedback (human validation of AI flags) ─────────────────────────────
CREATE TABLE IF NOT EXISTS flag_feedback (
    id           SERIAL PRIMARY KEY,
    agent_id     INTEGER NOT NULL REFERENCES accounts(id),
    agent_name   TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    red_flag     TEXT NOT NULL,
    evidence     TEXT,
    status       TEXT NOT NULL DEFAULT 'invalid',
    reason       TEXT,
    category     TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_flag_feedback_agent ON flag_feedback(agent_id);
CREATE INDEX IF NOT EXISTS idx_flag_feedback_flag  ON flag_feedback(red_flag);

-- ── session_events (self-learning trigger data) ───────────────────────────────
CREATE TABLE IF NOT EXISTS session_events (
    id                   SERIAL PRIMARY KEY,
    agent_id             INTEGER NOT NULL REFERENCES accounts(id),
    agent_name           TEXT NOT NULL,
    conversations_scored INTEGER DEFAULT 0,
    flags_generated      INTEGER DEFAULT 0,
    model_used           TEXT,
    run_timestamp        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_session_events_ts    ON session_events(run_timestamp);
CREATE INDEX IF NOT EXISTS idx_session_events_agent ON session_events(agent_id);

-- ── account_assignments (multi-account mapping) ───────────────────────────────
CREATE TABLE IF NOT EXISTS account_assignments (
    id            SERIAL PRIMARY KEY,
    account_email TEXT NOT NULL,
    agent_name    TEXT NOT NULL,
    groq_key_id   INTEGER,
    assigned_date DATE NOT NULL,
    assigned_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(account_email, assigned_date)
);

-- For existing databases, run this ALTER once:
-- ALTER TABLE account_assignments ADD COLUMN IF NOT EXISTS groq_key_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_assignments_date  ON account_assignments(assigned_date);
CREATE INDEX IF NOT EXISTS idx_assignments_email ON account_assignments(account_email);

-- ── trend_snapshots (daily performance rollup) ────────────────────────────────
CREATE TABLE IF NOT EXISTS trend_snapshots (
    id                     SERIAL PRIMARY KEY,
    agent_name             TEXT NOT NULL,
    audit_date             DATE NOT NULL,
    audit_timestamp        TIMESTAMPTZ NOT NULL,
    account_email          TEXT,
    total_issues           INTEGER DEFAULT 0,
    overall_score          DOUBLE PRECISION,
    compliance_score       DOUBLE PRECISION,
    sentiment_score        DOUBLE PRECISION,
    professionalism_score  DOUBLE PRECISION,
    script_adherence_score DOUBLE PRECISION,
    conversations_analyzed INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_trends_agent ON trend_snapshots(agent_name);
CREATE INDEX IF NOT EXISTS idx_trends_date  ON trend_snapshots(audit_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trends_unique ON trend_snapshots(agent_name, audit_date, account_email);

-- ── api_keys (Groq shared pool) ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id           SERIAL PRIMARY KEY,
    provider     TEXT NOT NULL,                -- 'groq'
    api_key      TEXT NOT NULL,
    agent_name   TEXT,                         -- NULL = shared pool key (preferred)
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(provider, api_key, agent_name)
);

CREATE INDEX IF NOT EXISTS idx_api_keys_provider ON api_keys(provider);

-- ── semantic_candidates (auto-learning queue) ────────────────────────────────
CREATE TABLE IF NOT EXISTS semantic_candidates (
    id                      SERIAL PRIMARY KEY,
    conversation_id         INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    funnel_tier             TEXT,
    embedding_hash          TEXT NOT NULL,
    top_similarity          DOUBLE PRECISION,
    nearest_conversation_id INTEGER,
    compliance_score        DOUBLE PRECISION,
    sentiment_score         DOUBLE PRECISION,
    professionalism_score   DOUBLE PRECISION,
    script_adherence_score  DOUBLE PRECISION,
    distinctive_phrases     JSONB,
    is_clean                BOOLEAN DEFAULT TRUE,
    promoted                BOOLEAN DEFAULT FALSE,
    promoted_at             TIMESTAMPTZ,
    rejected                BOOLEAN DEFAULT FALSE,
    rejected_reason         TEXT,
    capture_reason          TEXT DEFAULT 'novelty',
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(embedding_hash)
);
CREATE INDEX IF NOT EXISTS idx_sem_cand_promoted ON semantic_candidates(promoted, rejected, created_at);
CREATE INDEX IF NOT EXISTS idx_sem_cand_conv     ON semantic_candidates(conversation_id);

-- ── audit_overrides (Phase B+ — tracks manager Groq rescores) ────────────────
CREATE TABLE IF NOT EXISTS audit_overrides (
    id                  SERIAL PRIMARY KEY,
    conversation_id     INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    ml_result           JSONB NOT NULL,
    groq_result         JSONB NOT NULL,
    requested_by        TEXT,
    requested_at        TIMESTAMPTZ DEFAULT NOW(),
    disagreement_summary TEXT
);

-- ── validation_log (human validation history) ───────────────────────────────
CREATE TABLE IF NOT EXISTS validation_log (
    id              SERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES accounts(id),
    agent_name      TEXT NOT NULL,
    contact_name    TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    score_id        INTEGER,
    status          TEXT NOT NULL, -- 'valid', 'invalid', 'disputed'
    validated_by    TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validation_agent ON validation_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_validation_conv  ON validation_log(conversation_id);

-- ── prefilter_decisions additions ────────────────────────────────────────────
-- conversation_scores.source tracks which tier/provider produced the result
-- Values: 'groq' | 'prefilter_t1' | 'prefilter_t2' | 'prefilter_t3' | 'prefilter_t4' | 'groq_override'
ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS source TEXT;

-- ── tool_access (dashboard login allowlist) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_access (
    id        SERIAL PRIMARY KEY,
    email     TEXT NOT NULL UNIQUE,
    added_by  TEXT NOT NULL DEFAULT 'system',
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_tool_access_email ON tool_access(LOWER(email));
