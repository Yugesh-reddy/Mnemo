"""Evaluation meanings and stage evidence must survive production changes."""

from mnemo.eval import assertion_key, evaluate
from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn


def test_strict_normalization_is_frozen_independently_of_storage_aliases(monkeypatch):
    from mnemo.core import PREDICATE_ALIASES

    before = assertion_key(" USER ", "favorite_db", "Postgres")
    assert before == ("user|preferred_database", "postgresql")
    monkeypatch.setitem(PREDICATE_ALIASES, "favorite_db", "future_relation")
    assert assertion_key(" USER ", "favorite_db", "Postgres") == before


async def test_diagnostics_link_decisions_writes_visibility_and_retrieval(_disposable_test_db):
    dataset = EvalDataset(
        name="traceability",
        version="1",
        split="dev",
        turns=(
            EvalTurn(
                "t",
                "user",
                "I prefer Python.",
                "s",
                labels=(AtomicLabel("preferred_language", "Python", "must_keep"),),
            ),
        ),
    )
    report = await evaluate(_disposable_test_db, dataset=dataset, probe_retrieval=True)
    assert report["metadata"]["strict_scoring_version"] == "mnemo-strict-v1"
    decision = report["decisions"][0]
    write = report["gated"]["writes"][0]
    probe = report["gated"]["retrieval_probes"][0]
    assert decision["decision_id"]
    assert decision["event_id"] == write["event_id"]
    assert write["fact_id"]
    assert write["event_id"] in probe["visible_event_ids"]
    assert str(write["event_id"]) == str(probe["results"][0]["event_id"])
    assert probe["observed_at"] >= write["recorded_at"]
    assert report["metadata"]["cleanup_errors"] == []


async def test_original_mustkeep_query_survives_later_same_predicate_truth(_disposable_test_db):
    dataset = EvalDataset(
        name="goals",
        version="1",
        split="dev",
        turns=(
            EvalTurn(
                "a",
                "user",
                "I want plant-based food.",
                "s",
                labels=(AtomicLabel("diet_goal", "plant-based food", "must_keep"),),
            ),
            EvalTurn(
                "b",
                "user",
                "I want fermented food too.",
                "s",
                labels=(AtomicLabel("diet_goal", "fermented food", "truth"),),
            ),
        ),
    )
    result = await evaluate(
        _disposable_test_db, dataset=dataset, probe_retrieval=True, probe_all_must_keep=True
    )
    queries = [p["query"] for p in result["gated"]["retrieval_probes"]]
    assert queries == ["user diet_goal plant-based food"]
