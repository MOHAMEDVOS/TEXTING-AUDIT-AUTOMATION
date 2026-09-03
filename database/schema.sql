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

-- One conversation per (agent, contact, day). Without it, re-running an audit
-- appends a complete duplicate set of conversations, messages and scores every
-- time — production had 25,260 duplicate rows before migration 010 cleaned them.
-- save_extraction()'s ON CONFLICT clause names this constraint, and simply does
-- nothing until it exists.
--
-- Guarded, and deliberately not a bare ALTER: the dashboard executes this whole
-- file on every boot, and ADD CONSTRAINT throws on a database that still holds
-- duplicates — which would take the dashboard down at startup instead of just
-- leaving the constraint unapplied. A database with duplicates is left alone
-- for database/migrations/010_dedupe_conversations.sql to clean up first.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_conversations_agent_contact_day')
       AND NOT EXISTS (SELECT 1 FROM conversations a JOIN conversations b
                          ON a.agent_id   = b.agent_id
                         AND a.contact_id = b.contact_id
                         AND a.audit_date = b.audit_date
                         AND a.id <> b.id)
    THEN
        ALTER TABLE conversations
            ADD CONSTRAINT uq_conversations_agent_contact_day
            UNIQUE (agent_id, contact_id, audit_date);
    END IF;
END $$;

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

