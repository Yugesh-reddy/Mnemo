-- 0002_views.sql
-- HEAD view + as-of helper (PROJECT_SPEC.md §4).

-- memory_current = HEAD = current believed state (the live event per active fact).
-- Invariant: each active fact has exactly one non-superseded "live" event, pointed
-- to by memory_fact.current_event_id. add()/revert() maintain this pointer.
CREATE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id,
  f.subject, f.predicate, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level,
  e.valid_from, e.recorded_at
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id
WHERE f.status = 'active';

-- value of every fact as of a given event seq (for diff / time-travel)
CREATE OR REPLACE FUNCTION fact_state_as_of(p_namespace text, p_user text, p_seq bigint)
RETURNS TABLE(fact_id uuid, subject text, predicate text,
              object_text text, object_number numeric, object_json jsonb) AS $$
  SELECT s.fact_id, s.subject, s.predicate, s.object_text, s.object_number, s.object_json
  FROM (
    SELECT DISTINCT ON (e.fact_id)
      e.fact_id, f.subject, f.predicate, e.op,
      e.object_text, e.object_number, e.object_json
    FROM memory_event e
    JOIN memory_fact f ON f.fact_id = e.fact_id
    WHERE f.namespace = p_namespace AND f.user_id = p_user AND e.seq <= p_seq
    ORDER BY e.fact_id, e.seq DESC
  ) s
  WHERE s.op IN ('ADD','UPDATE','REVERT');   -- excludes facts ending in INVALIDATE/DELETE at <= seq
$$ LANGUAGE sql STABLE;
