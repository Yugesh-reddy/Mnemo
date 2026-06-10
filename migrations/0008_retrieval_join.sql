-- Use the fact primary key when following an HNSW candidate to its HEAD.
CREATE OR REPLACE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id AS fact_session_id,
  f.subject, f.predicate, f.fact_key, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level, e.reason, e.source_span,
  COALESCE(e.session_id, f.session_id) AS session_id,
  e.valid_from, e.valid_to, e.expires_at, e.recorded_at,
  e.importance, e.write_score, e.tier, e.strength, e.recall_count, e.last_used
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id AND e.fact_id = f.fact_id
WHERE f.status = 'active'
  AND e.op IN ('ADD', 'UPDATE', 'REVERT')
  AND e.tier IN ('durable', 'session')
  AND e.valid_from <= now()
  AND (e.valid_to IS NULL OR e.valid_to > now())
  AND (e.expires_at IS NULL OR e.expires_at > now());