-- Migration 004 (folded in): link each feedback row to its source conversation.
-- Self-heals existing deployments where the table predates this column.
ALTER TABLE flag_feedback ADD COLUMN IF NOT EXISTS conversation_id INTEGER
    REFERENCES conversations(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_flag_feedback_conversation ON flag_feedback(conversation_id);

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

-- ── Per-flag-type breakdown of total_issues (additive) ────────────────────────
-- All three counters mean VALIDATED flags only — an auditor's Mark Valid click
-- is what puts a number here (database/trend_counts.py). total_issues sums
-- validated flag instances across a day's conversations, so a conversation
-- with both a confirmed Late Response and a confirmed Wrong Label adds 2.
-- These two columns break that same total down by flag type so the Trends
-- table can show what's driving it — they can therefore sum to less than
-- total_issues (other flag types exist) or overlap with it (a convo counted
-- in both). Because they share one gate, a row can never show a red
-- total_issues tint next to two zero flag columns; that mismatch was the
-- symptom of the raw-count write ai/scorer.py used to do.
ALTER TABLE trend_snapshots ADD COLUMN IF NOT EXISTS late_response_flags INTEGER;
ALTER TABLE trend_snapshots ADD COLUMN IF NOT EXISTS wrong_label_flags   INTEGER;

-- Existing rows land NULL from the ADD COLUMN; normalize them to 0 so the
-- dashboard does not have to distinguish "never computed" from "nothing
-- validated" — under the validation gate those mean the same thing.
--
-- This block used to try to backfill real counts here, joining
-- `c.audit_date = ts.audit_date`. That is the SCRAPE day against a row keyed
-- by the CONVERSATION day (the department audits a day behind), so it matched
-- nothing, COALESCE wrote 0, and the IS NULL guard then made that wrong 0
-- permanent. It was also un-gated by validation_log, which the columns
-- require. Real counts come from database/trend_counts.py only — on a Mark
-- Valid click, or via scripts/repair_trend_counts.py for a bulk repair.
UPDATE trend_snapshots
   SET late_response_flags = COALESCE(late_response_flags, 0),
       wrong_label_flags   = COALESCE(wrong_label_flags, 0)
 WHERE late_response_flags IS NULL OR wrong_label_flags IS NULL;

-- ── ML pre-filter telemetry (folded in from migration 001) ───────────────────
-- These were ONLY ever in database/migrations/001_*.sql, which is applied by
-- hand. schema.sql is what actually runs on every boot, so on any database
-- provisioned the documented way these tables never existed and every
-- prefilter decision insert failed silently — leaving shadow mode, the eval
-- harness, and the FALSE-CLEAN promotion gate with no data at all
-- (deep review F8).
CREATE TABLE IF NOT EXISTS prefilter_decisions (
    id               BIGSERIAL PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tier_hit         SMALLINT NOT NULL CHECK (tier_hit BETWEEN 1 AND 4),
    decision         TEXT CHECK (decision IN ('short_circuit', 'escalate')),
    confidence       REAL,
    predicted_scores JSONB,
    groq_scores      JSONB,
    agreement        REAL,
    shadow_mode      BOOLEAN NOT NULL DEFAULT TRUE,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Repair path: migration 002_prefilter.sql (now deleted) declared this table
-- with an INCOMPATIBLE shape — short_circuited/predicted/groq_actual and no
-- decision/predicted_scores/notes. Both used CREATE TABLE IF NOT EXISTS, so
-- whichever ran first won and the other silently no-opped. These ALTERs make a
-- 002-shaped table accept the inserts the code actually issues, and are no-ops
-- on a correctly-shaped one.
ALTER TABLE prefilter_decisions ADD COLUMN IF NOT EXISTS decision         TEXT;
ALTER TABLE prefilter_decisions ADD COLUMN IF NOT EXISTS predicted_scores JSONB;
ALTER TABLE prefilter_decisions ADD COLUMN IF NOT EXISTS groq_scores      JSONB;
ALTER TABLE prefilter_decisions ADD COLUMN IF NOT EXISTS agreement        REAL;
ALTER TABLE prefilter_decisions ADD COLUMN IF NOT EXISTS notes            TEXT;

CREATE INDEX IF NOT EXISTS idx_prefilter_decisions_convo
    ON prefilter_decisions(conversation_id);
CREATE INDEX IF NOT EXISTS idx_prefilter_decisions_tier
    ON prefilter_decisions(tier_hit);
CREATE INDEX IF NOT EXISTS idx_prefilter_decisions_created
    ON prefilter_decisions(created_at);

-- Embedding cache: skip re-embedding the same conversation across runs.
CREATE TABLE IF NOT EXISTS conversation_embeddings (
    conversation_id INTEGER PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    embedding       REAL[] NOT NULL,
    model_name      TEXT NOT NULL,
    text_hash       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE conversation_embeddings ADD COLUMN IF NOT EXISTS text_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_conv_embeddings_model
    ON conversation_embeddings(model_name);

-- Drop the CHECK migration 001 used to install: its value list predates
-- 'groq_override' and the Groq decommission, so it rejects legitimate writes.
ALTER TABLE conversation_scores DROP CONSTRAINT IF EXISTS conversation_scores_source_check;
CREATE INDEX IF NOT EXISTS idx_conv_scores_source ON conversation_scores(source);


-- ── api_keys — REMOVED (deep review F36) ─────────────────────────────────────
-- Held the Groq shared key pool. Groq was decommissioned; the scoring pipeline
-- is ML-only and nothing reads this table. No longer created on fresh installs.
-- Existing databases still have the table; drop it manually once you have
-- confirmed no keys in it are needed elsewhere:
--     DROP TABLE IF EXISTS api_keys;

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

-- Migration 008 (folded in): validate per FLAG, not per conversation. NULL
-- means a legacy row from before this column existed — it covers every flag
-- on the conversation, so historical counts don't shift. Self-heals existing
-- deployments where the table predates this column.
ALTER TABLE validation_log ADD COLUMN IF NOT EXISTS flag_text TEXT;

-- ── validation_flag_key: how a stored validation matches a live flag ────────
-- A validation row stores the flag's text as it read at click time, and every
-- reader used to match it with plain LOWER() equality. Re-auditing a day can
-- reword the same finding — ai/scorer.py appends
-- DEFENSIBLE_ALTERNATIVE_SUFFIX to a "Wrong label:" flag when the texter's
-- label is also defensible, and prefilter wrong-label flags carry annotations
-- like "(contact said: 'six million')". The stored text then no longer equals
-- the live text, the auditor's click stops counting, and the conversation
-- silently drops out of the trend counters with nothing to show it happened.
--
-- The key is the flag's stem: lowercased, with ONE trailing parenthetical
-- annotation removed, whitespace collapsed, and trailing periods trimmed. Two
-- flags on one conversation that differ only inside those parentheses
-- deliberately collapse to the same key — they are the same finding with
-- different commentary, so one click covers both.
--
-- Kept separate from ai/prefilter/_guards.py's canon_flag_text, which feeds
-- T4 whitelist matching and learned-rule suppression: that one must not start
-- stripping annotations. ai/prefilter/_guards.py::validation_flag_key and
-- dashboard/views/index.html::validationFlagKey mirror THIS definition; the
-- legacy '*' sentinel must survive it unchanged.
CREATE OR REPLACE FUNCTION validation_flag_key(txt TEXT) RETURNS TEXT AS $fn$
    SELECT rtrim(
             regexp_replace(
               regexp_replace(lower(btrim(COALESCE(txt, ''))), '\s*\([^()]*\)\s*$', ''),
               '\s+', ' ', 'g'),
             ' .');
$fn$ LANGUAGE sql IMMUTABLE;

-- ── Manual flag validation (opt-in gate) ────────────────────────────────────
-- validation_log is now the authoritative "this flag counts" record. A flag
-- reaches the Trend/Detailed dashboards ONLY when an auditor has explicitly
-- clicked Valid on it, which writes a status='valid' row here. Nothing is
-- ever deleted from conversation_scores.red_flags — validation is a
-- non-destructive overlay, so it stays reversible.
--
-- Migration 009 (folded in): key validation on the CONVERSATION, not the
-- contact's name. The old key was (agent_id, LOWER(contact_name), flag_text)
-- and every reader matched the same way, so a row confirmed on one
-- conversation silently marked EVERY other conversation the same account had
-- with the same contact name as validated too — a second conversation with a
-- returning contact on a later day, or a duplicate row left behind by
-- re-running an audit for the same day (uq_conversations_agent_contact_day is
-- absent on databases provisioned from this file, so re-runs append copies).
-- Auditors saw conversations marked valid that they had never clicked, on
-- texters they had never reviewed, because account ownership moves between
-- texters while contact names do not.
--
-- Partial index: rows whose conversation was deleted (FK is ON DELETE SET
-- NULL) keep NULL here. They match no conversation under the new readers, so
-- they are inert — excluded from the key rather than deleted, because
-- validation history is never destroyed by this file.
-- COALESCE(flag_text, '') because Postgres treats NULL as distinct from NULL
-- in a unique index — without it, every legacy (pre-flag_text) row could
-- collide-free duplicate instead of being the one blanket slot per conversation.
-- Keyed on LOWER(), not validation_flag_key(): re-validating a flag a re-audit
-- has since reworded writes a second row rather than updating the first. Both
-- rows carry the same key, and every reader is an EXISTS, so the flag still
-- counts exactly once — narrowing the index would mean rewriting it over live
-- rows that may already collide under the looser key.
DELETE FROM validation_log a USING validation_log b
 WHERE a.id < b.id AND a.agent_id = b.agent_id
   AND a.conversation_id IS NOT NULL
   AND a.conversation_id = b.conversation_id
   AND COALESCE(LOWER(a.flag_text), '') = COALESCE(LOWER(b.flag_text), '');
DROP INDEX IF EXISTS idx_validation_unique;
CREATE UNIQUE INDEX IF NOT EXISTS idx_validation_unique_conv
    ON validation_log(agent_id, conversation_id, COALESCE(LOWER(flag_text), ''))
 WHERE conversation_id IS NOT NULL;

-- One-time historical backfill. Before the cutover a flag was valid BY DEFAULT
-- and dismissals physically deleted the flag from red_flags, so marking every
-- still-flagged pre-cutover conversation 'valid' reproduces the old numbers
-- exactly and leaves history untouched.
--
-- The hardcoded cutover date makes this permanently idempotent: the dashboard
-- re-runs this whole file on every boot, and the bounded date range can never
-- auto-validate new work. Anything audited on/after the cutover requires a
-- manual Valid click.
--
-- Two callers run this file, and they disagree — know which one you are
-- reasoning about:
--   * dashboard/app.py startup executes it UNCONDITIONALLY on every boot, so
--     every Railway deploy and container restart replays the whole file. This
--     is the path that matters in production (the Procfile boots the dashboard).
--   * database/db.py::initialize() runs it only when `conversations` is absent,
--     so the CLI audit runner skips it on an existing database.
-- Everything here must therefore be re-runnable, which is why the DDL is all
-- IF NOT EXISTS and the backfill ends in ON CONFLICT DO NOTHING.
--
-- What this file does NOT carry is anything only defined in a numbered
-- migration — migration 007's uq_conversations_agent_contact_day is absent
-- here, which is why it is still missing in production however many times the
-- dashboard reboots. A change that must reach live databases belongs in BOTH
-- this file and database/migrations/.
INSERT INTO validation_log (agent_id, agent_name, contact_name, conversation_id,
                            status, validated_by, notes)
SELECT c.agent_id, COALESCE(a.name, ''), ct.name, c.id, 'valid', 'system-backfill',
       'Pre-cutover audit, auto-validated to preserve historical counts'
  FROM conversations c
  JOIN accounts a  ON a.id  = c.agent_id
  JOIN contacts ct ON ct.id = c.contact_id
  JOIN LATERAL (
      SELECT red_flags, label_correct, label_assigned, label_should_be
        FROM conversation_scores cs2
       WHERE cs2.conversation_id = c.id
       ORDER BY cs2.id DESC LIMIT 1
  ) cs ON TRUE
 WHERE c.audit_date < DATE '2026-08-27'          -- CUTOVER DATE
   AND (
        jsonb_array_length(cs.red_flags::jsonb) > 0
        OR (cs.label_correct = false
            AND cs.label_assigned IS DISTINCT FROM cs.label_should_be)
   )
-- One row per pre-cutover CONVERSATION, not one per agent+contact. Validation
-- is keyed on conversation_id now, so collapsing to the newest conversation per
-- contact here would leave every older pre-cutover conversation unvalidated and
-- drop it out of the historical counts this backfill exists to preserve.
ON CONFLICT DO NOTHING;

-- ── flagged_conversation_reviews (manager reviewed flagged convos) ───────────
CREATE TABLE IF NOT EXISTS flagged_conversation_reviews (
    id              SERIAL PRIMARY KEY,
    agent_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    contact_name    TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    reviewed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(agent_id, contact_name)
);
CREATE INDEX IF NOT EXISTS idx_flagged_reviews_agent ON flagged_conversation_reviews(agent_id);

-- ── prefilter_decisions additions ────────────────────────────────────────────
-- conversation_scores.source tracks which tier/provider produced the result
-- Values: 'groq' | 'prefilter_t1' | 'prefilter_t2' | 'prefilter_t3' | 'prefilter_t4' | 'groq_override'
ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS source TEXT;

-- ── Phase 1: per-flag explainability (additive, backward-compatible) ─────────
-- flag_details mirrors red_flags but carries rich, rule-assigned metadata per
-- flag: {flag_id, flag_text, severity, confidence, confidence_tier, evidence,
-- explanation, coaching, source, origin}. red_flags (list[str]) stays the
-- canonical identity layer — flag_details is keyed by flag_text and is NULL on
-- legacy rows (UI falls back to plain strings).
ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS flag_details   JSONB;
ALTER TABLE conversation_scores ADD COLUMN IF NOT EXISTS prompt_version TEXT;
-- Note: conversation_scores.model_used already stores the model id (model version).

-- Needs-Review queue: GIN index supports the containment query
--   flag_details @> '[{"confidence_tier":"needs_review"}]'
CREATE INDEX IF NOT EXISTS idx_conv_scores_flag_details
    ON conversation_scores USING GIN(flag_details);

-- ── Phase 1: structured flag feedback (supports Phase 3 learning) ────────────
ALTER TABLE flag_feedback ADD COLUMN IF NOT EXISTS flag_id         TEXT;
ALTER TABLE flag_feedback ADD COLUMN IF NOT EXISTS confidence      DOUBLE PRECISION;
ALTER TABLE flag_feedback ADD COLUMN IF NOT EXISTS confidence_tier TEXT;
ALTER TABLE flag_feedback ADD COLUMN IF NOT EXISTS prompt_version  TEXT;
-- correctness: 'correct' | 'incorrect' | 'partial' | 'unclear' (default 'incorrect'
-- preserves today's behaviour where any feedback row = a rejected flag).
ALTER TABLE flag_feedback ADD COLUMN IF NOT EXISTS correctness     TEXT DEFAULT 'incorrect';

-- ── tool_access (dashboard login allowlist) ───────────────────────────────────
CREATE TABLE IF NOT EXISTS tool_access (
    id        SERIAL PRIMARY KEY,
    email     TEXT NOT NULL UNIQUE,
    added_by  TEXT NOT NULL DEFAULT 'system',
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_tool_access_email ON tool_access(LOWER(email));

-- ── custom_labels (for filtering in scraping UI) ───────────────────────────────
CREATE TABLE IF NOT EXISTS custom_labels (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── blacklist_labels (labels that cause a convo to be skipped) ───────────────
-- skip_mode = 'any'  → skip if this label appears anywhere in the label list
-- skip_mode = 'only' → skip only if ALL labels are in this set (e.g. "New Lead" alone)
CREATE TABLE IF NOT EXISTS blacklist_labels (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    skip_mode  TEXT NOT NULL DEFAULT 'any' CHECK (skip_mode IN ('any', 'only')),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
-- Seed built-in defaults ONLY on a fresh install.
-- ON CONFLICT DO NOTHING is idempotent against duplicates but NOT against
-- deletion: this file is executed on every boot, so a label a user removed via
-- the UI silently reappeared after the next redeploy (deep review F13).
-- The NOT EXISTS guard makes the seed a true first-run-only operation.
INSERT INTO blacklist_labels (name, skip_mode)
SELECT * FROM (VALUES
    ('Extra',    'any'),
    ('New Lead', 'only')
) AS seed(name, skip_mode)
WHERE NOT EXISTS (SELECT 1 FROM blacklist_labels);

-- ── Time-ranged account ownership (migration 006) ────────────────────────────
-- Replaces the day-grained account_assignments model for attribution purposes.
-- A mid-day shuffle closes the open period and opens a new one at the same
-- instant, so every message resolves to whoever owned the account at the minute
-- it was sent. account_assignments is still written for backward compatibility.
DO $do$
BEGIN
    CREATE EXTENSION IF NOT EXISTS btree_gist;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'btree_gist unavailable (%) — overlap constraint will be skipped', SQLERRM;
END $do$;

-- One row per "Save All" click.
CREATE TABLE IF NOT EXISTS assignment_saves (
    id          BIGSERIAL PRIMARY KEY,
    saved_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    saved_by    TEXT NOT NULL DEFAULT 'unknown',   -- Google OAuth session email
    target_date DATE NOT NULL,                     -- the DATE picker value
    changed     INTEGER NOT NULL DEFAULT 0,
    unchanged   INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'dashboard'
);

CREATE INDEX IF NOT EXISTS idx_asgn_saves_date ON assignment_saves(target_date);
CREATE INDEX IF NOT EXISTS idx_asgn_saves_at   ON assignment_saves(saved_at);

-- Who owned an account, and when. account_email / texter_name are TEXT rather
-- than FKs on purpose: this is an audit record, so deleting a texter from the
-- roster or an account from `accounts` must never erase ownership history.
CREATE TABLE IF NOT EXISTS assignment_periods (
    id            BIGSERIAL PRIMARY KEY,
    account_email TEXT NOT NULL,
    texter_name   TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    ended_at      TIMESTAMPTZ,                     -- NULL = still the owner
    period        TSTZRANGE GENERATED ALWAYS AS
                    (tstzrange(started_at, ended_at, '[)')) STORED,
    started_by    TEXT,
    ended_by      TEXT,
    save_id       BIGINT REFERENCES assignment_saves(id) ON DELETE SET NULL,
    source        TEXT NOT NULL DEFAULT 'dashboard'
                    CHECK (source IN ('dashboard', 'api', 'backfill', 'system')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT assignment_periods_valid_range
        CHECK (ended_at IS NULL OR ended_at > started_at)
);

CREATE INDEX IF NOT EXISTS idx_asgn_periods_email  ON assignment_periods(account_email);
CREATE INDEX IF NOT EXISTS idx_asgn_periods_texter ON assignment_periods(texter_name);
CREATE INDEX IF NOT EXISTS idx_asgn_periods_start  ON assignment_periods(started_at);

-- Invariant #1: at most ONE open period per account. Needs no extension.
CREATE UNIQUE INDEX IF NOT EXISTS idx_asgn_periods_one_open
    ON assignment_periods(account_email) WHERE ended_at IS NULL;

-- Invariant #2: two texters can never own one account at the same instant.
-- Requires btree_gist; added defensively so a restricted host degrades to
-- invariant #1 + application-level ordering instead of failing boot.
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'assignment_periods_no_overlap'
    ) THEN
        BEGIN
            ALTER TABLE assignment_periods
                ADD CONSTRAINT assignment_periods_no_overlap
                EXCLUDE USING gist (account_email WITH =, period WITH &&);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'assignment_periods: overlap constraint skipped (%)', SQLERRM;
        END;
    END IF;
END $do$;

-- Append-only change log; never updated.
-- effective_at = when the change applies (the shuffle instant).
-- performed_at = when it was recorded. They differ on backdated corrections,
-- which is the only way to tell a real 8:33 PM shuffle from one written at 11 PM.
CREATE TABLE IF NOT EXISTS assignment_audit_log (
    id            BIGSERIAL PRIMARY KEY,
    period_id     BIGINT REFERENCES assignment_periods(id) ON DELETE SET NULL,
    save_id       BIGINT REFERENCES assignment_saves(id) ON DELETE SET NULL,
    account_email TEXT NOT NULL,
    action        TEXT NOT NULL
                    CHECK (action IN ('open', 'close', 'shuffle', 'unassign',
                                      'correction', 'backfill')),
    from_texter   TEXT,
    to_texter     TEXT,
    effective_at  TIMESTAMPTZ NOT NULL,
    performed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    performed_by  TEXT,
    reason        TEXT
);

CREATE INDEX IF NOT EXISTS idx_asgn_log_email ON assignment_audit_log(account_email);
CREATE INDEX IF NOT EXISTS idx_asgn_log_eff   ON assignment_audit_log(effective_at);
CREATE INDEX IF NOT EXISTS idx_asgn_log_save  ON assignment_audit_log(save_id);

-- Point-in-time ownership resolver.
CREATE OR REPLACE FUNCTION texter_at(p_account_email TEXT, p_ts TIMESTAMPTZ)
RETURNS TEXT
LANGUAGE sql STABLE AS $fn$
    SELECT texter_name
      FROM assignment_periods
     WHERE account_email = p_account_email
       AND period @> p_ts
     LIMIT 1;
$fn$;

-- Interval ownership resolver.
-- texter_at() answers for a single instant (LIMIT 1). A flag that measures a
-- WAIT (F17) has two ends, and if the account changed hands in between the
-- delay belongs to both texters — so this returns every holder that overlaps
-- the range, with their slice clipped to it. The caller converts each slice to
-- shift minutes (ai/shift.py) rather than wall-clock ones.
-- Left texter_at() alone: other call sites depend on it.
CREATE OR REPLACE FUNCTION texters_in_range(p_account_email TEXT,
                                            p_from TIMESTAMPTZ,
                                            p_to   TIMESTAMPTZ)
RETURNS TABLE(texter_name TEXT, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ)
LANGUAGE sql STABLE AS $fn$
    SELECT ap.texter_name,
           GREATEST(ap.started_at, p_from),
           CASE WHEN ap.ended_at IS NULL THEN p_to
                ELSE LEAST(ap.ended_at, p_to) END
      FROM assignment_periods ap
     -- LEAST/GREATEST keep tstzrange() from raising on a NULL or inverted
     -- range; the p_to > p_from test is what actually rejects one.
     WHERE ap.account_email = p_account_email
       AND p_to > p_from
       AND ap.period && tstzrange(LEAST(p_from, p_to), GREATEST(p_from, p_to), '[)')
     ORDER BY ap.started_at;
$fn$;

-- Message attribution — resolved once at ingest and stored, so an audit stays
-- reproducible and a corrected period can be replayed over the affected range.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS texter_name TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS attribution TEXT;  -- exact | inferred | unassigned

CREATE INDEX IF NOT EXISTS idx_messages_texter
    ON messages(texter_name) WHERE texter_name IS NOT NULL;

-- ── Sequence repair (safe on every startup after restore/seed) ───────────────
-- Ensures SERIAL sequences are always ahead of existing rows.
SELECT setval(pg_get_serial_sequence('accounts',           'id'), COALESCE(MAX(id), 0) + 1, false) FROM accounts;
SELECT setval(pg_get_serial_sequence('texters',            'id'), COALESCE(MAX(id), 0) + 1, false) FROM texters;
SELECT setval(pg_get_serial_sequence('contacts',           'id'), COALESCE(MAX(id), 0) + 1, false) FROM contacts;
SELECT setval(pg_get_serial_sequence('conversations',      'id'), COALESCE(MAX(id), 0) + 1, false) FROM conversations;
SELECT setval(pg_get_serial_sequence('messages',           'id'), COALESCE(MAX(id), 0) + 1, false) FROM messages;
SELECT setval(pg_get_serial_sequence('extractions',        'id'), COALESCE(MAX(id), 0) + 1, false) FROM extractions;
SELECT setval(pg_get_serial_sequence('audit_scores',       'id'), COALESCE(MAX(id), 0) + 1, false) FROM audit_scores;
SELECT setval(pg_get_serial_sequence('conversation_scores','id'), COALESCE(MAX(id), 0) + 1, false) FROM conversation_scores;
SELECT setval(pg_get_serial_sequence('audited_chats',      'id'), COALESCE(MAX(id), 0) + 1, false) FROM audited_chats;
SELECT setval(pg_get_serial_sequence('flag_feedback',      'id'), COALESCE(MAX(id), 0) + 1, false) FROM flag_feedback;
SELECT setval(pg_get_serial_sequence('session_events',     'id'), COALESCE(MAX(id), 0) + 1, false) FROM session_events;
SELECT setval(pg_get_serial_sequence('account_assignments','id'), COALESCE(MAX(id), 0) + 1, false) FROM account_assignments;
SELECT setval(pg_get_serial_sequence('trend_snapshots',    'id'), COALESCE(MAX(id), 0) + 1, false) FROM trend_snapshots;
SELECT setval(pg_get_serial_sequence('semantic_candidates','id'), COALESCE(MAX(id), 0) + 1, false) FROM semantic_candidates;
SELECT setval(pg_get_serial_sequence('audit_overrides',    'id'), COALESCE(MAX(id), 0) + 1, false) FROM audit_overrides;
SELECT setval(pg_get_serial_sequence('validation_log',     'id'), COALESCE(MAX(id), 0) + 1, false) FROM validation_log;
SELECT setval(pg_get_serial_sequence('tool_access',        'id'), COALESCE(MAX(id), 0) + 1, false) FROM tool_access;
SELECT setval(pg_get_serial_sequence('custom_labels',       'id'), COALESCE(MAX(id), 0) + 1, false) FROM custom_labels;
SELECT setval(pg_get_serial_sequence('blacklist_labels',    'id'), COALESCE(MAX(id), 0) + 1, false) FROM blacklist_labels;
