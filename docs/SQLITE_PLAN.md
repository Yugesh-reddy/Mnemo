# Mnemo — single-file local stack plan

**Status: the owner asked for it to be implemented (November 14, 2026). Verified and
corrected (§3.1); Phase 0's test, Phase 1 and Phase 2 are done (D2 and D5 approved).
The owner put the trust features (§9) and an agent eval with a poisoning scenario
before Phase 3.** It changes the pinned stack, the schema and several spec
contracts under `AGENTS.md` rule 6. Checkout: `446e4b7`.

> **For the executing agent:** read `PROJECT_SPEC.md`, `AGENTS.md`, then this file.
> Work phase by phase. Each phase ends with green tests, lint and one commit. Do not
> start Phase 2 until the owner has answered the decisions in §2.

## 1. Goal

A new user runs `uv tool install …` and `mnemo install`, and memory works. No Docker,
no Postgres server, no Ollama, no port and no database password. The six MCP tools,
the scopes, history, guarded revert and retry receipts behave as they do today, and
retrieval is no worse.

**In scope:** replace Postgres with one SQLite file, replace Ollama with an embedding
model loaded inside the server, and carry the coding-agent path across.

**Out of scope:** any new behaviour. The review's other findings (a retract tool,
source on writes, ranking, OpenCode) are listed in §9 and stay separate, so the port
can be judged on parity alone. The extraction pipeline is not ported (§2, D2).

## 2. Decisions for the owner

| # | Decision | Recommendation | Why |
|---|---|---|---|
| D1 | Change the stack: add `sqlite-vec` and `fastembed`, remove `asyncpg` and Compose | Approve | It removes every service a user must keep running |
| D2 | The parked extraction pipeline: archive at a tag, or keep it on Postgres | Archive at tag `archive/extraction-pipeline` | Keeping it means two storage backends for the core store, roughly doubling the port. It is about 4,700 lines and 25 test files, and relies on Postgres row-locking queues |
| D3 | Default in-process model | `BAAI/bge-small-en-v1.5` (corrected; was nomic-Q) | Closest to Ollama through Mnemo's real search at the lowest memory cost (§3.1). The nomic recommendation rested on a measurement artifact |
| D4 | Store vectors in their own table, outside the append-only event payload | Approve | A vector is derived from the text and the model. As derived data it can be rebuilt after an import or a model change without rewriting history |
| D5 | Move `docs/quality-v*`, `docs/evaluation` and `mnemo/data` out of `main` with the pipeline | Approve | About 27 MB and 400 files of evidence for archived code; they stay reachable at the tag |

If D2 is "keep", stop: this plan does not cover a store with two backends.

D2 removes things `AGENTS.md` currently requires: required tests 2 to 4 (two-tier
handshake, quality gate, eval), `make eval`, `make demo`, and golden rules 2, 4 and 5.
Approving D2 approves rewriting those lines (§7).

## 3. Evidence (measured November 14, 2026)

All numbers come from throwaway scripts run on the owner's Mac against scratch data;
they are not in the repository yet (Phase 0 adds the retrieval set as a fixture).

**Retrieval.** 40 memories a coding project would hold, 28 reworded questions, 8
off-topic questions, stored and searched in one SQLite file. Ranking is by vector
similarity only. The set was written by the reviewing agent and each row ran once.

| Embeddings | Right memory first | In top 3 | Download | Peak RAM | Per query |
|---|---|---|---|---|---|
| Ollama `nomic-embed-text` (today) | 21/28 | 24/28 | via Ollama | 49 MB plus Ollama | 18.5 ms |
| `nomic-embed-text-v1.5-Q`, in process | 21/28 | 25/28 | 130 MB | 398 MB | 5.6 ms |
| `bge-small-en-v1.5`, in process | 21/28 | 25/28 | 67 MB | 320 MB | 3.2 ms |
| `all-MiniLM-L6-v2`, in process | 21/28 | 25/28 | 90 MB | 324 MB | 2.1 ms |
| `paraphrase-multilingual-MiniLM-L12-v2` | 19/28 | 23/28 | 220 MB | 1,091 MB | 3.3 ms |
| `snowflake-arctic-embed-xs` | 15/28 | 22/28 | 90 MB | 324 MB | 2.1 ms |
| `potion-retrieval-32M` (static) | 16/28 | 21/28 | 129 MB | 496 MB | 0.4 ms |
| Word match only (FTS5), no model | 15/28 | 19/28 | none | 37 MB | n/a |

