-- 0001_init.sql
-- Canonical schema (PROJECT_SPEC.md §4). The SQL is the product — keep it legible.
-- NOTE: embedding dimension is vector(768) for the default local Ollama backend
-- (nomic-embed-text). This is kept in sync with config.embed_dim. Switching to
-- OpenAI (text-embedding-3-small) means vector(1536) here AND embed_dim=1536.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---- enums -------------------------------------------------------------
CREATE TYPE mem_kind        AS ENUM ('text_fact','triple','kv','task','trace');
CREATE TYPE mem_op          AS ENUM ('ADD','UPDATE','INVALIDATE','REVERT','DELETE');
CREATE TYPE mem_provenance  AS ENUM ('direct_user_statement','agent_inference','tool_output','document','human_review');
CREATE TYPE mem_trust       AS ENUM ('high','medium','low');
CREATE TYPE mem_fact_status AS ENUM ('active','invalidated','deleted');

-- ---- entities (lightweight; subjects facts are about) ------------------
CREATE TABLE memory_entity (
  entity_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace     text NOT NULL DEFAULT 'default',
  entity_type   text NOT NULL,                 -- 'user' | 'project' | 'person' | 'concept' | ...
  canonical_name text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (namespace, entity_type, canonical_name)
);

-- ---- facts (stable identity + HEAD pointer) ----------------------------
CREATE TABLE memory_fact (
  fact_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace        text NOT NULL DEFAULT 'default',
  user_id          text NOT NULL DEFAULT 'default',
  agent_id         text NOT NULL DEFAULT 'default',
  session_id       text,                        -- nullable: long-term facts aren't session-scoped
  subject          text NOT NULL,
  predicate        text NOT NULL,
  fact_key         text NOT NULL,               -- canonical(subject|predicate[, key]); identity for dedup
  kind             mem_kind NOT NULL DEFAULT 'triple',
  status           mem_fact_status NOT NULL DEFAULT 'active',
  current_event_id uuid,                         -- FK added after memory_event exists
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (namespace, user_id, agent_id, fact_key)
);

-- ---- events (append-only log = source of truth) -----------------------
CREATE TABLE memory_event (
  event_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq           bigint GENERATED ALWAYS AS IDENTITY,  -- global monotonic order
  fact_id       uuid NOT NULL REFERENCES memory_fact(fact_id),
  op            mem_op NOT NULL,
  object_text   text,
  object_number numeric,
  object_json   jsonb,
  embedding     vector(768),                    -- embedding of the fact's content
  provenance    mem_provenance NOT NULL,
  actor         text,                            -- agent/user/session/tool id
  confidence    real NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
  trust_level   mem_trust NOT NULL DEFAULT 'medium',
  source_span   jsonb,                           -- {"turn_ids": [...], "doc": "...", ...}
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_to      timestamptz,
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,                     -- set when a later event replaces this one
  superseded_by uuid REFERENCES memory_event(event_id),
  parent_event_id uuid REFERENCES memory_event(event_id),  -- UPDATE: prev; REVERT: restored event
  commit_id     uuid                              -- FK added after commits exists
);

ALTER TABLE memory_fact
  ADD CONSTRAINT fk_fact_current_event
  FOREIGN KEY (current_event_id) REFERENCES memory_event(event_id);

-- ---- commits (named pointers into the event sequence) -----------------
CREATE TABLE memory_commit (
  commit_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace        text NOT NULL DEFAULT 'default',
  parent_commit_id uuid REFERENCES memory_commit(commit_id),
  label            text,
  at_seq           bigint NOT NULL,              -- high-water event seq captured by this commit
  created_by       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_event
  ADD CONSTRAINT fk_event_commit
  FOREIGN KEY (commit_id) REFERENCES memory_commit(commit_id);

-- ---- fast cache (synchronous working-memory tier) ---------------------
CREATE TABLE fast_cache (
  cache_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace           text NOT NULL DEFAULT 'default',
  user_id             text NOT NULL DEFAULT 'default',
  session_id          text NOT NULL,
  turn_id             text NOT NULL,
  raw_text            text NOT NULL,
  embedding           vector(768),
  reconciled          boolean NOT NULL DEFAULT false,
  reconciled_event_id uuid REFERENCES memory_event(event_id),
  created_at          timestamptz NOT NULL DEFAULT now()
);

-- ---- extraction job queue (MVP background worker) ---------------------
CREATE TABLE extraction_job (
  job_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace   text NOT NULL DEFAULT 'default',
  user_id     text NOT NULL DEFAULT 'default',
  agent_id    text NOT NULL DEFAULT 'default',
  session_id  text,
  turn_id     text NOT NULL,
  payload     jsonb NOT NULL,        -- {"text": "...", "role": "user"|"assistant", ...}
  status      text NOT NULL DEFAULT 'pending',  -- pending|processing|done|failed
  attempts    int NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- indexes ----------------------------------------------------------
CREATE INDEX idx_fact_key       ON memory_fact (namespace, user_id, agent_id, fact_key);
CREATE INDEX idx_event_fact     ON memory_event (fact_id, recorded_at DESC);
CREATE INDEX idx_event_seq      ON memory_event (seq);
CREATE INDEX idx_event_head     ON memory_event (fact_id) WHERE superseded_at IS NULL;
CREATE INDEX idx_event_embed    ON memory_event USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_cache_session  ON fast_cache (session_id, reconciled);
CREATE INDEX idx_cache_embed    ON fast_cache USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_job_status     ON extraction_job (status, created_at);
