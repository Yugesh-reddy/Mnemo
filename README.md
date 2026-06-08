# Mnemo

**Agent memory that stores less and remembers what matters.**

A write-quality gate keeps junk out (salience scoring → verification → tiering → dedup →
decay), provenance tells you where every fact came from, and one-click rollback fixes what
slips through — all on an **append-only, versioned, bitemporal** Postgres store, so every
keep / demote / drop / forget is auditable and reversible. Python SDK + MCP server,
self-hostable and local-first.

## The number (run it yourself: `make eval`)

Same conversation, same extractor — with and without the gate:

```
PRECISION / RECALL JUNK-RATE EVAL (naive vs gated — the north star)
  NAIVE : stored  15 | P  60.0% | R 100.0% | F1  75.0% | false 2
  GATED : stored  10 | P  90.0% | R 100.0% | F1  94.7% | false 0
```

The gate rejects false memories (negations like *"I **don't** use MongoDB"*, hypotheticals
like *"what if I switched…"*), drops junk (weather, math, chit-chat), demotes borderline
facts to a session tier instead of guessing — and keeps **100% of the facts that matter**.
And because every decision is an event in an append-only log, any of it can be audited
(`blame`) and undone (`revert`).

![Mnemo rollback demo](docs/rollback.gif)

> _(Recording the GIF is the last step — see [Record the demo GIF](#record-the-demo-gif).)_

---

## Quickstart (≈30s)

Prerequisites: **Docker**, **Python 3.12**, [**uv**](https://docs.astral.sh/uv/), and
[**Ollama**](https://ollama.com) (default, free/local) — or an OpenAI API key.

```bash
# 1. Models for the default local backend (free, offline)
ollama pull nomic-embed-text     # embeddings (768-dim)
ollama pull llama3.2:3b          # fact extraction

# 2. Set up and run
make install      # uv venv + install
make up           # Postgres 16 + pgvector
make migrate      # apply the schema
make eval         # the hero demo: naive vs gated precision/recall
make demo         # the rollback scenario, end to end
```

`make demo` prints the whole story: the agent learns "PostgreSQL", mis-infers a switch to
"MongoDB", answers wrongly — then a human **reverts** the memory and the answer flips back,
with the full history intact.

Want to do the revert yourself, in a browser?

```bash
python examples/agent.py setup   # leaves the corrupted "MongoDB" state
make ui                          # open http://127.0.0.1:8000
# → search "database" → click the fact → blame → "Revert to this"
```

---

## Why Mnemo

Agent memory has two diseases: it stores ~96–98% junk, and it invents false facts from
negations and hypotheticals (see Mem0 issue #4573 — 97.8% of 10,134 entries were junk).
And when a belief *is* wrong, incumbent systems **silently overwrite** memory — you can't
tell what changed, why, or how to undo it.

Mnemo attacks both:

**The quality gate (the product)** — every turn runs through a write-time pipeline:
- **extract** — salience-first fact decomposition with LLM-assigned importance (1–10)
- **verify** — negations/hypotheticals rejected before they become false memories
- **dedup** — canonical fact identity + embedding similarity route restatements to UPDATE
- **score + tier** — `write_score` from importance/specificity/novelty; **demote
  borderline facts to a session tier, drop only true noise** (recall is protected)
- **decay** — Ebbinghaus forgetting (`R = e^(−t/S)`), recall reinforces, faded facts are
  *archived reversibly*, never deleted

**The versioned store (the foundation)** — an append-only event log makes every gate
decision auditable and reversible:

- **`blame`** — for any belief: which turn introduced it, its score, tier, and reason.
- **`revert`** — roll a fact back to a previous value; history is never destroyed.
- **`invalidate`** — retire a fact that's no longer true (bitemporal `valid_to`), undoable.
- **`diff` / `log`** — how the agent's beliefs changed between two points in time.
- **`search`** — hybrid FTS + vector retrieval, reranked by relevance + recency + importance.
- **provenance & trust** — every belief records *how* it was learned and how much to trust it.

The event log is the source of truth; `memory_current` is just a view of HEAD. State changes
are **new events** — supersession is a flag, never a rewrite. The transparent SQL *is* the
product.

## How Mnemo compares

| Capability | Mem0 / Zep / Letta | **Mnemo** |
|---|:---:|:---:|
| Store & semantic search | ✅ | ✅ |
| Write-time quality gate (verify / score / tier) | ❌ | ✅ |
| Measured precision/recall (`make eval`) | ❌ | ✅ |
| Rejects negation/hypothetical false memories | ❌ | ✅ |
| Demote-don't-drop (recall protected) | ❌ | ✅ |
| Principled forgetting (Ebbinghaus, reversible) | ❌ | ✅ |
| Append-only history (never destroyed) | ❌ | ✅ |
| `blame` — provenance + reason for every belief | ❌ | ✅ |
| `revert` / `invalidate` — undo, bitemporally | partial | ✅ |
| `diff` / time-travel between commits | ❌ | ✅ |
| Trust levels from provenance | partial | ✅ |
| Transparent, inspectable SQL | ❌ | ✅ |
| Self-hostable / local-first (no cloud) | partial | ✅ |
| MCP server + Python SDK | partial | ✅ |

## How it works

- **Event log (`memory_event`)** — immutable, append-only; the source of truth. Ops:
  `ADD` / `UPDATE` / `INVALIDATE` / `REVERT` / `DELETE`.
- **Facts (`memory_fact`)** — stable identity (the "file"); a `current_event_id` HEAD pointer.
- **`memory_current`** — a SQL view = HEAD = the latest non-superseded event per fact.
- **Two-tier memory** — `observe()` writes a synchronous *fast cache* for immediate
  next-turn recall while an async worker extracts structured facts; a cache-invalidation
  **handshake** guarantees a belief shows up *exactly once* (no stale reads, no double counts).
- **Bitemporal + provenance** — events carry valid/recorded times, `provenance`, `actor`,
  `confidence`, and a derived `trust_level`. The extractor is an untrusted, low-trust actor.

## Usage

### Python SDK

```python
from mnemo import Mnemo
from mnemo.embedder import build_embedder

m = Mnemo("postgresql://mnemo:mnemo@localhost:5432/mnemo", build_embedder())

e = m.add("user", "preferred_database", "PostgreSQL", provenance="direct_user_statement")
m.add("user", "preferred_database", "MongoDB", provenance="agent_inference")  # a bad guess

m.search("database")                       # → MongoDB (current HEAD)
m.blame(subject="user", predicate="preferred_database")   # ADD(PostgreSQL) → UPDATE(MongoDB)
m.revert(e.fact_id, e.event_id)            # roll back to PostgreSQL
m.search("database")                       # → PostgreSQL again
```

### MCP server

```bash
make mcp     # stdio transport
```

Exposes `memory_add`, `memory_search`, `memory_get`, `memory_blame`, `memory_revert`,
`memory_diff`, `memory_log`, `memory_observe` to any MCP client (Claude Code, Cursor, …).

### Web UI

```bash
make ui      # http://127.0.0.1:8000
```

Search → fact detail (blame + revert + inline edit) → diff between snapshots.

## Configuration

All settings live in [`mnemo/config.py`](mnemo/config.py); copy `.env.example` → `.env` to
override. The default backend is **local Ollama** (`nomic-embed-text`, `vector(768)`).

To use OpenAI instead, set `MNEMO_BACKEND=openai`, `OPENAI_API_KEY`, `MNEMO_EMBED_DIM=1536`,
and change `vector(768)` → `vector(1536)` in the migrations (the dimension is kept in sync
between config and schema on purpose).

Thresholds (`update_sim` 0.90, `dedup_sim` 0.92, `confidence_floor` 0.5) are centralized in
config and covered by tests.

## Development

```bash
make test     # pytest against a disposable Postgres
make lint     # ruff + black --check
make fmt      # ruff --fix + black
```

The suite is hermetic by default (a deterministic fake embedder); live Ollama/OpenAI tests
skip automatically when the backend isn't available. Two tests encode the core promises:

- [`tests/test_lifecycle.py`](tests/test_lifecycle.py) — `ADD → UPDATE → BLAME → REVERT`;
  HEAD returns to PostgreSQL, exactly 3 events, history immutable.
- [`tests/test_twotier.py`](tests/test_twotier.py) — after `observe()`, search returns the
  fact before extraction; after reconcile it appears exactly once.

## Record the demo GIF

The launch asset is a <15s GIF of the rollback. Generate it with
[VHS](https://github.com/charmbracelet/vhs) (`brew install vhs`):

```bash
make gif      # runs `vhs docs/demo.tape` → docs/rollback.gif
```

Or screen-record the web-UI revert: `python examples/agent.py setup`, then `make ui` and
revert the fact in the browser.

## Project layout

```
mnemo/        SDK + core ops, embedder, extraction worker, MCP server, config
migrations/   numbered .sql (the schema is the product)
web/          FastAPI + HTMX UI
examples/     reference agent + the rollback demo
tests/        lifecycle + two-tier + ops/embeddings/mcp/web
```

See [`PROJECT_SPEC.md`](PROJECT_SPEC.md) for the full design and
[`CLAUDE.md`](CLAUDE.md) for how the project is built.
