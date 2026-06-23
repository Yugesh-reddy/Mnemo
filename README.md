# Mnemo

**Versioned, auditable agent memory on plain Postgres — with a write-quality pipeline and guarded undo.**

Mnemo combines quality gating, provenance, and reversible history. Six direct MCP
tools let an agent store facts, inspect revisions, and restore earlier values
with protection against concurrent changes. Automatic extraction can run behind
an immediate memory cache. Both paths use the same append-only event store.
Python SDK and a small web review UI are included.

[![CI disabled](https://img.shields.io/badge/CI-disabled-lightgrey)](.github/workflows/correctness.yml)
[![License MIT](https://img.shields.io/badge/license-MIT-blue)](pyproject.toml)

## Try it without a model

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and Docker. The supplied
Compose service runs Postgres 16 with pgvector >= 0.8.

```bash
git clone https://github.com/Yugesh-reddy/Mnemo.git
cd Mnemo
make install && make up && make migrate && make demo-direct
```

The scripted demo remembers PostgreSQL, changes it to MySQL, rejects a stale
update, inspects history, and restores PostgreSQL. Retrying the same restore
returns its receipt, leaving exactly three revisions:

```text
ADD(PostgreSQL) -> UPDATE(MySQL) -> REVERT(PostgreSQL)
Source trust preserved: agent_inference / low
```

Each run uses a fresh namespace and prints revision IDs. No host LLM, extraction,
or decay runs. The `hash` backend produces deterministic **non-semantic** vectors;
use keyword queries for this demo. Use a separate database when switching between
hash and semantic embeddings, even if their dimensions match.

The optional [host-agent pilot](docs/direct-pilot/README.md) lets Qwen or Azure
Luna choose the tool calls, one attempt per scenario. Both models finished with
the intended end state in 5/6 scenarios (Qwen's sixth ran out of budget without
writing or replying; Luna's wrote twice) and neither altered an existing event; the
strict read-before-write protocol was followed 2/6 (Qwen) and 4/6 (Luna) times.
Luna's one bad outcome shows where the guards stop: on an ambiguous "undo that"
it reverted both memories instead of asking. `expected_event_id` blocks stale and
racing writes; it cannot know whether a write was wanted. What it guarantees is
the blast radius: two events, attributed to the agent's actor and request IDs,
visible in `memory_history`, reversible with two `memory_revert` calls. These
runs measure host behavior; they do not establish general agent reliability.

## Connect an MCP client

After installation and migration, use this local-server configuration for
[Claude Desktop](https://modelcontextprotocol.io/docs/develop/connect-local-servers).
Replace the executable path with your checkout's absolute path. For
[Cursor](https://cursor.com/docs/mcp), put it in `.cursor/mcp.json` or
`~/.cursor/mcp.json` and add `"type": "stdio"` inside the `mnemo` entry.

```json
{
  "mcpServers": {
    "mnemo": {
      "command": "/absolute/path/to/Mnemo/.venv/bin/mnemo-mcp-direct",
      "env": {
        "MNEMO_DSN": "postgresql://mnemo:mnemo@localhost:5432/mnemo",
        "MNEMO_BACKEND": "hash",
        "MNEMO_EMBED_DIM": "768",
        "MNEMO_WORKER_ENABLED": "false",
        "MNEMO_NAMESPACE": "default"
      }
    }
  }
}
```

For semantic embeddings, install `nomic-embed-text` with Ollama and change
`MNEMO_BACKEND` to `ollama`, using a database with matching embeddings. The direct
server only needs an embedder. `make mcp-direct` runs the same stdio server from
the checkout. Configuration comes from `.env` / `MNEMO_*`; see [.env.example](.env.example).

| Tool | Contract |
|---|---|
| `memory_create` | Create a new subject/predicate identity; existing identity returns `ALREADY_EXISTS`. |
| `memory_get` | Read current value and event ID, or a full historical value and restore eligibility. |
| `memory_search` | Find candidates with fact/event IDs; no reinforcement or raw-cache merge. |
| `memory_update` | Update by fact ID using the expected event ID and a request UUID. |
| `memory_history` | Page through revisions, newest first, with value previews and a continuation cursor. |
| `memory_revert` | Copy a historical value into a new revision, preserving its provenance and trust. |

Read before changing memory. Use returned IDs, never a reconstructed old value.
On a conflict, read current state and reconsider. If “undo that” could mean several
changes, ask which one. Generate one request UUID per intended mutation and reuse
it only with the exact same parameters. A replay describes the original mutation;
read again to learn the current value. Exact-value updates return `no_change`.

Errors are JSON `{code, message, details}` with MCP's `isError` flag set:

| Code | What to do |
|---|---|
| `INVALID_INPUT` | Correct the input, UUID, value size, limit, or cursor. |
| `NOT_FOUND` | Check the memory ID and configured scope. |
| `ALREADY_EXISTS` | Read the returned fact ID, then use a guarded update. |
| `REVISION_CONFLICT` | Re-read and reconsider; a new mutation needs a new request ID. |
| `INVALID_RESTORE_TARGET` | Select a revision belonging to this memory. |
| `UNSUPPORTED_STATE` | This current state or historical revision cannot be restored here. |
| `REQUEST_ID_REUSED` | An ID was reused with different parameters. Retry the original or start a new request. |
| `UNSUPPORTED_OPERATION` | Creation undo is not supported by the direct API. |

## What the store guarantees

| Mechanism | Where it lives |
|---|---|
| Database triggers reject changes to event payloads; supersession and recall bookkeeping remain mutable. | [Integrity migration](migrations/0005_integrity.sql) |
| Scope and fact locks serialize writes; expected-event guards reject stale direct updates/reverts, including A→B→A. | [Direct API](mnemo/direct.py) |
| Event, HEAD, and append-only receipt commit together. Receipts survive restarts and remain for the store's lifetime. | [Receipt migration](migrations/0010_mutation_receipts.sql) |
| Direct revert copies source payload, embedding and lineage into a new event; it does not raise source trust. | [Core](mnemo/core.py), [direct contract](PROJECT_SPEC.md#15-additive-guarded-memory-contract--september-22-2026) |
| Historical reads distinguish recording time (`as_of`) from world validity (`valid_at`). Archive is reversible; `blame`, `diff`, and `commit` expose history. | [Temporal contract](PROJECT_SPEC.md#14-correctness-amendment--september-9-2026) |

The [Python guide](docs/DIRECT_SDK.md) shows `Mnemo.direct` and the async
`DirectMemory(store)` interface. Existing `add`, `search`, `blame`, `revert`,
`diff`, `log`, `commit`, and `observe` APIs remain available with their legacy
contracts. Legacy `add` performs alias deduplication; direct updates preserve
exact spelling changes. Legacy human-review revert is not revision-guarded.

### Back up or move a store

```bash
make export OUT=mnemo-export.json      # configured scope; stdout without OUT
uv run mnemo-transfer export --all-scopes --out all.json
make import FILE=mnemo-export.json     # into an empty, migrated store only
```

The export is one versioned JSON document: facts with their HEADs, every event,
commits, receipts, the applied migrations and the embedding backend/model/dim.
Import checks the schema and embedding configuration, then restores the rows in
one transaction. It re-exports what it wrote and rolls back if anything differs,
so IDs, sequence numbers, history and receipt replays come through unchanged.
Extraction-pipeline working tables are not exported ([spec §16](PROJECT_SPEC.md#16-exportimport--september-22-2026)).

## Review memory in the browser

```bash
MNEMO_BACKEND=hash MNEMO_WORKER_ENABLED=false make ui
# http://127.0.0.1:8000
```

Set `MNEMO_NAMESPACE` to the namespace printed by the demo to inspect its records.
Match any configured user/agent scope too. Search → open history → revert a
revision. A stale form returns HTTP 409, shows the latest state, and writes
nothing. Repeated submissions replay their receipt. Unrestorable revisions have
no restore button. **Manual correction** records a human-reviewed value through
the legacy write path; it does not have a revision guard.

## Automatic extraction and evaluation

`observe` can populate an immediate cache and queue extraction. The worker checks
source evidence, deduplicates, scores and tiers candidates, then records each
quality decision. `make mcp` starts the legacy tools and worker; `make worker`
runs a separate consumer. Use Ollama/OpenAI configuration, not `hash`, for this
path. [Setup and detailed measurements](docs/EXTRACTION.md) cover the heuristic,
optional NLI, and model verifiers. Consolidation remains disabled.

`make eval` is an **18-turn scripted regression**, currently gated precision/recall
**90.9% / 100%**, must-keep recall **100%**, and zero forbidden writes. It replays
labeled candidates; it is not real-conversation extraction accuracy, a LongMemEval
retrieval result, or a competitor comparison.

Small local models have not met the memory-quality target. The recorded 200-turn
authored development run achieved **1.41% strict precision / 2.41% recall**; the
48-turn external holdout had **0/9 exact must-keep matches**. These are different
datasets and measures, with provisional labels and recorded extraction failures.
The later bounded development baseline retrieved **16/23** complete distinct
targets; the v7 experiment fell to **15/23** and was rejected. Extraction is
unchanged since v7. See [v4](docs/quality-v4/README.md), [v7](docs/quality-v7/README.md),
and [current status](PROJECT_STATUS.md). The harness preserves evidence so future
models can be measured; these results do not establish general reliability.

## Architecture

```mermaid
flowchart LR
    Host[Host agent] --> Direct[Six direct MCP tools / Python SDK]
    UI[Web revert] --> Direct
    Direct --> Core[Scoped event store]
    Turns[Optional observations] --> Cache[Fast cache + extraction queue]
    Cache --> Worker[Evidence verification / scoring / tiering]
    Worker --> Core
    Core --> Events[Append-only events]
    Core --> Heads[Current HEADs]
    Direct --> Receipts[Durable mutation receipts]
    Events --> History[History / as-of reads / diff]
```

## Checks, layout and limits

```bash
make test-db   # requires Postgres; prints test and skip counts
make lint
uv build
bash scripts/check-wheel.sh
```

The packaging check uses a fresh environment and exercises installed SDK/MCP
behavior. The [correctness workflow](.github/workflows/correctness.yml) is retained;
**GitHub Actions is disabled at the owner's request**. No hosted green-CI claim
is made. Optional live-Ollama tests skip when their models are unavailable.

`mnemo/` contains the async store, direct API, SDK, MCP servers, and quality
pipeline; `migrations/` holds the SQL; `web/`, `examples/`, and `tests/`
hold the UI, demos, and checks. [The spec](PROJECT_SPEC.md) defines contracts;
[the plan](docs/MASTER_PLAN.md) and [status](PROJECT_STATUS.md) track delivery.

This is a local, single-tenant service. Scope columns do not provide authentication
or tenant access control. Identity is one current value per subject/predicate in
a scope. Direct mutations accept strings up to 8192 UTF-8 bytes and operate on
active, durable, non-expiring memories. Creation undo, grouped undo, branching,
merging, hosted billing, and multi-tenant authorization are out of scope. Hash
vectors do not measure semantic relevance; real-model extraction is experimental.

The project declares the **MIT license** in [pyproject.toml](pyproject.toml).
Bundled LongMemEval material comes from
[xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval) and its
[cleaned dataset](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned),
with the upstream [MIT license](mnemo/data/LongMemEval-LICENSE.txt) preserved.
Mnemo's atomic labels are provisional; see [data attribution](mnemo/data/README.md).
