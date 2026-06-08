# Memory-System Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the competitive-analysis improvements (hybrid FTS+vector search, compact MCP responses, bitemporal completion, observe dedup, labeled eval, predicate vocabulary, memory_entity removal) woven into the spec's C2–C6 council milestones in the spec-mandated order: eval harness first, then the quality gate, then decay + retrieval.

**Architecture:** Everything builds on the existing append-only event store (`memory_fact` HEAD pointer + `memory_event` log + `memory_current` view). New work: a pure-function quality gate (`mnemo/quality.py`), an eval harness (`mnemo/eval.py`) that measures naive-vs-gated precision/recall, decay math (`mnemo/decay.py`), a search rewrite (Postgres FTS + cosine + composite rerank), and small ergonomic wins (MCP compact results + pool, observe hash-dedup, `invalidate()`).

**Tech Stack:** Python 3.12, asyncpg, Pydantic v2, Postgres 16 + pgvector (`vector(768)`, Ollama default), FastMCP, pytest(-asyncio) against a disposable DB.

## Global Constraints

- **Append-only is sacred**: never `UPDATE`/`DELETE` a `memory_event` payload. State changes = new events; supersession is a flag. Exception (documented in schema): `strength`, `recall_count`, `last_used` are mutable decay bookkeeping, never payload.
- **Demote, don't drop**: borderline facts → `session` tier; only true noise is not stored.
- **Eval-first**: the eval harness + baseline lands before any gate knob exists (spec §11 order: eval → Layers 0–3 → decay/tiering; consolidation is OUT of this plan, ships last separately).
- All thresholds/weights live in `mnemo/config.py` (env prefix `MNEMO_`), never inline.
- Embedding dim is **768** (Ollama `nomic-embed-text`); tests use deterministic fake/planted embedders — hermetic, no network.
- TDD: failing test → minimal code → green → commit. One commit per task, message style `C2: ...` / `IMP: ...`.
- Verify per task: `uv run pytest -q` (needs `make up` once) and `uv run ruff check . && uv run black --check .`
- Python 3.12 type hints everywhere; `from __future__ import annotations` at top of every new module.

**File map (who owns what):**

| File | Role in this plan |
|---|---|
| `mnemo/core.py` (modify) | observe dedup; `invalidate()`; search rewrite; `reinforce`; `_max_cosine`; `add(embedding=...)` |
| `mnemo/quality.py` (create) | Verifier protocol + HeuristicVerifier, specificity, write_score, tier_for |
| `mnemo/eval.py` (create) | labeled conversation + naive-vs-gated harness + CLI |
| `mnemo/decay.py` (create) | retention math + decay_sweep |
| `mnemo/extraction.py` (modify) | gate wiring in worker; extractor prompt emits importance |
| `mnemo/mcp_server.py` (modify) | compact search results; connection pool (R2) |
| `mnemo/config.py` (modify) | gate weights/cutoffs, vocab, search weights |
| `mnemo/models.py` (modify) | `ExtractedFact.importance` |
| `migrations/0004_retrieval.sql` (create) | view: `last_used` + `valid_to` filter; drop `memory_entity` |
| `web/` (modify) | tier/importance columns (C6) |

---

## Phase A — standalone wins (no gate dependency)

### Task 1: `observe()` exact-duplicate skip (Mem0-v3 hash dedup)

**Files:**
- Modify: `mnemo/core.py` (the `observe` method, ~line 570)
- Test: `tests/test_twotier.py`

**Interfaces:**
- Consumes: existing `MnemoStore.observe(turn_id, text, session_id, *, role)`.
- Produces: same signature; returns early (no cache row, no job, no embed call) when an un-reconciled `fast_cache` row with identical `raw_text` exists for the same namespace/user/session.

- [ ] **Step 1: Write the failing test** (append to `tests/test_twotier.py`)

```python
class CountingEmbedder:
    """FakeEmbedder wrapper that counts embed() calls."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls = 0
        self.dim = inner.dim

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return self.inner.embed(text)


async def test_observe_skips_exact_duplicate_turn(
    db: asyncpg.Connection, fake_embedder
) -> None:
    from mnemo.core import MnemoStore

    counting = CountingEmbedder(fake_embedder)
    store = MnemoStore(db, counting)

    await store.observe("t1", "I use Postgres.", SESSION)
    calls_after_first = counting.calls
    await store.observe("t2", "I use Postgres.", SESSION)  # identical text, new turn

    # Second observe was a no-op: no new cache row, no new job, no embed call.
    assert await db.fetchval("SELECT count(*) FROM fast_cache") == 1
    assert await db.fetchval("SELECT count(*) FROM extraction_job") == 1
    assert counting.calls == calls_after_first

    # A genuinely different turn still goes through.
    await store.observe("t3", "I also use Redis.", SESSION)
    assert await db.fetchval("SELECT count(*) FROM fast_cache") == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_twotier.py::test_observe_skips_exact_duplicate_turn -v`
Expected: FAIL — counts are 2/2 (duplicate stored).

- [ ] **Step 3: Implement** — in `mnemo/core.py`, at the top of `observe()` **before** the `self._embed(text)` call, insert:

```python
        # Exact-duplicate turn for this session (Mem0-v3-style hash dedup): re-observing
        # identical text would re-embed, re-extract, and double-store the same belief
        # source. md5 comparison keeps the check index-friendly and cheap.
        duplicate = await self.conn.fetchval(
            """
            SELECT 1 FROM fast_cache
            WHERE namespace=$1 AND user_id=$2 AND session_id=$3
              AND md5(raw_text) = md5($4)
            LIMIT 1
            """,
            self.namespace,
            self.user_id,
            session_id,
            text,
        )
        if duplicate:
            return
```

- [ ] **Step 4: Run tests to verify green**

Run: `uv run pytest tests/test_twotier.py -q` then `uv run pytest -q`
Expected: all PASS (the exactly-once handshake tests must stay green).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/core.py tests/test_twotier.py
git commit -m "IMP: observe() skips exact-duplicate turns (hash dedup on the hot path)"
```

### Task 2: Compact MCP search results + connection pool (R2)

**Files:**
- Modify: `mnemo/mcp_server.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `MnemoStore.search/get/blame` (unchanged).
- Produces: `memory_search` now returns compact dicts with EXACTLY the keys `fact_id, subject, predicate, value, trust, tier, score, source` (claude-mem-style index → drill down with `memory_get`/`memory_blame`). `_run()` uses a lazy module-level `asyncpg.Pool` in production; the test override path keeps connect-per-call (pools are event-loop-bound; pytest creates a loop per test).

- [ ] **Step 1: Write the failing tests** (replace the search assertions in `tests/test_mcp.py`)

In `test_mcp_add_search_blame_revert_roundtrip`, replace the two `memory_search` result checks:

```python
    results = await srv.memory_search("database")
    assert any(f["value"] == "MongoDB" for f in results)
    # Compact contract: exactly these keys, nothing else (token-efficient index).
    assert set(results[0].keys()) == {
        "fact_id", "subject", "predicate", "value", "trust", "tier", "score", "source"
    }
```

And in `test_mcp_observe_then_log` replace the fast-cache assertion:

```python
    hits = await srv.memory_search("Postgres", session_id="sess-1")
    assert any(h["source"] == "fast_cache" for h in hits)
    assert all("recorded_at" not in h for h in hits)  # full dumps are gone
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_mcp.py -v`
Expected: FAIL — results still carry full `model_dump` keys like `object_text`, `recorded_at`.

- [ ] **Step 3: Implement** — in `mnemo/mcp_server.py`:

Replace `_run` and add the pool (keep `set_overrides`/`reset_overrides` as-is):

