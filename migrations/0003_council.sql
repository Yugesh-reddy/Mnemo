-- 0003_council.sql — Model-Council quality + decay fields (authorized scope add).
-- Adds the write-quality layer the starter centers on: salience (importance/write_score),
-- "demote don't drop" tiering, and Ebbinghaus decay/reinforcement (strength/recall_count).
-- Additive only — existing facts default to durable, so HEAD is unchanged.

CREATE TYPE mem_tier AS ENUM ('durable', 'session', 'ephemeral');

ALTER TABLE memory_event
  ADD COLUMN importance   int CHECK (importance BETWEEN 1 AND 10),
  ADD COLUMN write_score  real,
  ADD COLUMN tier         mem_tier NOT NULL DEFAULT 'durable',
  ADD COLUMN reason       text,                       -- why the gate kept/changed it
  ADD COLUMN strength     real NOT NULL DEFAULT 1.0,  -- MemoryBank S (grows on recall)
  ADD COLUMN recall_count int NOT NULL DEFAULT 0,
  ADD COLUMN last_used    timestamptz NOT NULL DEFAULT now();

-- Tier-aware HEAD: ephemeral facts (noise / decay-archived) drop out of the live view
-- but their events are never deleted — "demote, don't drop" stays reversible.
CREATE OR REPLACE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id,
  f.subject, f.predicate, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level,
  e.valid_from, e.recorded_at,
  e.importance, e.write_score, e.tier, e.strength, e.recall_count
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id
WHERE f.status = 'active' AND e.tier IN ('durable', 'session');
