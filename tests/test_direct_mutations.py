"""Guarded direct mutations, durable retry receipts and scoped reads."""


def test_structured_error_serialization() -> None:
    from mnemo.errors import ErrorCode, MnemoError

    error = MnemoError(ErrorCode.NOT_FOUND, "Memory not found", context="read")
    assert error.to_dict() == {
        "code": "NOT_FOUND",
        "message": "Memory not found",
        "details": {"context": "read"},
    }


async def test_revert_copy_preserves_payload_trust_and_confidence(store) -> None:
    first = await store.add(
        "user",
        "preferences",
        {"database": "PostgreSQL"},
        confidence=0.42,
        source_span={"turn_ids": ["source-turn"]},
        actor="original-agent",
    )
    copied = await store._copy_as_revert(
        first.fact_id,
        first.event_id,
        provenance=None,
        trust_level=None,
        actor="host-llm",
        reason="restore source payload",
    )
    assert copied.value == first.value
    assert copied.source_span == first.source_span
    assert copied.confidence == first.confidence
    assert (copied.provenance, copied.trust_level, copied.actor) == (
        "agent_inference",
        "low",
        "host-llm",
    )
    vectors = await store.conn.fetch(
        "SELECT embedding::text FROM memory_event WHERE event_id=ANY($1::uuid[])",
        [first.event_id, copied.event_id],
    )
    assert len(vectors) == 2 and vectors[0] == vectors[1]
    legacy = await store.revert(first.fact_id, first.event_id)
    assert (legacy.provenance, legacy.trust_level, legacy.confidence) == (
        "human_review",
        "high",
        1.0,
    )
