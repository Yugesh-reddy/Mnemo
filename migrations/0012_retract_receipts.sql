-- 0012_retract_receipts.sql — receipts for retraction (spec §24).
-- A retraction appends the existing INVALIDATE event; this only lets the guarded
-- direct API record a retry receipt for it. No row is changed.

ALTER TABLE memory_mutation_receipt
  DROP CONSTRAINT memory_mutation_receipt_operation_check,
  ADD CONSTRAINT memory_mutation_receipt_operation_check
    CHECK (operation IN ('create', 'update', 'revert', 'retract'));
