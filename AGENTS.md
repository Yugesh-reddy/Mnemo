# AGENTS.md — Operating Guide for Mnemo

**Read `PROJECT_SPEC.md` first — it is the single source of truth.** This file is how to work day-to-day; if anything conflicts, the spec wins. `MODEL_COUNCIL_solution.md` holds the design rationale + citations. `PROJECT_STATUS.md` tracks progress (spec = target, status = progress).

---

## What this is
Mnemo — **one shared, versioned memory for coding agents on the local machine.** Claude Code, Codex and other MCP clients read and write the same store through six guarded tools (`mnemo-mcp-direct`); the agent decides what to remember. The store is **append-only, versioned and bitemporal**, so every change is attributed, visible in history and reversible: `history`/`revert` are the defence against wrong or poisoned memories. The automatic extraction pipeline is archived at the git tag `archive/extraction-pipeline` (spec §23).

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
make up        # docker compose up -d  (Postgres + pgvector)
make migrate   # apply migrations/*.sql
make test-db   # pytest against a disposable database (fails if Postgres is down)
make lint / make fmt
make mcp       # the six guarded tools over MCP (stdio)
make demo      # direct write -> guarded update -> revert, no models
make ui        # FastAPI + HTMX web UI
make agents    # install the tool from this checkout and connect Claude Code + Codex
```
`make agents` and `mnemo install` rewrite real agent configs: verify the installer with temporary homes and a scratch database.

---

## Conventions
- Python 3.12, full type hints. ruff + black. Pydantic v2 + `pydantic-settings` (`.env`).
- asyncpg; SQL in numbered `migrations/000N_*.sql`. Core as-of/diff/HEAD queries stay readable SQL.
- Async core, thin sync wrappers. Centralize config incl. **embedding dimension** (768 Ollama / 384 local / 1536 OpenAI; one dimension per database).
- `Embedder` is pluggable (hash, Ollama, local, OpenAI), so tests run on the deterministic hash backend.

---

## Git dates
When committing, **do not use today's real date.** Continue the repo's existing timeline from `HEAD`:
- Read the last commit's author date first (`git log -1 --format='%ad' --date=iso-strict`).
- Count how many commits already sit on that calendar day (`git log --since='YYYY-MM-DD 00:00:00' --until='YYYY-MM-DD 23:59:59' --format='%H'`).
- **Same working day** as that commit → same calendar day, later timestamp than `HEAD` — **unless that day has already hit its cap.**
- **Cap: 7 commits per day** by default. Once a day hits its cap, further commits shift to the next calendar day. No skipped empty days.
- **No two 7-commit days in a row.** If the previous calendar day already has 7 commits, the next day's cap is a **random integer from 3 to 6** (sample 3–7, then reroll any 7). Do not fill that following day to 7.
- **New working day** → the next calendar day after `HEAD`, using the cap above. No skipped days.
- Commits that belong together stay on one day until the cap; each new work day is consecutive.
- Set both `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE`.

---

## Tests that must exist and pass
1. **Fact lifecycle** — `ADD→UPDATE→REVERT`; HEAD correct; exactly the right events in history; earlier events byte-for-byte unchanged after revert.
2. **Guarded revisions** — a stale `expected_event_id` is rejected (including A→B→A); a retried `request_id` replays its receipt instead of writing again.
3. **Immutable payloads** — database triggers reject changes to and deletion of event payloads.
4. **Retrieval** — the retrieval set meets its bar on each live backend.

If any is red, a central promise isn't real yet.

---

## Definition of done
A new user installs the tool and runs `mnemo install`; Claude Code and Codex then share one memory through the six tools. `make test-db`, `make lint` and `scripts/check-wheel.sh` pass, and the agent eval passes its scenarios.
