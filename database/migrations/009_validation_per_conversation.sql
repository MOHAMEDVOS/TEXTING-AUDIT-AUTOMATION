-- Migration 009: key validation on the CONVERSATION, not the contact's name.
--
-- Bug this fixes: validation_log was keyed (and read) on
-- (agent_id, LOWER(contact_name), flag_text). Every reader matched the same
-- way, so confirming a flag on one conversation silently marked EVERY other
-- conversation that account ever had with the same contact name as validated:
--
--   * the same contact texting again on a later day (a new conversations row),
--   * duplicate rows left by re-running an audit for the same day — the
--     uq_conversations_agent_contact_day constraint from migration 007 is
--     absent on databases provisioned from schema.sql, so re-runs append copies.
--
-- Because a SmarterContact account moves between texters while contact names
-- do not, this also charged confirmed flags to texters the auditor had never
-- reviewed. Auditors reported seeing conversations marked valid that they had
-- never clicked.
--
-- Readers now match through the validation row's own conversation_id, widened
-- only to duplicate rows for the same contact on the same conversation day
-- (copies of one real conversation). Nothing is deleted from
-- conversation_scores; this only changes which rows a validation covers.
--
-- Partial index: rows whose conversation was deleted (the FK is ON DELETE SET
-- NULL) keep NULL here. They match no conversation under the new readers, so
-- they are inert — excluded from the key rather than deleted, because
-- validation history is never destroyed.
-- COALESCE(flag_text, '') because Postgres treats NULL as distinct from NULL
-- in a unique index — without it, every legacy (pre-flag_text) blanket row
-- could duplicate freely instead of holding the one blanket slot.

DELETE FROM validation_log a USING validation_log b
 WHERE a.id < b.id AND a.agent_id = b.agent_id
   AND a.conversation_id IS NOT NULL
   AND a.conversation_id = b.conversation_id
   AND COALESCE(LOWER(a.flag_text), '') = COALESCE(LOWER(b.flag_text), '');

-- The 3-column contact-name key from migration 008. It must go: under
-- per-conversation validation two different conversations with the same
-- contact and the same flag are two legitimate rows, and that index rejects
-- the second one.
DROP INDEX IF EXISTS idx_validation_unique;

CREATE UNIQUE INDEX IF NOT EXISTS idx_validation_unique_conv
    ON validation_log(agent_id, conversation_id, COALESCE(LOWER(flag_text), ''))
 WHERE conversation_id IS NOT NULL;