**Search floor.** The floor decides whether an unrelated question returns anything.
Each cell is *right answers kept of 28 / off-topic questions of 8 that return a
memory*, by similarity alone. Mnemo also admits word matches below the floor, so
"kept" understates recall.

| Model | 0.45 | 0.50 | 0.55 | 0.60 | 0.65 |
|---|---|---|---|---|---|
| Ollama nomic (today) | 25 / 4 | **24 / 1** | 19 / 1 | 10 / 0 | 6 / 0 |
| nomic v1.5-Q, in process | 25 / 3 | **23 / 2** | 18 / 1 | 9 / 0 | 2 / 0 |
| bge-small-en-v1.5 | 28 / 7 | 28 / 6 | 27 / 3 | **26 / 3** | 20 / 1 |

The in-process nomic model behaves like today's at the existing floor. bge-small ranks
as well but separates off-topic questions worse at every floor. §3.1 corrects this
table: the nomic numbers were a batching artifact, and D3 now recommends bge-small.

**Guarantees.** A prototype schema with eight separate processes on one file:
rewriting or deleting an event was rejected by triggers; eight processes racing one
guarded update gave 1 applied and 7 conflicts; eight processes creating the same
subject/predicate stored 1 fact; 1,608 concurrent writes finished in 0.3 s with no
lock errors and no wrong HEADs; `PRAGMA integrity_check` returned `ok`.

**Search cost with no vector index** (scan the current memories of one scope; vectors
in a table keyed by the event's integer `seq`; three revisions per memory):

| Current memories in one scope | 384 dimensions | 768 dimensions |
|---|---|---|
| 1,000 | 2.1 ms | 2.1 ms |
| 5,000 | 10.2 ms | 12.3 ms |
| 20,000 | 49.2 ms | 60.8 ms |

Keying the vector table by a text ID instead cost 291 ms at 20,000, so the integer key
matters.

**Mechanics checked** on uv's Python 3.12.12 and 3.13.9 (SQLite 3.50.4, sqlite-vec
0.1.9, fastembed 0.9.1): extension loading, FTS5 with stemming, `?n` placeholders
reused in one statement, `INSERT … RETURNING`, savepoints, `pow()` and JSON
functions, `LIKE … ESCAPE`, a deferred circular foreign key, `AUTOINCREMENT`
continuing after an explicit `seq`. Opening a connection with the extension and
pragmas takes 3.6 ms. The macOS system Python (3.9.6) cannot load extensions.

### 3.1 Verification corrections (same day)

Re-measured through Mnemo's own search (`DirectMemory.search`, limit 3, its full
scoring), embedding one text at a time as the server does. The first table above
embedded memories in one padded batch, which flattered the quantized nomic model only:
one at a time it ranks first 18/28 on raw similarity, while the bge models are
unchanged (21/28).

| Backend, floor | Ranked first | In top 3 | Off-topic hits of 8 | Peak RAM per server |
|---|---|---|---|---|
| Ollama `nomic-embed-text`, 0.50 (today) | 20–21 | 24 | 1 | ~50 MB plus Ollama |
| local `bge-small-en-v1.5`, 0.60 | 19 | 25 | 3 | ~300 MB |
| local `bge-base-en-v1.5`, 0.55 | 19 | 26 | 2 | ~770 MB |
| local `nomic-embed-text-v1.5-Q`, 0.50 | 17 | 24 | 2 | ~400 MB |

Ollama's ranked-first count moves between 20 and 21 across runs because equal scores
(every word match scores a flat 0.85) are ordered by random fact IDs; ordering ties by
similarity changed nothing on this set. Recommended prefixes (`search_query:` for nomic,
the bge query instruction) did not improve ranking and fastembed applies none, so the
`kind` change in §4.7 is dropped. With 28 questions, one or two ranked-first differences
are close to noise; the off-topic difference (3 of 8 against 1) is the clearest cost of
`bge-small`. The agent-eval gate in Phase 8 decides.

**Not measured:** the real coding-agent eval on the new stack (Phase 8 is that gate),
Linux or Windows, more than 20,000 memories in one scope, and non-English text.

