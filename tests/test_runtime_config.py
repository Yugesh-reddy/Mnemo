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
