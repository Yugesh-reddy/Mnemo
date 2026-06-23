# Mnemo — Master Plan

**Execution update (September 22, 2026):** local Tasks 0.1–0.4 and Phases 1–5
are complete. Required-Postgres validation passes 351 tests; one live-Ollama test
skips. Lint and wheel/source packaging pass, including the installed pilot entry
point, guarded SDK lifecycle and real direct MCP stdio checks. Phase 5's single
local Qwen run passed **2/6 scenarios with zero unintended mutations**; the
Azure Luna rerun passed **4/6 with two unintended mutations**. See the
[pilot evidence](direct-pilot/README.md) for both complete transcripts and failures.
Phase 2's additive receipt schema and guarded API contract were explicitly
approved by the user. The GitHub remote is configured; hosted CI and merge
protection in Task 0.5 were declined by the owner. From Phase 6, export/import has
shipped (spec §16); the other Phase 6 items remain deferred.
See `PROJECT_STATUS.md` for verification details.

Written September 21, 2026 against checkout `0468977` (working tree: one uncommitted
`.gitignore` change). This document **supersedes** the three research documents as the
thing an executing agent follows; they remain as background:

- `MEMORY_SYSTEM_COMPARISON.md` — competitor source review (Mem0 / Cognee / Memory Engine)
- `MEMORY_VERSIONING_MVP_RESEARCH.md` — original direct-write / guarded-undo design
- `MEMORY_TOOL_IMPLEMENTATION_PLAN.md` — the earlier milestone proposal (M0–M7)

Everything in Part A was **verified by running it or reading the code**, not assumed.

> **For the executing agent:** read `PROJECT_SPEC.md`, then `AGENTS.md`, then this file.
> Work phase by phase. Each phase ends with green tests, lint, and one focused commit.
> Steps use `- [ ]` checkboxes. Do not skip Phase 0.

---

## 0. One-paragraph summary

The store is real and solid: append-only Postgres events with a trigger that makes payloads
immutable, scoped advisory + row locks, a single HEAD pointer per fact, bitemporal reads,
reversible archive, a lease-fenced extraction worker, an append-only decision audit, MCP
server, web UI, and **231 passing tests** (3 skipped only because Ollama is not running).
The headline *quality-gate-on-real-extraction* story, however, is unmet after seven tuning
cycles with a 3–4B local model (1.41% precision on the 200-turn run; 0/9 must-keep on the
last holdout). The research docs correctly pivot the product to **"versioned agent memory
with guarded, auditable undo, exposed over MCP"**, with automatic extraction as an optional,
honestly-labelled pipeline. This plan adopts that pivot, fixes the defects found during
verification (including a broken fresh-install `make migrate`), and lays out the work in
six phases with concrete files, code, tests and exit criteria.

---

# Part A — Verified state of the project

## A1. What was actually run (evidence)

| Check | Command | Result |
|---|---|---|
| Postgres | `docker compose up -d` (pgvector/pgvector:pg16) | PG 16.14, pgvector **0.8.3** |
| Full suite | `MNEMO_REQUIRE_DB=1 uv run pytest -q` | **231 passed, 3 skipped, 14 s** |
| Skips | `-rs` | 2× "Ollama server not running", 1× "llama3.2 instruct model not available" |
| Lint | `ruff check .` / `black --check .` | clean |
| North star | `uv run python -m mnemo.eval` | NAIVE P 60.0 / R 90.0 / false 2 · GATED P 90.9 / R 100 / false 0 (18-turn smoke, label replay, heuristic verifier) |
| Fresh-DB migrate | `CREATE DATABASE …; MNEMO_DSN=… python -m mnemo.db` | **CRASH** `ValueError: unknown type: public.vector` (see A3-1) |
| External MCP lifecycle | real `stdio_client` → `python -m mnemo.mcp_server`, `MNEMO_WORKER_ENABLED=false`, deterministic embedder injected via `sitecustomize`, extractor/verifier builders trip-wired | **Works end-to-end** (details in A2 / A4) |
| Installed versions | `mcp 1.28.1`, `asyncpg 0.31.0`, `pydantic 2.13.4` | (no lockfile in git — see A3-2) |

## A2. What works (verified by code read + runtime)

**Store (`mnemo/core.py`, 1150 lines; migrations 0001–0009)**
- Append-only: `guard_memory_event_payload_immutable` trigger (0005) blocks UPDATE/DELETE of
  everything except `superseded_at/superseded_by/strength/recall_count/last_used`. The
  lifecycle test snapshots rows and compares byte-for-byte after revert. Confirmed.
- One HEAD per `(namespace,user_id,agent_id,fact_key)`; `add()` is upsert-by-key with a
  savepoint-protected UNIQUE-race recovery (`_insert_fact`); a real two-connection race test
  exists (`test_foundation_integrity.py::test_two_connection_updates_form_one_head_chain`).
- `revert()` checks target ∈ fact, copies typed payload + embedding + `source_span` via
  `INSERT … SELECT`, supersedes HEAD, moves the pointer, reactivates the fact. `invalidate()`
  and `archive_if_head()` are also append-only; archive rechecks HEAD and `last_used`.
- Bitemporal: `memory_current` filters `valid_from/valid_to/expires_at`; `fact_snapshot_at`
  (recorded-time × valid-time) and `fact_snapshot_as_of` (by seq) back `search(as_of, valid_at)`
  and `diff`. Session tier has TTL; legacy session rows get a read-time 24 h expiry (0009).
- Search: hybrid FTS + HNSW with pgvector iterative scan, rerank by relevance/recency/importance,
  merged with unreconciled fast-cache rows for a session; plan tests verify HNSW use.

**Write pipeline (`extraction.py` 647, `quality.py` 614, `worker.py`, `decay.py`, `runtime.py`)**
- Worker: `FOR UPDATE SKIP LOCKED` claim, ownership token, lease renewal every lease/3,
  `LeaseLost` fencing, re-check under row lock before commit, exponential backoff, attempt
  cap, cancel → requeue. All covered by two-connection tests.
- Gate: extract → guards → verify → embed → score/tier → `store.add` → `quality_decision`
  → reconcile fast-cache. Decision table is append-only (trigger). Extractor output is
  always `agent_inference`/low trust; assistant turns are excluded in code.
- Decay: importance-modulated Ebbinghaus (`λ_eff = 0.16·(1−imp·0.8)`), archive = new
  `UPDATE` event with `tier='ephemeral'`, reversible via revert.
- `MNEMO_WORKER_ENABLED=false` skips extractor/verifier construction **and** decay in that
  process (verified: trip-wired builders were never called). A second `make worker` process
  on the same DB would still decay — deployment isolation is the boundary.

**Surfaces**
- MCP (`mcp_server.py`, FastMCP): 10 tools; pool from `background_runtime`; in-process and
  real-subprocess stdio tests exist.
- Sync SDK (`Mnemo`): one `asyncio.run` + one connection per call (documented).
- Web (`web/app.py`): list/search → fact detail (blame) → revert; diff; operations page;
  HTTP test covers the flow.
- CI workflow exists and is correctly shaped, but **has never run** (no git remote).

**Eval (`eval*.py` ≈2,430 lines + `benchmark.py`)**
- `make eval` = 18-turn synthetic smoke, label-replay extractor, deterministic embedder,
  heuristic verifier, two private Postgres schemas. Honest, reproducible, and locked by
  `tests/test_eval.py::test_gated_beats_naive_the_north_star`.
- `mnemo-eval --real …`, `mnemo-eval-suite`, `mnemo-benchmark`, `mnemo-audit-eval` and
  `eval_equivalence` are the seven-cycle measurement apparatus behind `docs/quality-v2…v7`.

## A3. Defects found during verification (NOT in the research docs)

Ordered by impact on "a stranger clones this and it works".

1. **`make migrate` crashes on a fresh database.** `mnemo/db.py::register_vector` catches
   `asyncpg.exceptions.UndefinedObjectError`, but asyncpg 0.31 raises
   `ValueError: unknown type: public.vector` when the extension does not exist yet.
   `db.connect()` → `register_vector` runs *before* migrations create the extension, so the
   README quickstart (`make up && make migrate`) fails on a fresh volume. Tests never hit it
   because `conftest.py` calls `apply_migrations` on a raw connection, and `prepare_isolated_schema`
   creates the extension first. **CI's `make migrate` step would also fail** (it runs against
   the compose `mnemo` DB after `make test-db`, which uses a *different* disposable DB).
2. **`uv.lock` is gitignored.** Fresh clones resolve whatever is newest — which is probably
   how defect 1 appeared. Reproducibility and the CI wheel check depend on a committed lock.
3. **`docs/` is gitignored.** All research, all seven evaluation evidence trees (13 MB), the
   identity proposals and this plan are outside the repository. For a portfolio, the evidence
   *is* the argument. The uncommitted `.gitignore` edit additionally ignores `AGENTS.md`.