```python
_pool: asyncpg.Pool | None = None
_embedder: Any | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            get_settings().dsn, init=register_vector, min_size=1, max_size=5
        )
    return _pool


async def _run(fn: Any) -> Any:
    settings = get_settings()
    if _OVERRIDE:
        # Test seam: short-lived connection per call (pools are loop-bound; the
        # test harness spins up a fresh event loop per test).
        conn = await asyncpg.connect(_OVERRIDE.get("dsn", settings.dsn))
        await register_vector(conn)
        try:
            embedder = _OVERRIDE.get("embedder") or build_embedder(settings)
            return await fn(MnemoStore(conn, embedder, settings=settings))
        finally:
            await conn.close()

    global _embedder
    if _embedder is None:
        _embedder = build_embedder(settings)
    pool = await _get_pool()
    async with pool.acquire() as conn:
        return await fn(MnemoStore(conn, _embedder, settings=settings))
```

Replace `memory_search`'s body:

```python
@mcp.tool()
async def memory_search(query: str, k: int = 8, session_id: str | None = None) -> list[dict]:
    """Search current memory (semantic + keyword). Returns a compact index —
    call memory_get(fact_id) or memory_blame(fact_id) for full detail/history."""
    facts = await _run(lambda s: s.search(query, k=k, session_id=session_id))
    return [
        {
            "fact_id": str(f.fact_id),
            "subject": f.subject,
            "predicate": f.predicate,
            "value": "" if f.value is None else str(f.value),
            "trust": f.trust_level,
            "tier": f.tier,
            "score": round(f.score, 3) if f.score is not None else None,
            "source": f.source,
        }
        for f in facts
    ]
```

- [ ] **Step 4: Verify green** — `uv run pytest tests/test_mcp.py -q && uv run pytest -q` → all PASS.

- [ ] **Step 5: Manual pool smoke** (production path, needs `make up`):

Run: `uv run python -c "
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
    async with stdio_client(StdioServerParameters(command='python', args=['-m','mnemo.mcp_server'])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool('memory_search', {'query': 'database'})
            print('tool ok:', out.isError is False)
anyio.run(main)"`
Expected: `tool ok: True`

- [ ] **Step 6: Lint + commit**

```bash
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/mcp_server.py tests/test_mcp.py
git commit -m "IMP+R2: compact MCP search results (index->detail) + pooled connections"
```

---

## Phase B — eval harness FIRST (spec §11.2, golden rule #2)

### Task 3: labeled dataset + metrics + naive-vs-gated harness (`mnemo/eval.py`, `make eval`)

**Files:**
- Create: `mnemo/eval.py`
- Modify: `Makefile` (replace nothing — add `eval` target)
- Test: `tests/test_eval.py`

**Interfaces:**
- Produces (later tasks rely on these exact names):
  - `EVAL_CONVERSATION: list[tuple[str, str, str]]` — `(turn_id, role, text)`
  - `GROUND_TRUTH: set[tuple[str, str]]`, `FORBIDDEN: set[tuple[str, str]]` — `(predicate, object_text)`
  - `EvalExtractor` — deterministic keyword extractor that mimics naive over-extraction *including its mistakes* (junk, negation-violations, hypotheticals), emitting `ExtractedFact`s. After Task 6 it also emits `importance`.
  - `def metrics(stored: set, truth: set, forbidden: set) -> dict` with keys `stored, precision, recall, f1, false`
  - `async def run_naive(conn, embedder) -> set[tuple[str, str]]` — stores every extracted candidate via `store.add` (bypasses any gate)
  - `async def run_gated(conn, embedder) -> set[tuple[str, str]]` — full `observe()` + `ExtractionWorker` drain pipeline (whatever the pipeline currently does; the gate arrives in Tasks 4–6 and improves this number)
  - `async def evaluate(dsn) -> dict` — `{"naive": metrics..., "gated": metrics...}` on a fresh (truncated) DB
- Consumes: `MnemoStore`, `ExtractionWorker`, `ExtractedFact`, `FakeEmbedder`-compatible embedder.

- [ ] **Step 1: Write the failing tests** (`tests/test_eval.py`)

```python
"""Eval harness: metrics math + the naive baseline mechanics.

The gated-beats-naive assertion (CLAUDE.md test #4) lands with the gate (Task 6);
here we prove the harness itself: metrics are correct and naive stores the junk.
"""

from __future__ import annotations

import asyncpg

from mnemo.eval import FORBIDDEN, GROUND_TRUTH, metrics, run_naive


def test_metrics_math() -> None:
    truth = {("a", "1"), ("b", "2"), ("c", "3"), ("d", "4")}
    forbidden = {("z", "9")}
    stored = {("a", "1"), ("b", "2"), ("z", "9")}  # 2 right, 1 forbidden
    m = metrics(stored, truth, forbidden)
    assert m["stored"] == 3
    assert abs(m["precision"] - 2 / 3) < 1e-9
    assert abs(m["recall"] - 2 / 4) < 1e-9
    assert m["false"] == 1
    assert 0 < m["f1"] < 1


def test_metrics_empty_store() -> None:
    m = metrics(set(), {("a", "1")}, set())
    assert m == {"stored": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0, "false": 0}


async def test_naive_baseline_stores_junk_and_false_facts(
    db: asyncpg.Connection, fake_embedder
) -> None:
    stored = await run_naive(db, fake_embedder)
    m = metrics(stored, GROUND_TRUTH, FORBIDDEN)
    # The naive path must exhibit the disease: junk stored, forbidden facts asserted.
    assert m["false"] > 0
    assert m["precision"] < 1.0
    # ...but it does capture the must-keep facts (recall is not the naive problem).
    assert m["recall"] == 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_eval.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mnemo.eval'`.

- [ ] **Step 3: Implement `mnemo/eval.py`**

