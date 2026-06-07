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

    # --- Extraction worker ---
    extractor_max_retries: int = 2


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