4. **Dead links on the front page.** `README.md`, `PROJECT_SPEC.md` and `PROJECT_STATUS.md`
   link to `docs/CORRECTNESS_PLAN.md`, `docs/QUALITY_RELIABILITY_PLAN.md` and
   `docs/demo.tape`; none exist anywhere (not just ignored — absent).
5. **Duplicate operating guides.** `CLAUDE.md` and `AGENTS.md` are near-identical; only
   `AGENTS.md` has the git-dates rule. Two sources of truth will drift.
6. **`memory_get` on an unknown/foreign fact returns empty content, not an error.** An agent
   cannot distinguish "not found" from "no value". (Probe: `isError=False`, text `""`.)
7. **Revert-to-current appends a new REVERT event** instead of a no-op (Memory Engine
   documents this case as a no-op; the research docs propose `no_change`). Probe confirmed.
8. **Error messages disclose IDs.** The foreign-target error echoes both UUIDs; the
   direct profile should return a non-disclosing `INVALID_RESTORE_TARGET`.
9. **FastMCP 1.x returns list results as one `TextContent` per element**, not one JSON
   array. Hosts cope, but the direct profile should return one structured object per call.
10. **Web UI** `POST /fact/{id}/revert/{event}` has no expected-HEAD guard, no CSRF, no
    confirm; `POST /fact/{id}/edit` writes `provenance='human_review'` from an anonymous form.
    Acceptable for a local demo; not acceptable if the UI is shown as "the safety net".
11. **Stale statements.** README says "200 tests pass" (231 now); `seed.py` docstring says
    the seed predates `core.add()`; `w_src` (assistant penalty) is dead on the worker path
    because assistants are excluded earlier; no dedicated Layer-1 regex prefilter exists
    before the extractor call (regexes live inside verification/scoring).
12. **Commit timeline vs document dates.** `git reflog` shows a `filter-branch` re-date; HEAD
    is dated 2026-05-29 while three tracked files carry September 2026 timestamps:
    `mnemo/audit.py:45` (`pipeline_version = "2026-09-13-v6.2"`, baked into every gate
    fingerprint and `quality_decision` row), `mnemo/data/quality-v3/manifest.json` (frozen
    2026-09-11) and `mnemo/data/quality-v4/manifest.json` (reserved 09-12, labeled 09-14).
    The gitignored `docs/evaluation/` and `docs/quality-v3..v7/` trees hold ~60 more files
    stamped 2026-09-08..09-19; un-ignoring `docs/` (Task 0.3) brings them into the repo.
    `PROJECT_SPEC.md`, `PROJECT_STATUS.md`, `README.md` and the research markdown carry no
    dates. `AGENTS.md` instructs continuing the synthetic timeline — that is your call, but
    the inconsistency should be a conscious decision (see D8).

## A4. Verification of the three research documents

### `MEMORY_VERSIONING_MVP_RESEARCH.md` — §2 "important current limitations"

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `memory_add` is an upsert by subject/predicate | **True** | `core.py:289-353`; probe E2 `UPDATE` on same `fact_id` |
| 2 | Updates cannot name a `fact_id` | **True** | `mcp_server.py:107-122` signature |
| 3 | Revert has no expected-current-event argument | **True** | `mcp_server.py:171`; probe: stale revert to E2 accepted after E3 |
| 4 | Revert always creates an event; same-value dedup ≠ request dedup | **True** | probe: revert-to-current created a 5th event |
| 5 | MCP search omits `event_id`; blame unpaginated | **True** | `mcp_server.py:138-149`; probe keys list |
| 6 | Revert hard-codes `human_review`/high; add accepts LLM provenance | **True** | `core.py:853`; probe: `provenance="human_review"` → trust `high` |
| 7 | Narrow DB-name aliases no-op | **True** | `core.py:95-100`; probe: `Postgres` after `PostgreSQL` returned E3 |
| 8 | `parent_event_id` on REVERT = restored event, not displaced HEAD | **True** | `core.py:857`; probe `parent-> E1`. **Note:** the displaced HEAD *is* derivable today: it is the event whose `superseded_by` = the REVERT's `event_id`. Receipts should still record it, but "undo the undo" does not need a schema change. |

§4 "smallest useful milestone" (external stdio proof): **already true** — the probe executed
exactly that sequence; what is missing is the *test + example in the repo* and a way to run
it without Ollama (see D1). §5 API and receipt sketch: sound; adopted below with two
corrections (error-code names unified; `memory_history` uses an opaque cursor, not `before_seq`).

### `MEMORY_SYSTEM_COMPARISON.md`

- Mnemo-side statements: all consistent with the code.
- Memory Engine `me_memory_revert` claims re-checked against the live docs on Sept 21:
  `expectedVersionHash` optional guard, 30-day audit window, revert = new forward version,
  **"reverting to the current state is a no-op"** — all confirmed. Its `NOT_FOUND`/`CONFLICT`
  error codes are a good precedent for ours.
- Mem0 / Cognee claims are pinned to commit hashes in `memory-system-research-sources.json`
  and were not re-fetched; nothing in this plan depends on them beyond "history/undo exist
  elsewhere, so don't claim novelty".
- Its "one-week budget" and "prove externally first" ordering are correct and kept.

### `MEMORY_TOOL_IMPLEMENTATION_PLAN.md`

What is right: the product framing (§1), the responsibility split host ⇄ store (§4), the
transaction recipe (§6), the 18 deterministic cases (§8A), "no paid calls without a cap".

What this plan changes:

| Issue | Change |
|---|---|
| M0 "restore the test environment" | Done: 231 pass. Replace M0 with **repo hygiene** (A3 items 1–5), which the old plan never addresses. |
| It never touches the portfolio *story* | Phase 4 rewrites README/spec/status around the pivot and labels the extraction pipeline honestly. |
| "Inject a fake embedder through a test-only launcher; no production fake provider" | Reversed (D1): a deterministic `hash` embedder backend makes `make demo-direct`, the stdio test and CI work with **zero** model dependencies on a fresh clone. It is loudly labelled non-semantic. |
| Two error-code vocabularies across the docs | One list, fixed in D3. |
| M5's 24-conversation live-host study with 90% gates | Reduced to an optional, scripted 6-scenario agent demo with saved transcripts (Phase 5). A portfolio does not need a measurement paper for the undo path. |
| Receipts must record displaced HEAD "because `parent_event_id` can't" | Also derivable via `superseded_by`; receipts record it for convenience. |
| Prose-heavy, no code, no test names | Tasks below name files, functions, SQL, test functions and assertions. |
| M6 Pilot B (identity_mode) and M7 export | Kept deferred (Phase 6), unchanged. |

## A5. Strategic assessment: the story problem

The repository currently tells two stories at once, and leads with the weaker one:

- **Story 1 (README lead):** "stores less and remembers what matters" — a quality gate on
  extracted facts. Verified reality: the gate itself works and is well-tested, but with the
  only models the project has run (3–4B local), real-conversation precision/recall is in
  the low single digits and seven documented cycles did not move it. The README prints
  those numbers. A reviewer reads "this failed".
- **Story 2 (what the code is genuinely good at):** an append-only, bitemporal, versioned
  memory store with immutability enforced in the database, auditable provenance, reversible
  archive, and `blame`/`revert` — the foundation. This is *unusual* among agent-memory
  projects, well-tested, and demoable without any model.

Recommendation (adopted below): **lead with Story 2, keep Story 1 as an optional, honestly
labelled pipeline.** The extraction pipeline and eval harness are not deleted — they are
real engineering and the honesty of the eval is itself a portfolio asset — but they stop
being the headline until a stronger model is measured.

---

# Part B — Decisions

These are contract or scope changes (`AGENTS.md` rule 6). Each has a recommendation; the
executing agent proceeds with the recommendation unless you say otherwise.

**D1. Add a deterministic embedder backend: `MNEMO_BACKEND=hash`.** Recommended: **yes**.
SHA-256 based, dimension from `embed_dim`, no network, identical text ⇒ identical vector.
Exact-match search still works (FTS/ILIKE path); ranking by similarity does not. With
`backend=hash`, `build_extractor`/`build_verifier` raise a clear error unless
`worker_enabled=false`. Purpose: fresh-clone `make demo-direct`, CI stdio tests, reviewers
without Ollama. Label it non-semantic everywhere it appears.

**D2. Opt-in direct MCP profile (`mnemo-mcp-direct`, `mnemo/mcp_direct.py`) with six tools;
legacy server unchanged.** Recommended: **yes**, exactly as the research proposes.

**D3. One error vocabulary.** `INVALID_INPUT`, `NOT_FOUND`, `ALREADY_EXISTS`,
`REVISION_CONFLICT`, `INVALID_RESTORE_TARGET`, `UNSUPPORTED_STATE`, `REQUEST_ID_REUSED`,
`UNSUPPORTED_OPERATION`. Errors are MCP tool errors whose message is a JSON object
`{"code","message","details"}`. Not-found and foreign IDs are indistinguishable.

