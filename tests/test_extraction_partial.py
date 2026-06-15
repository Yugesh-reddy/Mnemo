"""One malformed extraction must not discard independently grounded siblings."""

import json

import pytest

from mnemo.extraction import ExtractionWorker, OllamaExtractor, OpenAIExtractor
from mnemo.quality import HeuristicVerifier

SOURCE = "I use Postgres. I use Neovim as my editor."
GOOD = {
    "subject": "user",
    "predicate": "uses_database",
    "object": "Postgres",
    "evidence": "I use Postgres.",
    "importance": 8,
}
BAD = {
    "subject": "user",
    "predicate": "editor",
    "object": "Neovim",
    "evidence": "I use Neovim as my database.",
    "importance": 8,
}


@pytest.mark.parametrize("provider", [OllamaExtractor, OpenAIExtractor])
@pytest.mark.parametrize("invalid", [BAD, {**BAD, "object": None}, {"subject": "user"}, 17])
def test_final_mixed_batch_preserves_valid_facts_and_rejection_evidence(provider, invalid):
    extractor = provider(
        max_retries=1, **({"api_key": "stub"} if provider is OpenAIExtractor else {})
    )
    replies = iter([json.dumps({"facts": [invalid, GOOD]})] * 2)
    extractor._chat = lambda *args: next(replies)
    try:
        batch = extractor.extract(SOURCE)
        assert len(batch) == 1
        assert batch[0].object == "Postgres"
        assert batch[0].evidence == "I use Postgres."
        assert len(batch.rejections) == 1
        assert batch.rejections[0]["candidate"] == {"raw": invalid}
        assert "candidate 0" in batch.rejections[0]["reason"]
    finally:
        extractor.close()


def test_recovery_uses_only_final_batch_not_a_union_of_retry_attempts():
    extractor = OllamaExtractor(max_retries=1)
    corrected = {**BAD, "evidence": "I use Neovim as my editor."}
    replies = iter(
        [
            json.dumps({"facts": [GOOD, BAD]}),
            json.dumps({"facts": [corrected, {**GOOD, "evidence": "invented"}]}),
        ]
    )
    extractor._chat = lambda *args: next(replies)
    try:
        batch = extractor.extract(SOURCE)
        assert [f.object for f in batch] == ["Neovim"]
        assert batch.rejections[0]["candidate"]["raw"]["object"] == "Postgres"
    finally:
        extractor.close()


def test_final_invalid_json_does_not_resurrect_an_earlier_partial_batch():
    extractor = OllamaExtractor(max_retries=1)
    replies = iter([json.dumps({"facts": [GOOD, BAD]}), "not JSON"])
    extractor._chat = lambda *args: next(replies)
    try:
        with pytest.raises(ValueError, match="after retries"):
            extractor.extract(SOURCE)
    finally:
        extractor.close()


async def test_worker_audits_bad_member_and_verifies_valid_member(store, db):
    extractor = OllamaExtractor(max_retries=0)
    # A source quote makes this well-formed, but does not entail its changed value.
    unsupported = {**GOOD, "object": "SQLite"}
    extractor._chat = lambda *args: json.dumps({"facts": [GOOD, BAD, unsupported]})
    try:
        await store.observe("mixed", SOURCE, "s")
        await ExtractionWorker(db, store.embedder, extractor, HeuristicVerifier()).process_one()
    finally:
        extractor.close()
    assert await db.fetchval("SELECT status FROM extraction_job") == "done"
    assert await db.fetchval("SELECT count(*) FROM memory_event") == 1
    assert await db.fetchval("SELECT predicate FROM memory_fact") == "uses_database"
    assert await db.fetchval("SELECT provenance FROM memory_event") == "agent_inference"
    assert await db.fetchval("SELECT trust_level FROM memory_event") == "low"
    assert await db.fetchval("SELECT reconciled FROM fast_cache")
    decisions = await db.fetch("SELECT * FROM quality_decision ORDER BY candidate_index")
    assert len(decisions) == 3
    rejected = next(r for r in decisions if r["verification"] is None)
    assert json.loads(rejected["candidate"]) == {"raw": BAD}
    assert "evidence is absent from source" in rejected["reason"]
    assert rejected["verification"] is None
    verified_rejection = next(
        r for r in decisions if r["outcome"] == "rejected" and r["verification"] is not None
    )
    assert json.loads(verified_rejection["candidate"])["object"] == "SQLite"
    assert not json.loads(verified_rejection["verification"])["accepted"]


@pytest.mark.parametrize("mode", ["candidate_replay", "pipeline"])
async def test_candidate_replay_preserves_partial_rejections_and_recall_denominator(
    _disposable_test_db, mode
):
    from mnemo.eval import evaluate
    from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn

    dataset = EvalDataset(
        name="partial-dev",
        version="1",
        split="dev",
        turns=(
            EvalTurn(
                "partial",
                "user",
                SOURCE,
                "s",
                labels=(
                    AtomicLabel("uses_database", "Postgres", "must_keep"),
                    AtomicLabel("editor", "Neovim", "must_keep"),
                ),
            ),
        ),
    )
    extractor = OllamaExtractor(max_retries=0)
    extractor._chat = lambda *args: json.dumps({"facts": [GOOD, BAD]})
    try:
        report = await evaluate(
            _disposable_test_db, dataset=dataset, extractor=extractor, mode=mode
        )
    finally:
        extractor.close()
    assert report["gated"]["must_keep_recall"] == 0.5
    assert report["naive"]["must_keep_recall"] == 0.5
    assert report["gated"]["total_events"] == 1
    assert len(report["decisions"]) == 2
    assert any("evidence is absent from source" in r["reason"] for r in report["decisions"])
    assert report["metadata"]["cleanup_errors"] == []
