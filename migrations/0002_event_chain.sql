-- 0002_event_chain.sql — tamper evidence for the event log (PROJECT_SPEC.md §27).
--
-- Each event gets a SHA-256 over its payload and the previous event's hash, in seq
-- order, so changing, removing or inserting an event anywhere breaks every later
-- hash. `mnemo verify` recomputes the chain. The triggers on memory_event stop
-- changes made through SQL; the chain shows when someone went around them (for
-- example by dropping a trigger). It cannot stop someone who also rewrites every
-- later hash: compare the chain head `mnemo verify` prints with one you kept.
--
-- sha256_hex() is registered by Mnemo on every connection, like now(). The payload
-- is the same json_array() as mnemo/chain.py's CHAIN_COLUMNS, in the same order;
-- existing events are chained by Mnemo right after this file runs.

CREATE TABLE memory_event_chain (
  seq  INTEGER PRIMARY KEY REFERENCES memory_event(seq),
  hash TEXT NOT NULL CHECK (length(hash) = 64)
);

CREATE TRIGGER memory_event_chain_link AFTER INSERT ON memory_event
BEGIN
  INSERT INTO memory_event_chain (seq, hash)
  VALUES (NEW.seq, sha256_hex(
    coalesce((SELECT hash FROM memory_event_chain WHERE seq < NEW.seq
              ORDER BY seq DESC LIMIT 1), '')
    || json_array(NEW.seq, NEW.event_id, NEW.fact_id, NEW.op, NEW.object_text,
                  NEW.object_number, NEW.object_json, NEW.provenance, NEW.actor,
                  NEW.confidence, NEW.trust_level, NEW.source_span, NEW.valid_from,
                  NEW.valid_to, NEW.recorded_at, NEW.parent_event_id, NEW.commit_id,
                  NEW.importance, NEW.write_score, NEW.tier, NEW.reason,
                  NEW.session_id, NEW.expires_at)));
END;

CREATE TRIGGER memory_event_chain_no_replace BEFORE INSERT ON memory_event_chain
WHEN EXISTS (SELECT 1 FROM memory_event_chain WHERE seq = NEW.seq)
BEGIN
  SELECT RAISE(ABORT, 'memory_event_chain is append-only');
END;

CREATE TRIGGER memory_event_chain_no_update BEFORE UPDATE ON memory_event_chain
BEGIN
  SELECT RAISE(ABORT, 'memory_event_chain is append-only');
END;

CREATE TRIGGER memory_event_chain_no_delete BEFORE DELETE ON memory_event_chain
BEGIN
  SELECT RAISE(ABORT, 'memory_event_chain is append-only');
END;