**D4. Migration `0010_mutation_receipts.sql`** (additive table + immutability trigger).
Recommended: **yes**. Schema in Phase 2.

**D5. Un-ignore `docs/`, commit `uv.lock`, drop the `AGENTS.md` ignore.** Recommended: **yes**.
13 MB of evidence is fine for git. If you prefer a lighter repo, move `docs/quality-v2…v7`
to `docs/archive/` first — but track it.

**D6. Guarded revert preserves the restored event's provenance/trust and records the
requesting actor separately.** Legacy `MnemoStore.revert()` keeps `human_review`/high so the
existing tests and demo keep their meaning. Recommended: **yes**.

**D7. Story pivot in README/spec.** Lead with the versioned store + guarded undo over MCP;
keep the quality pipeline as "optional extraction pipeline (experimental; measured results
in `docs/quality-*`)". Soften spec §0 "incumbents with mutable stores literally cannot"
(Memory Engine does it with triggers). Recommended: **yes**.

**D8. Commit dating.** `AGENTS.md` says continue the synthetic timeline (HEAD 2026-05-29,
≤7 commits/day). `mnemo/audit.py`, the two tracked quality manifests, and the gitignored
`docs/evaluation` + `docs/quality-v*` artifacts carry September 2026 timestamps (A3-12).
Decide one of: (a) keep the rule and rename `pipeline_version` to a non-date tag (e.g.
`v6.2`; this changes the gate fingerprint, so expect `tests/test_audit.py` snapshot updates)
and either leave the manifests/artifacts as data or strip their timestamps, (b) drop the
rule and let commits carry real dates. This
plan does not decide for you; the executing agent follows `AGENTS.md` as written until told
otherwise.

**D9. Pilot B (member/occurrence identity) and export/import stay deferred** until Phases
0–4 ship and a concrete need exists. Consolidation stays disabled. Recommended: **yes**.

---

# Part C — The plan

Conventions for every task: TDD (write the failing test first), `make lint` clean, run the
focused tests then `make test-db` before each commit, one commit per task or per small group
of related tasks, message prefixes `fix:` / `feat:` / `test:` / `docs:` / `chore:`.
Commit dates follow `AGENTS.md` until D8 is resolved.

## Phase 0 — Repo hygiene and truth (½–1 day)

Goal: a stranger can `git clone && make install && make up && make migrate && make eval`
on a clean machine, and the front page contains no dead links.

### Task 0.1 — Fix fresh-database `make migrate`

**Files:** modify `mnemo/db.py:46-61`; test `tests/test_schema.py`.

- [x] Write the failing test (needs the maintenance DB like `conftest._disposable_test_db`):

```python
async def test_connect_and_migrate_on_database_without_vector_extension(_disposable_test_db):
    """Regression: `make migrate` on a fresh DB crashed with 'unknown type: public.vector'."""
    from urllib.parse import urlsplit, urlunsplit
    from uuid import uuid4
    import asyncpg
    from mnemo import db as mdb

    parts = urlsplit(_disposable_test_db)
    name = "fresh_" + uuid4().hex[:12]
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        dsn = urlunsplit(parts._replace(path="/" + name))
        conn = await mdb.connect(dsn)            # used to raise ValueError here
        try:
            applied = await mdb.apply_migrations(conn)
            assert applied[0] == "0001_init.sql"
            assert await conn.fetchval("SELECT count(*) FROM memory_fact") == 0
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()
```

- [x] Run: `MNEMO_REQUIRE_DB=1 uv run pytest tests/test_schema.py -k without_vector -v` → FAIL with `ValueError: unknown type: public.vector`.
- [x] Fix `register_vector`:

```python
    except (asyncpg.exceptions.UndefinedObjectError, ValueError):
        # asyncpg raises ValueError("unknown type: public.vector") before the
        # extension exists (fresh database, migrations not yet applied).
        pass
```

- [x] Re-run → PASS. Run the full suite. Commit: `fix: allow connect() before the vector extension exists (fresh-db make migrate)`.

### Task 0.2 — Track the lockfile; make CI use it

**Files:** `.gitignore` (remove `uv.lock`), `.github/workflows/correctness.yml:27`.

- [x] Remove the `uv.lock` line from `.gitignore`; `git add uv.lock`.
- [x] CI: `- run: uv sync --locked --extra dev`.
- [x] Use the same locked command for `make install`; include `uv.lock` in the
  source distribution and verify source installation in `scripts/check-wheel.sh`.
- [x] Commit: `chore: commit uv.lock and install with --locked in CI`.

### Task 0.3 — Un-ignore docs; one operating guide

**Files:** `.gitignore` (remove `docs/`, remove the uncommitted `AGENTS.md` line), `CLAUDE.md`.

- [x] Remove both lines. `git add docs/` (or move `docs/quality-v2..v7` → `docs/archive/quality/` first if you want a lighter tree; update the links in `PROJECT_STATUS.md` / `README.md` accordingly).
- [x] Replace `CLAUDE.md` body with a pointer so there is one source of truth:

```markdown
# CLAUDE.md
The operating guide lives in `AGENTS.md` (read it in full). Then read `PROJECT_SPEC.md`
and `docs/MASTER_PLAN.md`.
@AGENTS.md
```

- [x] Commit: `chore: track docs and lockfile; AGENTS.md is the single operating guide`.

### Task 0.4 — Remove dead links and stale numbers

**Files:** `README.md`, `PROJECT_SPEC.md:3-6`, `PROJECT_STATUS.md:3-6`, `Makefile:50-51`.

- [x] `rg -n "CORRECTNESS_PLAN|QUALITY_RELIABILITY_PLAN|demo.tape" .` — replace each reference: the two plans → `docs/MASTER_PLAN.md`; delete the `gif:` Makefile target (or add `docs/demo.tape` — there is none).
- [x] README: "200 tests pass" → "`make test-db` (231 tests as of this commit; the number is printed by the run)". Prefer wording that does not need updating.
- [x] `mnemo/seed.py:3-6` docstring: describe what it is now (demo seed data), not "before core exists".
- [x] `tests/test_eval.py:49-56`: the docstring promises must-keep recall but the assertion is
  `result["gated"]["recall"] == 1.0`. Add `assert result["gated"]["must_keep_recall"] == 1.0`
  (keep the existing line) so the named field in the README claim is what the test checks.
- [x] `tests/test_mcp.py` `EXPECTED_TOOLS`: add `memory_decisions` and `memory_health` so the
  schema-registration test covers all ten tools, not eight.
- [x] Commit: `docs: remove dead links and stale counts`.

### Task 0.5 — Push and get one green CI run (your action)

- [x] Create the GitHub repo, configure `origin`, and push `main`.
- Hosted CI and required merge checks were declined by the owner. GitHub Actions
  remains disabled; there is no hosted green-CI result. Local verification is
  recorded in `PROJECT_STATUS.md`.

**Phase 0 exit:** fresh clone → `make install && make up && make migrate && make eval` works
with no Ollama; local checks pass (hosted CI waived by owner);
`rg CORRECTNESS_PLAN` returns nothing; `git check-ignore docs/` fails.

---

## Phase 1 — Deterministic backend + external lifecycle proof (1 day)

Goal: the create → update → history → revert → read-back lifecycle is proven through a **real
stdio MCP client** in the test suite and as a runnable demo, with zero model dependencies.

### Task 1.1 — `HashEmbedder` and `MNEMO_BACKEND=hash` (D1)

**Files:** `mnemo/embedder.py`, `mnemo/config.py:31`, `mnemo/extraction.py::build_extractor`,
`mnemo/quality.py::build_verifier`, `mnemo/runtime.py`, `tests/test_embeddings.py`,
`tests/test_runtime_config.py`, `.env.example`.

- [x] Failing tests:

```python
def test_hash_embedder_is_deterministic_and_normalized():
    from mnemo.embedder import HashEmbedder
    e = HashEmbedder(dim=768)
    a, b = e.embed("PostgreSQL"), e.embed("PostgreSQL")
    assert a == b and len(a) == 768
    assert abs(sum(x * x for x in a) ** 0.5 - 1.0) < 1e-6
    assert e.embed("MySQL") != a

def test_hash_backend_builds_hash_embedder(monkeypatch):
    from mnemo.config import Settings
    from mnemo.embedder import HashEmbedder, build_embedder
    s = Settings(backend="hash", embed_dim=64)
    assert isinstance(build_embedder(s), HashEmbedder)
    assert build_embedder(s).dim == 64

def test_hash_backend_disables_worker_by_default_and_refuses_explicit_worker():
    from mnemo.config import Settings
    import pytest
    assert Settings(backend="hash").worker_enabled is False          # implied, like openai implies 1536
    with pytest.raises(ValueError, match="worker_enabled=false"):
        Settings(backend="hash", worker_enabled=True)                # explicit contradiction
```

