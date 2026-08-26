-- Migration 007: data-integrity constraints from the 2026-08-25 deep code review
--
-- Addresses F7 (re-ingestion duplicated every conversation on every run),
-- F29 (contacts had no unique constraint and was inserted check-then-act) and
-- the F12 FK that aborted agent deletion halfway.
--
-- NOT folded into schema.sql on purpose: schema.sql executes on EVERY boot, and
-- these statements fail on a database that still holds duplicates. Run this once,
-- deliberately, after taking a backup.
--
--   psql "$DATABASE_URL" -f database/migrations/007_data_integrity.sql
--
-- The application code works with or without this migration applied, so there is
-- no deploy-ordering hazard in either direction.

BEGIN;

-- ── 1. De-duplicate contacts, then enforce uniqueness ────────────────────────
-- Readers all normalise with LOWER(TRIM(name)); writers used an exact match, so
-- "John Smith" and "john smith" became two rows that the dashboard then silently
-- collapsed — dropping one of the two conversations from view.

-- Re-point every conversation at the lowest-id row for its normalised name.
WITH canonical AS (
    SELECT id,
           MIN(id) OVER (PARTITION BY LOWER(TRIM(name))) AS keep_id
    FROM contacts
)
UPDATE conversations c
   SET contact_id = canonical.keep_id
  FROM canonical
 WHERE c.contact_id = canonical.id
   AND canonical.id <> canonical.keep_id;

-- Remove the now-unreferenced duplicates.
DELETE FROM contacts c
 WHERE EXISTS (
     SELECT 1 FROM contacts c2
      WHERE LOWER(TRIM(c2.name)) = LOWER(TRIM(c.name))
        AND c2.id < c.id
 )
 AND NOT EXISTS (SELECT 1 FROM conversations cv WHERE cv.contact_id = c.id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_name_lower
    ON contacts (LOWER(TRIM(name)));

-- ── 2. One conversation per (agent, contact, day) ────────────────────────────
-- save_extraction() INSERTed unconditionally and mark_chat_audited() was never
-- called, so re-running an agent appended a complete duplicate set every time.
-- Keep the most recently extracted row in each group.
DELETE FROM conversations a
 USING conversations b
 WHERE a.agent_id   = b.agent_id
   AND a.contact_id = b.contact_id
   AND a.audit_date = b.audit_date
   AND (a.extracted_at, a.id) < (b.extracted_at, b.id);

ALTER TABLE conversations
    DROP CONSTRAINT IF EXISTS uq_conversations_agent_contact_day;
ALTER TABLE conversations
    ADD CONSTRAINT uq_conversations_agent_contact_day
    UNIQUE (agent_id, contact_id, audit_date);

-- ── 3. Let agent deletion cascade instead of aborting ────────────────────────
-- validation_log.agent_id is NOT NULL REFERENCES accounts(id) with no ON DELETE
-- action. DELETE /api/agents/{id} never deleted from it, so deleting an agent
-- with validation rows destroyed all their history and then 500'd with the
-- account row still present. The handler now deletes explicitly; this makes the
-- schema enforce it too.
ALTER TABLE validation_log
    DROP CONSTRAINT IF EXISTS validation_log_agent_id_fkey;
ALTER TABLE validation_log
    ADD CONSTRAINT validation_log_agent_id_fkey
    FOREIGN KEY (agent_id) REFERENCES accounts(id) ON DELETE CASCADE;

ALTER TABLE flag_feedback
    DROP CONSTRAINT IF EXISTS flag_feedback_agent_id_fkey;
ALTER TABLE flag_feedback
    ADD CONSTRAINT flag_feedback_agent_id_fkey
    FOREIGN KEY (agent_id) REFERENCES accounts(id) ON DELETE CASCADE;

-- ── 4. Index supporting the hot /api/agents path (F16) ───────────────────────
CREATE INDEX IF NOT EXISTS idx_conversations_active
    ON conversations (agent_id, extracted_at DESC)
    WHERE is_archived = FALSE;

COMMIT;

-- Sanity checks — run these after COMMIT and confirm all three return 0:
--   SELECT COUNT(*) FROM (SELECT LOWER(TRIM(name)) n FROM contacts
--                          GROUP BY 1 HAVING COUNT(*) > 1) d;
--   SELECT COUNT(*) FROM (SELECT agent_id, contact_id, audit_date FROM conversations
--                          GROUP BY 1,2,3 HAVING COUNT(*) > 1) d;
--   SELECT COUNT(*) FROM conversations c
--    WHERE NOT EXISTS (SELECT 1 FROM contacts ct WHERE ct.id = c.contact_id);
