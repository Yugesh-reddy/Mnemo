-- Legacy session facts receive a read-time TTL without rewriting any event.
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
  e.importance, e.write_score, e.tier, e.strength, e.recall_count, e.last_used
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

CREATE OR REPLACE FUNCTION fact_snapshot_as_of(
  p_namespace text, p_user text, p_agent text, p_seq bigint
)
RETURNS TABLE(
  fact_id uuid, subject text, predicate text, fact_key text, op mem_op,
  object_text text, object_number numeric, object_json jsonb,
  tier mem_tier, session_id text, valid_from timestamptz, valid_to timestamptz,
  expires_at timestamptz, recorded_at timestamptz
) AS $$
  SELECT DISTINCT ON (e.fact_id)
    e.fact_id, f.subject, f.predicate, f.fact_key, e.op,
    e.object_text, e.object_number, e.object_json,
    e.tier, COALESCE(e.session_id,f.session_id), e.valid_from, e.valid_to,
    COALESCE(e.expires_at, CASE WHEN e.tier='session'
      THEN e.recorded_at + interval '24 hours' END), e.recorded_at
  FROM memory_event e
  JOIN memory_fact f ON f.fact_id = e.fact_id
  WHERE f.namespace = p_namespace AND f.user_id = p_user
    AND f.agent_id = p_agent AND e.seq <= p_seq
  ORDER BY e.fact_id, e.seq DESC;
$$ LANGUAGE sql STABLE;

-- Bitemporal historical state: recorded time selects what the system knew;
-- valid time selects when the assertion was true in the represented world.
CREATE OR REPLACE FUNCTION fact_snapshot_at(
  p_namespace text, p_user text, p_agent text,
  p_recorded_at timestamptz, p_valid_at timestamptz
)
RETURNS TABLE(
  fact_id uuid, subject text, predicate text, fact_key text, kind mem_kind,
  event_id uuid, object_text text, object_number numeric, object_json jsonb,
  embedding vector, provenance mem_provenance, actor text, confidence real,
  trust_level mem_trust, source_span jsonb, session_id text,
  valid_from timestamptz, valid_to timestamptz, expires_at timestamptz,
  recorded_at timestamptz, importance int, write_score real, tier mem_tier,
  strength real, recall_count int, last_used timestamptz
) AS $$
  SELECT s.fact_id, s.subject, s.predicate, s.fact_key, s.kind,
         s.event_id, s.object_text, s.object_number, s.object_json,
         s.embedding, s.provenance, s.actor, s.confidence, s.trust_level,
         s.source_span, s.session_id, s.valid_from, s.valid_to, s.expires_at,
         s.recorded_at, s.importance, s.write_score, s.tier,
         s.strength, s.recall_count, s.last_used
  FROM (
    SELECT DISTINCT ON (e.fact_id)
      e.fact_id, f.subject, f.predicate, f.fact_key, f.kind,
      e.event_id, e.op, e.object_text, e.object_number, e.object_json,
      e.embedding, e.provenance, e.actor, e.confidence, e.trust_level,
      e.source_span, COALESCE(e.session_id,f.session_id) AS session_id,
      e.valid_from, e.valid_to, COALESCE(e.expires_at, CASE WHEN e.tier='session'
        THEN e.recorded_at + interval '24 hours' END) AS expires_at,
      e.recorded_at, e.importance, e.write_score, e.tier,
      e.strength, e.recall_count, e.last_used, e.seq
    FROM memory_event e
    JOIN memory_fact f ON f.fact_id = e.fact_id
    WHERE f.namespace = p_namespace AND f.user_id = p_user
      AND f.agent_id = p_agent AND e.recorded_at <= p_recorded_at
    ORDER BY e.fact_id, e.recorded_at DESC, e.seq DESC
  ) s
  WHERE s.op <> 'DELETE'
    AND s.tier IN ('durable', 'session')
    AND s.valid_from <= p_valid_at
    AND (s.valid_to IS NULL OR s.valid_to > p_valid_at)
    AND (s.expires_at IS NULL OR s.expires_at > p_recorded_at);
$$ LANGUAGE sql STABLE;