- [x] Implement. `embedder.py`:

```python
class HashEmbedder:
    """Deterministic, dependency-free embeddings. NOT semantic.

    SHA-256 of ``f"{i}:{text}"`` chunks -> fixed-length L2-normalized vector. Identical
    text gives an identical vector; unrelated text gives ~0 cosine. Exists so the direct
    write/undo path, demos and CI run with no model server. Never use it to claim search
    ranking quality.
    """

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        import hashlib
        raw = b""
        i = 0
        while len(raw) < self.dim * 4:
            raw += hashlib.sha256(f"{i}:{text}".encode()).digest()
            i += 1
        vals = [(int.from_bytes(raw[j * 4 : j * 4 + 4], "big") / 2**31) - 1.0 for j in range(self.dim)]
        norm = sum(v * v for v in vals) ** 0.5 or 1.0
        return [v / norm for v in vals]
```

  `build_embedder`: `if s.backend == "hash": return HashEmbedder(s.embed_dim)`.
  `config.py`: `backend: Literal["ollama", "openai", "hash"]`; in `validate_configuration`
  add, mirroring the existing `openai` block:

```python
        if self.backend == "hash":
            if "worker_enabled" not in self.model_fields_set:
                self.worker_enabled = False          # hash has nothing to extract/verify with
            elif self.worker_enabled:
                raise ValueError("backend=hash has no extractor/verifier; set MNEMO_WORKER_ENABLED=false")
```
  `build_extractor` / `build_verifier`: `if s.backend == "hash": raise ValueError(...)` (defensive).
  `.env.example`: document `MNEMO_BACKEND=hash  # deterministic, non-semantic; direct profile/demo only`.
- [x] Replace `tests/conftest.py::FakeEmbedder` with `from mnemo.embedder import HashEmbedder as FakeEmbedder` (same algorithm — vectors are identical, so no test fixtures change).
- [x] Run full suite, lint. Commit: `feat: deterministic hash embedder backend for model-free direct memory`.

### Task 1.2 — External stdio lifecycle test (legacy tools)

**Files:** create `tests/test_mcp_direct_stdio.py`.

- [x] Write the test (this is the probe that was run during verification, formalized):

```python
"""Real stdio client: create -> update -> history -> revert -> read back, no models."""
import asyncio, json, os, sys
from uuid import uuid4
import asyncpg
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _payload(result):
    items = [c.text for c in result.content if getattr(c, "text", None)]
    return json.loads(items[0]) if len(items) == 1 else [json.loads(i) for i in items]


async def test_external_lifecycle_postgres_mysql_postgres(_disposable_test_db, clean_memory):
    env = {**os.environ, "MNEMO_DSN": _disposable_test_db, "MNEMO_BACKEND": "hash",
           "MNEMO_WORKER_ENABLED": "false", "MNEMO_SEARCH_REINFORCE": "false",
           "MNEMO_NAMESPACE": "direct-" + uuid4().hex}
    params = StdioServerParameters(command=sys.executable, args=["-m", "mnemo.mcp_server"], env=env)
    async with asyncio.timeout(30):
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as c:
                await c.initialize()
                e1 = _payload(await c.call_tool("memory_add", {"subject": "user", "predicate": "preferred_database", "object": "PostgreSQL", "provenance": "direct_user_statement"}))
                e2 = _payload(await c.call_tool("memory_add", {"subject": "user", "predicate": "preferred_database", "object": "MySQL"}))
                assert e1["op"] == "ADD" and e2["op"] == "UPDATE" and e2["fact_id"] == e1["fact_id"]
                fid = e1["fact_id"]
                assert _payload(await c.call_tool("memory_get", {"fact_id": fid}))["object_text"] == "MySQL"
                hist = _payload(await c.call_tool("memory_blame", {"fact_id": fid}))
                assert [(h["op"], h["object_text"]) for h in hist] == [("ADD", "PostgreSQL"), ("UPDATE", "MySQL")]
                e3 = _payload(await c.call_tool("memory_revert", {"fact_id": fid, "to_event_id": e1["event_id"]}))
                assert e3["op"] == "REVERT" and e3["object_text"] == "PostgreSQL"
                got = _payload(await c.call_tool("memory_get", {"fact_id": fid}))
                assert got["object_text"] == "PostgreSQL" and got["event_id"] == e3["event_id"]
                hits = _payload(await c.call_tool("memory_search", {"query": "preferred_database"}))
                hits = hits if isinstance(hits, list) else [hits]
                assert [h["value"] for h in hits if h["fact_id"] == fid] == ["PostgreSQL"]
    # Restart the server: state survives, and nothing background ran.
    async with asyncio.timeout(30):
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as c:
                await c.initialize()
                assert _payload(await c.call_tool("memory_get", {"fact_id": fid}))["object_text"] == "PostgreSQL"
    conn = await asyncpg.connect(_disposable_test_db)
    try:
        rows = await conn.fetch("SELECT event_id, op, object_text, superseded_by FROM memory_event WHERE fact_id=$1 ORDER BY seq", fid)
        assert [(r["op"], r["object_text"]) for r in rows] == [("ADD", "PostgreSQL"), ("UPDATE", "MySQL"), ("REVERT", "PostgreSQL")]
        assert rows[0]["superseded_by"] == rows[1]["event_id"]      # E1 -> superseded by E2
        assert rows[1]["superseded_by"] == rows[2]["event_id"]      # E2 -> superseded by E3 (the displaced HEAD is derivable)
        assert rows[2]["superseded_by"] is None                      # E3 is HEAD
        assert await conn.fetchval("SELECT count(*) FROM memory_current WHERE fact_id=$1", fid) == 1
        assert await conn.fetchval("SELECT count(*) FROM extraction_job") == 0
        assert await conn.fetchval("SELECT count(*) FROM quality_decision") == 0
    finally:
        await conn.close()
```

- [x] Add to `tests/test_runtime_config.py` a unit test that `background_runtime` with
  `worker_enabled=False` never calls `build_extractor`/`build_verifier` (monkeypatch both to raise).
- [x] Run, lint, commit: `test: external stdio lifecycle proof with no model dependencies`.

### Task 1.3 — Runnable direct demo

**Files:** create `examples/direct_memory.py`; `Makefile` add `demo-direct`; README (one paragraph, full rewrite waits for Phase 4).

- [x] `examples/direct_memory.py`: Typer app; connects with `MNEMO_BACKEND=hash` unless the
  user has a real backend configured; runs the five-step conversation from the research
  doc, printing each returned `event_id`, then the `blame` table, then `get`; uses a fresh
  namespace `demo-direct-<hex>`. Prints "embeddings: hash (non-semantic)" when applicable.
- [x] `Makefile`: `demo-direct:  ## Direct write -> history -> revert through the SDK, no models\n\tMNEMO_BACKEND=hash MNEMO_WORKER_ENABLED=false $(RUN) python examples/direct_memory.py`
- [x] Hermetic test `tests/test_demo.py::test_direct_demo_runs` (call the scenario function
  against the disposable DB).
- [x] Commit: `feat: make demo-direct — model-free write/history/revert demo`.

**Phase 1 exit:** `make demo-direct` works on a fresh clone; the stdio test passes in CI;
no extractor/verifier is constructed when the worker is disabled (tested).

---

## Phase 2 — Guarded mutations and receipts in the core (2–3 days)

Goal: create-only / update-by-ID / guarded revert with expected revisions, durable idempotency
receipts, trust-preserving restore, structured errors — in one transaction each, on real
Postgres, covered by the 18 deterministic cases. No change to legacy `add()`/`revert()` behavior.

### Task 2.1 — Errors module

**Files:** create `mnemo/errors.py`; test `tests/test_direct_mutations.py` (starts here).

```python
"""Structured errors for the direct memory contract (D3)."""
from __future__ import annotations
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    INVALID_RESTORE_TARGET = "INVALID_RESTORE_TARGET"
    UNSUPPORTED_STATE = "UNSUPPORTED_STATE"
    REQUEST_ID_REUSED = "REQUEST_ID_REUSED"
    UNSUPPORTED_OPERATION = "UNSUPPORTED_OPERATION"


class MnemoError(Exception):
    def __init__(self, code: ErrorCode, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def to_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "details": self.details}
```

- [x] Test: `MnemoError(ErrorCode.NOT_FOUND, "x", fact_id="f").to_dict()` shape. Commit with 2.2.

### Task 2.2 — Migration `0010_mutation_receipts.sql` (D4)

**Files:** create `migrations/0010_mutation_receipts.sql`; `tests/test_schema.py`; `tests/conftest.py::clean_memory` (add the table to TRUNCATE).