## 4. Target design

### 4.1 What replaces what

| Today | After |
|---|---|
| Docker Desktop, Compose file, `mnemo up` / `mnemo down` | Removed |
| Postgres 16 container, port 5432, `mnemo`/`mnemo` superuser | `~/.mnemo/mnemo.db` (mode 0600, WAL) |
| pgvector HNSW with iterative scan | Exact scan of one scope's current vectors with `vec_distance_cosine` |
| Postgres full-text search | FTS5 table (`porter`), maintained by a trigger |
| Ollama + `nomic-embed-text` | `fastembed` running `bge-small-en-v1.5` (D3), loaded on first use |
| `asyncpg` pool | One `sqlite3` connection per operation |
| 11 Postgres migrations | One SQLite schema file, `migrations/0001_sqlite_init.sql` |

`ollama`, `openai` and `hash` stay as optional embedding backends; they need no new
code.

### 4.2 Settings

| Setting | Meaning |
|---|---|
| `MNEMO_DB` | Database file. Default `~/.mnemo/mnemo.db` |
| `MNEMO_BACKEND` | `local` (default after Phase 8), `ollama`, `openai`, `hash` |
| `MNEMO_EMBED_MODEL` | Model for the backend; the floor comes from a per-model table |
| `MNEMO_MODEL_DIR` | Model cache. Default `~/.mnemo/models` |

Removed: `MNEMO_DSN`, `MNEMO_TEST_DSN`, `MNEMO_DB_PORT`, and every pipeline setting
(extractor, verifier, gate, decay, worker, identity routing).

### 4.3 Schema

Columns match the Postgres schema at migration 0011 one for one, so imported history
keeps every field. The only structural change is that `embedding` leaves
`memory_event` (D4). Tables `fast_cache`, `extraction_job` and `quality_decision` are
not created.

```sql
CREATE TABLE memory_fact (                    -- identity + HEAD pointer, as today
  fact_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL, session_id TEXT, subject TEXT NOT NULL,
  predicate TEXT NOT NULL, fact_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('text_fact','triple','kv','task','trace')),
  status TEXT NOT NULL CHECK (status IN ('active','invalidated','deleted')),
  identity_mode TEXT NOT NULL, identity_ref TEXT NOT NULL,
  current_event_id TEXT REFERENCES memory_event(event_id) DEFERRABLE INITIALLY DEFERRED,
  created_at TEXT NOT NULL,
  UNIQUE (namespace, user_id, agent_id, fact_key, identity_mode, identity_ref));

CREATE TABLE memory_event (                   -- append-only log, as today
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
  fact_id TEXT NOT NULL REFERENCES memory_fact(fact_id),
  op TEXT NOT NULL CHECK (op IN ('ADD','UPDATE','INVALIDATE','REVERT','DELETE')),
  object_text TEXT, object_number TEXT, object_json TEXT,
  provenance TEXT NOT NULL, actor TEXT, confidence REAL NOT NULL,
  trust_level TEXT NOT NULL, source_span TEXT, session_id TEXT,
  valid_from TEXT NOT NULL, valid_to TEXT, expires_at TEXT, recorded_at TEXT NOT NULL,
  superseded_at TEXT, superseded_by TEXT, parent_event_id TEXT,
  importance INTEGER, write_score REAL, tier TEXT NOT NULL, reason TEXT,
  strength REAL NOT NULL, recall_count INTEGER NOT NULL, last_used TEXT NOT NULL,
  commit_id TEXT);

CREATE TABLE memory_embedding (               -- derived; rebuilt by `mnemo reindex`
  seq INTEGER PRIMARY KEY REFERENCES memory_event(seq),
  model TEXT NOT NULL, dim INTEGER NOT NULL, vector BLOB NOT NULL);

CREATE VIRTUAL TABLE memory_search_text       -- derived; one row per fact's HEAD text
  USING fts5(body, tokenize='porter unicode61');
```

`memory_commit`, `memory_mutation_receipt` and `schema_migrations` carry over with the
same columns. Triggers reject any `UPDATE` of an event's payload columns, any `DELETE`
of an event, and any `UPDATE` or `DELETE` of a receipt. One trigger on
`memory_fact.current_event_id` refreshes `memory_search_text`, so every HEAD move
(create, update, revert, invalidate) is covered in one place. `memory_current` stays a
view with today's filters.

