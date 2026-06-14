"""Failed extractions must remain visible and count as missed labeled facts."""

import pytest

from mnemo.config import get_settings
from mnemo.eval import _MaterializedExtractor, evaluate
from mnemo.eval_audit import analyze_report
from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn
from mnemo.models import ExtractedFact


@pytest.mark.parametrize("mode", ["candidate_replay", "pipeline"])
async def test_failed_extraction_retains_other_turns_errors_and_recall_denominator(
    _disposable_test_db, mode
):
    dataset = EvalDataset(
        name="failed-extraction",
        version="1",
        split="test",
        turns=(
            EvalTurn(
                "failed",
                "user",
                "I live in Oslo.",
                "s",
                labels=(AtomicLabel("location", "Oslo", "must_keep"),),
            ),
            EvalTurn(
                "good",
                "user",
                "I prefer Python.",
                "s",
                labels=(AtomicLabel("preferred_language", "Python", "must_keep"),),
            ),
        ),
    )

    class PartlyBroken:
        def extract(self, text, role="user"):
            if "Oslo" in text:
                raise ValueError("candidate 1 evidence is absent from source")
            return [
                ExtractedFact(
                    subject="user", predicate="preferred_language", object="Python", importance=9
                )
            ]

    settings = get_settings().model_copy(update={"job_retry_base_seconds": 0})
    result = await evaluate(
        _disposable_test_db,
        dataset=dataset,
        extractor=PartlyBroken(),
        mode=mode,
        settings=settings,
    )
    assert result["metadata"]["status"] == "scored_with_errors"
    for arm in ("naive", "gated"):
        assert result[arm]["must_keep_recall"] == 0.5
        assert result[arm]["historical_recall"] == 0.5
        assert result[arm]["total_events"] == 1
        assert len(result[arm]["turn_errors"]) == 1
        error = result[arm]["turn_errors"][0]
        assert error["turn_id"] == "failed"
        assert "evidence is absent from source" in error["error"]
    assert len([d for d in result["decisions"] if d["outcome"] == "error"]) == 3
    assert result["metadata"]["cleanup_errors"] == []
    audit = analyze_report(result, dataset)
    assert audit["missing_by_cause"] == {"extraction_error": 1}
    assert audit["misses"][0]["turn_error"]["turn_id"] == "failed"


def test_candidate_replay_rejects_conflicting_results_for_identical_source():
    turns = [
        EvalTurn("a", "user", "I live in Oslo.", "s"),
        EvalTurn("b", "user", "I live in Oslo.", "s"),
    ]
    oslo = ExtractedFact(subject="user", predicate="location", object="Oslo")
    boston = oslo.model_copy(update={"object": "Boston"})
    with pytest.raises(ValueError, match="conflicting"):
        _MaterializedExtractor(turns, [[oslo], [boston]])


def test_candidate_replay_allows_consistent_repeated_source():
    turns = [
        EvalTurn("a", "user", "I live in Oslo.", "s"),
        EvalTurn("b", "user", "I live in Oslo.", "s"),
    ]
    oslo = ExtractedFact(subject="user", predicate="location", object="Oslo")
    source = _MaterializedExtractor(turns, [[oslo], [oslo]])
    assert source.extract(turns[0].text) == [oslo]