```sql
-- 0010_mutation_receipts.sql — durable idempotency receipts for guarded direct mutations.
-- One row per successful or no-change mutation. Append-only like memory_event.
CREATE TABLE memory_mutation_receipt (
  receipt_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  namespace              text NOT NULL,
  user_id                text NOT NULL,
  agent_id               text NOT NULL,
  request_id             uuid NOT NULL,                 -- caller-generated once per intended mutation
  operation              text NOT NULL CHECK (operation IN ('create','update','revert')),
  request_payload        jsonb NOT NULL,                -- canonical validated parameters (for reuse detection)
  status                 text NOT NULL CHECK (status IN ('applied','no_change')),
  fact_id                uuid NOT NULL REFERENCES memory_fact(fact_id),
  event_id               uuid NOT NULL REFERENCES memory_event(event_id),   -- resulting (or unchanged HEAD) event
  previous_event_id      uuid REFERENCES memory_event(event_id),            -- displaced HEAD, NULL for create/no_change
  restored_from_event_id uuid REFERENCES memory_event(event_id),            -- revert target
  actor                  text,
  result                 jsonb NOT NULL,                -- exact MutationResult returned to the caller
  created_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (namespace, user_id, agent_id, request_id)
);
CREATE INDEX idx_receipt_fact ON memory_mutation_receipt (fact_id, created_at DESC);

CREATE FUNCTION protect_mutation_receipt() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'memory_mutation_receipt is append-only' USING ERRCODE = '23514';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER mutation_receipt_immutable
  BEFORE UPDATE OR DELETE ON memory_mutation_receipt
  FOR EACH ROW EXECUTE FUNCTION protect_mutation_receipt();
```

- [x] Schema test: table exists; UNIQUE on scope+request_id; UPDATE/DELETE raise `23514`.
- [x] `conftest.clean_memory`: add `memory_mutation_receipt` to the TRUNCATE list.
- [x] Commit: `feat: mutation receipt table (0010) and structured error codes`.

### Task 2.3 — Result models

**Files:** `mnemo/models.py` (append).

```python
class MutationResult(BaseModel):
    status: Literal["applied", "no_change"]
    fact_id: UUID
    event_id: UUID
    previous_event_id: UUID | None = None
    restored_from_event_id: UUID | None = None
    value: str
    request_id: UUID
    replayed: bool = False          # true when served from a receipt


class CurrentValue(BaseModel):
    fact_id: UUID
    subject: str
    predicate: str
    value: str
    current_event_id: UUID
    provenance: str
    trust_level: str
    recorded_at: datetime


class HistoricalValue(BaseModel):
    fact_id: UUID
    event_id: UUID
    op: str
    value: str
    provenance: str
    trust_level: str
    actor: str | None
    recorded_at: datetime
    current_event_id: UUID | None   # HEAD at read time
    restorable: bool                # eligibility under the direct profile


class HistoryEntry(BaseModel):
    event_id: UUID
    seq: int
    op: str
    value_preview: str
    value_truncated: bool
    provenance: str
    trust_level: str
    actor: str | None
    recorded_at: datetime
    restored_from_event_id: UUID | None = None   # for REVERT rows (= parent_event_id)


class HistoryPage(BaseModel):
    fact_id: UUID
    current_event_id: UUID | None
    entries: list[HistoryEntry]
    next_cursor: str | None


class SearchHit(BaseModel):
    fact_id: UUID
    event_id: UUID
    subject: str
    predicate: str
    value: str
    score: float
```

(`from typing import Literal` at top.) Commit with 2.4.

### Task 2.4 — Shared revert copy helper (refactor, behavior-preserving)

**Files:** `mnemo/core.py:829-881`.

- [x] Extract the `INSERT … SELECT` in `revert()` into:

```python
    async def _copy_as_revert(
        self, fact_id: UUID, to_event_id: UUID, *,
        provenance: str | None, trust_level: str | None, actor: str | None, reason: str,
    ) -> Event:
        """Append a REVERT copying ``to_event_id``'s payload/embedding/lineage.

        ``provenance``/``trust_level`` None => preserve the restored event's own values
        (direct profile). Legacy ``revert()`` passes 'human_review'/'high'."""
        row = await self.conn.fetchrow(
            f"""
            INSERT INTO memory_event
                (fact_id, op, object_text, object_number, object_json, embedding,
                 provenance, actor, confidence, trust_level, source_span,
                 session_id, expires_at, valid_from, parent_event_id, importance, write_score,
                 tier, reason, strength, recall_count, last_used)
            SELECT fact_id, 'REVERT', object_text, object_number, object_json, embedding,
                   COALESCE($3::mem_provenance, provenance), $4, confidence,
                   COALESCE($5::mem_trust, trust_level), source_span,
                   COALESCE(session_id, $6),
                   CASE WHEN tier='session' THEN clock_timestamp()+make_interval(secs => $7) END,
                   now(), event_id, importance, write_score, tier,
                   $8, strength, recall_count, last_used
            FROM memory_event WHERE event_id=$1 AND fact_id=$2
            RETURNING {_EVENT_COLS}
            """,
            to_event_id, fact_id, provenance, actor, trust_level,
            await self.conn.fetchval("SELECT session_id FROM memory_fact WHERE fact_id=$1", fact_id),
            self.settings.session_ttl_seconds, reason,
        )
        return Event.from_row(row)
```

  `revert()` calls it with `provenance="human_review", trust_level="high"`. All existing
  tests must stay green unchanged (`test_lifecycle`, `test_council`, `test_temporal_cache`).
- [x] Commit: `refactor: share the REVERT copy SQL between legacy and guarded revert`.

### Task 2.5 — `DirectMemory` mutations

**Files:** create `mnemo/direct.py`; test `tests/test_direct_mutations.py`.

**Interfaces (Produces):**

```python
class DirectMemory:
    MAX_VALUE_BYTES = 8192
    PREVIEW_CHARS = 256

    def __init__(self, store: MnemoStore) -> None: ...
    async def create(self, subject: str, predicate: str, value: str, *, request_id: UUID,
                     actor: str | None = None, source_span: Any | None = None) -> MutationResult
    async def update(self, fact_id: UUID, value: str, *, expected_event_id: UUID,
                     request_id: UUID, actor: str | None = None) -> MutationResult
    async def revert(self, fact_id: UUID, to_event_id: UUID, *, expected_event_id: UUID,
                     request_id: UUID, actor: str | None = None) -> MutationResult
    async def get(self, fact_id: UUID, event_id: UUID | None = None) -> CurrentValue | HistoricalValue
    async def history(self, fact_id: UUID, *, cursor: str | None = None, limit: int = 20) -> HistoryPage
    async def search(self, query: str, *, limit: int = 5) -> list[SearchHit]
```

**Mutation transaction (all three write methods):**

```python
    async def _mutate(self, operation, request_id, payload, *, actor, apply) -> MutationResult:
        """payload: canonical dict (sorted keys, UUIDs as str). apply(fact_row) -> MutationResult
        runs inside the transaction after the receipt check and returns the result to persist."""
        async with self.store.conn.transaction():
            await self.store._lock_scope()
            receipt = await self.store.conn.fetchrow(
                "SELECT operation, request_payload, result FROM memory_mutation_receipt "
                "WHERE namespace=$1 AND user_id=$2 AND agent_id=$3 AND request_id=$4",
                self.store.namespace, self.store.user_id, self.store.agent_id, request_id)
            if receipt is not None:
                stored = receipt["request_payload"]
                stored = json.loads(stored) if isinstance(stored, str) else stored
                if receipt["operation"] == operation and stored == payload:
                    result = receipt["result"]
                    result = json.loads(result) if isinstance(result, str) else result
                    return MutationResult(**{**result, "replayed": True})
                raise MnemoError(ErrorCode.REQUEST_ID_REUSED,
                                 "request_id was already used for a different mutation", request_id=str(request_id))
            result = await apply()                       # raises MnemoError on conflict etc.
            await self._insert_receipt(request_id, operation, payload, actor, result)
            return result

    async def _insert_receipt(self, request_id, operation, payload, actor, result: MutationResult) -> None:
        """Separate method on purpose: the rollback test patches it to simulate a crash
        between the event insert and the receipt insert (asyncpg's Connection uses
        __slots__, so its methods cannot be monkeypatched)."""
        await self.store.conn.execute(
            """INSERT INTO memory_mutation_receipt
               (namespace, user_id, agent_id, request_id, operation, request_payload, status,
                fact_id, event_id, previous_event_id, restored_from_event_id, actor, result)
               VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12,$13::jsonb)""",
            self.store.namespace, self.store.user_id, self.store.agent_id, request_id, operation,
            json.dumps(payload, sort_keys=True), result.status, result.fact_id, result.event_id,
            result.previous_event_id, result.restored_from_event_id, actor,
            result.model_dump_json())
```

Embeddings for `create`/`update` are computed in the public method **before** calling
`_mutate` (outside the lock) and passed into `apply` via closure; the expected-revision check
happens inside the transaction. `get()` on a legacy fact whose value is numeric/JSON returns
`str(fact.value)` — the direct profile is string-only, but reads must not fail on legacy data.

Rules inside `apply` (each raises `MnemoError`):

