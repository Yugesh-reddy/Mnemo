-- 0004_fact_identity_unique.sql — INSERT OR REPLACE cannot replace a memory by its
-- identity either (PROJECT_SPEC.md §31).
--
-- 0003's memory_fact_no_replace rejects an insert that reuses a fact_id. memory_fact
-- has a second unique key, (namespace, user_id, agent_id, fact_key, identity_mode,
-- identity_ref), and an INSERT OR REPLACE with a fresh fact_id and that key deleted the
-- existing row without firing memory_fact_no_delete (SQLite runs no delete triggers for
-- REPLACE unless recursive_triggers is on). `mnemo verify` reported the result; this
-- trigger refuses it, as memory_mutation_receipt_no_replace does for receipts.
--
-- Mnemo's own create path reaches this trigger when a memory with that identity already
-- exists; MnemoStore.insert_fact treats its message like the UNIQUE error it replaces.

CREATE TRIGGER memory_fact_identity_unique BEFORE INSERT ON memory_fact
WHEN EXISTS (SELECT 1 FROM memory_fact
             WHERE (namespace, user_id, agent_id, fact_key, identity_mode, identity_ref)
                 = (NEW.namespace, NEW.user_id, NEW.agent_id, NEW.fact_key,
                    NEW.identity_mode, NEW.identity_ref))
BEGIN
  SELECT RAISE(ABORT, 'memory_fact identity exists; update or retract that memory');
END;
