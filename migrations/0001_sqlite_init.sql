-- 0001_sqlite_init.sql — the whole store in one SQLite file (PROJECT_SPEC.md §3).
--
-- Columns match the Postgres schema at migration 0012 (tag v0.0.1-postgres) one for
-- one, so an imported history keeps every field. Differences:
--   * IDs are TEXT (canonical lowercase UUIDs), numbers are TEXT decimals, JSON is
--     TEXT, and timestamps are TEXT in the fixed form YYYY-MM-DDTHH:MM:SS.ffffffZ (UTC),
--     so they compare correctly as text.
--   * now() is not built into SQLite. Mnemo registers it on every connection: it
--     returns the time the current transaction began, as Postgres now() does. The
--     view and the defaults below need it, so open the file through Mnemo.
--   * Vectors live in memory_embedding, outside the append-only event (spec §3):
--     they are derived from the text and the model and can be rebuilt.
--   * memory_search_text is a full-text index of every event's text, keyed by the
--     event's seq. Events never change, so its rows never need updating.

CREATE TABLE schema_migrations (
  filename   TEXT PRIMARY KEY NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (now())
);

-- ---- facts: stable identity and the HEAD pointer ---------------------------
CREATE TABLE memory_fact (
  fact_id          TEXT PRIMARY KEY NOT NULL,
  namespace        TEXT NOT NULL DEFAULT 'default',
  user_id          TEXT NOT NULL DEFAULT 'default',
  agent_id         TEXT NOT NULL DEFAULT 'default',
  session_id       TEXT,
  subject          TEXT NOT NULL,
  predicate        TEXT NOT NULL,
  fact_key         TEXT NOT NULL,
  kind             TEXT NOT NULL DEFAULT 'triple'
                   CHECK (kind IN ('text_fact', 'triple', 'kv', 'task', 'trace')),
  status           TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'invalidated', 'deleted')),
  current_event_id TEXT REFERENCES memory_event(event_id) DEFERRABLE INITIALLY DEFERRED,
  created_at       TEXT NOT NULL DEFAULT (now()),
  identity_mode    TEXT NOT NULL DEFAULT 'attribute'
                   CHECK (identity_mode IN ('attribute', 'member', 'occurrence')),
  identity_ref     TEXT NOT NULL DEFAULT '00000000-0000-0000-0000-000000000000',
  CHECK ((identity_mode = 'attribute')
         = (identity_ref = '00000000-0000-0000-0000-000000000000')),
  UNIQUE (namespace, user_id, agent_id, fact_key, identity_mode, identity_ref)
);

-- ---- commits: named pointers into the event sequence ------------------------
CREATE TABLE memory_commit (
  commit_id        TEXT PRIMARY KEY NOT NULL,
  namespace        TEXT NOT NULL DEFAULT 'default',
  parent_commit_id TEXT REFERENCES memory_commit(commit_id),
  label            TEXT,
  at_seq           INTEGER NOT NULL,
  created_by       TEXT,
  created_at       TEXT NOT NULL DEFAULT (now()),
  user_id          TEXT NOT NULL DEFAULT 'default',
  agent_id         TEXT NOT NULL DEFAULT 'default'
);
CREATE INDEX idx_commit_scope ON memory_commit (namespace, user_id, agent_id, at_seq DESC);

