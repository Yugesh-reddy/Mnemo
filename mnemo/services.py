"""What Mnemo depends on at runtime, and what to tell the user when one is missing.

The direct MCP server and ``mnemo install`` share these diagnoses: the store file
(present, readable, migrated by this version) and the embedding backend (the local
model, Ollama with its model, or OpenAI), and whether the stored memories have
vectors from the configured model.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx

from mnemo.config import Settings
from mnemo.embedder import LocalModelUnavailable, model_id
from mnemo.errors import ErrorCode, MnemoError


def where(path: Path | str) -> str:
    """The store file as the user would type it (home shown as ~)."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def service_error(exc: BaseException, settings: Settings) -> MnemoError | None:
    """An actionable SERVICE_UNAVAILABLE for a store or embedding failure, else None."""
    from mnemo.sqlite import StoreUnavailable

    def unavailable(service: str, message: str) -> MnemoError:
        return MnemoError(ErrorCode.SERVICE_UNAVAILABLE, message, service=service)

    location = where(settings.db)
    if isinstance(exc, StoreUnavailable):
        return unavailable("store", str(exc))
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc)
        if "locked" in text or "busy" in text:
            return unavailable(
                "store",
                f"The store at {location} stayed locked by another process for 30 s. "
                "Try again; if it persists, look for a stuck Mnemo process.",
            )
        if "no such table" in text or "no such column" in text:
            return unavailable(
                "store",
                f"The store at {location} is missing Mnemo tables (not set up, or from "
                "another version). Run `mnemo install`.",
            )
        return unavailable("store", f"The store at {location} failed: {text}")
    if isinstance(exc, sqlite3.DatabaseError):
        return unavailable("store", f"{location} is not a readable Mnemo store: {exc}")
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


async def missing_vectors(settings: Settings, embedder: object) -> int:
    """Events in the store without a vector from ``embedder``'s model (0 if no store).

    Search compares a question only with vectors from the same model, so these
    memories are found by their words alone until `mnemo reindex` embeds them.
    """
    from mnemo.sqlite import connect

    if not settings.db.is_file():
        return 0
    conn = await connect(settings.db, create=False)
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM memory_event e WHERE NOT EXISTS ("
            "SELECT 1 FROM memory_embedding m WHERE m.seq = e.seq AND m.model = ?1)",
            model_id(embedder),
        )
    except sqlite3.OperationalError:
        return 0  # not migrated yet, so nothing is stored
    finally:
        await conn.close()


async def store_problem(settings: Settings) -> str | None:
    """None when the store file opens with this Python; else what to do.

    A missing file is fine: `mnemo install` creates it.
    """
    from mnemo.sqlite import StoreUnavailable, connect

    try:
        conn = await connect(settings.db, create=False)
    except StoreUnavailable as exc:
        if not settings.db.exists():
            return None
        return str(exc)
    try:
        await conn.fetchval("SELECT count(*) FROM sqlite_master")
    except sqlite3.DatabaseError as exc:
        return f"{where(settings.db)} is not a readable Mnemo store: {exc}"
    finally:
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
