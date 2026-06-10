"""Centralized configuration for Mnemo.

Every tunable lives here — DSN, backend selection, embedding dimension, and the
similarity/confidence thresholds from spec §5. No magic numbers scattered in code.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
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
    """Legacy similarity setting; value equivalence now governs no-op decisions."""

    dedup_sim: float = Field(0.92, ge=0.0, le=1.0)
    """Legacy similarity setting; identity aliases govern safe deduplication."""

    confidence_floor: float = Field(0.5, ge=0.0, le=1.0)
    """Drop extracted facts whose confidence is below this."""

    search_floor: float = Field(0.5, ge=0.0, le=1.0)
    """Minimum cosine for a vector-only search hit (keyword matches bypass this)."""

    search_candidate_multiplier: int = Field(8, ge=1, le=100)
    search_reinforce: bool = True
    session_ttl_seconds: float = Field(86400.0, gt=0.0)

    # Verification runs off the event loop. Optional NLI models load only when selected.
    verifier_backend: Literal["heuristic", "cross_encoder", "ollama", "openai"] = "heuristic"
    verifier_model: str = "cross-encoder/nli-deberta-v3-small"
    verifier_entailment_threshold: float = Field(0.99, ge=0.0, le=1.0)
    verifier_timeout_seconds: float = Field(20.0, gt=0.0)
    verifier_max_retries: int = Field(1, ge=0, le=3)
    verifier_fallback_backend: Literal["none", "ollama", "openai"] = "none"
    verifier_fallback_model: str | None = None

    # --- Retrieval rerank (spec §5: rerank over top-k, not a custom index) ---
    search_w_rel: float = 0.5
    """Rerank weight: relevance (best of cosine / FTS / keyword hit)."""

    search_w_rec: float = 0.2
    """Rerank weight: recency of last recall."""

    search_w_imp: float = 0.3
    """Rerank weight: importance/10."""

    recency_gamma: float = 0.995
    """Per-hour recency decay in the rerank (Generative Agents)."""

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

    # --- Decay / reinforcement (spec §4 Layer 5; MemoryBank R = e^(−t/S)) ---
    decay_lambda_base: float = 0.16
    """Base decay rate; effective λ = base · (1 − (importance/10) · 0.8)."""

    decay_archive_below: float = Field(0.35, ge=0.0, le=1.0)
    """Retention below this archives the fact (reversible tier demotion)."""

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
    extractor_max_retries: int = Field(2, ge=0, le=2)
    extractor_timeout_seconds: float = Field(120.0, gt=0.0)

    job_lease_seconds: float = Field(60.0, gt=0.0)
    """A 'processing' job whose updated_at is older than this is presumed orphaned
    (its worker died mid-extraction) and is eligible to be reclaimed. The
    worker refreshes the lease every third of this interval."""

    job_max_attempts: int = Field(3, ge=1)
    """A job is marked 'failed' (not requeued forever) once attempts exceeds this.
    Default 3 = the initial try + 2 retries (kept in lockstep with extractor_max_retries)."""

    job_retry_base_seconds: float = Field(1.0, ge=0.0)
    job_retry_max_seconds: float = Field(60.0, ge=0.0)
    worker_poll_seconds: float = Field(0.5, gt=0.0)
    worker_enabled: bool = True
    decay_interval_seconds: float = Field(3600.0, gt=0.0)
    namespace: str = "default"
    user_id: str = "default"
    agent_id: str = "default"

    @model_validator(mode="after")
    def validate_configuration(self) -> Settings:
        # A backend switch must not silently send Ollama model names to OpenAI.
        if self.backend == "openai":
            if "embed_model" not in self.model_fields_set:
                self.embed_model = "text-embedding-3-small"
            if "embed_dim" not in self.model_fields_set:
                self.embed_dim = 1536
            if "extractor_model" not in self.model_fields_set:
                self.extractor_model = "gpt-4o-mini"
        if not 1 <= self.embed_dim <= 2000:
            raise ValueError("embed_dim must be 1..2000 for the vector HNSW index")
        if self.ephemeral_floor > self.durable_cutoff:
            raise ValueError("ephemeral_floor must not exceed durable_cutoff")
        if self.job_retry_base_seconds > self.job_retry_max_seconds:
            raise ValueError("job_retry_base_seconds must not exceed job_retry_max_seconds")
        if not 0 < self.recency_gamma <= 1:
            raise ValueError("recency_gamma must be in (0, 1]")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached Settings instance."""
    return Settings()