-- ---- events: the append-only log, the source of truth ----------------------
CREATE TABLE memory_event (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,   -- global order
  event_id        TEXT NOT NULL UNIQUE,
  fact_id         TEXT NOT NULL REFERENCES memory_fact(fact_id),
  op              TEXT NOT NULL CHECK (op IN ('ADD', 'UPDATE', 'INVALIDATE', 'REVERT', 'DELETE')),
  object_text     TEXT,
  object_number   TEXT,                                -- a decimal, kept exact as text
  object_json     TEXT CHECK (object_json IS NULL OR json_valid(object_json)),
  provenance      TEXT NOT NULL CHECK (provenance IN ('direct_user_statement',
                    'agent_inference', 'tool_output', 'document', 'human_review')),
  actor           TEXT,
  confidence      REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
  trust_level     TEXT NOT NULL DEFAULT 'medium' CHECK (trust_level IN ('high', 'medium', 'low')),
  source_span     TEXT CHECK (source_span IS NULL OR json_valid(source_span)),
  valid_from      TEXT NOT NULL DEFAULT (now()),
  valid_to        TEXT,
  recorded_at     TEXT NOT NULL DEFAULT (now()),
  superseded_at   TEXT,
  superseded_by   TEXT REFERENCES memory_event(event_id),
  parent_event_id TEXT REFERENCES memory_event(event_id),
  commit_id       TEXT REFERENCES memory_commit(commit_id),
  importance      INTEGER CHECK (importance BETWEEN 1 AND 10),
  write_score     REAL,
  tier            TEXT NOT NULL DEFAULT 'durable' CHECK (tier IN ('durable', 'session', 'ephemeral')),
  reason          TEXT,
  strength        REAL NOT NULL DEFAULT 1.0,
  recall_count    INTEGER NOT NULL DEFAULT 0,
  last_used       TEXT NOT NULL DEFAULT (now()),
  session_id      TEXT,
  expires_at      TEXT
);
CREATE INDEX idx_event_fact ON memory_event (fact_id, seq);

-- ---- receipts: durable retry results for guarded direct writes -------------
CREATE TABLE memory_mutation_receipt (
  receipt_id             TEXT PRIMARY KEY NOT NULL,
  namespace              TEXT NOT NULL,
  user_id                TEXT NOT NULL,
  agent_id               TEXT NOT NULL,
  request_id             TEXT NOT NULL,
  operation              TEXT NOT NULL CHECK (operation IN ('create', 'update', 'revert', 'retract')),
  request_payload        TEXT NOT NULL CHECK (json_valid(request_payload)),
  status                 TEXT NOT NULL CHECK (status IN ('applied', 'no_change')),
  fact_id                TEXT NOT NULL REFERENCES memory_fact(fact_id),
  event_id               TEXT NOT NULL REFERENCES memory_event(event_id),
  previous_event_id      TEXT REFERENCES memory_event(event_id),
  restored_from_event_id TEXT REFERENCES memory_event(event_id),
  actor                  TEXT,
  result                 TEXT NOT NULL CHECK (json_valid(result)),
  created_at             TEXT NOT NULL DEFAULT (now()),
  UNIQUE (namespace, user_id, agent_id, request_id)
);
CREATE INDEX idx_receipt_fact ON memory_mutation_receipt (fact_id, created_at DESC);

-- ---- derived data: vectors and the word index -------------------------------
CREATE TABLE memory_embedding (
  seq    INTEGER PRIMARY KEY REFERENCES memory_event(seq),
  model  TEXT NOT NULL,                -- backend and model that produced the vector
  dim    INTEGER NOT NULL,
  vector BLOB NOT NULL                 -- float32, as sqlite-vec reads it
);

CREATE VIRTUAL TABLE memory_search_text USING fts5(body, tokenize = 'porter unicode61');

CREATE TRIGGER memory_event_index_text AFTER INSERT ON memory_event
BEGIN
  INSERT INTO memory_search_text (rowid, body)
  SELECT NEW.seq, f.subject || ' ' || f.predicate || ' ' || coalesce(NEW.object_text, '')
  FROM memory_fact f WHERE f.fact_id = NEW.fact_id;
END;

-- ---- append-only guards ------------------------------------------------------
-- Supersession and the recall counters are the only bookkeeping that may change
-- after an event is written. The insert guards stop INSERT OR REPLACE from
-- swapping out a row, which would otherwise bypass the delete guards.
CREATE TRIGGER memory_event_no_replace BEFORE INSERT ON memory_event
WHEN EXISTS (SELECT 1 FROM memory_event WHERE event_id = NEW.event_id OR seq = NEW.seq)
BEGIN
  SELECT RAISE(ABORT, 'memory_event payload is immutable');
END;

CREATE TRIGGER memory_event_no_delete BEFORE DELETE ON memory_event
BEGIN
  SELECT RAISE(ABORT, 'memory_event rows cannot be deleted');
