-- 0005_integrity.sql — foundation integrity and scoped checkpoints.

ALTER TABLE memory_commit
  ADD COLUMN user_id text NOT NULL DEFAULT 'default',
  ADD COLUMN agent_id text NOT NULL DEFAULT 'default';

CREATE INDEX idx_commit_scope
  ON memory_commit(namespace, user_id, agent_id, at_seq DESC, created_at DESC);

-- Event beliefs are append-only. Supersession and recall fields are the only
-- bookkeeping that may change after insertion.
CREATE OR REPLACE FUNCTION guard_memory_event_payload_immutable()
RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'memory_event rows cannot be deleted'
      USING ERRCODE = 'check_violation';
  END IF;
  IF (to_jsonb(NEW) - ARRAY[
        'superseded_at', 'superseded_by', 'strength', 'recall_count', 'last_used'
      ]) IS DISTINCT FROM
     (to_jsonb(OLD) - ARRAY[
        'superseded_at', 'superseded_by', 'strength', 'recall_count', 'last_used'
      ]) THEN
    RAISE EXCEPTION 'memory_event payload is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_memory_event_payload_immutable
BEFORE UPDATE OR DELETE ON memory_event
FOR EACH ROW EXECUTE FUNCTION guard_memory_event_payload_immutable();

CREATE OR REPLACE FUNCTION fact_state_as_of(
  p_namespace text, p_user text, p_agent text, p_seq bigint
)
RETURNS TABLE(fact_id uuid, subject text, predicate text,
              object_text text, object_number numeric, object_json jsonb) AS $$
  SELECT s.fact_id, s.subject, s.predicate, s.object_text, s.object_number, s.object_json
  FROM (
    SELECT DISTINCT ON (e.fact_id)
      e.fact_id, f.subject, f.predicate, e.op,
      e.object_text, e.object_number, e.object_json
    FROM memory_event e
    JOIN memory_fact f ON f.fact_id = e.fact_id
    WHERE f.namespace = p_namespace AND f.user_id = p_user
      AND f.agent_id = p_agent AND e.seq <= p_seq
    ORDER BY e.fact_id, e.seq DESC
  ) s
  WHERE s.op IN ('ADD','UPDATE','REVERT');
$$ LANGUAGE sql STABLE;
