-- Durable idempotency receipts for guarded direct mutations. Retained for the
-- lifetime of the store; event changes and their receipts commit atomically.
CREATE TABLE memory_mutation_receipt (
    receipt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    namespace text NOT NULL,
    user_id text NOT NULL,
    agent_id text NOT NULL,
    request_id uuid NOT NULL,
    operation text NOT NULL CHECK (operation IN ('create', 'update', 'revert')),
    request_payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('applied', 'no_change')),
    fact_id uuid NOT NULL REFERENCES memory_fact(fact_id),
    event_id uuid NOT NULL REFERENCES memory_event(event_id),
    previous_event_id uuid REFERENCES memory_event(event_id),
    restored_from_event_id uuid REFERENCES memory_event(event_id),
    actor text,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (namespace, user_id, agent_id, request_id)
);
CREATE INDEX idx_receipt_fact ON memory_mutation_receipt (fact_id, created_at DESC);

CREATE FUNCTION protect_mutation_receipt() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'memory_mutation_receipt is append-only' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER mutation_receipt_immutable
    BEFORE UPDATE OR DELETE ON memory_mutation_receipt
    FOR EACH ROW EXECUTE FUNCTION protect_mutation_receipt();
