"""Centralized configuration for Mnemo.

Every tunable lives here: the store file, backend selection, embedding dimension
and the search settings. No magic numbers scattered in code.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_MODELS: dict[str, tuple[int, float, int]] = {
    # model: (dimension, search floor, download size in MB). Measured through the
    # direct search on tests/data/retrieval_set.json (November 14, 2026): the floor
    # keeps unrelated questions from returning memories. bge-small peaks near 300 MB
    # per server process, bge-base near 770 MB.
    "BAAI/bge-small-en-v1.5": (384, 0.60, 67),
    "BAAI/bge-base-en-v1.5": (768, 0.55, 210),
}
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"

# Cosine at or above which a new memory is reported as a near-duplicate of a current
# one (spec §35). Measured on tests/data/duplicate_set.json against
# retrieval_set.json (November 21, 2026): every floor keeps the 40 unrelated memories
# apart. Unmeasured models get no warning.
DUPLICATE_FLOORS: dict[tuple[str, str], float] = {
    ("local", "BAAI/bge-small-en-v1.5"): 0.80,  # 19 of 20 near-duplicates
    ("ollama", "nomic-embed-text"): 0.75,  # 20 of 20
}


class Settings(BaseSettings):
    """Mnemo settings, populated from environment / .env (prefix ``MNEMO_``)."""

    model_config = SettingsConfigDict(
        env_prefix="MNEMO_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Store ---
    db: Path = Path.home() / ".mnemo" / "mnemo.db"
    """The SQLite file every agent shares. Keep it on a local disk, not a synced folder."""

    # --- Backend selection ---
    backend: Literal["ollama", "openai", "hash", "local"] = "local"
    """local (default): an embedding model running inside Mnemo, no model server.
    ollama: a local Ollama server. hash: no model, word matches only. openai: hosted."""

    model_dir: Path = Path.home() / ".mnemo" / "models"
    """Where the local backend keeps its model; `mnemo install` downloads it here."""

    # --- Ollama ---
    ollama_host: str = "http://localhost:11434"

    # --- OpenAI (alternate backend) ---
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MNEMO_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str = "https://api.openai.com/v1"

    # --- Optional Azure host for the direct-memory pilot (not extraction/embeddings) ---
    azure_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MNEMO_AZURE_ENDPOINT", "AZURE_OPENAI_ENDPOINT"),
    )
    azure_deployment: str | None = Field(
        default=None,
        validation_alias=AliasChoices("MNEMO_AZURE_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
    )
    azure_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("MNEMO_AZURE_API_KEY", "AZURE_OPENAI_API_KEY"),
    )

    # --- Embedding model ---
    # Each vector is stored with the model that made it; search uses only this model's.
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768

    # --- Search ---
    search_floor: float = Field(0.5, ge=0.0, le=1.0)
    """Minimum cosine for a vector-only search hit (keyword matches bypass this)."""

    duplicate_floor: float | None = Field(None, ge=0.0, le=1.0)
    """Cosine for reporting a new memory's near-duplicates. Unset: measured per model
    (DUPLICATE_FLOORS), else none reported."""

    search_reinforce: bool = True
    session_ttl_seconds: float = Field(86400.0, gt=0.0)

    # --- Retrieval rerank (spec §5: rerank over top-k, not a custom index) ---
    search_w_rel: float = 0.5
    """Rerank weight: relevance (best of cosine / FTS / keyword hit)."""

    search_w_rec: float = 0.2
    """Rerank weight: recency of last recall."""

    search_w_imp: float = 0.3
    """Rerank weight: importance/10."""

    recency_gamma: float = 0.995
    """Per-hour recency decay in the rerank (Generative Agents)."""

    namespace: str = "default"
    user_id: str = "default"
    agent_id: str = "default"
    actor: str | None = None
    """Who writes through this server (e.g. claude-code, codex); recorded on every change
    but never used to filter reads. Unset: the agent_id."""
    project: str | None = None
    """The direct server's project scope. Unset: detected from its working directory."""

    @model_validator(mode="after")
    def validate_configuration(self) -> Settings:
        self.db = self.db.expanduser()
        self.model_dir = self.model_dir.expanduser()
        if self.backend == "local":
            if "embed_model" not in self.model_fields_set:
                self.embed_model = DEFAULT_LOCAL_MODEL
            known = LOCAL_MODELS.get(self.embed_model)
            if known is None and "embed_dim" not in self.model_fields_set:
                raise ValueError(
                    f"set MNEMO_EMBED_DIM for the local model {self.embed_model!r}; "
                    f"known models: {', '.join(LOCAL_MODELS)}"
                )
            if known is not None:
                if "embed_dim" not in self.model_fields_set:
                    self.embed_dim = known[0]
                if "search_floor" not in self.model_fields_set:
                    self.search_floor = known[1]
        # A backend switch must not silently send Ollama model names to OpenAI.
        if self.backend == "openai":
            if "embed_model" not in self.model_fields_set:
                self.embed_model = "text-embedding-3-small"
            if "embed_dim" not in self.model_fields_set:
                self.embed_dim = 1536
        if "duplicate_floor" not in self.model_fields_set:
            self.duplicate_floor = DUPLICATE_FLOORS.get((self.backend, self.embed_model))
        if not 1 <= self.embed_dim <= 8192:
            raise ValueError("embed_dim must be 1..8192")
        if not 0 < self.recency_gamma <= 1:
            raise ValueError("recency_gamma must be in (0, 1]")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
