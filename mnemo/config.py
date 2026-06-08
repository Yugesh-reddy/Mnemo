"""Centralized configuration for Mnemo.

Every tunable lives here — DSN, backend selection, embedding dimension, and the
similarity/confidence thresholds from spec §5. No magic numbers scattered in code.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Mnemo settings, populated from environment / .env (prefix ``MNEMO_``)."""

    model_config = SettingsConfigDict(
        env_prefix="MNEMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database ---
    dsn: str = "postgresql://mnemo:mnemo@localhost:5432/mnemo"
    test_dsn: str = "postgresql://mnemo:mnemo@localhost:5432/mnemo_test"

    # --- Backend selection ---
    backend: Literal["ollama", "openai"] = "ollama"

    # --- Ollama (default backend) ---
    ollama_host: str = "http://localhost:11434"

    # --- OpenAI (alternate backend) ---
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MNEMO_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = "https://api.openai.com/v1"

    # --- Embedding / extraction models ---
    # Dimension MUST match the vector(N) used in migrations (see CLAUDE.md "Embedding dimension").
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    extractor_model: str = "llama3.2:3b"

    # --- Thresholds (spec §5) ---
    update_sim: float = Field(0.90, ge=0.0, le=1.0)
    """Same fact_key + cosine >= this => UPDATE the fact (otherwise no-op)."""

    dedup_sim: float = Field(0.92, ge=0.0, le=1.0)
    """New key but cosine >= this against an existing fact => UPDATE it (entity resolution)."""

    confidence_floor: float = Field(0.5, ge=0.0, le=1.0)
    """Drop extracted facts whose confidence is below this."""

    search_floor: float = Field(0.5, ge=0.0, le=1.0)
    """Minimum cosine for a vector-only search hit (keyword matches bypass this)."""

    # --- Quality gate (spec §4 Layer 2 / §13; tune ONLY against make eval) ---
    w_imp: float = 0.4
    """write_score weight: importance/10."""

    w_spec: float = 0.3
    """write_score weight: specificity (predicate in the controlled vocabulary)."""

    w_nov: float = 0.3
    """write_score weight: novelty (1 - max cosine to existing HEAD facts)."""

    w_src: float = 0.4
    """Penalty subtracted when the fact came from an assistant turn."""

    transient_penalty: float = 0.15
    """Penalty when the source turn is explicitly transient ('today', 'right now')."""

    durable_cutoff: float = Field(0.70, ge=0.0, le=1.0)
    """write_score >= this => durable; below => demote to session."""

    ephemeral_floor: float = Field(0.45, ge=0.0, le=1.0)
    """write_score < this => true noise: not stored at all."""

    predicate_vocab: list[str] = [
        "name",
        "role",
        "preferred_database",
        "preferred_language",
        "team_lead",
        "ship_day",
        "location",
        "timezone",
        "deploy_method",
        "goal",
        "currently_debugging",
        "dislikes",
    ]
    """Controlled predicate vocabulary — specificity=1.0 in-vocab, 0.2 otherwise."""

    # --- Extraction worker ---
    extractor_max_retries: int = 2

    job_lease_seconds: float = Field(60.0, gt=0.0)
    """A 'processing' job whose updated_at is older than this is presumed orphaned
    (its worker died mid-extraction) and is eligible to be reclaimed. Must exceed the
    worst-case extraction time for your backend."""

    job_max_attempts: int = Field(3, ge=1)
    """A job is marked 'failed' (not requeued forever) once attempts exceeds this.
    Default 3 = the initial try + 2 retries (kept in lockstep with extractor_max_retries)."""


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