END;

CREATE TRIGGER memory_event_payload_immutable BEFORE UPDATE ON memory_event
WHEN NEW.seq IS NOT OLD.seq
  OR NEW.event_id IS NOT OLD.event_id
  OR NEW.fact_id IS NOT OLD.fact_id
  OR NEW.op IS NOT OLD.op
  OR NEW.object_text IS NOT OLD.object_text
  OR NEW.object_number IS NOT OLD.object_number
  OR NEW.object_json IS NOT OLD.object_json
  OR NEW.provenance IS NOT OLD.provenance
  OR NEW.actor IS NOT OLD.actor
  OR NEW.confidence IS NOT OLD.confidence
  OR NEW.trust_level IS NOT OLD.trust_level
  OR NEW.source_span IS NOT OLD.source_span
  OR NEW.valid_from IS NOT OLD.valid_from
  OR NEW.valid_to IS NOT OLD.valid_to
  OR NEW.recorded_at IS NOT OLD.recorded_at
  OR NEW.parent_event_id IS NOT OLD.parent_event_id
  OR NEW.commit_id IS NOT OLD.commit_id
  OR NEW.importance IS NOT OLD.importance
  OR NEW.write_score IS NOT OLD.write_score
  OR NEW.tier IS NOT OLD.tier
  OR NEW.reason IS NOT OLD.reason
  OR NEW.session_id IS NOT OLD.session_id
  OR NEW.expires_at IS NOT OLD.expires_at
BEGIN
  SELECT RAISE(ABORT, 'memory_event payload is immutable');
END;

CREATE TRIGGER memory_mutation_receipt_no_replace BEFORE INSERT ON memory_mutation_receipt
WHEN EXISTS (SELECT 1 FROM memory_mutation_receipt WHERE receipt_id = NEW.receipt_id
             OR (namespace, user_id, agent_id, request_id)
                = (NEW.namespace, NEW.user_id, NEW.agent_id, NEW.request_id))
BEGIN
  SELECT RAISE(ABORT, 'memory_mutation_receipt is append-only');
END;

CREATE TRIGGER memory_mutation_receipt_no_update BEFORE UPDATE ON memory_mutation_receipt
BEGIN
  SELECT RAISE(ABORT, 'memory_mutation_receipt is append-only');
END;

CREATE TRIGGER memory_mutation_receipt_no_delete BEFORE DELETE ON memory_mutation_receipt
BEGIN
  SELECT RAISE(ABORT, 'memory_mutation_receipt is append-only');
END;

-- ---- HEAD: the current believed value of every visible fact -----------------
-- A session-tier event written before expires_at existed lives 24 hours.
CREATE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id AS fact_session_id,
  f.subject, f.predicate, f.fact_key, f.kind,
  e.seq, e.event_id, e.object_text, e.object_number, e.object_json,
  e.provenance, e.actor, e.confidence, e.trust_level, e.reason, e.source_span,
  COALESCE(e.session_id, f.session_id) AS session_id,
  e.valid_from, e.valid_to,
  COALESCE(e.expires_at, CASE WHEN e.tier = 'session' THEN
    strftime('%Y-%m-%dT%H:%M:%S', e.recorded_at, '+24 hours') || substr(e.recorded_at, 20)
  END) AS expires_at,
  e.recorded_at,
  e.importance, e.write_score, e.tier, e.strength, e.recall_count, e.last_used,
  f.identity_mode, f.identity_ref
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id AND e.fact_id = f.fact_id
WHERE f.status = 'active'
  AND e.op IN ('ADD', 'UPDATE', 'REVERT')
  AND e.tier IN ('durable', 'session')
  AND e.valid_from <= now()
  AND (e.valid_to IS NULL OR e.valid_to > now())
  AND COALESCE(e.expires_at, CASE WHEN e.tier = 'session' THEN
        strftime('%Y-%m-%dT%H:%M:%S', e.recorded_at, '+24 hours') || substr(e.recorded_at, 20)
      END, '9999') > now();
