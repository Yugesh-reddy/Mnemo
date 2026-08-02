"""Embeddings from a model inside Mnemo (default), Ollama, OpenAI, or non-semantic hashes.

The core is async; ``embed`` here is sync and the store calls it via
``asyncio.to_thread`` so a network round-trip or model run never blocks the event
loop. The local backend runs an ONNX model in process, so no model server is needed.
The hash backend supports tests and direct-memory demos without any model.
"""

from __future__ import annotations

import ctypes
import gc
import hashlib
import math
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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


class LocalModelUnavailable(RuntimeError):
    """The local embedding model is not downloaded (or cannot be loaded)."""

    def __init__(self, model: str, cache_dir: Path, reason: str) -> None:
        super().__init__(f"{model} in {cache_dir}: {reason}")
        self.model = model
        self.cache_dir = cache_dir
        self.reason = reason


def _text_embedding(model: str, cache_dir: Path, *, offline: bool) -> Any:
    from fastembed import TextEmbedding
    from loguru import logger

    # fastembed logs a cache miss as an ERROR before raising; the raise is enough.
    logger.disable("fastembed")
    return TextEmbedding(model, cache_dir=str(cache_dir), local_files_only=offline)


class LocalEmbedder:
    """Embeddings from an ONNX model run inside this process (fastembed). No server.

    The model loads on the first call, so tools that need no embedding never load
    it, and it is never downloaded here: ``mnemo install`` downloads it into
    ``cache_dir`` (see :func:`download_local_model`). When ``idle_seconds > 0``,
    a daemon timer releases the model after that much idle time and the next call
    reloads it transparently. Queries and memories are embedded the same way, as
    with the other backends.
    """

    def __init__(
        self, *, model: str, dim: int, cache_dir: Path, idle_seconds: float = 300.0
    ) -> None:
        if not math.isfinite(idle_seconds) or idle_seconds < 0:
            raise ValueError("idle_seconds must be a finite number >= 0")
        self.model = model
        self.dim = dim
        self.cache_dir = cache_dir
        self.idle_seconds = float(idle_seconds)
        self._lock = threading.Lock()
        self._model: Any = None
        self._last_used: float = 0.0
        self._timer: threading.Timer | None = None
        self._timer_generation: int = 0

    def _loaded(self) -> Any:
        if self._model is None:
            try:
                self._model = _text_embedding(self.model, self.cache_dir, offline=True)
            except ValueError as exc:
                raise LocalModelUnavailable(self.model, self.cache_dir, str(exc)) from exc
        return self._model

    def _arm_timer_locked(self, delay: float) -> None:
        self._timer_generation += 1
        generation = self._timer_generation
        timer = threading.Timer(delay, self._unload_if_idle, args=(generation,))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _unload_if_idle(self, generation: int) -> None:
        unloaded = False
        with self._lock:
            if generation != self._timer_generation or self._model is None:
                return
            remaining = (self._last_used + self.idle_seconds) - time.monotonic()
            if remaining > 0:
                self._arm_timer_locked(remaining)
                return
            self._model = None
            self._timer = None
            unloaded = True
        if unloaded:
            _release_memory()

    def embed(self, text: str) -> list[float]:
        # One lock covers loading, inference and idle unloading: calls are a few ms each.
        with self._lock:
            try:
                vector = next(iter(self._loaded().embed([text])))
            finally:
                if self._model is not None and self.idle_seconds > 0:
                    self._last_used = time.monotonic()
                    if self._timer is None:
                        self._arm_timer_locked(self.idle_seconds)
        if len(vector) != self.dim:
            raise ValueError(f"{self.model} returned dim {len(vector)}, expected {self.dim}")
        return [float(x) for x in vector]

    def close(self) -> None:
        """Cancel any pending idle timer and release the loaded model."""
        unloaded = False
        with self._lock:
            self._timer_generation += 1
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._model is not None:
                self._model = None
                unloaded = True
        if unloaded:
            _release_memory()


def _release_memory() -> None:
    """Free the unloaded model's objects and, on glibc, hand the freed heap back to the
    OS: without malloc_trim Linux keeps about 70 MB of the ~90 MB the model freed
    (measured November 21, 2026). macOS reclaims freed pages lazily on its own."""
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (OSError, AttributeError):  # not glibc (e.g. musl)
            pass


def private_dirs(path: Path) -> None:
    """Create ``path`` and any missing parents readable only by this user (0700)."""
    missing = []
    while not path.exists():
        missing.append(path)
        parent = path.parent
        if parent == path:
            break
        path = parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)


def local_model_ready(model: str, cache_dir: Path) -> bool:
    """True when the model loads from the cache without a download. Writes nothing."""
    if not cache_dir.is_dir():
        return False
    try:
        _text_embedding(model, cache_dir, offline=True)
    except ValueError:
        return False
    return True


def download_local_model(model: str, cache_dir: Path) -> None:
    """Download the model into cache_dir (from Hugging Face) so the server can load it."""
    private_dirs(cache_dir)
    _text_embedding(model, cache_dir, offline=False)


def build_embedder(settings: Settings | None = None) -> Embedder:
    """Construct the configured embedder; hash vectors are non-semantic."""
    s = settings or get_settings()
    if s.backend == "hash":
        return HashEmbedder(s.embed_dim)
    if s.backend == "local":
        return LocalEmbedder(
            model=s.embed_model,
            dim=s.embed_dim,
            cache_dir=s.model_dir,
            idle_seconds=s.local_model_idle_seconds,
        )
    if s.backend == "openai":
        return OpenAIEmbedder(
            model=s.embed_model,
            api_key=s.openai_api_key,
            base_url=s.openai_base_url,
            dim=s.embed_dim,
        )
    return OllamaEmbedder(model=s.embed_model, host=s.ollama_host, dim=s.embed_dim)


def model_id(embedder: Any) -> str:
    """Which model made a vector. Search compares only vectors from the same model."""
    embedder = getattr(embedder, "wrapped", embedder)
    kind = type(embedder).__name__.removesuffix("Embedder").lower() or "custom"
    model = getattr(embedder, "model", None)
    return f"{kind}:{model}" if isinstance(model, str) and model else kind


def vector_blob(vector: list[float]) -> bytes:
    """float32 bytes, the form sqlite-vec reads."""
    from array import array

    return array("f", vector).tobytes()