- **Validation (before the transaction, before embedding):** `value` must be `str`, not
  whitespace-only, ≤ 8192 UTF-8 bytes → else `INVALID_INPUT`. Store the exact string.
- **create:** `fact_key = canonicalize(subject, predicate)`; embed outside the lock; inside:
  if a fact row exists for the key → `ALREADY_EXISTS` (details: `fact_id`,
  `current_event_id`, resolved `fact_key`). Else `_insert_fact` + `_insert_event(op="ADD",
  provenance="agent_inference", trust_level="low", tier="durable", actor=actor)` +
  `_set_head`. `status="applied"`, `previous_event_id=None`.
- **update:** `_locked_fact(fact_id)` None → `NOT_FOUND`. `fact.status != 'active'` or the
  HEAD event is not durable / has `valid_to` / `expires_at` → `UNSUPPORTED_STATE`.
  `fact.current_event_id != expected_event_id` → `REVISION_CONFLICT` (details:
  `current_event_id`). Exact same `object_text` → `status="no_change"`, `event_id=HEAD`,
  no event inserted (receipt still inserted). Else `_emit_update(fact_id, head, value,
  provenance="agent_inference", trust_level="low", actor=actor, …)`. **Do not route through
  `add()`** — no alias no-op in this profile ("Postgres" after "PostgreSQL" is a revision).
  Embedding is computed before the transaction; the expected-revision check happens inside.
- **revert:** `_locked_fact` None → `NOT_FOUND`. Same state checks as update →
  `UNSUPPORTED_STATE`. Expected check → `REVISION_CONFLICT` (this also rejects A→B→A: the
  expected *event id* differs even when the value is the same). Target row
  `SELECT … WHERE event_id=$1 AND fact_id=$2` missing → `INVALID_RESTORE_TARGET` (message
  does not echo IDs). Target `op NOT IN ('ADD','UPDATE','REVERT')` or `tier<>'durable'` or
  `session_id IS NOT NULL` or `valid_to IS NOT NULL` or `expires_at IS NOT NULL` →
  `UNSUPPORTED_STATE`. Target == current HEAD → `status="no_change"`. Else
  `_copy_as_revert(provenance=None, trust_level=None, actor=actor, reason=f"revert to
  {target[:8]} requested by {actor or 'agent'}")`, `_supersede(head, new)`, `_set_head`.
  `previous_event_id=head`, `restored_from_event_id=to_event_id`.

**Reads:**

- `get(fact_id)`: scoped `memory_current` row → `CurrentValue`; none → `NOT_FOUND`.
  `get(fact_id, event_id)`: scoped fact + `event_id ∈ fact` → `HistoricalValue` with
  `restorable` computed by the same eligibility predicate; otherwise `NOT_FOUND` (not
  distinguishable from foreign).
- `history(fact_id, cursor, limit)`: `limit` clamped to 1..100. Cursor = URL-safe base64 of
  `{"f": fact_id, "max": max_seq_at_first_page, "before": last_seq_returned}`. Page SQL:
  `SELECT … FROM memory_event WHERE fact_id=$1 AND seq <= $max AND seq < $before ORDER BY seq
  DESC LIMIT $limit+1` in **one** `repeatable_read` transaction with the `current_event_id`
  read. Cursor with a different `fact_id` or unparseable → `INVALID_INPUT`. Scope is
  re-checked on every page. `value_preview` = first 256 chars; `value_truncated` flag.
- `search(query, limit)`: `store.search(query, k=limit, reinforce=False)` (no `session_id`
  ⇒ no fast-cache rows), mapped to `SearchHit` with `event_id`.

- [x] Tests — `tests/test_direct_mutations.py`. Use fixtures `store` (rolled back) for
  single-connection cases and `_disposable_test_db` + `clean_memory` with two real
  `asyncpg.connect`s for race/rollback/restart cases. One test per numbered case in Part D;
  function names are fixed there. Minimal skeletons for the non-obvious ones:

```python
async def test_05_two_writers_same_expected_head_one_conflicts(_disposable_test_db, clean_memory, fake_embedder):
    a, b = await asyncpg.connect(_disposable_test_db), await asyncpg.connect(_disposable_test_db)
    for c in (a, b): await register_vector(c)
    try:
        da, db_ = DirectMemory(MnemoStore(a, fake_embedder)), DirectMemory(MnemoStore(b, fake_embedder))
        e1 = await da.create("user", "preferred_database", "PostgreSQL", request_id=uuid4())
        r1, r2 = await asyncio.gather(
            da.update(e1.fact_id, "MySQL", expected_event_id=e1.event_id, request_id=uuid4()),
            db_.update(e1.fact_id, "SQLite", expected_event_id=e1.event_id, request_id=uuid4()),
            return_exceptions=True)
        ok = [r for r in (r1, r2) if isinstance(r, MutationResult)]
        bad = [r for r in (r1, r2) if isinstance(r, MnemoError)]
        assert len(ok) == 1 and len(bad) == 1 and bad[0].code == ErrorCode.REVISION_CONFLICT
        assert await a.fetchval("SELECT count(*) FROM memory_event WHERE fact_id=$1", e1.fact_id) == 2
    finally:
        await a.close(); await b.close()


async def test_06_a_b_a_still_rejects_stale_expected_event(store, fake_embedder):
    d = DirectMemory(store)
    e1 = await d.create("user", "preferred_database", "PostgreSQL", request_id=uuid4())
    e2 = await d.update(e1.fact_id, "MySQL", expected_event_id=e1.event_id, request_id=uuid4())
    e3 = await d.update(e1.fact_id, "PostgreSQL", expected_event_id=e2.event_id, request_id=uuid4())
    assert (await d.get(e1.fact_id)).value == "PostgreSQL"
    with pytest.raises(MnemoError) as err:
        await d.update(e1.fact_id, "SQLite", expected_event_id=e1.event_id, request_id=uuid4())
    assert err.value.code == ErrorCode.REVISION_CONFLICT
    assert err.value.details["current_event_id"] == str(e3.event_id)


async def test_10_failure_before_receipt_rolls_back_event_and_head(store, monkeypatch):
    d = DirectMemory(store)
    e1 = await d.create("user", "preferred_database", "PostgreSQL", request_id=uuid4())

    async def boom(self, *args, **kwargs):
        raise RuntimeError("simulated crash between event insert and receipt insert")

    monkeypatch.setattr(DirectMemory, "_insert_receipt", boom)
    with pytest.raises(RuntimeError):
        await d.update(e1.fact_id, "MySQL", expected_event_id=e1.event_id, request_id=uuid4())
    monkeypatch.undo()
    assert await store.conn.fetchval("SELECT count(*) FROM memory_event WHERE fact_id=$1", e1.fact_id) == 1
    assert (await d.get(e1.fact_id)).current_event_id == e1.event_id
    assert await store.conn.fetchval("SELECT count(*) FROM memory_mutation_receipt WHERE fact_id=$1", e1.fact_id) == 1  # only the create's


async def test_14_direct_revert_preserves_source_trust_and_records_actor(store):
    d = DirectMemory(store)
    e1 = await d.create("user", "preferred_database", "PostgreSQL", request_id=uuid4())   # agent_inference/low
    e2 = await d.update(e1.fact_id, "MySQL", expected_event_id=e1.event_id, request_id=uuid4())
    e3 = await d.revert(e1.fact_id, e1.event_id, expected_event_id=e2.event_id, request_id=uuid4(), actor="host-llm")
    ev = await store._get_event(e3.event_id)
    assert ev.provenance == "agent_inference" and ev.trust_level == "low"
    assert ev.actor == "host-llm" and e3.previous_event_id == e2.event_id
    # legacy path is unchanged:
    legacy = await store.revert(e1.fact_id, e1.event_id)
    assert legacy.provenance == "human_review" and legacy.trust_level == "high"
```

- [x] Run the new file: 36 tests pass (acceptance cases 1–16 plus boundary checks). Full suite green. Lint. Commit:
  `feat: guarded create/update/revert with expected revisions and idempotency receipts`.

### Task 2.6 — Sync SDK wrappers

**Files:** `mnemo/__init__.py`.

- [x] Add `Mnemo.direct` returning thin sync wrappers (`create/update/revert/get/history/search_direct`)
  built on the same `_run` pattern; export `MutationResult`, `MnemoError`, `ErrorCode` in `__all__`.
- [x] `tests/test_sdk.py`: one test that runs create → update → revert and asserts a
  `REVISION_CONFLICT` on a stale update. Commit: `feat: sync SDK access to guarded mutations`.

**Phase 2 exit:** Part D cases 1–16 and 18 green; migration applies on a fresh DB and on a DB
migrated through 0009 with existing facts — add `tests/test_schema.py::test_0010_applies_over_populated_0009`:
copy `migrations/0001…0009` into `tmp_path`, `apply_migrations(conn, tmp_path)`, insert two facts via
`MnemoStore.add`, then `apply_migrations(conn, MIGRATIONS_DIR)` and assert only `0010` was applied and
the facts/HEADs are unchanged; legacy tests untouched.

