-- 006_assignment_periods.sql
-- Time-ranged account ownership, replacing the day-grained account_assignments
-- model. A mid-day shuffle closes the open period and opens a new one at the
-- same instant, so every message can be attributed to whoever owned the account
-- at the minute it was sent.
--
-- This file is a standalone copy for reference — the same statements live in
-- schema.sql, which dashboard/app.py executes on every boot (idempotent).

DO $do$
BEGIN
    CREATE EXTENSION IF NOT EXISTS btree_gist;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'btree_gist unavailable (%) — overlap constraint will be skipped', SQLERRM;
END $do$;

-- ── assignment_saves (one row per "Save All" click) ──────────────────────────
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

-- ── assignment_periods (who owned an account, and when) ──────────────────────
-- account_email / texter_name are TEXT (not FKs) on purpose: this is an audit
-- record. Deleting a texter from the roster or an account from `accounts` must
-- never erase who owned what — every other historical table here keys on the
-- same two text columns.
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

-- Core invariant #1: at most ONE open period per account. Needs no extension,
-- so it holds even where btree_gist is unavailable.
CREATE UNIQUE INDEX IF NOT EXISTS idx_asgn_periods_one_open
    ON assignment_periods(account_email) WHERE ended_at IS NULL;

-- Core invariant #2: two texters can never own one account at the same instant.
-- Requires btree_gist for the `account_email WITH =` operand. Added defensively
-- so a managed host that blocks the extension degrades instead of failing boot.
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

-- ── assignment_audit_log (append-only; never updated) ────────────────────────
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

-- ── texter_at(): point-in-time ownership resolver ────────────────────────────
CREATE OR REPLACE FUNCTION texter_at(p_account_email TEXT, p_ts TIMESTAMPTZ)
RETURNS TEXT
LANGUAGE sql STABLE AS $fn$
    SELECT texter_name
      FROM assignment_periods
     WHERE account_email = p_account_email
       AND period @> p_ts
     LIMIT 1;
$fn$;

-- ── message attribution ──────────────────────────────────────────────────────
-- Resolved once at ingest and stored, so an audit stays reproducible and a
-- corrected period can be replayed over the affected range.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS texter_name TEXT;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS attribution TEXT;  -- exact | inferred | unassigned

CREATE INDEX IF NOT EXISTS idx_messages_texter
    ON messages(texter_name) WHERE texter_name IS NOT NULL;
