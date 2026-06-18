"""Backend switches and validation fail early, before writing incompatible vectors."""

import pytest
from pydantic import ValidationError

from mnemo.config import Settings
from mnemo.db import validate_embedding_dimension


def test_backend_switch_selects_matching_models_and_dimension(monkeypatch):
    for key in ("MNEMO_EMBED_MODEL", "MNEMO_EMBED_DIM", "MNEMO_EXTRACTOR_MODEL"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None, backend="openai")
    assert (settings.embed_model, settings.embed_dim, settings.extractor_model) == (
        "text-embedding-3-small",
        1536,
        "gpt-4o-mini",
    )
    explicit = Settings(_env_file=None, backend="openai", embed_dim=512, embed_model="custom")
    assert (explicit.embed_model, explicit.embed_dim) == ("custom", 512)


@pytest.mark.parametrize(
    "values",
    [
        {"embed_dim": 0},
        {"embed_dim": 2001},
        {"ephemeral_floor": 0.8, "durable_cutoff": 0.7},
        {"job_retry_base_seconds": 10, "job_retry_max_seconds": 1},
        {"recency_gamma": 1.1},
    ],
)
def test_incoherent_configuration_is_rejected(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


async def test_database_dimension_mismatch_fails_before_a_write(db):
    with pytest.raises(ValueError, match="explicitly migrate and re-embed"):
        await validate_embedding_dimension(db, 1536)
    await validate_embedding_dimension(db, 768)


def test_hash_backend_disables_worker_by_default_and_refuses_explicit_worker(monkeypatch):
    monkeypatch.delenv("MNEMO_WORKER_ENABLED", raising=False)
    assert Settings(_env_file=None, backend="hash").worker_enabled is False
    assert Settings(_env_file=None, backend="hash", worker_enabled=False).worker_enabled is False
    with pytest.raises(ValidationError, match="MNEMO_WORKER_ENABLED=false"):
        Settings(_env_file=None, backend="hash", worker_enabled=True)


def test_hash_backend_rejects_worker_enabled_from_environment(monkeypatch):
    monkeypatch.setenv("MNEMO_BACKEND", "hash")
    monkeypatch.setenv("MNEMO_WORKER_ENABLED", "true")
    with pytest.raises(ValidationError, match="MNEMO_WORKER_ENABLED=false"):
        Settings(_env_file=None)


@pytest.mark.parametrize("component", ["extractor", "verifier"])
def test_hash_backend_rejects_extraction_components(component):
    from mnemo.extraction import build_extractor
    from mnemo.quality import build_verifier

    settings = Settings(_env_file=None, backend="hash", worker_enabled=False)
    builder = build_extractor if component == "extractor" else build_verifier
    with pytest.raises(ValueError, match="backend=hash"):
        builder(settings)


@pytest.mark.parametrize("backend", ["hash", "ollama"])
async def test_disabled_worker_never_constructs_extraction_or_schedules_decay(
    _disposable_test_db, fake_embedder, monkeypatch, backend
):
    import mnemo.runtime as runtime

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled background work must not be constructed or scheduled")

    for name in ("build_extractor", "build_verifier", "scheduled_decay"):
        monkeypatch.setattr(runtime, name, forbidden)
    settings = Settings(
        _env_file=None,
        dsn=_disposable_test_db,
        backend=backend,
        embed_dim=fake_embedder.dim,
        worker_enabled=False,
    )
    async with runtime.background_runtime(
        settings, embedder=fake_embedder if backend == "ollama" else None
    ) as state:
        async with state["pool"].acquire() as conn:
            assert await conn.fetchval("SELECT 1") == 1
        assert not state["stop"].is_set()
    assert state["stop"].is_set()
