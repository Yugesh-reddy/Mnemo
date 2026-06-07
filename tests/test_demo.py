"""M6: the §10 rollback scenario, driven hermetically (stub extractor).

Proves the demo's core claim without a live model: the agent's answer flips from the
mis-inferred value back to the original because a human reverted the memory, and the
full history (ADD → UPDATE → REVERT) is preserved.
"""

from __future__ import annotations

import asyncpg

from examples.agent import run_scenario
from mnemo.core import MnemoStore
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact


class _StubExtractor:
    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        if "postgres" in text.lower():
            return [
                ExtractedFact(
                    subject="user",
                    predicate="preferred_database",
                    object="PostgreSQL",
                    confidence=0.97,
                    assertion_type="direct_user_statement",
                )
            ]
        return []


async def test_rollback_scenario_changes_behavior(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    worker = ExtractionWorker(db, store.embedder, _StubExtractor())
    result = await run_scenario(store, worker)

    assert result["ops"] == ["ADD", "UPDATE", "REVERT"]
    assert "mongo" in str(result["answer_wrong"]).lower()
    assert "postg" in str(result["answer_fixed"]).lower()
    assert str(result["answer_wrong"]).lower() != str(result["answer_fixed"]).lower()
