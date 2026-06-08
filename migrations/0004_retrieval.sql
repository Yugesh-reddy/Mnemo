-- 0004_retrieval.sql — bitemporal enforcement + decay columns in HEAD; schema hygiene.
-- Spec v3 §3/§5: memory_current is tier- AND validity-filtered; memory_entity is not
-- part of the v3 schema (was never referenced by code) — dropped.

DROP TABLE IF EXISTS memory_entity;

-- DROP+CREATE (not OR REPLACE): the column set/order changes, which Postgres
-- forbids for in-place view replacement.
DROP VIEW memory_current;

CREATE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id,
  f.subject, f.predicate, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level, e.reason,
  e.valid_from, e.valid_to, e.recorded_at,
  e.importance, e.write_score, e.tier, e.strength, e.recall_count, e.last_used
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id
WHERE f.status = 'active'
  AND e.tier IN ('durable', 'session')
  AND (e.valid_to IS NULL OR e.valid_to > now());   -- bitemporal: only currently-true
