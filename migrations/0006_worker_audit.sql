-- Scoped observation identity, fenced queue ownership and auditable write decisions.
-- Legacy cache rows retain their original IDs/text; new ingestion uses ingest_key.
ALTER TABLE fast_cache
  ADD COLUMN agent_id text NOT NULL DEFAULT 'default',
  ADD COLUMN role text NOT NULL DEFAULT 'user',
  ADD COLUMN ingest_key text;

CREATE UNIQUE INDEX idx_cache_ingest_key
  ON fast_cache(namespace, user_id, agent_id, session_id, ingest_key)
  WHERE ingest_key IS NOT NULL;
CREATE INDEX idx_cache_scope ON fast_cache(namespace, user_id, agent_id, session_id)
  WHERE NOT reconciled;

ALTER TABLE extraction_job
  ADD COLUMN cache_id uuid REFERENCES fast_cache(cache_id),
  ADD COLUMN locked_at timestamptz,
  ADD COLUMN locked_by uuid,
  ADD COLUMN lease_expires_at timestamptz,
  ADD COLUMN available_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN last_error text,
  ADD COLUMN completed_at timestamptz,
  ADD COLUMN processing_ms double precision;

CREATE INDEX idx_job_ready ON extraction_job(available_at, created_at)
  WHERE status = 'pending';
CREATE INDEX idx_job_lease ON extraction_job(lease_expires_at)
  WHERE status = 'processing';

CREATE TABLE quality_decision (
  decision_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid REFERENCES extraction_job(job_id),
  namespace text NOT NULL,
  user_id text NOT NULL,
  agent_id text NOT NULL,
  session_id text,
  turn_id text,
  candidate_index int NOT NULL DEFAULT 0,
  candidate jsonb NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('accepted', 'demoted', 'rejected', 'duplicate', 'error')),
  reason text NOT NULL,
  verification jsonb,
  score_components jsonb,
  config_snapshot jsonb NOT NULL,
  source_span jsonb NOT NULL,
  event_id uuid REFERENCES memory_event(event_id),
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_decision_scope ON quality_decision(namespace, user_id, agent_id, recorded_at DESC);
CREATE INDEX idx_decision_job ON quality_decision(job_id);

CREATE FUNCTION protect_quality_decision() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'quality_decision is append-only' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER quality_decision_immutable BEFORE UPDATE OR DELETE ON quality_decision
  FOR EACH ROW EXECUTE FUNCTION protect_quality_decision();
