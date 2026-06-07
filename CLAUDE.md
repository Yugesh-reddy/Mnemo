# CLAUDE.md — Operating Guide for Mnemo

**Read `PROJECT_SPEC.md` first — it is the source of truth.** This file is how to work day-to-day. If anything here conflicts with `PROJECT_SPEC.md`, the spec wins.

---

## What this is
Mnemo — Git for agent memory. An append-only, inspectable, reversible memory layer for LLM agents: `add` / `search` / `blame` / `revert` / `diff` / `log`, on plain Postgres, exposed via an MCP server + Python SDK. The MVP is **done when the rollback demo (spec §10) runs end to end** — nothing else matters until that works.

---

## Golden rules (do not break these)
1. **One milestone at a time.** Follow spec §12 (M0 → M8) in order. Finish and test a milestone before starting the next. Don't jump ahead "while you're in there."
2. **Respect the Non-goals (spec §13).** Do **not** build: the OpenAI proxy, branching/merge, full PR-for-beliefs, multi-tenant auth, graph memory, Memory CI/CD, or benchmark harnesses. If one feels necessary, **stop and ask** — don't build it.
3. **Ask before changing contracts.** Get confirmation before: adding a dependency, changing the §4 schema, or altering the §5 operation semantics. Small reviewable diffs over big rewrites.
4. **Tests ship with code.** Write/extend tests in the same change as the implementation. A milestone isn't done until its tests are green.
5. **Append-only is sacred.** Never write code that `UPDATE`s or `DELETE`s a `memory_event` row's payload. State changes are *new events*; supersession is a flag. If you're mutating history, you've made a mistake.
6. **The SQL is the product.** Keep the data model legible (raw SQL in migration files, thin Python wrappers). Don't hide it behind a heavy ORM.

---

## Commands (wire these up as Makefile targets in M0)
```bash
make up          # docker compose up -d  (Postgres + pgvector)
make migrate     # apply migrations/*.sql in order
make seed        # load demo seed data
make test        # pytest (spins up / uses the disposable test DB)
make lint        # ruff check + black --check
make fmt         # ruff --fix + black
make mcp         # run the MCP server (stdio)
make demo        # run the end-to-end rollback demo (spec §10)
make ui          # run the FastAPI + HTMX web UI
```
Keep `make demo` and `make up` as the two commands a new user needs.

---

## Conventions
- **Python 3.12**, full type hints. `ruff` + `black`. Pydantic v2 for payloads/config (`pydantic-settings`, `.env`).
- **DB:** `asyncpg`; SQL lives in numbered `migrations/000N_*.sql`. Core as-of/diff/HEAD queries stay as readable SQL, not generated.
- **Async core, sync wrappers.** Implement the store async; expose thin sync methods for the CLI/demo.
- **Config centralized** (including embedding dimension — see below). No magic numbers scattered in code; thresholds (similarity 0.90/0.92, confidence floor 0.5) live in config and are covered by tests.
- **Errors:** fail loud in the worker, validate-and-retry on extractor output (max 2), drop malformed/low-confidence facts rather than storing junk.
- **Commits:** one per milestone, clear message (e.g. `M2: core ops + fact-lifecycle test`).

---

## Repo layout (target)
```
mnemo/
  __init__.py        # Mnemo SDK class (public surface)
  core.py            # add/search/blame/revert/diff/log/commit (transactions)
  extraction.py      # async worker: extract -> reconcile -> handshake
  embedder.py        # Embedder abstraction (OpenAI + Ollama)
  models.py          # Pydantic models: Fact, Event, Diff, Commit
  mcp_server.py      # MCP tools wrapping the SDK
  config.py          # settings (DSN, model, dims, thresholds)
migrations/          # 0001_init.sql, 0002_views.sql, ...
web/                 # FastAPI + HTMX UI (list/search, blame/detail+revert, diff)
examples/
  agent.py           # reference CLI agent for the demo
tests/
  test_lifecycle.py  # CANONICAL: ADD->UPDATE->BLAME->REVERT (history immutable)
  test_diff.py
  test_twotier.py    # no stale read; no double-count after reconcile
docker-compose.yml
pyproject.toml
Makefile
PROJECT_SPEC.md
CLAUDE.md
README.md
```

---

## Two tests that must exist and pass
1. **Fact lifecycle (`test_lifecycle.py`)** — `ADD(Postgres)` → `UPDATE(Mongo)` → `BLAME` → `REVERT(Postgres)`. Assert: `memory_current` = Postgres; exactly 3 events in `log`; the first two events are byte-for-byte unchanged after the revert (history is immutable). This test *is* the proof of the core claim.
2. **Two-tier handshake (`test_twotier.py`)** — after `observe()`, `search()` returns the fact *before* extraction completes; after the worker reconciles, the fact appears **exactly once** (semantic), never twice (raw + semantic).

If either of these is red, the project's central promises aren't real yet.

---

## Embedding dimension
Schema defaults to `vector(1536)` (OpenAI `text-embedding-3-small`). For local/free embeddings (Ollama `nomic-embed-text` = 768, `bge-small` = 384), change the dimension in **config + migrations together**. Don't mix dimensions.

---

## Definition of done (MVP)
`docker compose up` + `make demo` runs the §10 scenario; the two tests above pass; the MCP server is callable from an external client; the web UI does search → blame → revert with a visible behavior change; README + the <15s rollback GIF exist. Build the smallest thing that makes the demo real.
