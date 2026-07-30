-- 0003_fact_identity.sql — a memory's identity is permanent (PROJECT_SPEC.md §31).
--
-- A memory_fact row names what a memory is about and whose it is: its scope
-- (namespace, user_id, agent_id, session_id), subject, predicate, key, kind and
-- identity. Its events hold the values. Until now a SQL UPDATE could move a memory to
-- another project or change its subject without touching an event, so neither the
-- append-only guards nor the event chain noticed.
--
-- The triggers below reject such updates, any DELETE, an INSERT OR REPLACE over an
-- existing fact, and any status but 'active' and 'invalidated' (nothing has ever
-- written 'deleted'; setting it would hide a memory from every read). status and
-- current_event_id remain the only columns Mnemo changes, and `mnemo verify` checks
-- both against the memory's newest event. Each fact also gets a seal: SHA-256 over the
-- previous seal and a json_array() of its identity columns, in the same order as
-- mnemo/chain.py's FACT_COLUMNS, so `mnemo verify` notices a change made around the
-- triggers, and its witness notices a rewrite of every later seal. Existing facts are
-- sealed by Mnemo right after this file runs, in the order they were stored.

CREATE TRIGGER memory_fact_identity_immutable BEFORE UPDATE ON memory_fact
WHEN NEW.fact_id IS NOT OLD.fact_id
  OR NEW.namespace IS NOT OLD.namespace
  OR NEW.user_id IS NOT OLD.user_id
  OR NEW.agent_id IS NOT OLD.agent_id
  OR NEW.session_id IS NOT OLD.session_id
  OR NEW.subject IS NOT OLD.subject
  OR NEW.predicate IS NOT OLD.predicate
  OR NEW.fact_key IS NOT OLD.fact_key
  OR NEW.kind IS NOT OLD.kind
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.identity_mode IS NOT OLD.identity_mode
  OR NEW.identity_ref IS NOT OLD.identity_ref
BEGIN
  SELECT RAISE(ABORT, 'memory_fact identity is immutable');
END;

CREATE TRIGGER memory_fact_status_guard BEFORE UPDATE OF status ON memory_fact
WHEN NEW.status NOT IN ('active', 'invalidated')
BEGIN
  SELECT RAISE(ABORT, 'memory_fact status is active or invalidated; retract the memory instead');
END;

CREATE TRIGGER memory_fact_no_replace BEFORE INSERT ON memory_fact
WHEN EXISTS (SELECT 1 FROM memory_fact WHERE fact_id = NEW.fact_id)
BEGIN
  SELECT RAISE(ABORT, 'memory_fact rows are permanent; retract the memory instead');
END;

CREATE TRIGGER memory_fact_no_delete BEFORE DELETE ON memory_fact
BEGIN
  SELECT RAISE(ABORT, 'memory_fact rows are permanent; retract the memory instead');
END;

CREATE TABLE memory_fact_seal (
  seq     INTEGER PRIMARY KEY AUTOINCREMENT,
  fact_id TEXT NOT NULL UNIQUE REFERENCES memory_fact(fact_id),
  hash    TEXT NOT NULL CHECK (length(hash) = 64)
);

CREATE TRIGGER memory_fact_seal_link AFTER INSERT ON memory_fact
BEGIN
  INSERT INTO memory_fact_seal (fact_id, hash)
  VALUES (NEW.fact_id, sha256_hex(
    coalesce((SELECT hash FROM memory_fact_seal ORDER BY seq DESC LIMIT 1), '')
    || json_array(NEW.fact_id, NEW.namespace, NEW.user_id, NEW.agent_id,
                  NEW.session_id, NEW.subject, NEW.predicate, NEW.fact_key, NEW.kind,
                  NEW.created_at, NEW.identity_mode, NEW.identity_ref)));
END;

CREATE TRIGGER memory_fact_seal_no_replace BEFORE INSERT ON memory_fact_seal
WHEN EXISTS (SELECT 1 FROM memory_fact_seal WHERE fact_id = NEW.fact_id OR seq = NEW.seq)
BEGIN
  SELECT RAISE(ABORT, 'memory_fact_seal is append-only');
END;

CREATE TRIGGER memory_fact_seal_no_update BEFORE UPDATE ON memory_fact_seal
BEGIN
  SELECT RAISE(ABORT, 'memory_fact_seal is append-only');
END;

CREATE TRIGGER memory_fact_seal_no_delete BEFORE DELETE ON memory_fact_seal
BEGIN
  SELECT RAISE(ABORT, 'memory_fact_seal is append-only');
END;