---

## Phase 3 — Direct MCP profile (1–2 days)

Goal: `mnemo-mcp-direct` advertises exactly six tools, returns structured results and
structured errors, never starts background work, and is covered by a real stdio test.

### Task 3.1 — `mnemo/mcp_direct.py`

**Files:** create `mnemo/mcp_direct.py`; `pyproject.toml` `[project.scripts] mnemo-mcp-direct = "mnemo.mcp_direct:main"`; `Makefile` `mcp-direct`.

- [x] Server skeleton:

```python
"""Direct-profile MCP server: six guarded tools, no extraction, no decay.

Scope (namespace/user/agent) and actor come from trusted local configuration, never from
tool arguments. Errors are ToolError whose message is a JSON {"code","message","details"}.
"""
from __future__ import annotations
import json
from uuid import UUID
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.direct import DirectMemory
from mnemo.errors import ErrorCode, MnemoError

def _settings():
    s = get_settings()
    return s.model_copy(update={"worker_enabled": False, "search_reinforce": False})

# lifespan: asyncpg pool with register_vector + validate_embedding_dimension init;
# build_embedder(settings) only. Never import mnemo.extraction / mnemo.quality here.

async def _run(fn):
    try:
        async with _pool.acquire() as conn:
            s = _settings()
            return await fn(DirectMemory(MnemoStore(conn, _embedder, settings=s,
                              namespace=s.namespace, user_id=s.user_id, agent_id=s.agent_id)))
    except MnemoError as e:
        raise ToolError(json.dumps(e.to_dict())) from e
    except ValueError as e:                      # bad UUID strings etc.
        raise ToolError(json.dumps({"code": ErrorCode.INVALID_INPUT, "message": str(e), "details": {}})) from e

mcp = FastMCP("mnemo-direct", lifespan=lifespan)

@mcp.tool()
async def memory_create(subject: str, predicate: str, value: str, request_id: str) -> dict:
    """Create a NEW memory (subject, predicate, value). Fails with ALREADY_EXISTS if one exists —
    then use memory_update with its fact_id. request_id: a UUID you generate once per intended
    change and reuse only when retrying that same change."""
    r = await _run(lambda d: d.create(subject, predicate, value, request_id=UUID(request_id), actor=_settings().agent_id))
    return r.model_dump(mode="json")

@mcp.tool()
async def memory_get(fact_id: str, event_id: str | None = None) -> dict:
    """Current value of a memory (fact_id) with its current_event_id — pass that id as
    expected_event_id when you change it. With event_id: that exact historical value and
    whether it is restorable. NOT_FOUND if the memory is unknown."""
    r = await _run(lambda d: d.get(UUID(fact_id), UUID(event_id) if event_id else None))
    return r.model_dump(mode="json")

@mcp.tool()
async def memory_search(query: str, limit: int = 5) -> dict:
    """Find candidate memories for a query. Returns {"hits": [...]} with fact_id, event_id,
    subject, predicate, value, score. Similarity finds candidates; it never authorizes a merge
    or overwrite — confirm with memory_get before updating."""
    hits = await _run(lambda d: d.search(query, limit=limit))
    return {"hits": [h.model_dump(mode="json") for h in hits]}

@mcp.tool()
async def memory_update(fact_id: str, value: str, expected_event_id: str, request_id: str) -> dict:
    """Change an existing memory's value. expected_event_id must be the current_event_id you
    read; REVISION_CONFLICT means it changed — re-read and reconsider. Identical value returns
    status=no_change."""
    r = await _run(lambda d: d.update(UUID(fact_id), value, expected_event_id=UUID(expected_event_id),
                                      request_id=UUID(request_id), actor=_settings().agent_id))
    return r.model_dump(mode="json")

@mcp.tool()
async def memory_history(fact_id: str, cursor: str | None = None, limit: int = 20) -> dict:
    """Newest-first history of one memory: event_id, op, value_preview, provenance, actor,
    recorded_at, plus current_event_id and next_cursor. Use it to pick a revert target; never
    reconstruct an old value from memory."""
    page = await _run(lambda d: d.history(UUID(fact_id), cursor=cursor, limit=limit))
    return page.model_dump(mode="json")

@mcp.tool()
async def memory_revert(fact_id: str, to_event_id: str, expected_event_id: str, request_id: str) -> dict:
    """Restore a memory to an earlier event's exact value as a NEW revision (history is kept).
    Requires the current_event_id you read. Reverting to the current event is no_change.
    Undoing a memory's creation is not supported in this profile (UNSUPPORTED_OPERATION)."""
    r = await _run(lambda d: d.revert(UUID(fact_id), UUID(to_event_id), expected_event_id=UUID(expected_event_id),
                                      request_id=UUID(request_id), actor=_settings().agent_id))
    return r.model_dump(mode="json")

def main() -> None:
    mcp.run(transport="stdio")
```

  Tool descriptions carry the host instructions from the research (search/get before
  changing; use returned IDs; pass the revision you read as `expected_event_id`; on
  `REVISION_CONFLICT` re-read and reconsider; ask the user when several memories fit
  "undo that"; "undo the creation" is unsupported in this profile).
- [x] Unit test (`tests/test_mcp_direct_profile.py`, in-process): tool set ==
  `{"memory_create","memory_get","memory_search","memory_update","memory_history","memory_revert"}`;
  each has an `inputSchema` with the listed required fields; `mnemo.mcp_direct` does not
  import `mnemo.extraction` or `mnemo.quality` (`assert "mnemo.extraction" not in sys.modules`
  after `import mnemo.mcp_direct` in a subprocess).

### Task 3.2 — External stdio test for the direct profile

**Files:** create `tests/test_mcp_direct_profile_stdio.py` (same harness as Task 1.2, `python -m mnemo.mcp_direct`).

- [x] Success path: create → update (with `expected_event_id`) → history (page of 2,
  `current_event_id` == update) → revert (expected = update) → get == restored; `search`
  returns `event_id`.
- [x] Error paths (each asserts `result.isError` and `json.loads(text)["code"]`):
  create twice → `ALREADY_EXISTS`; update with stale expected → `REVISION_CONFLICT`;
  same `request_id` different value → `REQUEST_ID_REUSED`; identical retry → same
  `event_id`, `replayed: true`, and DB event count unchanged; revert to foreign event →
  `INVALID_RESTORE_TARGET`; `memory_get` unknown → `NOT_FOUND`; malformed UUID → `INVALID_INPUT`.
- [x] Restart the server mid-test and replay a request_id → still replayed (receipts durable).
- [x] Assert `extraction_job` and `quality_decision` are empty at the end.
- [x] Commit: `feat: direct-profile MCP server (mnemo-mcp-direct) with structured errors`.

### Task 3.3 — Wheel and CI

- [x] `scripts/check-wheel.sh`: add `mnemo-mcp-direct --help` (or a `--version` flag) to the
  console-script checks; assert `mnemo/migrations/0010_mutation_receipts.sql` is packaged.
- [x] Commit: `build: package the direct profile`.

**Phase 3 exit:** Part D case 17 green; both stdio tests green in CI; `make mcp-direct` runs
with `MNEMO_BACKEND=hash` and with `ollama`.

---

## Phase 4 — Safety net UI, demo, and the story (1–2 days)

### Task 4.1 — Guarded revert in the web UI (A3-10)

**Files:** `web/app.py:96-99`, `web/templates/fact.html`, `tests/test_web.py`.

- [x] Revert form posts a hidden `expected_event_id` (the HEAD rendered on the page) and a
  `request_id` (uuid4 rendered per row); handler calls `DirectMemory.revert`; on
  `REVISION_CONFLICT` re-render the fact page with a banner "This memory changed since you
  loaded the page — review and try again" (HTTP 409). Keep the `/edit` form but label it
  "manual correction" and set `provenance="human_review"` only there (it is a human form).
- [x] Tests: happy path unchanged; stale form → 409 and no new event.
- [x] Commit: `feat(web): revert requires the revision the reviewer saw`.

### Task 4.2 — README rewrite (D7)

**Files:** `README.md`.

Structure (write it, do not leave as an outline):

1. One-liner: *Versioned, auditable memory for AI agents on plain Postgres — every change
   is an immutable event, every belief has provenance, every mistake can be reverted with a
   guard against concurrent changes. Exposed over MCP.*
2. 60-second demo: `make install && make up && make migrate && make demo-direct` (no models), then
   the MCP config snippet for Claude Desktop/Cursor pointing at `mnemo-mcp-direct` with
   `MNEMO_BACKEND=hash` or `ollama`.
3. What makes it different (with pointers to the code): DB-enforced immutability trigger,
   scoped locking + expected-revision guards, idempotency receipts, bitemporal reads
   (`as_of`/`valid_at`), reversible archive, `blame`/`diff`/`commit`.
