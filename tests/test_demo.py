"""The direct demo: write, guarded update, revert, with the full history preserved."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest

from mnemo import Mnemo
from mnemo.sqlite import connect


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
    assert result["conflict_code"] == "REVISION_CONFLICT"
    assert result["replayed"] is True
    assert len(set(result["event_ids"])) == 3
    assert all(str(event_id) in "\n".join(messages) for event_id in result["event_ids"])
    current = memory.get(result["fact_id"])
    assert current is not None and current.event_id == result["event_ids"][-1]
    assert current.provenance == "agent_inference" and current.trust_level == "low"
    assert [fact.value for fact in memory.search("preferred_database", reinforce=False)] == [
        "PostgreSQL"
    ]

    async def assert_one_receipt_per_write() -> None:
        conn = await connect(_disposable_test_db)
        try:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM memory_mutation_receipt WHERE namespace=?1", namespace
                )
                == 3
            )
        finally:
            await conn.close()

    asyncio.run(assert_one_receipt_per_write())


@pytest.mark.parametrize("backend", [None, "ollama", "openai", "hash"])
def test_direct_demo_defaults_to_hash_and_respects_configured_backend(
    monkeypatch, tmp_path, backend
) -> None:
    from examples.direct_memory import demo_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNEMO_BACKEND", raising=False)
    if backend:
        monkeypatch.setenv("MNEMO_BACKEND", backend)
    settings = demo_settings()
    assert settings.backend == (backend or "hash")


def test_direct_demo_respects_backend_from_dotenv(monkeypatch, tmp_path) -> None:
    from examples.direct_memory import demo_settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNEMO_BACKEND", raising=False)
    (tmp_path / ".env").write_text("MNEMO_BACKEND=ollama\n")
    assert demo_settings().backend == "ollama"


def test_direct_demo_cli_defaults_to_hash_and_never_touches_the_store(
    monkeypatch, tmp_path, capsys
) -> None:
    from examples.direct_memory import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MNEMO_BACKEND", raising=False)
    store = tmp_path / "real" / "mnemo.db"
    monkeypatch.setenv("MNEMO_DB", str(store))

    def no_http_client(*args, **kwargs):
        raise AssertionError("the default demo must not construct a model client")

    monkeypatch.setattr(httpx, "Client", no_http_client)
    namespaces = []
    for _ in range(2):
        main([])
        output = capsys.readouterr().out
        assert "embeddings: hash (non-semantic)" in output
        assert "[5] Read back: PostgreSQL" in output
        namespaces.append(
            next(line for line in output.splitlines() if line.startswith("namespace: "))
        )
    assert namespaces[0] != namespaces[1]
    assert not store.exists()
