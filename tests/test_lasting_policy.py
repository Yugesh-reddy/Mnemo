"""v10 candidate tiering ("lasting"): verified, non-transient facts with importance >= 5
go durable; decay decides what fades. Configured entirely through settings."""

from __future__ import annotations

import pytest

from mnemo.audit import gate_snapshot
from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact
from mnemo.quality import Verdict, is_transient, tier_for, write_score

LEGACY = Settings(_env_file=None)
LASTING = Settings(
    _env_file=None,
    w_imp=0.6,
    w_spec=0.0,
    w_nov=0.4,
    novelty_mode="identity",
    transient_markers=[
        "today",
        "right now",
        "currently",
        "at the moment",
        "this morning",
        "waiting for",
    ],
)


def test_legacy_defaults_and_fingerprint_are_unchanged():
    assert LEGACY.novelty_mode == "cosine"
    assert "just" in LEGACY.transient_markers
    snapshot = gate_snapshot(LEGACY)
    assert "novelty_mode" not in snapshot and "transient_markers" not in snapshot
    assert snapshot["fingerprint"] == gate_snapshot(Settings(_env_file=None))["fingerprint"]
    lasting = gate_snapshot(LASTING)
    assert lasting["novelty_mode"] == "identity" and "just" not in lasting["transient_markers"]
    assert lasting["fingerprint"] != snapshot["fingerprint"]


@pytest.mark.parametrize(
    ("text", "legacy", "lasting"),
    [
        ("I just baked a chocolate cake for my sister's birthday.", True, False),
        ("Today I'm debugging the auth service.", True, True),
        ("I'm currently waiting for CI.", True, True),
        ("My timezone is US Central.", False, False),
        ("I adjusted the recipe.", False, False),  # 'just' only as a whole word
    ],
)
def test_transient_markers_come_from_settings(text, legacy, lasting):
    assert is_transient(text, LEGACY.transient_markers) is legacy
    assert is_transient(text, LASTING.transient_markers) is lasting
    assert is_transient(text) is legacy  # default markers are the legacy list


@pytest.mark.parametrize(
    ("importance", "transient", "tier"),
    [
        (10, False, "durable"),
        (5, False, "durable"),
        (4, False, "session"),
        (1, False, "session"),
        (7, True, "session"),
        (8, True, "durable"),
        (3, True, None),
    ],
)
def test_lasting_scores_make_importance_five_the_durable_line(importance, transient, tier):
    score = write_score(
        importance=importance,
        spec=0.2,
        novelty=1.0,
        from_assistant=False,
        transient=transient,
        settings=LASTING,
    )
    assert tier_for(score, LASTING) == tier


class OneFact:
    def __init__(self, predicate, value, importance):
        self.fact = ExtractedFact(
            subject="user", predicate=predicate, object=value, importance=importance
        )

    def extract(self, text, role="user"):
        return [self.fact]

    def verify(self, candidate, source_text):
        return Verdict(accepted=True, label="entailment", probability=1.0, reason="s", backend="t")


@pytest.mark.parametrize(("settings", "tier"), [(LEGACY, "session"), (LASTING, "durable")])
async def test_worker_tiers_a_verified_open_vocabulary_fact(
    worker_connections, fake_embedder, settings, tier
):
    conn, _ = worker_connections
    store = MnemoStore(conn, fake_embedder, settings=settings)
    await store.add("user", "learned_to_make", "bread")  # similar existing memory
    stub = OneFact("attended_class", "a class on vegan cuisine", 5)
    worker = ExtractionWorker(conn, fake_embedder, stub, stub, settings=settings)
    await store.observe("t", "I just attended a class on vegan cuisine.", "s")
    assert await worker.process_one()
    row = await conn.fetchrow(
        "SELECT tier, write_score FROM memory_current WHERE predicate='attended_class'"
    )
    assert row["tier"] == tier
    components = await conn.fetchval(
        "SELECT score_components->>'novelty' FROM quality_decision "
        "ORDER BY recorded_at DESC LIMIT 1"
    )
    if settings.novelty_mode == "identity":
        assert float(components) == 1.0
