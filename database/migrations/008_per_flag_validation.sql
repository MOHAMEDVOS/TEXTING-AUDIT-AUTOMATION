-- Migration 008: validate per FLAG, not per conversation.
--
-- validation_log was keyed on (agent_id, contact_name) — one "Valid" click
-- confirmed every flag on a conversation at once. A conversation carrying two
-- unrelated flags (e.g. "Slow response time to an engaged lead" and a
-- "Wrong label: ..." flag) had no way to confirm one without the other.
--
-- flag_text pins a validation to one specific flag. NULL means a row written
-- before this migration — it's treated as "covers every flag on the
-- conversation" (see dashboard/app.py's validated_flags "*" sentinel), so
-- historical counts don't shift. Every new validation from here on sets an
-- explicit flag_text.
--
-- Same shape-drift risk schema.sql already documents for this table (some
-- deployments have UNIQUE(agent_id, contact_name) from migration 003, others
-- only idx_validation_unique from schema.sql) — this migration handles both:
-- drop whichever unique constraint/index exists on the old 2-column shape,
-- then create the 3-column replacement.

ALTER TABLE validation_log ADD COLUMN IF NOT EXISTS flag_text TEXT;

-- De-dup before tightening the index — collapses any rows that would now
-- collide under the new key (COALESCE(flag_text,'') so legacy NULL rows,
-- which are otherwise distinct from each other under a plain unique index,
-- count as the one blanket slot per conversation).
DELETE FROM validation_log a USING validation_log b
 WHERE a.id < b.id AND a.agent_id = b.agent_id
   AND LOWER(a.contact_name) = LOWER(b.contact_name)
   AND COALESCE(LOWER(a.flag_text), '') = COALESCE(LOWER(b.flag_text), '');

ALTER TABLE validation_log DROP CONSTRAINT IF EXISTS validation_log_agent_id_contact_name_key;
DROP INDEX IF EXISTS idx_validation_unique;

CREATE UNIQUE INDEX IF NOT EXISTS idx_validation_unique
    ON validation_log(agent_id, LOWER(contact_name), COALESCE(LOWER(flag_text), ''));
