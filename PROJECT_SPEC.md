# Mnemo — Implementation Spec (MVP)

> **Working name:** Mnemo (verify availability on GitHub / PyPI / npm before publishing).
> **One-line:** Git for agent memory — see, diff, blame, and roll back what your AI remembers.
> **This file is the build brief for Claude Code.** Put it at the repo root as `PROJECT_SPEC.md`, point Claude Code at it, and build **one milestone at a time** (see §12). Do not implement anything in §13 (Non-goals).

---

## 1. What we're building (and why)

A transparent, version-controlled memory layer for LLM agents. Existing memory systems (Mem0, Zep, Letta) silently overwrite memory — when a stored fact is wrong (bad extraction, drift, poisoning) you can't see what the agent believes, why, or how to undo it.

Mnemo makes agent memory an **append-only, inspectable, reversible** store:
- **`add` / `search`** — store and retrieve facts (the table-stakes layer).
- **`blame`** — for any belief, show which turn/source introduced it and when.
- **`revert`** — roll a fact back to a previous value; history is never destroyed.
- **`diff` / `log`** — see how the agent's beliefs changed between two points in time.

It handles **both** conversational/personalization memory and agent task/working memory, is **framework-agnostic** (MCP server + Python SDK), and is **self-hostable/local-first** (plain Postgres).

**The MVP is done when the rollback demo in §10 works end to end.** Everything else is secondary.

---

## 2. Tech stack (pinned)