### 4.4 Postgres constructs and their SQLite replacements

| Postgres | SQLite |
|---|---|
| `pg_advisory_xact_lock` per scope | `BEGIN IMMEDIATE` (one writer for the whole file) |
| `SELECT … FOR UPDATE` | Plain `SELECT` inside the write transaction |
| Nested `conn.transaction()` | `SAVEPOINT` |
| `isolation="repeatable_read"` reads | `BEGIN` read transaction (a WAL snapshot) |
| `uuid`, `gen_random_uuid()` | `TEXT`, `uuid4()` in Python |
| `timestamptz`, `now()` | `TEXT` in the fixed form `YYYY-MM-DDTHH:MM:SS.ffffffZ` (UTC), written by Python; the view uses `strftime('%Y-%m-%dT%H:%M:%f000Z','now')` |
| Enum types | `CHECK (col IN (…))` |
| `jsonb` | `TEXT` with `json_valid`; receipts compare one canonical JSON string |
| `numeric` | `TEXT` decimal string, `Decimal` in Python |
| Trigger comparing `to_jsonb(NEW)` and `to_jsonb(OLD)` | Trigger naming each payload column, plus a test that tries every column |
| SQL functions `fact_snapshot_at`, `fact_snapshot_as_of` | Parametrized queries in `core.py` |
| `to_jsonb` / `jsonb_populate_recordset` export | Row dictionaries built in Python |
| `$n` placeholders | `?n` |

Two behaviours differ and must be written into the spec: the write lock covers the
whole store, not one scope; and `LIKE` ignores case for ASCII only, where `ILIKE`
covered all of Unicode.

### 4.5 Search

One statement over the scope's current memories computes three signals per row: cosine
similarity to the query vector, a full-text hit (query words minus an English stop
list, ANDed), and a substring hit (with `%` and `_` escaped). Rows pass with a word
hit or a similarity at or above the floor. The score keeps today's formula and weights
(`search_w_rel`, `search_w_rec`, `search_w_imp`, `recency_gamma`). Every row in the
scope is scored, so none is lost to a candidate limit before ranking, as can happen
today. A memory without a vector for the current model is still found by words.

### 4.6 Concurrency

Each agent session runs its own server process. A tool call opens a connection, runs,
and closes it, as `pool.acquire()` does today. Statements run through
`asyncio.to_thread`. Embedding calls stay outside the write transaction, as now.
`busy_timeout` is 30 s, so a call waits for another writer instead of failing.

### 4.7 Embedding

- The model loads on the first search or write, so `memory_get`, `memory_history` and
  `memory_revert` never load it.
- Queries and memories are embedded the same way, as today (§3.1: prefixes did not help).
- The model cache is `~/.mnemo/models`. fastembed's default is the OS temporary
  directory, which a reboot can clear.
- The server never downloads. A missing model returns `SERVICE_UNAVAILABLE` naming
  `mnemo install`, which downloads it after confirmation, as it does for Ollama today.

### 4.8 Export and import

Export becomes `format_version` 2: the same sections, rows built in Python, vectors
omitted. Import of a version 2 document restores into an empty store and verifies by
re-exporting, as now. Import also accepts version 1 documents from Postgres: it
normalizes timestamps, re-canonicalizes receipt payloads so old request IDs still
replay, drops the vectors, then runs `reindex`.

Upgrade path for an existing user: export with the old install (kept reachable as tag
`v0.0.1-postgres`), upgrade, import.

## 5. Phases

Sizes are relative guesses, not measurements. Phases 2 to 8 happen on a short-lived
branch `sqlite`; `main` must stay installable because users install it directly.

### Phase 0 — Baseline and safety net (small)

- [ ] Tag `main` as `v0.0.1-postgres` and push the tag (needs the owner: the repo is public).
- [ ] Run `scripts/agent_eval.py` three times on the current stack and keep the runs.
      Each run cost $0.74 to $0.91 of Claude usage plus Codex tokens in September
      (needs the owner: it spends their usage, and Codex returned 401 in September).
