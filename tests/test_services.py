"""Service probes shared by the direct server and mnemo-install."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

from mnemo import services
from mnemo.config import Settings


@pytest.mark.parametrize(
    ("installed", "model", "found"),
    [
        (["nomic-embed-text:latest"], "nomic-embed-text", True),
        (["nomic-embed-text:v1.5"], "nomic-embed-text", True),
        (["nomic-embed-text-v2:latest"], "nomic-embed-text", False),
        (["nomic-embed-text:latest"], "nomic-embed-text:v1.5", False),
        ([], "nomic-embed-text", False),
    ],
)
def test_model_names_match_with_or_without_a_tag(installed, model, found):
    assert services.has_model(installed, model) is found


async def test_store_problem_is_none_for_a_store_or_no_file_and_actionable_otherwise(
    _disposable_test_db, tmp_path
):
    assert await services.store_problem(Settings(_env_file=None, db=_disposable_test_db)) is None
    missing = Settings(_env_file=None, db=tmp_path / "new" / "mnemo.db")
    assert await services.store_problem(missing) is None  # mnemo install creates it
    junk = tmp_path / "junk.db"
    junk.write_text("not a database " * 100)
    problem = await services.store_problem(Settings(_env_file=None, db=junk))
    assert "not a readable Mnemo store" in problem and str(junk) in problem


def test_embedding_errors_name_the_fix():
    settings = Settings(_env_file=None, backend="ollama", ollama_host="http://localhost:11434")
    request = httpx.Request("POST", "http://localhost:11434/api/embeddings")
    missing = httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )
    assert "ollama pull nomic-embed-text" in services.service_error(missing, settings).message
    refused = httpx.ConnectError("refused", request=request)
    assert "ollama serve" in services.service_error(refused, settings).message
    assert services.service_error(ValueError("x"), settings) is None


@pytest.mark.parametrize(
    ("exc", "fix"),
    [
        (sqlite3.OperationalError("no such table: memory_mutation_receipt"), "mnemo install"),
        (sqlite3.OperationalError("no such column: identity_mode"), "mnemo install"),
        (sqlite3.OperationalError("database is locked"), "Try again"),
        (sqlite3.DatabaseError("file is not a database"), "not a readable Mnemo store"),
    ],
)
def test_store_errors_name_the_fix(exc, fix):
    error = services.service_error(exc, Settings(_env_file=None))
    assert error.details == {"service": "store"} and fix in error.message


def test_store_paths_are_shown_from_home():
    from pathlib import Path

    assert services.where(Path.home() / ".mnemo" / "mnemo.db") == "~/.mnemo/mnemo.db"
    assert services.where("/srv/mnemo.db") == "/srv/mnemo.db"


def test_a_missing_local_model_says_to_run_the_installer(tmp_path):
    from mnemo.embedder import LocalModelUnavailable

    settings = Settings(_env_file=None, backend="local")
    missing = LocalModelUnavailable("BAAI/bge-small-en-v1.5", tmp_path, "Could not load model")
    error = services.service_error(missing, settings)
    assert error.details == {"service": "local"} and "mnemo install" in error.message
    unknown = LocalModelUnavailable("x/y", tmp_path, "Model x/y is not supported in TextEmbedding")
    assert "MNEMO_EMBED_MODEL" in services.service_error(unknown, settings).message


async def test_memories_without_a_vector_from_this_model_are_counted(_disposable_test_db, tmp_path):
    from uuid import uuid4

    from mnemo.core import MnemoStore, reindex
    from mnemo.direct import DirectMemory
    from mnemo.embedder import HashEmbedder
    from mnemo.sqlite import connect

    class OtherEmbedder(HashEmbedder):
        """Another model: its vectors are stored under a different model name."""

    settings = Settings(_env_file=None, db=_disposable_test_db, backend="hash")
    same, other = HashEmbedder(settings.embed_dim), OtherEmbedder(settings.embed_dim)
    nothing = Settings(_env_file=None, db=tmp_path / "none.db")
    assert await services.missing_vectors(nothing, same) == 0
    assert await services.missing_vectors(settings, same) == 0
    conn = await connect(_disposable_test_db)
    try:
        memory = DirectMemory(MnemoStore(conn, same, settings=settings))
        await memory.create("tests", "run command", "make test-db", request_id=uuid4())
        assert await services.missing_vectors(settings, same) == 0
        assert await services.missing_vectors(settings, other) == 1
        await reindex(conn, other)
        assert await services.missing_vectors(settings, other) == 0
    finally:
        await conn.close()


async def test_a_python_that_cannot_load_the_extension_is_caught_before_any_file(
    tmp_path, monkeypatch
):
    import mnemo.sqlite as sq

    assert sq.capability_problem() is None

    def refuse(raw):
        raise sq.StoreUnavailable("this Python cannot load SQLite extensions. Install …")

    monkeypatch.setattr(sq, "_load_vector_extension", refuse)
    fresh = Settings(_env_file=None, db=tmp_path / "new" / "mnemo.db")
    problem = await services.store_problem(fresh)
    assert "cannot load SQLite extensions" in problem
    assert not (tmp_path / "new").exists()
