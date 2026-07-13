"""What Mnemo depends on at runtime, and what to tell the user when one is missing.

The direct MCP server and ``mnemo-install`` share these diagnoses: Postgres (reachable,
existing, migrated) and the embedding backend (the local model, Ollama with its model,
or OpenAI), and whether a store's memories were embedded by the configured model.
"""

from __future__ import annotations

from collections.abc import Callable

import asyncpg
import httpx

from mnemo.config import Settings
from mnemo.embedder import LocalModelUnavailable
from mnemo.errors import ErrorCode, MnemoError


def where(dsn: str) -> str:
    """host:port/database of a DSN, without credentials."""
    return dsn.rsplit("@", 1)[-1]


def service_error(exc: BaseException, settings: Settings) -> MnemoError | None:
    """An actionable SERVICE_UNAVAILABLE for a Postgres or embedding failure, else None."""

    def unavailable(service: str, message: str) -> MnemoError:
        return MnemoError(ErrorCode.SERVICE_UNAVAILABLE, message, service=service)

    location = where(settings.dsn)
    if isinstance(exc, asyncpg.UndefinedTableError | asyncpg.UndefinedColumnError):
        return unavailable(
            "postgres",
            f"The database at {location} is missing Mnemo tables or columns (not set up, "
            "or older than this version). Run `make migrate` (or `mnemo-install`) in the "
            "Mnemo checkout.",
        )
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return unavailable(
            "postgres", f"The database at {location} does not exist. Check MNEMO_DSN."
        )
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return unavailable(
            "postgres", f"Postgres at {location} rejected the MNEMO_DSN credentials."
        )
    if isinstance(
        exc,
        OSError
        | TimeoutError
        | asyncpg.PostgresConnectionError
        | asyncpg.OperatorInterventionError,
    ):
        return unavailable(
            "postgres",
            f"Can't reach Postgres at {location}. Start Docker, then run `make up` in the "
            "Mnemo checkout.",
        )
    if isinstance(exc, LocalModelUnavailable):
        if "not supported" in exc.reason:
            return unavailable(
                "local",
                f"The embedding model {exc.model!r} is not one Mnemo can run locally. "
                "Unset MNEMO_EMBED_MODEL to use the default.",
            )
        return unavailable(
            "local",
            f"The embedding model {exc.model} is not downloaded to {exc.cache_dir}. "
            "Run `mnemo install` (or `make agents` in a checkout) to download it.",
        )
    if not isinstance(exc, httpx.HTTPError):
        return None
    if settings.backend == "ollama":
        host, model = settings.ollama_host, settings.embed_model
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            return unavailable(
                "ollama",
                f"Ollama at {host} does not have the embedding model {model!r}. "
                f"Run `ollama pull {model}`.",
            )
        if isinstance(exc, httpx.TransportError):
            return unavailable(
                "ollama",
                f"Can't reach Ollama at {host}. Start the Ollama app (or run `ollama serve`).",
            )
        return unavailable("ollama", f"Ollama at {host} failed to embed text: {exc}")
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
        return unavailable("openai", "The OpenAI API rejected OPENAI_API_KEY.")
    return unavailable(settings.backend, f"The {settings.backend} embedding call failed: {exc}")


SAME_MODEL = 0.99
"""Re-embedding a stored memory's text with its own model gives cosine 1.0; Ollama's
nomic-embed-text against the local quantized nomic model gives 0.96 (measured)."""


async def embedding_mismatch(
    settings: Settings, backend: str, embed: Callable[[str], list[float]]
) -> str | None:
    """What to do when the store's newest memories were embedded by another model.

    None for an empty or not-yet-migrated store, or when re-embedding a stored
    memory's text reproduces its vector. Search compares query vectors with stored
    ones, so a different model would silently stop finding existing memories.
    """
    try:
        conn = await asyncpg.connect(settings.dsn, timeout=5)
    except Exception:
        return None  # postgres_problem reports an unreachable database
    try:
        rows = await conn.fetch(
            "SELECT f.subject, f.predicate, e.object_text, e.embedding::text AS vector "
            "FROM memory_event e JOIN memory_fact f USING (fact_id) "
            "WHERE e.embedding IS NOT NULL AND e.object_text IS NOT NULL "
            "ORDER BY e.seq DESC LIMIT 5"
        )
    except asyncpg.PostgresError:
        return None  # not migrated yet, so nothing is stored
    finally:
        await conn.close()
    best = 0.0
    for row in rows:
        stored = [float(x) for x in row["vector"].strip("[]").split(",")]
        fresh = embed(f"{row['subject']} {row['predicate']} {row['object_text']}")
        if len(fresh) == len(stored):
            dot = sum(a * b for a, b in zip(fresh, stored, strict=True))
            norms = sum(a * a for a in fresh) ** 0.5 * sum(b * b for b in stored) ** 0.5
            best = max(best, dot / norms if norms else 0.0)
    if not rows or best >= SAME_MODEL:
        return None
    model = "hash vectors" if backend == "hash" else f"{backend} {settings.embed_model}"
    return (
        f"The memories in {where(settings.dsn)} were embedded by a different model than "
        f"{model}, so search would stop finding them. Install with the backend and model "
        "used before (--backend, MNEMO_EMBED_MODEL), or point MNEMO_DSN at a new database."
    )


async def postgres_problem(settings: Settings) -> str | None:
    """None when Postgres accepts a connection with settings.dsn; else what to do."""
    try:
        conn = await asyncpg.connect(settings.dsn, timeout=5)
    except Exception as exc:
        error = service_error(exc, settings)
        return error.message if error else f"Postgres at {where(settings.dsn)} failed: {exc}"
    await conn.close()
    return None


def ollama_models(host: str, *, timeout: float = 3.0) -> list[str]:
    """Installed model names. Raises httpx.HTTPError when Ollama is unreachable."""
    response = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
    response.raise_for_status()
    return [model["name"] for model in response.json().get("models", [])]


def has_model(installed: list[str], model: str) -> bool:
    """`nomic-embed-text` matches an installed `nomic-embed-text:latest`."""
    if ":" in model:
        return model in installed
    return any(name.split(":", 1)[0] == model for name in installed)


def pull_model(host: str, model: str, *, timeout: float = 1800.0) -> None:
    response = httpx.post(
        f"{host.rstrip('/')}/api/pull", json={"model": model, "stream": False}, timeout=timeout
    )
    response.raise_for_status()
    if response.json().get("status") != "success":
        raise RuntimeError(f"Ollama did not finish pulling {model}: {response.text[:200]}")