- [x] Add the 40-memory, 28-question, 8-off-topic set as `tests/data/retrieval_set.json`
      and a live test `tests/test_retrieval_quality.py` (skipped when no model is
      available). It searches through `DirectMemory`, each backend in its own schema at
      its own dimension. Bar (§3.1): first ≥ 19/28, top 3 ≥ 24/28, off-topic ≤ 3/8.

**Exit:** tag pushed, three baseline runs recorded, the retrieval test passes on the
current stack (done: Ollama and local both pass).

### Phase 1 — In-process embedder, on `main`, still on Postgres (small to medium)

Files: `mnemo/embedder.py`, `mnemo/config.py`, `mnemo/core.py` (the `_embed` call
site), `mnemo/services.py`, `mnemo/install.py`, `pyproject.toml`, `uv.lock`,
`tests/test_embeddings.py`, `tests/test_install.py`, `tests/test_services.py`.

- [x] `LocalEmbedder` with lazy load, a lock around load and inference, and the cache
      directory. fastembed 0.9.1 takes `local_files_only=True`; a missing model raises
      `ValueError`, mapped to `LocalModelUnavailable`, then `SERVICE_UNAVAILABLE`.
- [x] Per-model dimension and floor table in `config.py` (`LOCAL_MODELS`).
- [x] Installer: `--backend local`, model download step, pinned model and directory in
      the agents' configuration, and refusal when stored vectors came from another
      model (re-embed up to five newest memories; cosine < 0.99 refuses). No backend is
      recorded in the database, so the vectors themselves are the evidence.
- [x] Fixed on the way: the installer's migration checked the dimension of the
      process-wide settings, so reinstalling with `--backend local` on its own
      384-dimensional store failed (regression test added).
- [x] `scripts/agent_eval.py`: accept `--backend local`.
- [x] The default stays `ollama` until Phase 8.
- [ ] One agent-eval run with `--backend local` (needs the owner, as in Phase 0).

**Exit:** the retrieval test passes with `MNEMO_BACKEND=local`; one agent-eval run
with `--backend local` passes at least as many scenarios as the worst Phase 0 run; the
tool installs on Python 3.12 and 3.13.

### Phase 2 — Archive the pipeline (medium; needs D2)

Subtract first, so the port is of a smaller codebase.

- [x] Tag `archive/extraction-pipeline`.
- [x] Remove `extraction`, `quality`, `worker`, `runtime`, `decay`, `audit`,
      `mcp_server`, `benchmark`, the seven `eval*` modules, `mnemo/data`,
      `examples/agent.py`, and the eight pipeline scripts under `scripts/`.
- [x] `core.py`: remove `observe`, the fast-cache search and merge, `_max_cosine`,
      `archive_if_head`.
- [x] `web/app.py`: remove `/health`, `/decisions`, `/operations` and the template.
- [x] `pyproject.toml`: remove seven console scripts and the `nli` extra.
- [x] Remove 24 test files; split `test_temporal_cache`, `test_demo`, `test_web` (§6).
      `test_retrieval_plan` (HNSW plans) is Postgres-specific, not pipeline: remove it
      in Phase 4.
- [x] If D5: remove the evidence folders from `main`.

**Exit:** the remaining suite is green on Postgres; lint and the wheel check pass.

Done November 15, 2026. Beyond the list: the pipeline settings went too (unknown
`MNEMO_` variables are ignored), `make demo` now runs the direct demo, and
`test_mcp_direct_stdio` was removed because it drove the legacy server. Tests that read
an archived HEAD now build it with `tests/conftest.py::legacy_archive`, since stores
upgraded from earlier versions can still contain one. 280 passed with Ollama and the
local model available.

### Phase 3 — SQLite store (large)

Files: new `mnemo/sqlite.py`, `migrations/0001_sqlite_init.sql`, `mnemo/db.py`,
`mnemo/core.py`, `mnemo/models.py`, `mnemo/seed.py`, `tests/conftest.py`.

- [ ] `mnemo/sqlite.py`: open (pragmas, extension, capability check) and a thin async
      wrapper with `fetch`, `fetchrow`, `fetchval`, `execute`, `transaction()`, so call
      sites change their SQL and little else.
- [ ] Schema, triggers and view from §4.3.
- [ ] Port `add`, `revert`, `invalidate`, `reinforce`, `commit`, `diff`, `blame`,
      `log`, `get`, `list_current`, `list_commits`.
