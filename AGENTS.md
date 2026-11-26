# AGENTS.md — Operating Guide for Mnemo

**Read `PROJECT_SPEC.md` first — it is the single source of truth.** This file is how to work day-to-day; if anything conflicts, the spec wins. `MODEL_COUNCIL_solution.md` holds the design rationale + citations. `PROJECT_STATUS.md` tracks progress (spec = target, status = progress).

---

## What this is
Mnemo — **one shared, versioned memory for coding agents on the local machine.** Claude Code, Codex, OpenCode and other MCP clients read and write the same store through seven guarded tools (`mnemo-mcp-direct`); the agent decides what to remember and says where each value came from. The store is **append-only, versioned and bitemporal**, so every change is attributed, visible in history and reversible: `history`, `revert` and `retract` are the defence against wrong or poisoned memories. The automatic extraction pipeline is archived at the git tag `archive/extraction-pipeline` (spec §23).

---

## Golden rules
1. **The spec is the map.** `PROJECT_SPEC.md` §0 and §14–§23 are current; sections marked *(archived)* describe the archived pipeline.
2. **Measure against agents.** The agent eval (`scripts/agent_eval.py`, `docs/agent-eval/`) and the retrieval set (`tests/test_retrieval_quality.py`) are the yardsticks. Change ranking, search floors or tool text only with a before/after run.
3. **Append-only is sacred.** Never `UPDATE`/`DELETE` a `memory_event` payload. State changes are new events; supersession is a flag; revert copies an old revision forward.
4. **Ask before changing contracts.** Confirm before adding a dependency, changing the §3 schema, or altering §15 tool semantics. Small reviewable diffs, one commit per milestone.
5. **The SQL is the product.** Keep the data model legible (raw SQL migrations + thin wrappers), not buried in an ORM.

---

## Commands (Makefile targets)
```bash
make migrate   # create the store file if needed and apply migrations/*.sql (MNEMO_DB)
make test      # pytest; every test gets its own temporary store file
make reindex   # rebuild vectors for the configured model and the word index
make lint / make fmt
make mcp       # the seven guarded tools over MCP (stdio)
make demo      # direct write -> guarded update -> revert, no models
make ui        # FastAPI + HTMX web UI
make agents    # install the tool from this checkout and connect the coding agents
```
`make agents` and `mnemo install` rewrite real agent configs: verify the installer with a temporary `HOME` (Claude Code's `claude mcp add` writes `~/.claude.json`), temporary config dirs and a scratch `MNEMO_DB`.

---

## Conventions
- Python 3.12, full type hints. ruff + black. Pydantic v2 + `pydantic-settings` (`.env`).
- One SQLite file (`mnemo/sqlite.py` wraps `sqlite3` with asyncpg's call shape; `?n` placeholders); schema in numbered `migrations/000N_*.sql`. Core as-of/diff/HEAD queries stay readable SQL. Timestamps are fixed-width UTC text; `now()` is the transaction's start.
- Async core, thin sync wrappers. Centralize config in `mnemo/config.py`. Vectors live in `memory_embedding`, tagged with the model that made them (768 Ollama / 384 local / 1536 OpenAI); search uses only the configured model's, and `reindex` rebuilds them.
- `Embedder` is pluggable (hash, Ollama, local, OpenAI), so tests run on the deterministic hash backend.

---

## Git dates
Commit at the **real current date and time**: no backdating, and no `GIT_AUTHOR_DATE` or
`GIT_COMMITTER_DATE` overrides. GitHub sets release dates when a release is published,
and they cannot be backdated, so commit dates have to be real to match them. History
pushed before v0.1.7 keeps its earlier dates and is not rewritten.

---

## Tests that must exist and pass
1. **Fact lifecycle** — `ADD→UPDATE→REVERT`; HEAD correct; exactly the right events in history; earlier events byte-for-byte unchanged after revert.
2. **Guarded revisions** — a stale `expected_event_id` is rejected (including A→B→A); a retried `request_id` replays its receipt instead of writing again.
3. **Immutable payloads** — database triggers reject changes to and deletion of event payloads.
4. **Retrieval** — the retrieval set meets its bar on each live backend.

If any is red, a central promise isn't real yet.

---

## Definition of done
A new user installs the tool and runs `mnemo install`; Claude Code, Codex and OpenCode then share one memory through the seven tools. `make test`, `make lint` and `scripts/check-wheel.sh` pass, and the agent eval passes its scenarios.