- **Language:** Python 3.12, full type hints.
- **DB:** PostgreSQL 16 + `pgvector` (>= 0.7, for HNSW).
- **DB access:** `asyncpg` with **raw SQL in versioned `.sql` migration files**. (The transparent SQL data model *is* the project's value — keep it legible. Thin Python wrappers over SQL, not a heavy ORM. SQLAlchemy 2.0 async is acceptable if you prefer, but keep the core event/diff/as-of queries as readable SQL.)
- **Models/validation:** Pydantic v2 for payloads and config (`pydantic-settings` + `.env`).
- **LLM/embeddings:** an `Embedder` and `Extractor` abstraction with two backends — OpenAI (`text-embedding-3-small`, `gpt-4o-mini`) and local via Ollama (`nomic-embed-text` / a small instruct model). Default to OpenAI; make the local path work too.
- **MCP:** the official Python MCP SDK (`mcp`), stdio transport.
- **Web UI:** FastAPI + HTMX + Tailwind (server-rendered, one language, minimal). A small React/Vite app is acceptable only if the diff view needs it — default to HTMX.
- **CLI:** Typer.
- **Tests:** pytest + pytest-asyncio against a disposable Postgres (docker-compose).
- **Lint/format:** ruff + black. **Packaging:** `uv` or `pip` + `pyproject.toml`.
- **Runtime:** `docker compose up` brings up Postgres + the app; one command to seed and run the demo.

**Embedding dimension:** default schema uses `vector(1536)` (OpenAI `text-embedding-3-small`). If using a local model, change the dimension everywhere (e.g. `nomic-embed-text` = 768, `bge-small` = 384). Centralize the dimension in config and migrations.

---

## 3. Core concepts / glossary

- **Fact** — a single belief with a **stable identity** (`fact_id`), e.g. *(user, preferred_database, "PostgreSQL")*. The fact is the unit of versioning (the "file" in git terms). Its **value lives in events**, not on the fact row.
- **Event** — an immutable, append-only record of a change to a fact (`ADD` / `UPDATE` / `INVALIDATE` / `REVERT` / `DELETE`). The event log is the source of truth.
- **`memory_current`** — a SQL **view** = HEAD = the current believed state (latest non-superseded event per fact).
- **Commit** — a named pointer to a point in the event sequence, used for `diff`/`log` across time. (Lightweight: stores a high-water `seq`. Not a Merkle/content-addressed store — that's a future enhancement.)
- **Fast cache** — a synchronous working-memory tier for immediate next-turn recall while async extraction catches up (§8).
- **Provenance / actor / trust** — every event records *how* it was learned (`direct_user_statement`, `agent_inference`, `tool_output`, `document`, `human_review`), *who* produced it, and a derived `trust_level`. The extractor is an **untrusted actor** (`agent_inference`, low trust).
- **Bitemporal** — events carry `valid_from`/`valid_to` (when the fact was true in the world) and `recorded_at`/`superseded_at` (when the system learned/changed it). This enables "what did the agent believe last Tuesday" and "filter to what's true *now*."

---

## 4. Database schema (full DDL)

Implement as numbered migration files (`migrations/0001_init.sql`, …). This is the canonical schema.

```sql
-- 0001_init.sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---- enums -------------------------------------------------------------
CREATE TYPE mem_kind        AS ENUM ('text_fact','triple','kv','task','trace');
CREATE TYPE mem_op          AS ENUM ('ADD','UPDATE','INVALIDATE','REVERT','DELETE');
CREATE TYPE mem_provenance  AS ENUM ('direct_user_statement','agent_inference','tool_output','document','human_review');
CREATE TYPE mem_trust       AS ENUM ('high','medium','low');
CREATE TYPE mem_fact_status AS ENUM ('active','invalidated','deleted');

-- ---- entities (lightweight; subjects facts are about) ------------------
CREATE TABLE memory_entity (
  entity_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace     text NOT NULL DEFAULT 'default',
  entity_type   text NOT NULL,                 -- 'user' | 'project' | 'person' | 'concept' | ...
  canonical_name text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (namespace, entity_type, canonical_name)
);

-- ---- facts (stable identity + HEAD pointer) ----------------------------
CREATE TABLE memory_fact (
  fact_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace        text NOT NULL DEFAULT 'default',
  user_id          text NOT NULL DEFAULT 'default',
  agent_id         text NOT NULL DEFAULT 'default',
  session_id       text,                        -- nullable: long-term facts aren't session-scoped
  subject          text NOT NULL,
  predicate        text NOT NULL,
  fact_key         text NOT NULL,               -- canonical(subject|predicate[, key]); identity for dedup
  kind             mem_kind NOT NULL DEFAULT 'triple',
  status           mem_fact_status NOT NULL DEFAULT 'active',
  current_event_id uuid,                         -- FK added after memory_event exists
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (namespace, user_id, agent_id, fact_key)
);

-- ---- events (append-only log = source of truth) -----------------------
CREATE TABLE memory_event (
  event_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq           bigint GENERATED ALWAYS AS IDENTITY,  -- global monotonic order
  fact_id       uuid NOT NULL REFERENCES memory_fact(fact_id),
  op            mem_op NOT NULL,
  object_text   text,
  object_number numeric,
  object_json   jsonb,
  embedding     vector(1536),                   -- embedding of the fact's content
  provenance    mem_provenance NOT NULL,
  actor         text,                            -- agent/user/session/tool id
  confidence    real NOT NULL DEFAULT 1.0 CHECK (confidence >= 0 AND confidence <= 1),
  trust_level   mem_trust NOT NULL DEFAULT 'medium',
  source_span   jsonb,                           -- {"turn_ids": [...], "doc": "...", ...}
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_to      timestamptz,
  recorded_at   timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,                     -- set when a later event replaces this one
  superseded_by uuid REFERENCES memory_event(event_id),
  parent_event_id uuid REFERENCES memory_event(event_id),  -- UPDATE: prev; REVERT: restored event
  commit_id     uuid                              -- FK added after commits exists
);

ALTER TABLE memory_fact
  ADD CONSTRAINT fk_fact_current_event
  FOREIGN KEY (current_event_id) REFERENCES memory_event(event_id);

-- ---- commits (named pointers into the event sequence) -----------------
CREATE TABLE memory_commit (
  commit_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace        text NOT NULL DEFAULT 'default',
  parent_commit_id uuid REFERENCES memory_commit(commit_id),
  label            text,
  at_seq           bigint NOT NULL,              -- high-water event seq captured by this commit
  created_by       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memory_event
  ADD CONSTRAINT fk_event_commit
  FOREIGN KEY (commit_id) REFERENCES memory_commit(commit_id);

-- ---- fast cache (synchronous working-memory tier) ---------------------
CREATE TABLE fast_cache (
  cache_id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace           text NOT NULL DEFAULT 'default',
  user_id             text NOT NULL DEFAULT 'default',
  session_id          text NOT NULL,
  turn_id             text NOT NULL,
  raw_text            text NOT NULL,
  embedding           vector(1536),
  reconciled          boolean NOT NULL DEFAULT false,
  reconciled_event_id uuid REFERENCES memory_event(event_id),
  created_at          timestamptz NOT NULL DEFAULT now()
);

-- ---- extraction job queue (MVP background worker) ---------------------
CREATE TABLE extraction_job (
  job_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace   text NOT NULL DEFAULT 'default',
  user_id     text NOT NULL DEFAULT 'default',
  agent_id    text NOT NULL DEFAULT 'default',
  session_id  text,
  turn_id     text NOT NULL,
  payload     jsonb NOT NULL,        -- {"text": "...", "role": "user"|"assistant", ...}
  status      text NOT NULL DEFAULT 'pending',  -- pending|processing|done|failed
  attempts    int NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- indexes ----------------------------------------------------------
CREATE INDEX idx_fact_key       ON memory_fact (namespace, user_id, agent_id, fact_key);
CREATE INDEX idx_event_fact     ON memory_event (fact_id, recorded_at DESC);
CREATE INDEX idx_event_seq      ON memory_event (seq);
CREATE INDEX idx_event_head     ON memory_event (fact_id) WHERE superseded_at IS NULL;
CREATE INDEX idx_event_embed    ON memory_event USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_cache_session  ON fast_cache (session_id, reconciled);
CREATE INDEX idx_cache_embed    ON fast_cache USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_job_status     ON extraction_job (status, created_at);
```

### `memory_current` view (HEAD)

```sql
-- 0002_views.sql
CREATE VIEW memory_current AS
SELECT
  f.fact_id, f.namespace, f.user_id, f.agent_id, f.session_id,
  f.subject, f.predicate, f.kind,
  e.event_id, e.object_text, e.object_number, e.object_json, e.embedding,
  e.provenance, e.actor, e.confidence, e.trust_level,
  e.valid_from, e.recorded_at
FROM memory_fact f
JOIN memory_event e ON e.event_id = f.current_event_id
WHERE f.status = 'active';
```

> Invariant: each active fact has exactly one non-superseded "live" event, pointed to by `memory_fact.current_event_id`. `add`/`revert` maintain this pointer transactionally.

### As-of state + diff helpers

```sql
-- value of every fact as of a given event seq (for diff / time-travel)
CREATE OR REPLACE FUNCTION fact_state_as_of(p_namespace text, p_user text, p_seq bigint)
RETURNS TABLE(fact_id uuid, subject text, predicate text,
              object_text text, object_number numeric, object_json jsonb) AS $$
  SELECT s.fact_id, s.subject, s.predicate, s.object_text, s.object_number, s.object_json
  FROM (
    SELECT DISTINCT ON (e.fact_id)
      e.fact_id, f.subject, f.predicate, e.op,
      e.object_text, e.object_number, e.object_json
    FROM memory_event e
    JOIN memory_fact f ON f.fact_id = e.fact_id
    WHERE f.namespace = p_namespace AND f.user_id = p_user AND e.seq <= p_seq
    ORDER BY e.fact_id, e.seq DESC
  ) s
  WHERE s.op IN ('ADD','UPDATE','REVERT');   -- excludes facts ending in INVALIDATE/DELETE at <= seq
$$ LANGUAGE sql STABLE;
```

`diff(seq_a, seq_b)` = `FULL OUTER JOIN fact_state_as_of(.., seq_a) a ON .. fact_state_as_of(.., seq_b) b` on `fact_id`, emitting:
- `+ added` where present in B, absent in A
- `- removed` where present in A, absent in B
- `~ changed` where the object differs

---

## 5. Operation semantics (the heart of the system)

Implement these in a `mnemo/core.py` (or `store.py`). Each is a transaction.

### `add(subject, predicate, object, *, kind='triple', provenance='agent_inference', actor=None, confidence=1.0, source_span=None, valid_from=None) -> Event`
1. `fact_key = canonicalize(subject, predicate)` — lowercase, strip, collapse whitespace, map predicate synonyms via a small alias table (e.g. `favorite_db` → `preferred_database`). Keep the alias map in code/config.
2. `emb = embedder.embed(f"{subject} {predicate} {object}")`.
3. Look up fact by `(namespace, user_id, agent_id, fact_key)`.
   - **Exists:** compare `emb` to the current event's embedding (cosine).
     - If the new object **differs** from current AND `cosine >= 0.90` (same fact, new value) → **UPDATE**: insert `UPDATE` event with the new payload; set the old live event `superseded_at = now(), superseded_by = <new>`; set `parent_event_id = <old live event>`; update `fact.current_event_id`.
     - If object is effectively equal → **no-op** (optionally bump confidence / refresh `recorded_at`).
   - **Not found:** optional semantic dedup — search existing facts for `cosine >= 0.92`; if a strong match exists under a different key, treat as an UPDATE to that fact (entity resolution). Otherwise insert a new `memory_fact` + `ADD` event and set `current_event_id`.
4. Derive `trust_level` (see §7) and persist it on the event.
5. Return the event.

> **Threshold (0.90 / 0.92) is configurable.** Over-updating destroys nuance; under-updating floods context. Make it tunable and cover it with tests.

### `search(query, *, k=8, as_of=None, include_superseded=False, session_id=None) -> list[Fact]`
1. `emb = embedder.embed(query)`.
2. If `as_of` is None: vector-search `memory_current` by cosine, top-k.
   If `as_of` (a commit/seq) is given: search over `fact_state_as_of(...)`.
3. Bitemporal filter: only facts valid now (`valid_to IS NULL OR valid_to > now()`), unless `include_superseded`.
4. (Optional) hybrid: blend vector score with a keyword match on `subject/predicate/object_text`.
5. **Merge fast cache:** include un-reconciled `fast_cache` rows for `session_id`, dedupe against semantic facts by `fact_key`/`turn_id` (§8).
6. Return ranked facts with value + `provenance`/`confidence`/`trust_level`.

### `blame(*, fact_id=None, subject=None, predicate=None) -> list[Event]`
Return the full ordered event history (`ORDER BY seq`) for the fact: each event's `op`, object, `provenance`, `actor`, `confidence`, `source_span`, `recorded_at`. This answers "which turn/source introduced this belief, and when." (`git blame` for memory.)

### `revert(fact_id, to_event_id, *, actor='human_review') -> Event`
1. Load the target historical event's payload.
2. Insert a new **`REVERT`** event with that payload, `provenance='human_review'`, high confidence, `parent_event_id = to_event_id`.
3. Supersede the current live event (`superseded_at`, `superseded_by`).
4. Update `fact.current_event_id` to the new REVERT event.
5. **Never** mutate or delete prior events — history stays intact (`e1 → e2 → e3`).

### `diff(commit_a, commit_b) -> Diff` / `log(*, fact_id=None, limit=50)`
- `diff`: as described in §4 (as-of join, emit +/~/-).
- `log`: if `fact_id`, the event history for that fact; else the recent global event timeline.

### `commit(label=None) -> Commit`
Capture `max(seq)` as `at_seq`, store with `label` + `parent_commit_id` (the previous commit).

### `observe(turn_id, text, session_id, *, role='user') -> None`
Synchronous: write a fast-cache row (embed `text`) **and** enqueue an `extraction_job`. This is what an agent calls every turn (§8).

---

## 6. Extraction pipeline (async worker)

A background worker (`mnemo/extraction.py`) polls `extraction_job` (MVP: an asyncio loop or a separate process; note Celery/RQ as optional later).

**Phase 1 — extract.** Call the `Extractor` (gpt-4o-mini) with **structured output** (JSON schema / function-calling), fixed shape:

```json
{ "facts": [
  { "subject": "user", "predicate": "preferred_database",
    "object": "PostgreSQL", "kind": "triple",
    "confidence": 0.97, "assertion_type": "direct_user_statement" }
] }
```

Reliability requirements (the extractor is untrusted):
- Constrain `predicate` to a **controlled vocabulary** in the prompt where possible; provide 3–4 few-shot examples.
- **Validate** every item against the Pydantic schema; **drop/retry** malformed output (max 2 retries).
- **Drop** items with `confidence < 0.5` (configurable).
- Map `assertion_type` → `provenance` (`direct_user_statement` for explicit user assertions, else `agent_inference`).

**Phase 2 — reconcile.** For each surviving candidate, call `add(...)` with the mapped `provenance`, `actor = session/model id`, `source_span = {"turn_ids": [turn_id]}`. `add()` handles ADD-vs-UPDATE.

**Phase 3 — handshake (critical, see §8).** After writing events, mark the originating `fast_cache` rows `reconciled = true, reconciled_event_id = <event>`.

---

## 7. Trust derivation

`trust_level` from `provenance` (+ `confidence`):
- `direct_user_statement`, `human_review` → **high**
- `tool_output`, `document` → **medium**
- `agent_inference` → **low** (downgrade further if `confidence < 0.5`)

Expose trust in `search` results and the UI. This powers filtering and the audit story (and later a policy: "don't auto-use low-trust facts in high-risk actions" — not in MVP).

---

## 8. Two-tier memory + the cache-invalidation handshake

**Problem:** async extraction creates a race — an agent learns a fact in turn N but the extraction isn't done by turn N+1, so it answers from stale memory.

**Design:**
- `observe()` writes the turn to `fast_cache` **synchronously** (immediate next-turn recall) and enqueues extraction.
- `search()` merges (a) un-reconciled `fast_cache` rows for the session and (b) `memory_current` semantic facts, **deduped by `fact_key`/`turn_id`**.
- **Handshake:** when the worker commits a reconciled fact, it sets `fast_cache.reconciled = true` for the originating `turn_id`. The merge step **drops reconciled rows** in favor of the semantic fact.

**Test the handshake:** after `observe()`, `search()` returns the fact *before* extraction; after extraction completes, the fact appears **once** (semantic), not twice (raw + semantic). Double-counting here is the bug to prevent.

---

## 9. MCP server + Python SDK

### Python SDK (`mnemo/__init__.py`)
```python
class Mnemo:
    def __init__(self, dsn: str, embedder: Embedder, *,
                 namespace="default", user_id="default", agent_id="default"): ...
    def add(self, subject, predicate, object, *, kind="triple",
            provenance="agent_inference", actor=None, confidence=1.0,
            source_span=None, valid_from=None) -> Event: ...
    def search(self, query, *, k=8, as_of=None,
               include_superseded=False, session_id=None) -> list[Fact]: ...
    def blame(self, *, fact_id=None, subject=None, predicate=None) -> list[Event]: ...
    def revert(self, fact_id, to_event_id, *, actor="human_review") -> Event: ...
    def diff(self, commit_a, commit_b) -> Diff: ...
    def log(self, *, fact_id=None, limit=50) -> list[Event]: ...
    def commit(self, label=None) -> Commit: ...
    def observe(self, turn_id, text, session_id, *, role="user") -> None: ...
```
Provide both async and sync surfaces (async core, thin sync wrappers).

### MCP server (`mnemo/mcp_server.py`)
Expose these tools (stdio), each wrapping the SDK with a JSON schema:
- `memory_add`, `memory_search`, `memory_get`, `memory_blame`, `memory_revert`, `memory_diff`, `memory_log`, `memory_observe`.

Keep tool descriptions tight and action-oriented so any MCP client (Claude Code, Cursor, etc.) can use them.

---

## 10. The demo (this defines "done") — acceptance criteria

A scripted, reproducible scenario proving see → blame → revert → behavior-change:

1. **Reference agent** (`examples/agent.py`): a CLI chat loop wired to Mnemo via the SDK (calls `observe()` each turn, `search()` to build context).
2. **Scenario:**
   - User: *"I use Postgres for my project."* → stored as a fact, `provenance=direct_user_statement`, high trust.
   - Later the agent mis-infers a switch to MongoDB → an `UPDATE` event, `provenance=agent_inference`, low confidence/trust.
   - User: *"What database do I use?"* → agent answers **"MongoDB"** (wrong, from the bad memory).
3. **In the web UI:** search "database" → see the fact → **blame** shows MongoDB came from `agent_inference` at turn N, previous value "PostgreSQL" → click **Revert**.
4. User asks again → agent answers **"Postgres."** Behavior visibly changed because a human fixed the memory.
5. **History intact:** the `blame`/`log` view still shows `ADD(Postgres) → UPDATE(Mongo) → REVERT(Postgres)`.

**Acceptance:** the above runs via a single documented command (e.g. `make demo`), and the UI revert round-trip works. Record a <15s GIF of steps 2–4. This is the launch asset.

---

## 11. Web UI (minimal)

FastAPI + HTMX + Tailwind. Three views:
- **Memory list / search** — table: subject, predicate, current value, provenance, confidence, trust, updated_at; a search box (calls `search`).
- **Fact detail (blame view)** — the full event history for a fact, newest-first; per historical event a **Revert** button; inline **Edit** (edit = `add` with `provenance=human_review`).
- **Diff view** — pick two commits/timestamps → render +/~/- lines.

Keep it clean and legible; this is demo surface, not a product. (When building UI, follow sensible design tokens — spacing scale, one accent color, readable mono for values/diffs. Don't over-build.)

---

## 12. Build milestones (do these IN ORDER; tests green before moving on)

- **M0 — Scaffold.** Repo layout, `pyproject.toml`, `docker-compose.yml` (Postgres+pgvector), config (`pydantic-settings`), migration runner, pytest harness against a disposable DB, ruff/black. CI optional.
- **M1 — Schema.** Migrations `0001`/`0002`, the `memory_current` view, `fact_state_as_of`, a seed script. Test: migrations apply cleanly; view returns seeded facts.
- **M2 — Core ops + the canonical test.** `add`, `search` (keyword first), `blame`, `revert`, `diff`, `log`, `commit`. **Canonical integration test = the fact lifecycle:** `ADD(Postgres) → UPDATE(Mongo) → BLAME → REVERT(Postgres)`; assert `memory_current` = Postgres, exactly 3 events in `log`, and that the first two events are unchanged (history immutable). Add a `diff` test across two commits.
- **M3 — Embeddings.** `Embedder` abstraction (OpenAI + Ollama); wire vector search into `add` (similarity routing) and `search`. Test ADD-vs-UPDATE threshold behavior.
- **M4 — Extraction + fast cache.** `observe()`, the worker, structured-output extraction with validate/retry, and the §8 handshake. Test: no stale memory before extraction; no double-count after.
- **M5 — MCP server.** Expose the tools; smoke-test from an MCP client.
- **M6 — Reference agent + demo.** `examples/agent.py` + `make demo` running the §10 scenario end to end.
- **M7 — Web UI.** List/search, blame/detail + revert, diff.
- **M8 — Polish.** README (one-liner → GIF → 30s quickstart → "Why" → feature bullets [blame/rollback/audit first] → comparison table), record the GIF, `docker compose up` one-command run.

**Working style for Claude Code:** work milestone by milestone; write tests for core ops *before/with* the implementation; commit per milestone with a clear message; **ask before** adding a dependency, changing the schema contract in §4, or altering operation semantics in §5. Prefer small, reviewable diffs.

---

## 13. Non-goals (DO NOT build in the MVP)

Explicitly out of scope — do not implement, even if it seems natural:
- ❌ OpenAI-compatible **proxy** (auto-capture/inject). Phase 1.5. MCP + SDK only for now.
- ❌ Full git-style **branching / merge**. Phase 2.
- ❌ "**Pull Requests for beliefs**" approval workflow beyond, at most, a non-blocking UI stub. Phase 2.
- ❌ **Multi-tenant** auth/isolation. Single-tenant; namespace/scope columns exist but no auth.
- ❌ **Graph / network** memory views. Future *view* over the same event log — leave a note, build nothing.
- ❌ "**Memory CI/CD**" / automated checks. Phase 3 vision.
- ❌ **Benchmark leaderboard** chasing (LoCoMo/LongMemEval). At most a tiny optional regression harness later to prove versioning doesn't degrade retrieval — not an MVP task.
- ❌ Hosted/cloud mode, billing, dashboards beyond §11.

Keeping these out is what makes the MVP shippable. If one seems necessary, stop and ask first.

---

## 14. Definition of done (MVP)

- `docker compose up` + one command runs the §10 demo end to end.
- The fact-lifecycle integration test (M2) passes, proving append-only history + working revert.
- The two-tier handshake test (M4) passes (no stale reads, no double-count).
- MCP server exposes the verbs and is callable from an external MCP client.
- Web UI does search → blame → revert with a visible behavior change.
- README + the <15s rollback GIF exist.

Build the smallest thing that makes the demo real. Everything else is later.