- [ ] `conftest.py`: a fresh temporary file per test; `MNEMO_REQUIRE_DB` goes away.
- [ ] New tests: the trigger rejects a change to every payload column; timestamps are
      fixed width; the eight-process races from §3.

**Exit:** `test_core`, `test_lifecycle`, `test_diff`, `test_council`,
`test_foundation_integrity`, `test_identity`, `test_schema` and the new tests pass.

### Phase 4 — Search (medium to large)

- [ ] Rewrite `search` and `_search_semantic` per §4.5, including `as_of`,
      `valid_at` and `include_superseded`.
- [ ] Write a vector on create and update; copy it on revert; tolerate its absence.
- [ ] `mnemo reindex` rebuilds `memory_embedding` and `memory_search_text`.
- [ ] Tests: scores equal hand-computed values for fixed vectors; `%` and `_` are
      literal; scope isolation; a slow-marked test for 5,000 memories.

**Exit:** `test_search`, `test_embeddings` and the retrieval test meet the Phase 0
bar; search at 5,000 memories in a scope takes under 50 ms.

### Phase 5 — Direct API and MCP server (medium)

Files: `mnemo/direct.py`, `mnemo/mcp_direct.py`, `mnemo/services.py`,
`mnemo/__init__.py`.

- [ ] Receipts compare canonical JSON text; reads use snapshot transactions.
- [ ] `mcp_direct._run` opens a connection per call; the pool and its lock go.
- [ ] `services.py`: errors for an unreadable or unmigrated file, a Python build that
      cannot load extensions, and a missing model. Each names its fix.
- [ ] `Mnemo(dsn, …)` becomes `Mnemo(path, …)`.

**Exit:** `test_direct_mutations`, the five direct stdio test files,
`test_sdk`, `test_agent_undo` and `test_azure_pilot` pass.

### Phase 6 — Export, import and upgrade (medium)

- [ ] `transfer.py` per §4.8.
- [ ] Check in a small version 1 export produced at `v0.0.1-postgres` as a fixture.
- [ ] Tests: a version 2 round trip is exact; the version 1 fixture imports with the
      same IDs, `seq`, values and HEADs; a pre-upgrade request ID replays its receipt.

**Exit:** `test_transfer` passes and the upgrade steps are written into the README.

### Phase 7 — Installer, CLI, web, packaging, documents (medium)

- [ ] `install.py`: write `MNEMO_DB`, check SQLite capabilities and the model, create
      `~/.mnemo` (0700) and the file (0600), migrate.
- [ ] `cli.py`: remove `up` and `down` (print a one-line notice for one release); add
      `reindex`.
- [ ] `web/app.py`: a connection per request.
- [ ] `pyproject.toml`, `Makefile`, `scripts/check-wheel.sh`,
      `.github/workflows/correctness.yml`; delete `docker-compose.yml`.
- [ ] `scripts/agent_eval.py`: the scratch store is a temporary file.
- [ ] Documents per §7.

**Exit:** the full suite, lint and the wheel check pass; installing the branch as a
tool and running `mnemo install` against temporary `--claude-dir` and `--codex-home`
works. Never run it against the owner's real agent configuration.

### Phase 8 — Gate and cut-over (small, plus eval cost)

- [ ] Three agent-eval runs against the installed SQLite build. Pass: across the 30
      scenario runs, no fewer passes than the Phase 0 baseline and no new tool errors.
      Read the transcript of every failure.
- [ ] Record install time, first-search latency, memory per server and file size.
- [ ] Make `local` the default backend (decide `bge-small` against `bge-base` on the
      eval and on memory use), set the version to 0.1.0, fast-forward
      `sqlite` into `main`, delete the branch.

## 6. Tests (55 files today)

