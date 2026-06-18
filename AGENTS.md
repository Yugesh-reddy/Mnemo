# AGENTS.md — Operating Guide for Mnemo

**Read `PROJECT_SPEC.md` first — it is the single source of truth.** This file is how to work day-to-day; if anything conflicts, the spec wins. `MODEL_COUNCIL_solution.md` holds the design rationale + citations. `PROJECT_STATUS.md` tracks progress (spec = target, status = progress).

---

## What this is
Mnemo — **agent memory that stores less and remembers what matters.** A write-quality pipeline (salience scoring → NLI verification → tiering → dedup → decay) on an **append-only, versioned, bitemporal store**, so every keep/demote/drop/forget is auditable and reversible. `blame`/`revert` are the safety net. MCP server + Python SDK; self-hostable on plain Postgres.

**One product, not two.** The quality pipeline is the product; the versioned event store is its foundation; `blame`/`revert` is the safety net. Don't treat "versioning" and "quality" as a fork — they're one thing.

---

## Golden rules
1. **The spec is the map.** Follow `PROJECT_SPEC.md` §11 order: foundation → **eval harness** → extraction+Layers 0–3 → decay+tiering → consolidation **last**.
2. **Eval-first.** The write-score weights/thresholds/decay constants are unvalidated priors. Build/extend the precision-recall eval (`mnemo/eval.py`) and a baseline **before** tuning the gate. The number is the north star.
3. **Append-only is sacred.** Never `UPDATE`/`DELETE` a `memory_event` payload. State changes are new events; supersession is a flag; "forgetting" = reversible archive, not delete.
4. **Demote, don't drop.** Borderline facts → session/low-trust/TTL, never deleted. Two failure modes exist (stores junk AND misses critical); a blunt threshold worsens the second.
5. **Consolidation must not launder trust.** Synthesized facts: `provenance='agent_reflection'`, **medium** trust, all source `event_id`s in `source_span`, NLI-gated against all sources.
6. **Ask before changing contracts.** Confirm before adding a dependency, changing the §3 schema, or altering §4/§5 semantics. Small reviewable diffs, one commit per milestone.
7. **The SQL is the product.** Keep the data model legible (raw SQL migrations + thin wrappers), not buried in an ORM.

---

## Commands (Makefile targets)
```bash
make up        # docker compose up -d  (Postgres + pgvector)
make migrate   # apply migrations/*.sql
make test      # pytest (disposable test DB)
make eval      # precision/recall junk-rate eval — the north star
make lint / make fmt
make mcp       # MCP server (stdio)
make demo      # end-to-end: gated write path + a blame->revert rollback
make ui        # FastAPI + HTMX web UI
```
The two commands a new user needs: `make eval` (the pitch) and `make demo`.

---

## Conventions
- Python 3.12, full type hints. ruff + black. Pydantic v2 + `pydantic-settings` (`.env`).
- asyncpg; SQL in numbered `migrations/000N_*.sql`. Core as-of/diff/HEAD queries stay readable SQL.
- Async core, thin sync wrappers. Centralize config incl. **embedding dimension** (1536 OpenAI / 768 Ollama — don't mix).
- `Embedder`, `Extractor`, `Verifier` are pluggable abstractions (mock + real backends), so the eval can swap a real LLM without touching the gate.
- Worker: validate-and-retry extractor output (max 2), drop malformed/low-confidence; treat the extractor as an **untrusted actor** (`agent_inference`, low trust).

---

## Git dates
When committing, **do not use today's real date.** Continue the repo's existing timeline from `HEAD`:
- Read the last commit's author date first (`git log -1 --format='%ad' --date=iso-strict`).
- Count how many commits already sit on that calendar day (`git log --since='YYYY-MM-DD 00:00:00' --until='YYYY-MM-DD 23:59:59' --format='%H'`).
- **Same working day** as that commit → same calendar day, later timestamp than `HEAD` — **unless that day already has 7 commits.**
- **Cap: 7 commits per day.** The 8th commit (and anything after) shifts to the next calendar day. If that next day also hits 7, keep rolling forward one day at a time. No skipped empty days.
- **New working day** → the next calendar day after `HEAD`. No skipped days.
- Commits that belong together stay on one day until the cap; each new work day is consecutive.
- Set both `GIT_AUTHOR_DATE` and `GIT_COMMITTER_DATE`.

---

## Repo layout (target)
```
mnemo/  __init__.py · engine.py(or store.py) · quality.py · decay.py · extraction.py
        embedder.py · models.py · mcp_server.py · eval.py · config.py
migrations/  0001_init.sql ...
web/         FastAPI + HTMX UI
examples/    agent.py · demo.py
tests/       test_lifecycle.py · test_diff.py · test_twotier.py · test_quality.py
docker-compose.yml · pyproject.toml · Makefile
PROJECT_SPEC.md · AGENTS.md · MODEL_COUNCIL_solution.md · PROJECT_STATUS.md · README.md
```

---

## Tests that must exist and pass
1. **Fact lifecycle** — `ADD→UPDATE→REVERT`; HEAD correct; exactly the right events in `log`; the first two events byte-for-byte unchanged after revert (history immutable).
2. **Two-tier handshake** — fact available before extraction; appears **exactly once** after reconcile (no double-count).
3. **Quality gate** — negations + hypotheticals rejected (no false memories); repeated facts deduped; borderline fact demoted, not dropped (recall protected).
4. **Eval** — gated precision > naive; gated false-count = 0 while naive > 0; recall of must-keep facts stays 100%.

If any is red, a central promise isn't real yet.

---

## Reliability fixes in flight
R1 orphaned-job recovery (worker) · R2 MCP connection pool · R3 concurrent-`add` race (`add()`/`_insert_fact`). Each = focused diff + test + commit; fold into whichever milestone touches the same code.

---

## Definition of done (MVP)
`docker compose up` + `make eval` + `make demo` run; the four tests above pass; MCP server callable from an external client; web UI does search→blame→revert. Build the smallest thing that makes the two demos real; tune everything against the eval; ship consolidation last.
