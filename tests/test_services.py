"""Service probes shared by the direct server and mnemo-install."""

from __future__ import annotations

import asyncpg
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


async def test_postgres_problem_is_none_when_reachable_and_actionable_when_not(
    _disposable_test_db,
):
    assert (
        await services.postgres_problem(Settings(_env_file=None, dsn=_disposable_test_db)) is None
    )
    down = Settings(_env_file=None, dsn="postgresql://mnemo:secret@127.0.0.1:1/mnemo")
    problem = await services.postgres_problem(down)
    assert "make up" in problem and "127.0.0.1:1/mnemo" in problem and "secret" not in problem


def test_embedding_errors_name_the_fix():
    settings = Settings(_env_file=None, ollama_host="http://localhost:11434")
    request = httpx.Request("POST", "http://localhost:11434/api/embeddings")
    missing = httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )
    assert "ollama pull nomic-embed-text" in services.service_error(missing, settings).message
    refused = httpx.ConnectError("refused", request=request)
    assert "ollama serve" in services.service_error(refused, settings).message
    assert services.service_error(ValueError("x"), settings) is None


@pytest.mark.parametrize(
    "exc",
    [
        asyncpg.UndefinedTableError('relation "memory_mutation_receipt" does not exist'),
        asyncpg.UndefinedColumnError('column "identity_mode" does not exist'),
    ],
)
def test_a_database_behind_on_migrations_says_to_migrate(exc):
    error = services.service_error(exc, Settings(_env_file=None))
    assert error.details == {"service": "postgres"} and "make migrate" in error.message


def test_a_missing_local_model_says_to_run_the_installer(tmp_path):
    from mnemo.embedder import LocalModelUnavailable

    settings = Settings(_env_file=None, backend="local")
    missing = LocalModelUnavailable("BAAI/bge-small-en-v1.5", tmp_path, "Could not load model")
    error = services.service_error(missing, settings)
    assert error.details == {"service": "local"} and "mnemo install" in error.message
    unknown = LocalModelUnavailable("x/y", tmp_path, "Model x/y is not supported in TextEmbedding")
    assert "MNEMO_EMBED_MODEL" in services.service_error(unknown, settings).message


async def test_stored_memories_from_another_model_are_detected(_disposable_test_db):
    from urllib.parse import urlsplit, urlunsplit
    from uuid import uuid4

    from mnemo.core import MnemoStore
    from mnemo.db import apply_migrations, register_vector
    from mnemo.direct import DirectMemory
    from mnemo.embedder import HashEmbedder

    parts = urlsplit(_disposable_test_db)
    admin_dsn = urlunsplit(parts._replace(path="/postgres"))
    name = "mnemo_mismatch_" + uuid4().hex[:12]
    dsn = urlunsplit(parts._replace(path="/" + name))
    admin = await asyncpg.connect(admin_dsn)
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        settings = Settings(_env_file=None, dsn=dsn, backend="hash")
        same = HashEmbedder(settings.embed_dim)
        other = HashEmbedder(settings.embed_dim)
        other_model = lambda text: other.embed(text + " ")  # noqa: E731 - a different "model"
        assert await services.embedding_mismatch(settings, "hash", same.embed) is None  # bare
        conn = await asyncpg.connect(dsn)
        try:
            await apply_migrations(conn, embed_dim=settings.embed_dim)
            await register_vector(conn)
            assert await services.embedding_mismatch(settings, "hash", other_model) is None
            memory = DirectMemory(MnemoStore(conn, same, settings=settings))
            await memory.create("tests", "run command", "make test-db", request_id=uuid4())
        finally:
            await conn.close()
        assert await services.embedding_mismatch(settings, "hash", same.embed) is None
        problem = await services.embedding_mismatch(settings, "hash", other_model)
        assert problem and "different model" in problem and name in problem
        assert "mnemo:mnemo" not in problem  # no credentials
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
