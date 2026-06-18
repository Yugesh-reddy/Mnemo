"""M6: the §10 rollback scenario, driven hermetically (stub extractor).

Proves the demo's core claim without a live model: the agent's answer flips from the
mis-inferred value back to the original because a human reverted the memory, and the
full history (ADD → UPDATE → REVERT) is preserved.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import asyncpg
import httpx
import pytest
from typer.testing import CliRunner

from examples.agent import run_scenario
from mnemo import Mnemo
from mnemo.core import MnemoStore
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact


class _StubExtractor:
    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        if "postgres" in text.lower():
            return [
                ExtractedFact(
                    subject="user",
                    predicate="uses_database",
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


def test_direct_demo_runs(_disposable_test_db: str, fake_embedder, clean_memory) -> None:
    from examples.direct_memory import run_scenario as run_direct_scenario

    namespace = "demo-test-" + uuid4().hex
    memory = Mnemo(_disposable_test_db, fake_embedder, namespace=namespace)
    messages: list[str] = []
    result = run_direct_scenario(memory, say=messages.append)
    assert result["before"] == "MySQL"
    assert result["after"] == "PostgreSQL"
    assert result["ops"] == ["ADD", "UPDATE", "REVERT"]
    assert result["values"] == ["PostgreSQL", "MySQL", "PostgreSQL"]
    assert len(set(result["event_ids"])) == 3
    assert all(str(event_id) in "\n".join(messages) for event_id in result["event_ids"])
    current = memory.get(result["fact_id"])
    assert current is not None and current.event_id == result["event_ids"][-1]
    assert [fact.value for fact in memory.search("preferred_database", reinforce=False)] == [
        "PostgreSQL"
    ]

    async def assert_no_background_writes() -> None:
        conn = await asyncpg.connect(_disposable_test_db)
        try:
            for table in ("fast_cache", "extraction_job", "quality_decision"):
                assert (
                    await conn.fetchval(
                        f"SELECT count(*) FROM {table} WHERE namespace=$1", namespace
                    )
                    == 0
                )
        finally:
            await conn.close()

    asyncio.run(assert_no_background_writes())


@pytest.mark.parametrize("backend", [None, "ollama", "openai", "hash"])
def test_direct_demo_defaults_to_hash_and_respects_configured_backend(
    monkeypatch, tmp_path, backend
) -> None:
    from examples.direct_memory import demo_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNEMO_BACKEND", raising=False)
    monkeypatch.setenv("MNEMO_WORKER_ENABLED", "false" if backend == "hash" else "true")
    if backend:
        monkeypatch.setenv("MNEMO_BACKEND", backend)
    settings = demo_settings()
    assert settings.backend == (backend or "hash")
    assert settings.worker_enabled is False


def test_direct_demo_rejects_contradictory_hash_worker_configuration(monkeypatch) -> None:
    from examples.direct_memory import demo_settings

    monkeypatch.setenv("MNEMO_BACKEND", "hash")
    monkeypatch.setenv("MNEMO_WORKER_ENABLED", "true")
    with pytest.raises(ValueError, match="MNEMO_WORKER_ENABLED=false"):
        demo_settings()


def test_direct_demo_respects_backend_from_dotenv(monkeypatch, tmp_path) -> None:
    from examples.direct_memory import demo_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNEMO_BACKEND", raising=False)
    (tmp_path / ".env").write_text("MNEMO_BACKEND=ollama\n")
    assert demo_settings().backend == "ollama"


def test_direct_demo_cli_defaults_to_hash_and_uses_fresh_namespaces(
    _disposable_test_db: str, clean_memory, monkeypatch, tmp_path
) -> None:
    from examples.direct_memory import app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNEMO_BACKEND", raising=False)
    monkeypatch.setenv("MNEMO_DSN", _disposable_test_db)

    def no_http_client(*args, **kwargs):
        raise AssertionError("the default demo must not construct a model client")

    monkeypatch.setattr(httpx, "Client", no_http_client)
    namespaces = []
    for _ in range(2):
        result = CliRunner().invoke(app, [])
        assert result.exit_code == 0, result.output
        assert "embeddings: hash (non-semantic)" in result.output
        assert "[5] Read back: PostgreSQL" in result.output
        namespaces.append(
            next(line for line in result.output.splitlines() if line.startswith("namespace: "))
        )
    assert namespaces[0] != namespaces[1]
