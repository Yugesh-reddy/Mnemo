-- 0011_identity.sql — member/occurrence fact identity (master plan Phase 6, Pilot B).
-- Approved by the owner under AGENTS.md rule 6. Follows the reviewed sketch in
-- docs/quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md.
--
-- Existing facts become attribute identities (one HEAD per scoped subject/predicate)
-- with their IDs, keys and HEADs unchanged. No memory_event payload is touched.
-- A member or occurrence carries a caller- or runtime-managed UUID, never derived
-- from its value, so several can share one subject/predicate.
--
-- Rollback: once member/occurrence facts exist, the old unique key cannot be
-- restored without collapsing histories. Disable new modes instead.

ALTER TABLE memory_fact
  ADD COLUMN identity_mode text NOT NULL DEFAULT 'attribute',
  ADD COLUMN identity_ref uuid NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
  ADD CONSTRAINT memory_fact_identity_mode_check
    CHECK (identity_mode IN ('attribute', 'member', 'occurrence')),
  ADD CONSTRAINT memory_fact_identity_ref_check CHECK (
    (identity_mode = 'attribute') =
    (identity_ref = '00000000-0000-0000-0000-000000000000'::uuid)
  );

ALTER TABLE memory_fact
  DROP CONSTRAINT memory_fact_namespace_user_id_agent_id_fact_key_key,
  ADD CONSTRAINT memory_fact_scoped_identity_key
    UNIQUE (namespace, user_id, agent_id, fact_key, identity_mode, identity_ref);

-- HEAD view: unchanged filters and column order, identity appended at the end.
CREATE OR REPLACE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id AS fact_session_id,
  f.subject, f.predicate, f.fact_key, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level, e.reason, e.source_span,
  COALESCE(e.session_id, f.session_id) AS session_id,
  e.valid_from, e.valid_to,
  COALESCE(e.expires_at, CASE WHEN e.tier='session'
    THEN e.recorded_at + interval '24 hours' END) AS expires_at, e.recorded_at,
  e.importance, e.write_score, e.tier, e.strength, e.recall_count, e.last_used,
  f.identity_mode, f.identity_ref
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id AND e.fact_id = f.fact_id
WHERE f.status = 'active'
  AND e.op IN ('ADD', 'UPDATE', 'REVERT')
  AND e.tier IN ('durable', 'session')
  AND e.valid_from <= now()
  AND (e.valid_to IS NULL OR e.valid_to > now())
  AND (COALESCE(e.expires_at, CASE WHEN e.tier='session'
    THEN e.recorded_at + interval '24 hours' END) IS NULL
    OR COALESCE(e.expires_at, e.recorded_at + interval '24 hours') > now());