```python
"""Precision/recall junk-rate eval — the north star (spec §7).

Feeds one labeled conversation through two pipelines and reports
precision / recall / F1 / false-fact count for each:
- naive: store every extracted candidate (what incumbent memory layers do)
- gated: the full observe() -> worker pipeline (the product)

Deterministic by construction: EvalExtractor is keyword-driven (mimicking a naive
LLM extractor, mistakes included) and the embedder is injected. Swap a labeled
LoCoMo/LongMemEval conversation + a real extractor for the headline number.

Run: make eval   (python -m mnemo.eval)
"""

from __future__ import annotations

import asyncio

import asyncpg

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import apply_migrations, register_vector
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact

# ---- the labeled conversation (turn_id, role, text) ----------------------
# Hand-labeled, LoCoMo-flavored: durable facts, transient noise, a negation trap,
# a hypothetical trap, assistant chatter, and repetition (dedup pressure).
EVAL_CONVERSATION: list[tuple[str, str, str]] = [
    ("t01", "user", "Hey, I'm Sai. I'm a data engineer and I use PostgreSQL for my main project."),
    ("t02", "assistant", "Nice! PostgreSQL is great. By the way it's a sunny 72F here today."),
    ("t03", "user", "Today I'm just debugging the auth service, nothing major."),
    ("t04", "user", "I don't use MongoDB, never liked it."),
    ("t05", "user", "What if I switched to a graph database someday?"),
    ("t06", "user", "My team lead is Priya and we ship on Fridays."),
    ("t07", "user", "Also I prefer Postgres, just confirming."),
    ("t08", "assistant", "Got it, logging that you prefer PostgreSQL."),
    ("t09", "user", "2 + 2 is 4 right?"),
    ("t10", "user", "I live in Austin and work remote."),
    ("t11", "user", "My main language is Python, though I dabble in Rust."),
    ("t12", "user", "Maybe I'll learn Go someday, who knows."),
    ("t13", "assistant", "The weather in Austin is 95F today!"),
    ("t14", "user", "We deploy with Docker Compose on a single VM."),
    ("t15", "user", "I don't work weekends anymore."),
    ("t16", "user", "My team lead is Priya, as I said."),
    ("t17", "user", "Right now I'm waiting for CI to finish."),
    ("t18", "user", "My timezone is US Central."),
]

GROUND_TRUTH: set[tuple[str, str]] = {
    ("name", "Sai"),
    ("role", "data engineer"),
    ("preferred_database", "PostgreSQL"),
    ("team_lead", "Priya"),
    ("ship_day", "Friday"),
    ("location", "Austin"),
    ("preferred_language", "Python"),
    ("deploy_method", "Docker Compose"),
    ("timezone", "US Central"),
}

FORBIDDEN: set[tuple[str, str]] = {
    ("uses_database", "MongoDB"),        # negation: "I don't use MongoDB"
    ("uses_database", "graph database"),  # hypothetical: "what if I switched"
    ("learning_language", "Go"),          # hypothetical: "maybe I'll learn Go"
}


class EvalExtractor:
    """Deterministic keyword extractor mimicking naive over-extraction, mistakes
    included — junk (weather/math/transient), negation and hypothetical traps."""

    RULES: list[tuple[str, str, str, int]] = [
        # (trigger substring in lowercase turn, predicate, object, importance)
        ("i'm sai", "name", "Sai", 8),
        ("data engineer", "role", "data engineer", 8),
        ("postgresql", "preferred_database", "PostgreSQL", 8),
        ("prefer postgres", "preferred_database", "PostgreSQL", 8),
        ("mongodb", "uses_database", "MongoDB", 6),                # WRONG: negated
        ("graph database", "uses_database", "graph database", 6),  # WRONG: hypothetical
        ("team lead is priya", "team_lead", "Priya", 7),
        ("ship on fridays", "ship_day", "Friday", 7),
        ("72f", "weather", "72F sunny", 2),                        # junk
        ("95f", "weather", "95F", 2),                              # junk
        ("debugging the auth service", "currently_debugging", "auth service", 3),
        ("live in austin", "location", "Austin", 8),
        ("language is python", "preferred_language", "Python", 8),
        ("learn go", "learning_language", "Go", 5),                # WRONG: hypothetical
        ("docker compose", "deploy_method", "Docker Compose", 7),
        ("waiting for ci", "current_activity", "waiting for CI", 2),  # transient junk
        ("timezone is us central", "timezone", "US Central", 7),
        ("2 + 2", "math_fact", "2 + 2 = 4", 1),                    # noise
    ]

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        t = text.lower()
        out: list[ExtractedFact] = []
        for trigger, predicate, obj, importance in self.RULES:
            if trigger in t:
                out.append(
                    ExtractedFact(
                        subject="user",
                        predicate=predicate,
                        object=obj,
                        confidence=0.9,
                        importance=importance,
                        assertion_type=(
                            "direct_user_statement" if role == "user" else "agent_inference"
                        ),
                    )
                )
        return out


def metrics(
    stored: set[tuple[str, str]],
    truth: set[tuple[str, str]],
    forbidden: set[tuple[str, str]],
) -> dict:
    tp = len(stored & truth)
    precision = tp / len(stored) if stored else 0.0
    recall = tp / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "stored": len(stored),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false": len(stored & forbidden),
    }


async def _stored_pairs(conn: asyncpg.Connection) -> set[tuple[str, str]]:
    rows = await conn.fetch("SELECT predicate, object_text FROM memory_current")
    return {(r["predicate"], r["object_text"]) for r in rows}


async def run_naive(conn: asyncpg.Connection, embedder) -> set[tuple[str, str]]:
    """Naive baseline: store everything the extractor emits, no gate."""
    store = MnemoStore(conn, embedder)
    extractor = EvalExtractor()
    for _turn_id, role, text in EVAL_CONVERSATION:
        for cand in extractor.extract(text, role):
            await store.add(
                cand.subject,
                cand.predicate,
                cand.object,
                provenance=(
                    "direct_user_statement" if role == "user" else "agent_inference"
                ),
                confidence=cand.confidence,
                importance=cand.importance,
            )
    return await _stored_pairs(conn)


async def run_gated(conn: asyncpg.Connection, embedder) -> set[tuple[str, str]]:
    """The product: observe() -> extraction worker (gate included once built)."""
    store = MnemoStore(conn, embedder)
    worker = ExtractionWorker(conn, embedder, EvalExtractor())
    for turn_id, role, text in EVAL_CONVERSATION:
        await store.observe(turn_id, text, "eval-session", role=role)
    while await worker.process_one():
        pass
    return await _stored_pairs(conn)


async def _truncate(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "TRUNCATE memory_event, memory_fact, memory_commit, fast_cache, "
        "extraction_job RESTART IDENTITY CASCADE"
    )


async def evaluate(dsn: str | None = None) -> dict:
    """Run both pipelines on a clean DB and return {'naive': ..., 'gated': ...}."""
    from tests.conftest import FakeEmbedder  # deterministic; no model, no network

    settings = get_settings()
    conn = await asyncpg.connect(dsn or settings.test_dsn)
    await register_vector(conn)
    embedder = FakeEmbedder(settings.embed_dim)
    try:
        await apply_migrations(conn)
        await _truncate(conn)
        naive = metrics(await run_naive(conn, embedder), GROUND_TRUTH, FORBIDDEN)
        await _truncate(conn)
        gated = metrics(await run_gated(conn, embedder), GROUND_TRUTH, FORBIDDEN)
        return {"naive": naive, "gated": gated}
    finally:
        await conn.close()


def report(result: dict) -> None:
    def line(name: str, r: dict) -> str:
        return (
            f"  {name:<6}: stored {r['stored']:3d} | P {r['precision'] * 100:5.1f}% | "
            f"R {r['recall'] * 100:5.1f}% | F1 {r['f1'] * 100:5.1f}% | false {r['false']}"
        )

    print("PRECISION / RECALL JUNK-RATE EVAL (naive vs gated — the north star)")
    print(line("NAIVE", result["naive"]))
    print(line("GATED", result["gated"]))


if __name__ == "__main__":
    report(asyncio.run(evaluate()))
```

Note: importing `FakeEmbedder` from `tests.conftest` requires tests to be importable — they are (`tests/__init__.py` exists). If ruff complains about the intra-function import, it's deliberate (keeps `mnemo/` free of a test dependency at import time).

- [ ] **Step 4: `ExtractedFact.importance`** — the eval constructs it, so add the field now (also needed by Task 6). In `mnemo/models.py`, add to `ExtractedFact`:

```python
    importance: int = Field(default=5, ge=1, le=10)
```

- [ ] **Step 5: Makefile target** — add after `test:` block:

```make
eval:  ## Precision/recall junk-rate eval — the north star
	$(RUN) python -m mnemo.eval
```

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/test_eval.py -v` → 3 PASS.
Run: `make eval` → prints NAIVE and GATED lines. Expected right now: NAIVE has `false 3`, precision well under 100%; GATED differs only via the confidence floor (numbers close to naive — **that's the point**: this is the baseline the gate must beat).
Run: `uv run pytest -q` → all PASS.

- [ ] **Step 7: Record the baseline + commit**

Paste the `make eval` output into the commit message body:

```bash
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/eval.py mnemo/models.py Makefile tests/test_eval.py
git commit -m "C-EVAL: precision/recall harness + labeled conversation + naive baseline (make eval)"
```

---

## Phase C — the quality gate (C2, Layers 1–2 + vocabulary)

### Task 4: config knobs + predicate vocabulary

**Files:**
- Modify: `mnemo/config.py`, `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces (exact names later tasks use): `Settings.w_imp=0.4, w_spec=0.3, w_nov=0.3, w_src=0.4, transient_penalty=0.15, durable_cutoff=0.70, ephemeral_floor=0.45, predicate_vocab: list[str]`.

- [ ] **Step 1: Failing test** (append to `tests/test_config.py`)

```python
def test_gate_knobs_have_spec_defaults() -> None:
    s = _clean()
    assert (s.w_imp, s.w_spec, s.w_nov) == (0.4, 0.3, 0.3)
    assert s.w_src == 0.4
    assert s.durable_cutoff == 0.70
    assert s.ephemeral_floor == 0.45
    assert "preferred_database" in s.predicate_vocab
    assert "timezone" in s.predicate_vocab
```

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_config.py -v` → AttributeError.

- [ ] **Step 3: Implement** — in `mnemo/config.py` after the `search_floor` field:

```python
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
        "name", "role", "preferred_database", "preferred_language", "team_lead",
        "ship_day", "location", "timezone", "deploy_method", "goal",
        "currently_debugging", "dislikes",
    ]
    """Controlled predicate vocabulary — specificity=1.0 in-vocab, 0.2 otherwise."""
