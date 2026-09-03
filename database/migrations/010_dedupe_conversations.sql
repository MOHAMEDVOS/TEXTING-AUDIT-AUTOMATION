-- Migration 010: remove duplicate conversations and enforce one per
-- (agent, contact, day) — the work migration 007 §2 describes, done in an order
-- that does not destroy validations.
--
-- WHY THIS EXISTS SEPARATELY FROM 007
-- validation_log.conversation_id is ON DELETE SET NULL, and since migration 009
-- every reader matches a validation through that conversation_id. Running 007's
-- bare DELETE today would leave 2,578 validation rows with a NULL
-- conversation_id: still present, matching no conversation, silently no longer
-- counting — and invisible to the auditor who confirmed them. flag_feedback and
-- flagged_conversation_reviews have the same SET NULL exposure.
--
-- So every dependent row is re-pointed at the surviving conversation BEFORE any
-- delete, and the re-pointing is what makes the delete safe rather than the
-- delete being safe on its own.
--
-- Run once, deliberately, after taking a backup:
--   psql "$DATABASE_URL" -f database/migrations/010_dedupe_conversations.sql
--
-- Measured against production on 2026-09-03 before running:
--   330,319 conversations, 22,130 duplicate rows deleted (330,319 -> 308,189)
--   191,680 messages and 22,123 scores cascade with them
--   244,540 contacts -> 244,310 after the name-case merge
--   2,578 validations, 12 flag_feedback and 28 reviews re-pointed
--   2,194 validation rows collapse into their surviving twin (34,870 -> 32,676)
--
-- Verified by running this whole file against production inside a transaction
-- and rolling back: 0 validations left orphaned, and of the surviving
-- conversations only 2 lose validated status — both carry zero red flags, so no
-- counter and no agent's record moves. Those 2 exist because a duplicate group
-- is keyed on audit_date (the scrape day) while validation coverage is keyed on
-- convo_date (the day the conversation happened), and one scrape day can carry
-- two conversation days for the same contact.

BEGIN;

-- ── 1. De-duplicate contacts first (migration 007 §1) ────────────────────────
-- Readers normalise with LOWER(TRIM(name)); the old writer matched exactly, so
-- "John Smith" and "john smith" became two rows. This runs BEFORE the
-- conversation dedupe on purpose: merging two contacts can put two
-- conversations onto the same (agent, contact, day), and step 3 must see them.
WITH canonical AS (
    SELECT id, MIN(id) OVER (PARTITION BY LOWER(TRIM(name))) AS keep_id
    FROM contacts
)
UPDATE conversations c
   SET contact_id = canonical.keep_id
  FROM canonical
 WHERE c.contact_id = canonical.id
   AND canonical.id <> canonical.keep_id;

DELETE FROM contacts c
 WHERE EXISTS (SELECT 1 FROM contacts c2
                WHERE LOWER(TRIM(c2.name)) = LOWER(TRIM(c.name)) AND c2.id < c.id)
   AND NOT EXISTS (SELECT 1 FROM conversations cv WHERE cv.contact_id = c.id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_name_lower
    ON contacts (LOWER(TRIM(name)));

-- ── 2. Re-point every dependent row onto the surviving conversation ──────────
-- The survivor is the most recently extracted row in each group, matching the
-- readers, which already collapse to the highest id per contact.
CREATE TEMP TABLE _dupe_map ON COMMIT DROP AS
SELECT a.id AS doomed_id,
       (SELECT b.id FROM conversations b
         WHERE b.agent_id   = a.agent_id
           AND b.contact_id = a.contact_id
           AND b.audit_date = a.audit_date
         ORDER BY b.extracted_at DESC, b.id DESC
         LIMIT 1) AS keep_id
  FROM conversations a
 WHERE EXISTS (SELECT 1 FROM conversations b
                WHERE a.agent_id   = b.agent_id
                  AND a.contact_id = b.contact_id
                  AND a.audit_date = b.audit_date
                  AND (a.extracted_at, a.id) < (b.extracted_at, b.id));

CREATE INDEX ON _dupe_map (doomed_id);

-- Collapse duplicate validations BEFORE re-pointing, not after. Re-pointing
-- lands copies of one real conversation on a single row, so validations of the
-- same flag on two copies become the same validation twice — and
-- idx_validation_unique_conv rejects the second one mid-UPDATE, aborting the
-- migration. Deciding the survivor against each row's POST-move conversation
-- means the UPDATE below can no longer collide with anything.
-- Keeps the earliest row in each group: the auditor's original click.
WITH target AS (
    SELECT v.id, v.agent_id,
           COALESCE(m.keep_id, v.conversation_id) AS conv,
           COALESCE(LOWER(v.flag_text), '')       AS flag
      FROM validation_log v
      LEFT JOIN _dupe_map m ON m.doomed_id = v.conversation_id
     WHERE v.conversation_id IS NOT NULL
), losers AS (
    SELECT a.id FROM target a JOIN target b
      ON a.agent_id = b.agent_id AND a.conv = b.conv AND a.flag = b.flag
     WHERE a.id > b.id
)
DELETE FROM validation_log WHERE id IN (SELECT id FROM losers);

UPDATE validation_log v SET conversation_id = m.keep_id
  FROM _dupe_map m WHERE v.conversation_id = m.doomed_id;

UPDATE flag_feedback f SET conversation_id = m.keep_id
  FROM _dupe_map m WHERE f.conversation_id = m.doomed_id;

UPDATE flagged_conversation_reviews r SET conversation_id = m.keep_id
  FROM _dupe_map m WHERE r.conversation_id = m.doomed_id;

-- ── 3. Delete the duplicates (migration 007 §2) ──────────────────────────────
-- messages, conversation_scores, prefilter_decisions, embeddings, overrides and
-- semantic_candidates cascade. Nothing that gates a flag does — step 2 moved it.
DELETE FROM conversations a USING _dupe_map m WHERE a.id = m.doomed_id;

-- ── 4. Enforce it from here on ───────────────────────────────────────────────
-- Without this, every audit re-run appends a full duplicate set again and the
-- whole cleanup above is temporary.
ALTER TABLE conversations
    DROP CONSTRAINT IF EXISTS uq_conversations_agent_contact_day;
ALTER TABLE conversations
    ADD CONSTRAINT uq_conversations_agent_contact_day
    UNIQUE (agent_id, contact_id, audit_date);

COMMIT;
