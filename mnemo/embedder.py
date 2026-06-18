"""Embeddings via Ollama (default), OpenAI, or deterministic non-semantic hashes.

The core is async; ``embed`` here is sync (simple httpx) and the store calls it via
``asyncio.to_thread`` so a network round-trip never blocks the event loop. The hash
backend supports tests and direct-memory demos without a model server.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

import httpx

from mnemo.config import Settings, get_settings
from mnemo.telemetry import record_usage


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into a fixed-length vector."""

    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashEmbedder:
    """Deterministic SHA-256 vectors for model-free demos and tests. NOT semantic.

    Identical text gives an identical L2-normalized vector. Similarity between
    different texts does not measure meaning; use keyword search for this backend.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        raw = b""
        i = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(f"{i}:{text}".encode()).digest()
            i += 1
        values = [
            (int.from_bytes(raw[j * 4 : j * 4 + 4], "big") / 2**31) - 1.0 for j in range(self.dim)
        ]
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [value / norm for value in values]


class OllamaEmbedder:
    """Local embeddings via an Ollama server (default: nomic-embed-text, 768-dim)."""

    def __init__(
        self,
        *,
        model: str = "nomic-embed-text",
        host: str = "http://localhost:11434",
        dim: int = 768,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.dim = dim
        self._client = httpx.Client(timeout=timeout)

    def embed(self, text: str) -> list[float]:
        resp = self._client.post(
            f"{self.host}/api/embeddings", json={"model": self.model, "prompt": text}
        )
        resp.raise_for_status()
        record_usage(self, resp.json())
        vec = resp.json()["embedding"]
        if len(vec) != self.dim:
            raise ValueError(f"{self.model} returned dim {len(vec)}, expected {self.dim}")
        return [float(x) for x in vec]

    def close(self) -> None:
        self._client.close()


class OpenAIEmbedder:
    """Embeddings via the OpenAI API (default: text-embedding-3-small, 1536-dim)."""

    def __init__(
        self,
        *,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        dim: int = 1536,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("OpenAI backend requires an API key (set OPENAI_API_KEY)")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dim = dim
        self._client = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"})

    def embed(self, text: str) -> list[float]:
        resp = self._client.post(
            f"{self.base_url}/embeddings", json={"model": self.model, "input": text}
        )
        resp.raise_for_status()
        record_usage(self, resp.json())
        vec = resp.json()["data"][0]["embedding"]
        if len(vec) != self.dim:
            raise ValueError(f"{self.model} returned dim {len(vec)}, expected {self.dim}")
        return [float(x) for x in vec]

    def close(self) -> None:
        self._client.close()


def build_embedder(settings: Settings | None = None) -> Embedder:
    """Construct the configured embedder; hash vectors are non-semantic."""
    s = settings or get_settings()
    if s.backend == "hash":
        return HashEmbedder(s.embed_dim)
    if s.backend == "openai":
        return OpenAIEmbedder(
            model=s.embed_model,
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            dim=s.embed_dim,
        )
    return OllamaEmbedder(model=s.embed_model, host=s.ollama_host, dim=s.embed_dim)