```

Add the same knobs to `.env.example` under the thresholds section (env form, e.g. `MNEMO_DURABLE_CUTOFF=0.70`, `MNEMO_PREDICATE_VOCAB='["name","role",...]'` noting JSON-list syntax for list envs).

- [ ] **Step 4: Verify green + commit**

```bash
uv run pytest tests/test_config.py -q && uv run pytest -q
git add mnemo/config.py .env.example tests/test_config.py
git commit -m "C2a: gate knobs + controlled predicate vocabulary in config"
```

### Task 5: `mnemo/quality.py` — Verifier + scoring + tiering (pure functions)

**Files:**
- Create: `mnemo/quality.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Produces (Task 6 consumes exactly these):
  - `class Verdict(BaseModel): accepted: bool; label: str; reason: str`
  - `class HeuristicVerifier: def verify(self, object_text: str, source_text: str) -> Verdict` (protocol-compatible: any object with that method works — NLI backend slots in later)
  - `def specificity(predicate: str, vocab: list[str]) -> float`
  - `def is_transient(source_text: str) -> bool`
  - `def write_score(*, importance: int, spec: float, novelty: float, from_assistant: bool, transient: bool, settings: Settings) -> float`
  - `def tier_for(score: float, settings: Settings) -> str | None` — `"durable" | "session" | None` (None = don't store)

- [ ] **Step 1: Failing tests** (`tests/test_quality.py`)

```python
"""C2: the quality gate's pure functions — verification, scoring, tiering.

CLAUDE.md test #3: negations + hypotheticals rejected (no false memories);
borderline facts demoted, not dropped (recall protected).
"""

from __future__ import annotations

import pytest

from mnemo.config import Settings
from mnemo.quality import HeuristicVerifier, is_transient, specificity, tier_for, write_score

S = Settings(_env_file=None)
V = HeuristicVerifier()


# ---- verification (Layer 1 pre-filter) -----------------------------------

def test_negation_rejected() -> None:
    v = V.verify("MongoDB", "I don't use MongoDB, never liked it.")
    assert not v.accepted
    assert v.label == "contradiction"


def test_hypothetical_rejected() -> None:
    v = V.verify("graph database", "What if I switched to a graph database someday?")
    assert not v.accepted
    assert v.label == "neutral"


@pytest.mark.parametrize(
    "obj, src",
    [
        ("PostgreSQL", "I use PostgreSQL for my main project."),
        ("Priya", "My team lead is Priya and we ship on Fridays."),
    ],
)
def test_plain_assertion_accepted(obj: str, src: str) -> None:
    v = V.verify(obj, src)
    assert v.accepted
    assert v.label == "entailment"


def test_negation_of_a_different_object_is_not_rejected() -> None:
    # "I don't work weekends" must not poison an unrelated fact from the same turn.
    v = V.verify("Austin", "I don't work weekends anymore, I moved to Austin.")
    assert v.accepted


# ---- scoring + tiering (Layer 2) ------------------------------------------

def test_specificity_vocab_vs_not() -> None:
    assert specificity("preferred_database", S.predicate_vocab) == 1.0
    assert specificity("weather", S.predicate_vocab) == 0.2


def test_is_transient() -> None:
    assert is_transient("Right now I'm waiting for CI to finish.")
    assert not is_transient("My timezone is US Central.")


def test_write_score_high_importance_in_vocab_novel_is_durable() -> None:
    score = write_score(
        importance=8, spec=1.0, novelty=1.0, from_assistant=False, transient=False, settings=S
    )
    assert score >= S.durable_cutoff
    assert tier_for(score, S) == "durable"


def test_junk_scores_below_floor_and_is_dropped() -> None:
    # weather: importance 2, out-of-vocab, transient turn
    score = write_score(
        importance=2, spec=0.2, novelty=1.0, from_assistant=True, transient=True, settings=S
    )
    assert score < S.ephemeral_floor
    assert tier_for(score, S) is None


def test_borderline_is_demoted_to_session_not_dropped() -> None:
    # mid importance, out-of-vocab, novel -> between floor and cutoff
    score = write_score(
        importance=5, spec=0.2, novelty=1.0, from_assistant=False, transient=False, settings=S
    )
    assert S.ephemeral_floor <= score < S.durable_cutoff
    assert tier_for(score, S) == "session"


def test_duplicate_novelty_zero_drags_score_down() -> None:
    fresh = write_score(
        importance=7, spec=1.0, novelty=1.0, from_assistant=False, transient=False, settings=S
    )
    dupe = write_score(
        importance=7, spec=1.0, novelty=0.0, from_assistant=False, transient=False, settings=S
    )
    assert dupe < fresh
```

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_quality.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement `mnemo/quality.py`**

```python
"""The quality gate — Layers 1–2 of the write path (spec §4).

Pure functions + a pluggable Verifier. The heuristic verifier is the free regex
pre-filter; an NLI entailment backend can replace it without touching the gate
(same .verify() shape). All weights/cutoffs come from Settings — tune ONLY
against `make eval`.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from mnemo.config import Settings

NEGATION = re.compile(r"\b(don'?t|do not|never|not|no longer|stopped using)\b", re.I)
HYPOTHETICAL = re.compile(
    r"\b(what if|someday|if i|would i|maybe i'?ll|might|thinking about|who knows)\b", re.I
)
TRANSIENT = re.compile(
    r"\b(today|right now|just|currently|at the moment|this morning|waiting for)\b", re.I
)


class Verdict(BaseModel):
    accepted: bool
    label: str  # entailment | contradiction | neutral
    reason: str


class HeuristicVerifier:
    """Free regex stand-in for NLI entailment (spec §4 Layer 1->2).

    contradiction: the candidate object appears inside a negated source turn.
    neutral: the source turn is hypothetical, not an assertion.
    entailment: everything else (the NLI/LLM backend refines this later).
    """

    def verify(self, object_text: str, source_text: str) -> Verdict:
        src = source_text.lower()
        obj = object_text.lower()
        if HYPOTHETICAL.search(src):
            return Verdict(
                accepted=False, label="neutral", reason="hypothetical, not an assertion"
            )
        if NEGATION.search(src) and obj in src:
            neg_idx = NEGATION.search(src).start()
            obj_idx = src.find(obj)
            if obj_idx > neg_idx:  # the object is inside the negation's scope
                return Verdict(
                    accepted=False,
                    label="contradiction",
                    reason="refused to assert a denied fact",
                )
        return Verdict(accepted=True, label="entailment", reason="asserted by source turn")


def specificity(predicate: str, vocab: list[str]) -> float:
    return 1.0 if predicate in vocab else 0.2


def is_transient(source_text: str) -> bool:
    return bool(TRANSIENT.search(source_text))


def write_score(
    *,
    importance: int,
    spec: float,
    novelty: float,
    from_assistant: bool,
    transient: bool,
    settings: Settings,
) -> float:
    """spec §4 Layer 2: w_imp·(imp/10) + w_spec·spec + w_nov·nov − w_src·assistant − transient."""
    score = (
        settings.w_imp * (importance / 10.0)
        + settings.w_spec * spec
        + settings.w_nov * novelty
    )
    if from_assistant:
        score -= settings.w_src
    if transient:
        score -= settings.transient_penalty
    return max(0.0, score)


def tier_for(score: float, settings: Settings) -> str | None:
    """durable >= cutoff; session >= floor (demote, don't drop); None = true noise."""
    if score >= settings.durable_cutoff:
        return "durable"
    if score >= settings.ephemeral_floor:
        return "session"
    return None
```

- [ ] **Step 4: Verify green + commit**

```bash
uv run pytest tests/test_quality.py -q && uv run pytest -q
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/quality.py tests/test_quality.py
git commit -m "C2b: quality gate pure functions — verifier, write_score, tier_for"
```

### Task 6: wire the gate into the worker (C3) — the eval number moves

**Files:**
- Modify: `mnemo/extraction.py` (worker + Ollama/OpenAI extractor prompt), `mnemo/core.py` (`add(embedding=...)` pass-through + `_max_cosine`)
- Test: `tests/test_twotier.py` (gate integration), `tests/test_eval.py` (the north-star assertion)

**Interfaces:**
- Consumes: `HeuristicVerifier`, `write_score`, `tier_for`, `specificity`, `is_transient` from Task 5; `ExtractedFact.importance` from Task 3.
- Produces: `ExtractionWorker(conn, embedder, extractor, verifier=None, ...)` — verifier defaults to `HeuristicVerifier()`; `MnemoStore.add(..., embedding: list[float] | None = None)` (skips re-embedding when provided); `MnemoStore._max_cosine(vec_literal) -> float` (0.0 when store empty).

- [ ] **Step 1: Failing integration tests** (append to `tests/test_twotier.py`)

```python
async def test_gate_rejects_negation_no_false_memory(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    from mnemo.eval import EvalExtractor

    worker = ExtractionWorker(db, store.embedder, EvalExtractor())
    await store.observe("t1", "I don't use MongoDB, never liked it.", SESSION)
    await worker.process_one()
    assert await db.fetchval("SELECT count(*) FROM memory_current") == 0
    assert await db.fetchval(
        "SELECT count(*) FROM memory_event WHERE object_text='MongoDB'"
    ) == 0  # not even hidden — never stored


async def test_gate_drops_junk_but_demotes_borderline(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    from mnemo.eval import EvalExtractor

    worker = ExtractionWorker(db, store.embedder, EvalExtractor())
    # junk: weather (imp 2, out-of-vocab, assistant turn) -> dropped entirely
    await store.observe("t1", "By the way it's a sunny 72F here today.", SESSION, role="assistant")
    # borderline: debugging (imp 3, in-vocab 'currently_debugging', transient) -> session
    await store.observe("t2", "Today I'm just debugging the auth service.", SESSION)
    while await worker.process_one():
        pass

    assert await db.fetchval(
        "SELECT count(*) FROM memory_event WHERE object_text LIKE '72F%'"
    ) == 0
    row = await db.fetchrow(
        "SELECT tier, write_score, reason FROM memory_current "
        "WHERE predicate='currently_debugging'"
    )
    assert row is not None, "borderline fact must be demoted, not dropped"
    assert row["tier"] == "session"
    assert row["write_score"] is not None
    assert "score=" in row["reason"]


async def test_gate_stores_good_fact_durable_with_importance(
    store: MnemoStore, db: asyncpg.Connection
) -> None:
    from mnemo.eval import EvalExtractor

    worker = ExtractionWorker(db, store.embedder, EvalExtractor())
    await store.observe("t1", "I use PostgreSQL for my main project.", SESSION)
    await worker.process_one()
    row = await db.fetchrow(
        "SELECT tier, importance FROM memory_current WHERE predicate='preferred_database'"
    )
    assert row["tier"] == "durable"
    assert row["importance"] == 8
```

And the north-star test (append to `tests/test_eval.py`):

```python
async def test_gated_beats_naive_the_north_star(_disposable_test_db: str, clean_memory) -> None:
    """CLAUDE.md test #4: gated precision > naive; gated false-count = 0 while
    naive > 0; recall of must-keep facts stays 100%."""
    from mnemo.eval import evaluate

    result = await evaluate(_disposable_test_db)
    assert result["gated"]["precision"] > result["naive"]["precision"]
    assert result["gated"]["false"] == 0
    assert result["naive"]["false"] > 0
    assert result["gated"]["recall"] == 1.0
```

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_twotier.py tests/test_eval.py -v` → the four new tests FAIL (worker stores everything above the confidence floor).

- [ ] **Step 3: core support** — in `mnemo/core.py`:

(a) `add()` signature gains `embedding: list[float] | None = None` (after `reason`), and the embed line becomes:

```python
        if embedding is None:
            embedding = await self._embed(f"{subject} {predicate} {object}")
        vec = to_vector_literal(embedding)
```

(b) New helper next to `_nearest_fact`:

```python
    async def _max_cosine(self, vec_literal: str | None) -> float:
        """Best cosine between a candidate and any HEAD fact (0.0 on empty store)."""
        if vec_literal is None:
            return 0.0
        value = await self.conn.fetchval(
            """
            SELECT max(1 - (embedding <=> $4::vector)) FROM memory_current
            WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND embedding IS NOT NULL
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            vec_literal,
        )
        return float(value) if value is not None else 0.0
```

- [ ] **Step 4: worker gate** — in `mnemo/extraction.py`:

(a) Imports: `from mnemo.db import to_vector_literal` and `from mnemo.quality import HeuristicVerifier, is_transient, specificity, tier_for, write_score`.

(b) `ExtractionWorker.__init__` gains `verifier: Any | None = None` (after `extractor`); store `self.verifier = verifier or HeuristicVerifier()`.

(c) Replace the reconcile loop body (currently: confidence floor → `store.add(...)`) with the full gate:

```python
            for cand in candidates:
                if cand.confidence < self.settings.confidence_floor:
                    continue  # malformed/low-confidence junk (Layer 0 floor)

                # Layer 1: verification — no false memories from negation/hypothetical.
                verdict = self.verifier.verify(str(cand.object), text)
                if not verdict.accepted:
                    continue

                # Layer 2: salience score + tier.
                emb = await asyncio.to_thread(
                    self.embedder.embed, f"{cand.subject} {cand.predicate} {cand.object}"
                )
                novelty = 1.0 - await store._max_cosine(to_vector_literal(emb))
                score = write_score(
                    importance=cand.importance,
                    spec=specificity(cand.predicate, self.settings.predicate_vocab),
                    novelty=novelty,
                    from_assistant=(role == "assistant"),
                    transient=is_transient(text),
                    settings=self.settings,
                )
                tier = tier_for(score, self.settings)
                if tier is None:
                    continue  # true ephemeral noise: below the salience floor

                provenance = ASSERTION_TO_PROVENANCE.get(cand.assertion_type, "agent_inference")
                event = await store.add(
                    cand.subject,
                    cand.predicate,
                    cand.object,
                    kind=cand.kind,
                    provenance=provenance,
                    actor=self.actor,
                    confidence=cand.confidence,
                    source_span={"turn_ids": [job["turn_id"]]},
                    session_id=job["session_id"],
                    importance=cand.importance,
                    write_score=score,
                    tier=tier,
                    reason=f"{verdict.label}; score={score:.2f}",
                    embedding=emb,
                )
                first_event_id = first_event_id or event.event_id
```

(d) In `_SYSTEM_PROMPT`, extend the JSON shape line with `"importance": 1-10` and add two rules: `- importance: 1 (trivia/transient) to 10 (identity-defining durable fact).` and `- NEVER extract facts about the user from assistant turns.` Update both few-shot examples to include `"importance": 8` (Postgres example).

- [ ] **Step 5: Verify green — the number moves**

Run: `uv run pytest -q` → all PASS (including the north-star test).
Run: `make eval` → GATED now shows `false 0`, `R 100.0%`, precision strictly above NAIVE. Copy the two output lines.

- [ ] **Step 6: Commit** (paste eval lines into the body)

```bash
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/extraction.py mnemo/core.py tests/test_twotier.py tests/test_eval.py
git commit -m "C3: gated write path — verify -> score -> tier in the worker (north star green)"
```

---

## Phase D — decay + retrieval (C4 + hybrid search + bitemporal)

### Task 7: migration 0004 — view upgrade + drop `memory_entity`

**Files:**
- Create: `migrations/0004_retrieval.sql`
- Test: `tests/test_schema.py`

**Interfaces:**
- Produces: `memory_current` additionally exposes `last_used, reason, valid_to` and filters `(valid_to IS NULL OR valid_to > now())`; `memory_entity` is gone (spec v3 §3 omits it — dead schema).

- [ ] **Step 1: Failing test** — in `tests/test_schema.py`, remove `"memory_entity"` from `CORE_TABLES` and append:

```python
async def test_memory_entity_dropped_and_view_has_decay_columns(
    db: asyncpg.Connection,
) -> None:
    assert await db.fetchval("SELECT to_regclass('public.memory_entity')") is None
    cols = {
        r["column_name"]
        for r in await db.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='memory_current'"
        )
    }
    assert {"last_used", "valid_to", "reason"} <= cols


async def test_expired_valid_to_hides_fact_from_head(store, db: asyncpg.Connection) -> None:
    ev = await store.add(
        "user", "location", "Austin", provenance="direct_user_statement"
    )
    # Simulate a fact whose world-validity ended (bitemporal filter, spec §5 step 3).
    await db.execute(
        "UPDATE memory_event SET valid_to = now() - interval '1 day' WHERE event_id=$1",
        ev.event_id,
    )
    assert await db.fetchval(
        "SELECT count(*) FROM memory_current WHERE fact_id=$1", ev.fact_id
    ) == 0
```

(The `UPDATE` here is test scaffolding to simulate time passing — production code never mutates `valid_to` on an existing event; `invalidate()` in Task 8 appends.)

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_schema.py -v` → new tests FAIL.

- [ ] **Step 3: Implement `migrations/0004_retrieval.sql`**

```sql
-- 0004_retrieval.sql — bitemporal enforcement + decay columns in HEAD; schema hygiene.
-- Spec v3 §3/§5: memory_current is tier- AND validity-filtered; memory_entity is not
-- part of the v3 schema (was never referenced by code) — dropped.

DROP TABLE IF EXISTS memory_entity;

CREATE OR REPLACE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id,
  f.subject, f.predicate, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level, e.reason,
  e.valid_from, e.valid_to, e.recorded_at,
  e.importance, e.write_score, e.tier, e.strength, e.recall_count, e.last_used
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id
WHERE f.status = 'active'
  AND e.tier IN ('durable', 'session')
  AND (e.valid_to IS NULL OR e.valid_to > now());   -- bitemporal: only currently-true
```

- [ ] **Step 4: Verify green + commit**

```bash
uv run pytest tests/test_schema.py -q && uv run pytest -q   # conftest rebuilds the DB with 0004
make migrate                                                 # apply to the main DB too
git add migrations/0004_retrieval.sql tests/test_schema.py
git commit -m "C4a: bitemporal HEAD filter + decay cols in view; drop dead memory_entity"
```

### Task 8: `invalidate()` — the missing bitemporal op

**Files:**
- Modify: `mnemo/core.py` (new method + one line in `revert`), `mnemo/__init__.py` (sync wrapper)
- Test: `tests/test_council.py`

**Interfaces:**
- Produces: `MnemoStore.invalidate(fact_id, *, actor=None, reason=None) -> Event` — appends an `INVALIDATE` event copying the current payload with `valid_to=now()`, supersedes current, moves HEAD, sets `memory_fact.status='invalidated'`. `revert()` restores `status='active'` (undo is the safety net). Sync `Mnemo.invalidate(fact_id, **kwargs)`.

- [ ] **Step 1: Failing test** (append to `tests/test_council.py`)

```python
async def test_invalidate_hides_fact_reversibly(store, db: asyncpg.Connection) -> None:
    e1 = await store.add(
        "user", "location", "Austin", provenance="direct_user_statement"
    )
    inv = await store.invalidate(e1.fact_id, reason="user moved away")
    assert inv.op == "INVALIDATE"
    assert inv.valid_to is not None
    assert inv.parent_event_id == e1.event_id

    # Hidden from HEAD and search; history fully preserved.
    assert await store.get(e1.fact_id) is None
    assert await store.search("Austin") == []
    assert [e.op for e in await store.blame(fact_id=e1.fact_id)] == ["ADD", "INVALIDATE"]

    # Reversible: revert to the original ADD restores the fact.
    await store.revert(e1.fact_id, e1.event_id)
    restored = await store.get(e1.fact_id)
    assert restored is not None and restored.object_text == "Austin"
```

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_council.py -v` → AttributeError: no `invalidate`.

- [ ] **Step 3: Implement** — in `mnemo/core.py`, after `revert()`:

```python
    async def invalidate(
        self, fact_id: UUID, *, actor: str | None = None, reason: str | None = None
    ) -> Event:
        """Mark a fact no longer true in the world (bitemporal): append an
        INVALIDATE event with valid_to=now(). Reversible via revert()."""
        async with self.conn.transaction():
            fact = await self.conn.fetchrow(
                "SELECT current_event_id FROM memory_fact WHERE fact_id=$1", fact_id
            )
            if fact is None or fact["current_event_id"] is None:
                raise ValueError(f"fact {fact_id} not found")
            current = await self._get_event(fact["current_event_id"])

            object_json = current.object_json
            if object_json is not None and not isinstance(object_json, str):
                object_json = json.dumps(object_json, sort_keys=True)

            row = await self.conn.fetchrow(
                f"""
                INSERT INTO memory_event
                    (fact_id, op, object_text, object_number, object_json,
                     provenance, actor, confidence, trust_level, parent_event_id,
                     importance, tier, reason, valid_to)
                VALUES ($1, 'INVALIDATE', $2, $3, $4::jsonb,
                        $5::mem_provenance, $6, $7, $8::mem_trust, $9,
                        $10, $11::mem_tier, $12, now())
                RETURNING {_EVENT_COLS}
                """,
                fact_id,
                current.object_text,
                current.object_number,
                object_json,
                current.provenance,
                actor,
                current.confidence,
                current.trust_level,
                current.event_id,
                current.importance,
                current.tier,
                reason or "invalidated",
            )
            event = Event.from_row(row)
            await self._supersede(current.event_id, event.event_id)
            await self._set_head(fact_id, event.event_id)
            await self.conn.execute(
                "UPDATE memory_fact SET status='invalidated' WHERE fact_id=$1", fact_id
            )
            return event
```

In `revert()`, after the `_set_head` call, add:

```python
            await self.conn.execute(
                "UPDATE memory_fact SET status='active' WHERE fact_id=$1", fact_id
            )
```

In `mnemo/__init__.py`, after the `revert` wrapper:

```python
    def invalidate(self, fact_id: UUID, **kwargs: Any) -> Event:
        return self._run(lambda s: s.invalidate(fact_id, **kwargs))
```

- [ ] **Step 4: Verify green + commit**

```bash
uv run pytest -q
git add mnemo/core.py mnemo/__init__.py tests/test_council.py
git commit -m "C4b: invalidate() — bitemporal fact retirement, reversible via revert"
```

### Task 9: decay + reinforcement (`mnemo/decay.py`)

**Files:**
- Create: `mnemo/decay.py`
- Modify: `mnemo/config.py` (decay knobs), `mnemo/core.py` (`reinforce`)
- Test: `tests/test_decay.py`

**Interfaces:**
- Produces:
  - Config: `decay_lambda_base=0.16`, `decay_archive_below=0.35`
  - `def retention(*, strength: float, importance: int | None, last_used: datetime, now: datetime, lambda_base: float) -> float` — `exp(-λ_eff · t_days / max(S,1))`, `λ_eff = λ_base·(1 − (imp/10)·0.8)`
  - `async def decay_sweep(store: MnemoStore, *, now: datetime | None = None) -> int` — appends `UPDATE` events (same payload, `tier='ephemeral'`, `reason='archived by decay (reversible)'`, actor `'decay_sweep'`) for faded durable facts; returns count. Append-only: never mutates tier in place.
  - `MnemoStore.reinforce(fact_id) -> None` — `strength+=1, recall_count+=1, last_used=now()` on the live event (documented mutable bookkeeping).

- [ ] **Step 1: Failing tests** (`tests/test_decay.py`)

```python
"""C4: Ebbinghaus decay + recall reinforcement (spec §4 Layer 5).

Archival is an appended event (tier=ephemeral), never an in-place tier mutation —
append-only stays sacred, and revert() un-archives.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg

from mnemo.config import Settings
from mnemo.decay import decay_sweep, retention

S = Settings(_env_file=None)
NOW = datetime(2026, 7, 3, tzinfo=UTC)


def test_retention_decays_over_time_and_importance_slows_it() -> None:
    fresh = retention(
        strength=1.0, importance=5, last_used=NOW, now=NOW, lambda_base=S.decay_lambda_base
    )
    month_old = retention(
        strength=1.0, importance=5, last_used=NOW - timedelta(days=30), now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    month_old_vital = retention(
        strength=1.0, importance=10, last_used=NOW - timedelta(days=30), now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    assert fresh == 1.0
    assert month_old < S.decay_archive_below      # unused mid-importance fact fades out
    assert month_old_vital > month_old            # importance slows decay


def test_strength_from_recall_slows_decay() -> None:
    weak = retention(
        strength=1.0, importance=5, last_used=NOW - timedelta(days=30), now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    reinforced = retention(
        strength=5.0, importance=5, last_used=NOW - timedelta(days=30), now=NOW,
        lambda_base=S.decay_lambda_base,
    )
    assert reinforced > weak


async def test_sweep_archives_faded_fact_reversibly(store, db: asyncpg.Connection) -> None:
    ev = await store.add(
        "user", "old_project", "legacy-api", provenance="direct_user_statement", importance=3
    )
    await db.execute(  # simulate a month of disuse
        "UPDATE memory_event SET last_used = now() - interval '30 days' WHERE event_id=$1",
        ev.event_id,
    )
    archived = await decay_sweep(store)
    assert archived == 1

    # Gone from HEAD, but history shows the archival event and revert restores it.
    assert await store.get(ev.fact_id) is None
    history = await store.blame(fact_id=ev.fact_id)
    assert [e.op for e in history] == ["ADD", "UPDATE"]
    assert history[-1].tier == "ephemeral"
    assert "decay" in history[-1].reason
    await store.revert(ev.fact_id, ev.event_id)
    assert (await store.get(ev.fact_id)).object_text == "legacy-api"


async def test_reinforce_bumps_strength_and_recall(store, db: asyncpg.Connection) -> None:
    ev = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    await store.reinforce(ev.fact_id)
    row = await db.fetchrow(
        "SELECT strength, recall_count FROM memory_event WHERE event_id=$1", ev.event_id
    )
    assert row["strength"] == 2.0
    assert row["recall_count"] == 1
```

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_decay.py -v` → ModuleNotFoundError.

- [ ] **Step 3: Implement**

Config (after `ephemeral_floor` block in `mnemo/config.py`):

```python
    # --- Decay / reinforcement (spec §4 Layer 5; MemoryBank R = e^(−t/S)) ---
    decay_lambda_base: float = 0.16
    """Base decay rate; effective λ = base · (1 − (importance/10) · 0.8)."""
    decay_archive_below: float = Field(0.35, ge=0.0, le=1.0)
    """Retention below this archives the fact (reversible tier demotion)."""
```

`mnemo/decay.py`:

```python
"""Ebbinghaus decay + reinforcement (spec §4 Layer 5, MemoryBank).

R = exp(−λ_eff · t_days / max(S, 1)), λ_eff = λ_base · (1 − (importance/10) · 0.8).
Recall reinforces (S += 1, t → 0). Below the threshold a fact is ARCHIVED — an
appended UPDATE event with tier='ephemeral' — never deleted, always revertible.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from mnemo.core import MnemoStore


def retention(
    *,
    strength: float,
    importance: int | None,
    last_used: datetime,
    now: datetime,
    lambda_base: float,
) -> float:
    imp = (importance if importance is not None else 5) / 10.0
    lam = lambda_base * (1.0 - imp * 0.8)
    t_days = max(0.0, (now - last_used).total_seconds() / 86400.0)
    return math.exp(-lam * t_days / max(strength, 1.0))


async def decay_sweep(store: MnemoStore, *, now: datetime | None = None) -> int:
    """Archive durable/session HEAD facts whose retention has faded. Returns count."""
    now = now or datetime.now(tz=UTC)
    settings = store.settings
    rows = await store.conn.fetch(
        """
        SELECT fact_id, event_id, subject, predicate, object_text, object_number,
               object_json, provenance, confidence, importance, strength, last_used
        FROM memory_current
        WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
        """,
        store.namespace,
        store.user_id,
        store.agent_id,
    )
    archived = 0
    for r in rows:
        score = retention(
            strength=r["strength"],
            importance=r["importance"],
            last_used=r["last_used"],
            now=now,
            lambda_base=settings.decay_lambda_base,
        )
        if score >= settings.decay_archive_below:
            continue
        # Append-only archival: same payload, demoted tier, auditable reason.
        await store.add(
            r["subject"],
            r["predicate"],
            r["object_text"] or r["object_number"] or r["object_json"],
            provenance=r["provenance"],
            actor="decay_sweep",
            confidence=r["confidence"],
            importance=r["importance"] or 5,
            tier="ephemeral",
            reason=f"archived by decay (reversible); retention={score:.2f}",
        )
        archived += 1
    return archived
```

Wait — `store.add()` with the same value hits the exact-no-op guard. `decay_sweep` must bypass routing: use the internal emit instead. Replace the `await store.add(...)` block above with:

```python
        await store._emit_update(
            r["fact_id"],
            r["event_id"],
            r["object_text"] or r["object_number"] or r["object_json"],
            provenance=r["provenance"],
            actor="decay_sweep",
            confidence=float(r["confidence"]),
            trust_level=(await store._get_event(r["event_id"])).trust_level,
            source_span=None,
            valid_from=None,
            embedding=None,
            importance=r["importance"],
            write_score=None,
            tier="ephemeral",
            reason=f"archived by decay (reversible); retention={score:.2f}",
        )
```

`MnemoStore.reinforce` (in `mnemo/core.py`, after `get()`):

```python
    async def reinforce(self, fact_id: UUID) -> None:
        """Recall reinforcement (MemoryBank): S += 1, t -> 0 on the live event.

        strength/recall_count/last_used are the documented mutable decay
        bookkeeping — never payload."""
        await self.conn.execute(
            """
            UPDATE memory_event SET strength = strength + 1,
                   recall_count = recall_count + 1, last_used = now()
            WHERE event_id = (SELECT current_event_id FROM memory_fact WHERE fact_id=$1)
            """,
            fact_id,
        )
```

- [ ] **Step 4: Verify green + commit**

```bash
uv run pytest tests/test_decay.py -q && uv run pytest -q
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/decay.py mnemo/core.py mnemo/config.py tests/test_decay.py
git commit -m "C4c: Ebbinghaus decay sweep (append-only archive) + recall reinforcement"
```

### Task 10: hybrid search — FTS + cosine + composite rerank (+ auto-reinforce)

**Files:**
- Modify: `mnemo/core.py` (`search`), `mnemo/config.py` (rerank weights)
- Test: `tests/test_search.py` (new)

**Interfaces:**
- Consumes: view columns from Task 7 (`last_used`, `importance`), `reinforce` from Task 9.
- Produces: `search()` same signature; candidates = FTS match OR ILIKE OR cosine ≥ `search_floor`; ranked by `w_rel·relevance + w_rec·recency + w_imp·importance/10` where `relevance = GREATEST(cosine, 0.85·fts_hit, 0.80·kw_hit)` and `recency = 0.995^hours_since_last_used`. Returned semantic facts are reinforced (S+1). `Fact.score` carries the composite.

- [ ] **Step 1: Failing tests** (`tests/test_search.py`)

```python
"""C4: hybrid retrieval — FTS stemming, composite rerank, auto-reinforcement."""

from __future__ import annotations

import asyncpg


async def test_fts_stemming_matches_inflected_query(store) -> None:
    # ILIKE '%languages%' can never match 'preferred_language Python';
    # english FTS stems languages -> languag and finds it.
    await store.add(
        "user", "preferred_language", "Python", provenance="direct_user_statement"
    )
    results = await store.search("what languages")
    assert any(f.predicate == "preferred_language" for f in results)


async def test_importance_boosts_rank_between_equal_matches(store) -> None:
    await store.add(
        "user", "project_alpha_note", "uses Postgres", provenance="direct_user_statement",
        importance=2,
    )
    await store.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement",
        importance=9,
    )
    results = await store.search("postgres")
    assert results[0].predicate == "preferred_database"
    assert results[0].score is not None


async def test_search_reinforces_returned_facts(store, db: asyncpg.Connection) -> None:
    ev = await store.add("user", "name", "Sai", provenance="direct_user_statement")
    await store.search("name")
    row = await db.fetchrow(
        "SELECT recall_count FROM memory_event WHERE event_id=$1", ev.event_id
    )
    assert row["recall_count"] == 1
```

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_search.py -v` → first test FAILS (ILIKE can't match), third FAILS (no reinforcement).

- [ ] **Step 3: Config** (after `search_floor`):

```python
    # --- Retrieval rerank (spec §5: rerank over top-k, not a custom index) ---
    search_w_rel: float = 0.5
    search_w_rec: float = 0.2
    search_w_imp: float = 0.3
    recency_gamma: float = 0.995
    """Per-hour recency decay in the rerank (Generative Agents)."""
```

- [ ] **Step 4: Rewrite the semantic block of `search()`** — replace the current SELECT in `search()` with:

```python
        rows = await self.conn.fetch(
            """
            WITH scored AS (
              SELECT fact_id, namespace, user_id, agent_id, session_id, subject, predicate,
                     kind, event_id, object_text, object_number, object_json,
                     provenance, confidence, trust_level, valid_from, recorded_at,
                     importance, write_score, tier, strength, recall_count,
                     COALESCE(1 - (embedding <=> $5::vector), 0) AS rel_vec,
                     to_tsvector('english', subject || ' ' || predicate || ' '
                                 || coalesce(object_text, ''))
                         @@ plainto_tsquery('english', $4) AS fts_hit,
                     (subject ILIKE $6 OR predicate ILIKE $6 OR object_text ILIKE $6) AS kw_hit,
                     power($9, EXTRACT(EPOCH FROM (now() - last_used)) / 3600.0) AS recency
              FROM memory_current
              WHERE namespace=$1 AND user_id=$2 AND agent_id=$3
            )
            SELECT *,
                   ($10 * GREATEST(rel_vec,
                                   CASE WHEN fts_hit THEN 0.85 ELSE 0 END,
                                   CASE WHEN kw_hit THEN 0.80 ELSE 0 END)
                    + $11 * recency
                    + $12 * COALESCE(importance, 5) / 10.0) AS score
            FROM scored
            WHERE fts_hit OR kw_hit OR rel_vec >= $7
            ORDER BY score DESC
            LIMIT $8
            """,
            self.namespace,
            self.user_id,
            self.agent_id,
            query,
            vec,
            like,
            self.settings.search_floor,
            k,
            self.settings.recency_gamma,
            self.settings.search_w_rel,
            self.settings.search_w_rec,
            self.settings.search_w_imp,
        )
        results = [Fact.from_row(r, score=float(r["score"])) for r in rows]
        for fact in results:
            await self.reinforce(fact.fact_id)  # recall reinforcement (S+1, t->0)
```

Keep the fast-cache merge below unchanged.

- [ ] **Step 5: Full-suite pass + fix ripples**

Run: `uv run pytest -q`. Two known ripples to fix if red:
- `tests/test_embeddings.py::test_search_ranks_by_vector_similarity` asserts `score >= 0.9` — the composite tops out lower (0.5·rel + 0.2·rec + 0.3·imp). Change that assertion to `results[0].score is not None and results[0].score == max(f.score for f in results)`.
- Any test asserting exact event-row equality *after calling search* would see `strength/last_used` moved — none do today; if one appears, snapshot before searching.

- [ ] **Step 6: Eval re-run + commit**

Run: `make eval` — confirm GATED numbers didn't regress (recall must stay 100%).

```bash
uv run ruff check --fix . && uv run black . && uv run ruff check .
git add mnemo/core.py mnemo/config.py tests/test_search.py tests/test_embeddings.py
git commit -m "C4d: hybrid FTS+vector search with composite rerank + auto-reinforcement"
```

---

## Phase E — polish (C6)

### Task 11: web UI tier/importance columns + README quality story

**Files:**
- Modify: `web/templates/_rows.html`, `web/templates/fact.html`, `README.md`, `PROJECT_STATUS.md`
- Test: `tests/test_web.py`

**Interfaces:** consumes `Fact.tier`, `Fact.importance` (already exposed by the view since Task 7).

- [ ] **Step 1: Failing test** (append to `tests/test_web.py`, inside a new test using the same override pattern as the existing one)

```python
async def test_list_shows_tier_and_importance(db: asyncpg.Connection, fake_embedder) -> None:
    from mnemo.core import MnemoStore
    from web.app import app, get_store

    store = MnemoStore(db, fake_embedder)
    await store.add(
        "user", "preferred_database", "PostgreSQL",
        provenance="direct_user_statement", importance=8, tier="durable",
    )

    async def _override():
        yield MnemoStore(db, fake_embedder)

    app.dependency_overrides[get_store] = _override
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/")
            assert "durable" in r.text
            assert "imp 8" in r.text
    finally:
        app.dependency_overrides.clear()
```

(Add `import httpx` to the file's imports if not present — it is.)

- [ ] **Step 2: Verify fail** — `uv run pytest tests/test_web.py -v`.

- [ ] **Step 3: Implement** — in `web/templates/_rows.html` add two cells after the trust badge cell (and matching `<th>Tier</th><th>Imp</th>` headers in `list.html` after Trust):

```html
  <td class="py-2.5"><span class="px-2 py-0.5 rounded-full text-xs
    {{ 'bg-indigo-100 text-indigo-700' if f.tier == 'durable' else 'bg-slate-100 text-slate-600' }}">{{ f.tier }}</span></td>
  <td class="py-2.5 text-xs text-slate-500">imp {{ f.importance or 5 }}</td>
```

In `fact.html`'s history loop, after the trust badge, add:

```html
    <span class="text-xs text-slate-400">{{ e.tier }} · imp {{ e.importance or '—' }}</span>
    {% if e.reason %}<span class="text-xs italic text-slate-400">{{ e.reason }}</span>{% endif %}
```

- [ ] **Step 4: README + status** — update `README.md`: lead with the quality story ("stores less and remembers what matters"), add a "The eval" section showing the real `make eval` output block, extend the comparison table rows: `Write-time quality gate (verify/score/tier)`, `Measured precision/recall (make eval)`, `Principled forgetting (Ebbinghaus, reversible)` — Mnemo ✅. Update `PROJECT_STATUS.md`: mark eval/gate/decay/tiering done with the real numbers; consolidation remains the only open layer.

- [ ] **Step 5: Verify + commit**

```bash
uv run pytest -q && make eval
git add web/ README.md PROJECT_STATUS.md tests/test_web.py
git commit -m "C6: web tier/importance columns + README tells the (real) quality story"
```

---

## Self-review notes (already applied)

- **Spec coverage:** eval-first order preserved (Task 3 before 4–6); demote-don't-drop is Task 5/6 behavior + tests; bitemporal = Tasks 7–8; decay = Task 9; rerank-over-top-k (not a custom index) = Task 10; consolidation deliberately absent (spec: ship last, separately).
- **Append-only:** the only in-place writes anywhere are supersession flags, HEAD pointer, `fact.status`, and the documented `strength/recall_count/last_used` bookkeeping. Decay archival appends an event (Task 9 explicitly avoids `store.add`'s no-op guard via `_emit_update`).
- **Type consistency:** `Verdict.accepted/label/reason` (5→6); `ExtractedFact.importance` (3→6); `add(embedding=...)` (6→9 unused, 6 only); `retention(strength, importance, last_used, now, lambda_base)` (9); view columns `last_used/valid_to/reason` (7→9,10,11).
- **Known risk:** Task 10's FTS has no index (HEAD sets are small at MVP scale); if `make eval` or the UI ever feels slow, add a GIN expression index on `memory_event` in an 0005 migration — deliberately out of scope now (YAGNI).