| Action | Count | Files |
|---|---|---|
| Remove with the pipeline | 24 | `test_audit`, `test_azure_extractor`, `test_azure_judge`, `test_decay`, `test_eval`, `test_eval_audit`, `test_eval_equivalence`, `test_eval_failures`, `test_eval_traceability`, `test_extraction_evidence`, `test_extraction_fidelity`, `test_extraction_partial`, `test_extraction_report_rejections`, `test_identity_routing`, `test_identity_suite`, `test_lasting_policy`, `test_mcp`, `test_mcp_stdio`, `test_measurement`, `test_quality`, `test_runtime_config`, `test_twotier`, `test_verifier_assertions`, `test_worker_reliability` |
| Remove in Phase 4 (HNSW plans) | 1 | `test_retrieval_plan` |
| Split | 3 | `test_temporal_cache` (keep validity and history, drop cache and decay), `test_demo` (keep the direct demo), `test_web` (drop the operations page) |
| Rewrite | 3 | `test_schema`, `test_services`, `test_transfer` |
| Port (fixtures and SQL only) | 24 | `test_agent_eval`, `test_agent_undo`, `test_azure_pilot`, `test_cli`, `test_config`, `test_core`, `test_council`, `test_diff`, `test_direct_mutations`, `test_embeddings`, `test_foundation_integrity`, `test_identity`, `test_install`, `test_lifecycle`, `test_mcp_direct_profile`, `test_mcp_direct_profile_stdio`, `test_mcp_direct_stdio`, `test_mcp_request_ids_stdio`, `test_mcp_service_errors_stdio`, `test_mcp_shared_scope_stdio`, `test_packaging`, `test_project`, `test_sdk`, `test_search` |

## 7. Contract and document changes to approve

- **`PROJECT_SPEC.md`:** §1 stack; §3 schema baseline; §14 transactions (store-wide
  write lock) and retrieval (exact scan, no HNSW); §15 revert wording on embeddings
  and the SDK constructor; §16 export format 2 and version 1 import; §21 services,
  pre-flight and the removal of `mnemo up`. With D2: §0, §2, §4, §6 to §9, §13, the
  routing part of §17, and §18 marked as archived.
- **`AGENTS.md`:** "self-hostable on plain Postgres"; the `asyncpg` and
  embedding-dimension conventions; the command list; required tests 2 to 4; the
  reliability fixes (R1 and R2 no longer apply); the definition of done.
- **Dependencies:** add `sqlite-vec` and `fastembed` (which brings `onnxruntime`,
  `tokenizers`, `numpy`, `huggingface-hub`); remove `asyncpg`.
- **Also:** `README.md`, `docs/DIRECT_SDK.md`, `.env.example`, `PROJECT_STATUS.md`.

## 8. Risks

| Risk | Mitigation |
|---|---|
| The rewrite introduces bugs the Postgres tests would have caught | Port each phase's tests before its code; Phase 8 gate |
| 300 to 400 MB of RAM per open agent session | Lazy load; `ollama` stays available; revisit a shared embedder only if users report it |
| `sqlite-vec` is pre-1.0 | It supplies one function. Keep it behind one helper so cosine can move to Python (`numpy` is already installed) |
| A Python build that cannot load SQLite extensions | Capability check at open and in the installer; the message names `uv tool install --python 3.12` |
| The model download fails or Hugging Face is unreachable | Download only in the installer, with a clear error; pin `fastembed` in `uv.lock` |
| Stemming and stop words differ between Postgres and FTS5 | Retrieval test and a stemming test in Phase 4 |
| The database file sits in a synced or network folder | Document that it must be on local disk |
| One file is easier to delete than a Docker volume | `mnemo export`; mode 0600; say so in the README |
| An old server process is still running after an upgrade | Schema version check on open returns `SERVICE_UNAVAILABLE` naming a restart |
| Search slows beyond about 20,000 memories in one scope | Add a HEAD-only `vec0` index then; not before it is measured |

## 9. Follow-ups, deliberately not in this plan

From the November 14 review: a retract tool for a newly created memory; a source on
every write mapped to provenance and trust; actor, time and trust in search hits;
recency in ranking; near-duplicate detection on create; OpenCode in the installer;
the unauthenticated manual-correction form; UI searches changing recall counters; a
hash chain for tamper evidence.

The port settles three findings on its own: the superuser password and open port
disappear, `%` and `_` in a query become literal, and with decay archived Mnemo can no
longer put a memory into the stuck archived state.

## 10. Rollback

Before Phase 8 nothing on `main` changes except the additive Phase 1. After the
cut-over, a user who needs the old stack installs `@v0.0.1-postgres`; their Postgres
data is untouched by the upgrade, because import writes a new file.
