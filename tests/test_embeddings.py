"""Structured identity and correction routing, plus real Ollama retrieval checks.

Planted vectors exercise high similarity without permitting it to erase value
changes or merge unrelated identities. Live tests check the same behavior using
Ollama embeddings when the local model is available.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import httpx
import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.sqlite import Connection

DIM = 768


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    from mnemo.embedder import HashEmbedder

    embedder = HashEmbedder(dim=768)
    vector = embedder.embed("PostgreSQL")
    assert vector == HashEmbedder(dim=768).embed("PostgreSQL")
    assert len(vector) == 768
    assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=1e-6)
    assert embedder.embed("MySQL") != vector


def test_hash_backend_builds_hash_embedder_without_network(monkeypatch) -> None:
    from mnemo.embedder import HashEmbedder, build_embedder

    def no_http_client(*args, **kwargs):
        raise AssertionError("hash embeddings must not construct a network client")

    monkeypatch.setattr(httpx, "Client", no_http_client)
    settings = Settings(_env_file=None, backend="hash", embed_dim=64)
    embedder = build_embedder(settings)
    assert isinstance(embedder, HashEmbedder)
    assert embedder.dim == 64
    assert len(embedder.embed("PostgreSQL")) == 64


def _planted(*coords: float) -> list[float]:
    """A unit 768-vector with the given leading components (rest zero)."""
    v = list(coords) + [0.0] * (DIM - len(coords))
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _at_cosine(c: float, *, axis: int = 0) -> list[float]:
    """Unit vector whose cosine with the ``axis`` basis vector is exactly ``c``."""
    coords = [0.0] * (axis + 2)
    coords[axis] = c
    coords[axis + 1] = math.sqrt(max(0.0, 1.0 - c * c))
    return _planted(*coords)


class PlantedEmbedder:
    """Returns pre-planted vectors per exact text; far-from-everything otherwise."""

    dim = DIM

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    def embed(self, text: str) -> list[float]:
        if text in self.mapping:
            return self.mapping[text]
        v = [0.0] * DIM
        v[-1] = 1.0  # orthogonal to all planted (low-index) vectors
        return v


def _store(db: Connection, mapping: dict[str, list[float]]) -> MnemoStore:
    # _env_file=None => deterministic default thresholds regardless of any local .env
    return MnemoStore(db, PlantedEmbedder(mapping), settings=Settings(_env_file=None))


# ---- same identity, aliases versus changed values ----------------------


async def test_same_key_high_cosine_restatement_is_noop(db: Connection) -> None:
    base = _planted(1.0, 0.0)
    mapping = {
        "user preferred_database PostgreSQL": base,
        "user preferred_database Postgres": _at_cosine(0.95),
        "user preferred_database MongoDB": _at_cosine(0.30),
    }
    store = _store(db, mapping)

    e1 = await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement"
    )
    # "Postgres" differs textually but is semantically near-identical -> no churn.
    e2 = await store.add(
        "user", "preferred_database", "Postgres", provenance="direct_user_statement"
    )
    assert e2.event_id == e1.event_id
    assert await db.fetchval("SELECT count(*) FROM memory_event WHERE fact_id=?1", e1.fact_id) == 1

    # "MongoDB" is a genuinely different value -> UPDATE.
    e3 = await store.add("user", "preferred_database", "MongoDB", provenance="agent_inference")
    assert e3.op == "UPDATE"
    assert e3.fact_id == e1.fact_id


# ---- structured predicate aliases -------------------------------------


async def test_known_predicate_and_object_aliases_resolve_to_existing_fact(
    db: Connection,
) -> None:
    mapping = {
        "user db_engine PostgreSQL": _at_cosine(1.0, axis=2),
        "user database_system Postgres": _at_cosine(0.95, axis=2),
    }
    store = _store(db, mapping)

    e1 = await store.add("user", "db_engine", "PostgreSQL", provenance="direct_user_statement")
    assert e1.op == "ADD"

    # The narrow predicate and object aliases resolve to one existing event.
    e2 = await store.add("user", "database_system", "Postgres", provenance="agent_inference")
    assert e2.event_id == e1.event_id
    assert e2.fact_id == e1.fact_id
    assert await db.fetchval("SELECT count(*) FROM memory_fact") == 1


async def test_new_key_low_cosine_creates_separate_fact(db: Connection) -> None:
    mapping = {
        "user db_engine PostgreSQL": _at_cosine(1.0, axis=2),
        "user favorite_color blue": _at_cosine(0.10, axis=2),
    }
    store = _store(db, mapping)

    await store.add("user", "db_engine", "PostgreSQL", provenance="direct_user_statement")
    e2 = await store.add("user", "favorite_color", "blue", provenance="direct_user_statement")
    assert e2.op == "ADD"
    assert await db.fetchval("SELECT count(*) FROM memory_fact") == 2


async def test_search_ranks_by_vector_similarity(db: Connection) -> None:
    # Two unrelated facts; a query vector close to one of them returns it first.
    mapping = {
        "user db_engine PostgreSQL": _at_cosine(1.0, axis=2),
        "user favorite_color blue": _at_cosine(0.0, axis=4),
        "what database": _at_cosine(0.97, axis=2),  # close to the db_engine fact
    }
    store = _store(db, mapping)
    await store.add("user", "db_engine", "PostgreSQL", provenance="direct_user_statement")
    await store.add("user", "favorite_color", "blue", provenance="direct_user_statement")

    results = await store.search("what database")
    assert results
    assert results[0].predicate == "db_engine"
    # score is a composite (relevance + recency + importance), so assert ordering,
    # not the raw cosine magnitude.
    assert results[0].score is not None
    assert results[0].score == max(f.score for f in results)


# ---- live Ollama --------------------------------------------------------


def _ollama_up() -> bool:
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ollama_up(), reason="Ollama server not running")
def test_ollama_embedder_live_returns_expected_dim() -> None:
    from mnemo.embedder import OllamaEmbedder

    emb = OllamaEmbedder()
    try:
        vec = emb.embed("the user prefers PostgreSQL")
        assert len(vec) == DIM
        assert any(x != 0.0 for x in vec)
    finally:
        emb.close()


@pytest.mark.skipif(not _ollama_up(), reason="Ollama server not running")
async def test_live_embeddings_preserve_the_demo_correction(db) -> None:
    from mnemo.embedder import OllamaEmbedder

    emb = OllamaEmbedder()
    try:
        store = MnemoStore(db, emb)
        first = await store.add("user", "preferred_database", "PostgreSQL")
        changed = await store.add("user", "preferred_database", "MongoDB")
        assert changed.op == "UPDATE" and changed.fact_id == first.fact_id
        assert (await store.get(first.fact_id)).value == "MongoDB"
        results = await store.search("database")
        assert results and results[0].value == "MongoDB"
    finally:
        emb.close()


# ---- local backend (in-process model) ------------------------------------------


def test_local_backend_resolves_model_dimension_and_floor(tmp_path) -> None:
    settings = Settings(_env_file=None, backend="local", model_dir=tmp_path)
    assert settings.embed_model == "BAAI/bge-small-en-v1.5"
    assert (settings.embed_dim, settings.search_floor) == (384, 0.60)
    base = Settings(_env_file=None, backend="local", embed_model="BAAI/bge-base-en-v1.5")
    assert (base.embed_dim, base.search_floor) == (768, 0.55)
    explicit = Settings(_env_file=None, backend="local", search_floor=0.7, embed_dim=384)
    assert (explicit.embed_dim, explicit.search_floor) == (384, 0.7)
    with pytest.raises(ValueError, match="MNEMO_EMBED_DIM"):
        Settings(_env_file=None, backend="local", embed_model="some/other-model")
    assert Settings(_env_file=None, model_dir="~/x").model_dir == Path.home() / "x"


def test_local_embedder_loads_lazily_and_never_downloads(tmp_path) -> None:
    from mnemo.embedder import LocalEmbedder, LocalModelUnavailable, build_embedder

    settings = Settings(_env_file=None, backend="local", model_dir=tmp_path)
    embedder = build_embedder(settings)
    assert isinstance(embedder, LocalEmbedder) and embedder._model is None
    with pytest.raises(LocalModelUnavailable) as raised:
        embedder.embed("PostgreSQL")  # empty cache: refuses instead of downloading
    assert raised.value.model == "BAAI/bge-small-en-v1.5" and raised.value.cache_dir == tmp_path
    assert list(tmp_path.iterdir()) == [] or not any(tmp_path.rglob("*.onnx"))


def _local_model_ready() -> bool:
    from mnemo.embedder import local_model_ready

    settings = Settings(backend="local")
    return local_model_ready(settings.embed_model, settings.model_dir)


@pytest.mark.skipif(not _local_model_ready(), reason="local embedding model not downloaded")
def test_local_embedder_live_is_deterministic_and_normalized() -> None:
    from mnemo.embedder import build_embedder

    embedder = build_embedder(Settings(backend="local"))
    vector = embedder.embed("the user prefers PostgreSQL")
    assert len(vector) == embedder.dim
    assert vector == embedder.embed("the user prefers PostgreSQL")
    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, abs_tol=1e-3)


def test_checking_for_the_local_model_creates_nothing(tmp_path) -> None:
    from mnemo.embedder import local_model_ready

    cache = tmp_path / ".mnemo" / "models"
    assert not local_model_ready("BAAI/bge-small-en-v1.5", cache)
    assert not (tmp_path / ".mnemo").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_mnemo_directories_are_private_however_they_are_created(tmp_path) -> None:
    import stat

    from mnemo.embedder import private_dirs

    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    private_dirs(existing / "home" / ".mnemo" / "models")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    for path in (
        existing / "home",
        existing / "home" / ".mnemo",
        existing / "home" / ".mnemo" / "models",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_private_dirs_creates_nested_dirs_and_stops_at_missing_root(tmp_path) -> None:
    from mnemo.embedder import private_dirs

    target = tmp_path / "a" / "b" / "c"
    private_dirs(target)
    assert target.is_dir()

    # Simulate a nonexistent Windows drive root (e.g. Z:\missing) where parent == self.
    class MissingPath:
        def __init__(self, name: str, parent: MissingPath | None = None) -> None:
            self.name = name
            self._parent = parent
            self.created = False

        @property
        def parent(self) -> MissingPath:
            return self if self._parent is None else self._parent

        def exists(self) -> bool:
            return False

        def mkdir(self, *, mode: int = 0o777, exist_ok: bool = False) -> None:
            raise FileNotFoundError(f"missing drive: {self.name}")

    root = MissingPath("Z:\\")
    child = MissingPath("Z:\\mnemo", root)
    with pytest.raises(FileNotFoundError, match=r"Z:\\"):
        private_dirs(child)  # type: ignore[arg-type]


def _wait_until(predicate, *, timeout: float = 2.0, step: float = 0.01) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def test_local_model_idle_seconds_setting(tmp_path, monkeypatch) -> None:
    from pydantic import ValidationError

    from mnemo.embedder import LocalEmbedder, build_embedder

    default = Settings(_env_file=None, backend="local", model_dir=tmp_path)
    assert default.local_model_idle_seconds == 300.0
    embedder = build_embedder(default)
    assert isinstance(embedder, LocalEmbedder) and embedder.idle_seconds == 300.0

    monkeypatch.setenv("MNEMO_LOCAL_MODEL_IDLE_SECONDS", "45")
    from_env = Settings(_env_file=None, backend="local", model_dir=tmp_path)
    assert from_env.local_model_idle_seconds == 45.0
    assert build_embedder(from_env).idle_seconds == 45.0

    monkeypatch.setenv("MNEMO_LOCAL_MODEL_IDLE_SECONDS", "0")
    disabled = Settings(_env_file=None, backend="local", model_dir=tmp_path)
    assert disabled.local_model_idle_seconds == 0.0

    with pytest.raises(ValidationError):
        Settings(_env_file=None, backend="local", local_model_idle_seconds=-1)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, backend="local", local_model_idle_seconds=float("inf"))
    with pytest.raises(ValueError, match="idle_seconds"):
        LocalEmbedder(model="stub", dim=4, cache_dir=tmp_path, idle_seconds=-0.5)
    with pytest.raises(ValueError, match="idle_seconds"):
        LocalEmbedder(model="stub", dim=4, cache_dir=tmp_path, idle_seconds=float("nan"))


def test_local_embedder_unloads_after_idle_and_reloads_on_next_call(tmp_path, monkeypatch) -> None:
    import weakref

    import mnemo.embedder as emb_mod

    loads = 0
    refs: list[weakref.ref] = []

    class StubModel:
        def embed(self, texts: list[str]):
            for text in texts:
                yield [float(len(text)), 1.0, 2.0, 3.0]

    def fake_loader(model: str, cache_dir: Path, *, offline: bool) -> StubModel:
        nonlocal loads
        assert offline is True
        loads += 1
        instance = StubModel()
        refs.append(weakref.ref(instance))
        return instance

    monkeypatch.setattr(emb_mod, "_text_embedding", fake_loader)
    embedder = emb_mod.LocalEmbedder(model="stub", dim=4, cache_dir=tmp_path, idle_seconds=0.05)
    try:
        assert embedder._model is None and embedder._timer is None
        assert embedder.embed("hello") == [5.0, 1.0, 2.0, 3.0]
        assert loads == 1 and embedder._model is not None and embedder._timer is not None

        assert _wait_until(lambda: embedder._model is None)
        assert embedder._timer is None
        assert refs[0]() is None

        # Next call reloads transparently.
        assert embedder.embed("reloaded") == [8.0, 1.0, 2.0, 3.0]
        assert loads == 2 and embedder._model is not None
    finally:
        embedder.close()
    assert embedder._model is None and embedder._timer is None
    assert refs[-1]() is None


def test_local_embedder_activity_extends_idle_window(tmp_path, monkeypatch) -> None:
    import time

    import mnemo.embedder as emb_mod

    loads = 0

    class StubModel:
        def embed(self, texts: list[str]):
            for _ in texts:
                yield [1.0, 0.0, 0.0, 0.0]

    def fake_loader(model: str, cache_dir: Path, *, offline: bool) -> StubModel:
        nonlocal loads
        loads += 1
        return StubModel()

    monkeypatch.setattr(emb_mod, "_text_embedding", fake_loader)
    embedder = emb_mod.LocalEmbedder(model="stub", dim=4, cache_dir=tmp_path, idle_seconds=0.15)
    try:
        embedder.embed("a")
        time.sleep(0.07)
        embedder.embed("b")
        time.sleep(0.07)
        embedder.embed("c")
        assert loads == 1 and embedder._model is not None

        assert _wait_until(lambda: embedder._model is None)
        assert loads == 1 and embedder._timer is None
    finally:
        embedder.close()


def test_local_embedder_zero_idle_seconds_disables_unloading(tmp_path, monkeypatch) -> None:
    import time

    import mnemo.embedder as emb_mod

    loads = 0

    class StubModel:
        def embed(self, texts: list[str]):
            for _ in texts:
                yield [0.5, 0.5, 0.5, 0.5]

    def fake_loader(model: str, cache_dir: Path, *, offline: bool) -> StubModel:
        nonlocal loads
        loads += 1
        return StubModel()

    monkeypatch.setattr(emb_mod, "_text_embedding", fake_loader)
    embedder = emb_mod.LocalEmbedder(model="stub", dim=4, cache_dir=tmp_path, idle_seconds=0)
    try:
        assert embedder.embed("first") == [0.5, 0.5, 0.5, 0.5]
        assert loads == 1 and embedder._model is not None and embedder._timer is None
        time.sleep(0.08)
        assert embedder._model is not None
        assert embedder.embed("second") == [0.5, 0.5, 0.5, 0.5]
        assert loads == 1 and embedder._timer is None
    finally:
        embedder.close()
    assert embedder._model is None


def test_local_embedder_thread_safety_under_concurrent_embeds_and_unloads(
    tmp_path, monkeypatch
) -> None:
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import mnemo.embedder as emb_mod

    loads = 0
    in_flight = 0
    overlap_detected = False
    state_lock = threading.Lock()

    class StubModel:
        def embed(self, texts: list[str]):
            nonlocal in_flight, overlap_detected
            with state_lock:
                in_flight += 1
                if in_flight > 1:
                    overlap_detected = True
            try:
                time.sleep(0.002)
                for text in texts:
                    yield [float(len(text)), 0.0, 0.0, 1.0]
            finally:
                with state_lock:
                    in_flight -= 1

    def fake_loader(model: str, cache_dir: Path, *, offline: bool) -> StubModel:
        nonlocal loads
        time.sleep(0.005)
        loads += 1
        return StubModel()

    monkeypatch.setattr(emb_mod, "_text_embedding", fake_loader)
    embedder = emb_mod.LocalEmbedder(model="stub", dim=4, cache_dir=tmp_path, idle_seconds=0.02)
    barrier = threading.Barrier(6)
    try:

        def worker(worker_id: int) -> None:
            for round_idx in range(3):
                text = f"w{worker_id}-r{round_idx}"
                vec = embedder.embed(text)
                assert vec == [float(len(text)), 0.0, 0.0, 1.0]
                if round_idx < 2:
                    # Wait for the round to finish, then pause past idle_seconds so the
                    # unload timer fires before all workers race the next reload together.
                    barrier.wait()
                    time.sleep(0.05)

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(worker, i) for i in range(6)]
            for future in futures:
                future.result()

        assert not overlap_detected
        assert loads >= 2  # at least one idle unload + reload occurred across the pauses
        assert _wait_until(lambda: embedder._model is None)
    finally:
        embedder.close()
    assert embedder._model is None and embedder._timer is None
