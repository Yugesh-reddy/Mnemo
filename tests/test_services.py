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