4. The six direct tools and the error codes (table).
5. Optional: the extraction quality pipeline — what it does, how to turn it on
   (`make mcp` / `make worker` with Ollama), `make eval` and exactly what the 18-turn number
   is and is not, and one honest paragraph linking to `docs/quality-v*/README.md`:
   "with 3–4B local models, real-conversation precision/recall is low (numbers); the harness
   is the point — it is built so a stronger model can be measured fairly."
6. Architecture diagram (existing spec §2 picture, updated with the direct profile).
7. Tests/CI badge, layout, license, LongMemEval attribution.
8. Non-goals and known limits (single-tenant local scope; hash embeddings are not
   semantic; extraction pipeline experimental; no branching/merge).

- [x] README rewrite shipped in `docs: explain guarded memory and make the quick start reliable`.

### Task 4.3 — Spec and status

**Files:** `PROJECT_SPEC.md`, `PROJECT_STATUS.md`.

- [x] Spec §0: soften "incumbents… literally cannot" → "most incumbents mutate in place;
  Timescale's Memory Engine also versions via triggers — Mnemo's difference is the
  DB-enforced append-only log plus expected-revision guards and receipts." Add **§15 Direct
  memory contract (Sept 2026)** summarizing Phase 2/3 semantics (copy the rules from Task
  2.5, the error table from D3, receipt retention = lifetime of the store).
- [x] Status: replace the "Current milestone" narrative with: what shipped in Phases 0–4,
  the verified test count, and a short "Extraction quality: unchanged since v7; see
  `docs/quality-v7/README.md`". Move the long cycle history under a "History" heading.
- [x] Spec/status shipped in `docs: record the guarded web contract and Phase 4 verification`.

**Phase 4 exit:** a reviewer can understand the project from the README in two minutes and
run the demo in five; nothing on the front page is false.

---

## Phase 5 — Optional: scripted agent demo with a real host model

Only if you want the "an LLM actually uses it" evidence. No paid calls without a cap; Ollama
is fine.

**Files:** create `examples/agent_undo.py`, `docs/direct-pilot/README.md` + transcripts.

- [x] Minimal tool-calling loop (Ollama `/api/chat` with tools, or OpenAI) wired to the
  six direct tools via the in-process `DirectMemory`. Six fixed scenarios:
  (1) "remember PostgreSQL" → create; (2) "change it to MySQL" → update with expected id;
  (3) "undo that" → history + revert; (4) two memories then "undo the database one" →
  correct target; (5) ambiguous "undo that" after two changes → asks, no mutation;
  (6) inject a `REVISION_CONFLICT` on first attempt → model re-reads and retries or asks.
- [x] Record: model name, transcript, tool calls, unintended mutations (must be 0), wall time.
  Report the denominators exactly (6 scenarios, 1 run each). Do not blend with the
  extraction eval numbers.
- [x] Record the frozen result: **2/6**, `qwen3.5:4b-mlx`, zero unintended
  mutations, 158.28 seconds. Four protocol failures remain visible in the
  [run review and transcript](direct-pilot/run-2026-09-22/README.md).

---

## Phase 6 — Deferred (do not start without a concrete need)

- **Pilot B — member/occurrence identity** (`identity_mode`, `identity_ref`): follow
  `docs/quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md`. Requires its own rule-6 decision and a
  reader/writer co-deploy. Trigger: a user story that genuinely needs two facts under one
  subject/predicate.
- [x] **Export/import**: versioned JSON of facts, events, HEADs, receipts, embedding model/dim;
  restore only into an empty destination first. Shipped as `mnemo/transfer.py`
  (`mnemo-transfer`, `make export` / `make import`), with commits included (events
  reference them) and pipeline tables excluded. Contract in spec §16; tests in
  `tests/test_transfer.py`.
- **Grouped undo / archive-as-undo**: needs all affected expected revisions in one transaction.
- **Consolidation (Layer 4)**: stays off; `mem_provenance` has no `agent_reflection` member —
  adding it is a schema decision that must come with source lineage + NLI gating + trust cap.
- **Extraction quality**: only with a stronger model and a new frozen protocol; do not
  reopen `b46e15ed`; do not tune thresholds on holdout output.

---

# Part D — Acceptance matrix (deterministic; all must pass)

Test file: `tests/test_direct_mutations.py` unless noted. Real Postgres; two connections where stated.

| # | Case | Test function |
|---|---|---|
| 1 | create → update → revert preserves exact strings; E1/E2 rows byte-identical after | `test_01_lifecycle_exact_strings_and_immutable_history` |
| 2 | revert → revert restores the displaced revision; other facts untouched | `test_02_revert_of_revert_targets_displaced_head` |
| 3 | identical update / revert-to-current → `no_change`, no new event, receipt written | `test_03_no_change_is_explicit_and_receipted` |
| 4 | "Postgres" after "PostgreSQL" is a real revision in the direct profile (legacy alias test still passes) | `test_04_spelling_change_is_a_revision_here_not_in_legacy_add` |
| 5 | two connections, same expected HEAD: one applied, one `REVISION_CONFLICT`, 2 events total | `test_05_two_writers_same_expected_head_one_conflicts` (two conns) |
| 6 | A→B→A: expected=E1 still rejected | `test_06_a_b_a_still_rejects_stale_expected_event` |
| 7 | immediate duplicate request_id → same result, `replayed=True`, no new event | `test_07_duplicate_request_replays_receipt` |
| 8 | lost-response retry after a later update → replay of the original result; later HEAD intact | `test_08_replay_after_later_update_leaves_head_intact` |
| 9 | same request_id, different payload/operation → `REQUEST_ID_REUSED`, nothing written | `test_09_request_id_reuse_with_different_payload_fails` |
| 10 | failure between event insert and receipt insert → full rollback | `test_10_failure_before_receipt_rolls_back_event_and_head` |
| 11 | receipts survive a new connection (two conns) | `test_11_receipts_persist_across_connections` |
| 12 | foreign fact / foreign event → `NOT_FOUND` / `INVALID_RESTORE_TARGET`; message has no IDs; nothing written | `test_12_foreign_ids_disclose_nothing_and_write_nothing` |
| 13 | archived (ephemeral), invalidated, session, expired targets/current → `UNSUPPORTED_STATE` | `test_13_unsupported_states_cannot_be_revived` |
| 14 | direct create/update = `agent_inference`/low; direct revert preserves source trust, records actor; legacy revert unchanged | `test_14_direct_revert_preserves_source_trust_and_records_actor` |
| 15 | after revert, `get` and `search` show only the restored value | `test_15_current_reads_exclude_superseded_after_revert` |
| 16 | history pagination under concurrent writes: no duplicates/skips of pre-existing rows | `test_16_history_pages_are_stable_under_concurrent_writes` (two conns) |
| 17 | direct server never constructs extractor/verifier or decay; only six tools | `tests/test_mcp_direct_profile_stdio.py::test_direct_profile_lifecycle_and_errors` |
| 18 | full legacy suite unchanged | `make test-db` (all pre-existing tests) |

Plus Phase 0/1: `test_connect_and_migrate_on_database_without_vector_extension`,
`test_hash_embedder_*`, `test_external_lifecycle_postgres_mysql_postgres`, `test_direct_demo_runs`.

---

# Part E — Do not

- Do not `UPDATE`/`DELETE` `memory_event` or receipt rows (triggers will stop you; don't work around them).
- Do not route direct-profile `update` through `add()` (alias no-op would silently swallow revisions).
- Do not let tool arguments choose `namespace`/`user_id`/`agent_id`, `provenance`, or `trust_level`.
- Do not promote a revert to `human_review` unless a human did it (the web `/edit` form is the only such path).
- Do not present `make eval` (18-turn synthetic label replay) as a LongMemEval result, or hash-embedding search as semantic search.
- Do not reopen `b46e15ed`, tune gate thresholds, or re-run extraction probes as part of this plan.
- Do not add dependencies. Everything above uses `asyncpg`, `pydantic`, `mcp`, `fastapi` already present.
- Do not bundle unrelated changes into a milestone commit.

---

# Appendix — Prompt for the executing agent

```text
Read PROJECT_SPEC.md, then AGENTS.md, then docs/MASTER_PLAN.md in full.

Start with Phase 0 (repo hygiene) and stop at the end of each phase for review.
For every task: write the failing test first, run it, implement, run the focused test,
run `make lint` and `MNEMO_REQUIRE_DB=1 uv run pytest -q`, then commit with the message
given in the plan (dates per AGENTS.md until D8 is resolved).

Decisions D1–D7 and D9 are approved as recommended unless the user says otherwise; D8 is
the user's call — ask once, then proceed.

Do not change legacy add()/revert()/search() semantics; the direct profile is additive.
Report at each phase end: what changed, what ran (exact commands and counts), anything red,
and the next task. Never describe the hash embedder as semantic or the 18-turn eval as a
real-conversation result.
```
