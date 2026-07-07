# Mnemo

**One shared, versioned memory for your coding agents.** Claude Code, Codex and other
MCP clients on your machine read and write the same memory, for each project and
across projects. Every change is kept, attributed to the agent that made it, and can
be undone. Everything runs locally.

[![CI disabled](https://img.shields.io/badge/CI-disabled-lightgrey)](.github/workflows/correctness.yml)
[![License MIT](https://img.shields.io/badge/license-MIT-blue)](pyproject.toml)

## Quick start

You need macOS or Linux with Python 3.12 and [uv](https://docs.astral.sh/uv/),
[Docker](https://www.docker.com/) running, and [Ollama](https://ollama.com/) running.

```bash
git clone https://github.com/Yugesh-reddy/Mnemo.git && cd Mnemo
make install   # Python environment
make up        # Postgres + pgvector in Docker, reachable from this machine only
make agents    # connect Claude Code and Codex (shows every change, asks first)
```

`make agents` checks that Postgres and Ollama are running, downloads the
`nomic-embed-text` embedding model if it is missing, sets up the database, registers
Mnemo with Claude Code and Codex, and adds a short
[memory policy](mnemo/memory_policy.md) to their global instructions. Then open a
new session in any repository:

> **You, in Claude Code:** Heads-up: in this repo we run tests with `make test-db`.
>
> **You, later, in Codex (same repo):** How do I run the tests here?
> **Codex:** `make test-db`

`uv run mnemo-install --uninstall` removes exactly what `make agents` added; your
memories stay in the database. If port 5432 is taken on your machine, run
`echo MNEMO_DB_PORT=5433 >> .env` before `make up`.

## What you get

- **Shared across agents.** Every agent reads the same memory. Each one writes under
  its own name (`claude-code`, `codex`), so history shows who changed what.
- **Project and global scopes.** Memories belong to the git repository the agent is
  working in (SSH and HTTPS clones share them) and stay out of other repositories.
  Things you say apply everywhere, like "always use uv", go to the global scope.
- **History and undo.** A change never overwrites the past: every revision is kept,
  `memory_history` shows it, and `memory_revert` restores an earlier value as a new
  revision. Stale writes are rejected, and retrying a change doesn't apply it twice.
- **Clear failures.** If Postgres, Ollama or the embedding model is missing, the
  agent gets an error that names the command that fixes it. The server keeps
  running and reconnects when Postgres comes back.
- **Local and private.** Memories stay in your Postgres, embeddings come from your
  Ollama, and nothing is sent anywhere else. There is no telemetry.

## Does it work?

The [coding-agent eval](docs/agent-eval/README.md) runs ten scenarios with the real
Claude Code and Codex CLIs in throwaway repositories: a convention told to one agent
and used by the other, an end-of-day handoff, a correction, several values of one
kind, isolation between projects, a preference for all projects, a restated rule, a
retracted memory, small talk, and a question nothing answers. Without instructions,
Codex never saved what it was told, and the setup passed **4 of 10**. With the memory
policy that `make agents` installs, it passed **10 of 10**, including with Claude
Code's own auto-memory left on. Each scenario ran once on tiny planted repositories,
so this shows the approach works, not how often it works in long, real sessions.

## How agents use it

Six MCP tools, served by `mnemo-mcp-direct`:

| Tool | Contract |
|---|---|
| `memory_create` | Save a new memory in the project (default) or global scope; an existing subject/predicate in that scope returns `ALREADY_EXISTS` with its ID. |
| `memory_get` | Read the current value and event ID, or a full historical value and whether it can be restored. |
| `memory_search` | Search the project and global scopes; hits carry fact ID, event ID and scope. |
| `memory_update` | Change a memory by fact ID, passing the event ID you read as `expected_event_id`. |
| `memory_history` | Page through revisions, newest first, with value previews. |
| `memory_revert` | Copy a historical value into a new revision, keeping its provenance and trust. |

Agents read before changing a memory, pass the revision they read, re-read on a
conflict, and ask which change to undo when "undo that" is ambiguous. `request_id` is
optional; sending the same one again replays the original result instead of
applying the change twice. Errors are JSON `{code, message, details}` with MCP's
`isError` flag set:

| Code | What to do |
|---|---|
| `INVALID_INPUT` | Correct the input, ID, value size, limit, or cursor. |
| `NOT_FOUND` | Check the memory ID; memories from other projects are not visible. |
| `ALREADY_EXISTS` | Read the returned fact ID, then use a guarded update. |
| `REVISION_CONFLICT` | Re-read and reconsider; the memory changed since you read it. |
| `INVALID_RESTORE_TARGET` | Select a revision belonging to this memory. |
| `UNSUPPORTED_STATE` | This current state or historical revision cannot be restored here. |
| `REQUEST_ID_REUSED` | A request ID was reused with different parameters; retry the original or leave it out. |
| `UNSUPPORTED_OPERATION` | Undoing a creation is not supported. |
| `SERVICE_UNAVAILABLE` | Postgres or the embedding service is down, missing a model, or not set up; the message names the fix. |

## Configuration

Settings come from `MNEMO_*` environment variables or `.env`; see
[.env.example](.env.example). The ones that matter most:

| Setting | Default | Meaning |
|---|---|---|
| `MNEMO_DB_PORT` | `5432` | Host port of the Compose Postgres; the default DSNs follow it. |
| `MNEMO_DSN` | `postgresql://mnemo:mnemo@localhost:<port>/mnemo` | Database for memories. |
| `MNEMO_BACKEND` | `ollama` | Embeddings: `ollama` (local), `hash` (no model, matches exact words only) or `openai` (sends text to OpenAI). |
| `MNEMO_ACTOR` | the agent ID | Who is writing; set per agent by `make agents`. |
| `MNEMO_PROJECT` | detected | Overrides the project detected from the git repository. |
| `MNEMO_NAMESPACE`, `MNEMO_USER_ID` | `default` | Shared by every agent that should see the same memory. |

`mnemo-install` options: `--dry-run` shows the changes only, `--agent claude|codex`
limits them to one agent, `--codex-home` points at another Codex home (a `codex`
wrapper that pins one is detected), `--backend hash` avoids Ollama, and
`--claude-auto-memory off` also turns off Claude Code's built-in memory.

### Configure by hand

Claude Code, once for every repository (use your checkout's absolute path):

```bash
claude mcp add mnemo --scope user \
  -e MNEMO_BACKEND=ollama -e MNEMO_EMBED_DIM=768 \
  -e MNEMO_WORKER_ENABLED=false -e MNEMO_ACTOR=claude-code \
  -- /absolute/path/to/Mnemo/.venv/bin/mnemo-mcp-direct
```

Codex, in `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`):

```toml
[mcp_servers.mnemo]
command = "/absolute/path/to/Mnemo/.venv/bin/mnemo-mcp-direct"
default_tools_approval_mode = "approve"  # lets `codex exec` call the tools unattended

[mcp_servers.mnemo.env]
MNEMO_BACKEND = "ollama"
MNEMO_EMBED_DIM = "768"
MNEMO_WORKER_ENABLED = "false"
MNEMO_ACTOR = "codex"
```

Also paste [the memory policy](mnemo/memory_policy.md) into `~/.claude/CLAUDE.md` and
`$CODEX_HOME/AGENTS.md`: without it, Codex did not save anything in the eval. Other
clients such as Claude Desktop or [Cursor](https://cursor.com/docs/mcp) take the same
command and environment in their JSON configuration (Cursor also needs
`"type": "stdio"`). They are not started inside a repository, so they see only the
global scope unless you set `MNEMO_PROJECT`. `make mcp-direct` runs the server from
the checkout.

## What Mnemo depends on

| Service | Used for | If it is down |
|---|---|---|
| Postgres 16 + pgvector (`make up`, Docker) | All memories, history and receipts | Tools return `SERVICE_UNAVAILABLE` until it is back |
| Ollama with `nomic-embed-text` | Embeddings for search and writes | Search and writes fail; reading by ID, history and revert still work |
| git | Detecting the project | Only the global scope is available |

No language model runs inside Mnemo for this path: the agent decides what to save.
The optional web UI loads Tailwind and htmx from public CDNs.

## What the store guarantees

| Mechanism | Where it lives |
|---|---|
| Database triggers reject changes to event payloads; supersession and recall bookkeeping remain mutable. | [Integrity migration](migrations/0005_integrity.sql) |
| Scope and fact locks serialize writes; expected-event guards reject stale updates/reverts, including A→B→A. | [Direct API](mnemo/direct.py) |
| Event, HEAD, and append-only receipt commit together. Receipts survive restarts and remain for the store's lifetime. | [Receipt migration](migrations/0010_mutation_receipts.sql) |
| Revert copies source payload, embedding and lineage into a new event; it does not raise source trust. | [Core](mnemo/core.py), [direct contract](PROJECT_SPEC.md#15-additive-guarded-memory-contract--september-22-2026) |
| Historical reads distinguish recording time (`as_of`) from world validity (`valid_at`). Archive is reversible; `blame`, `diff`, and `commit` expose history. | [Temporal contract](PROJECT_SPEC.md#14-correctness-amendment--september-9-2026) |

To see the versioning without any agent or model, run `make demo-direct`. It
remembers PostgreSQL, changes it to MySQL, rejects a stale update, inspects history
and restores PostgreSQL; retrying the restore returns its receipt:

```text
ADD(PostgreSQL) -> UPDATE(MySQL) -> REVERT(PostgreSQL)
Source trust preserved: agent_inference / low
```

The demo uses the `hash` backend in a fresh namespace. Use a separate database when
switching between hash and semantic embeddings for the same scope.

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

Set `MNEMO_NAMESPACE` to the namespace you want to inspect (project scopes are
`<namespace>@<project>`). Search → open history → revert a revision. A stale form
returns HTTP 409, shows the latest state, and writes nothing. Repeated submissions
replay their receipt. Unrestorable revisions have no restore button. **Manual
correction** records a human-reviewed value through the legacy write path; it does
not have a revision guard.

## Checks, layout and limits

```bash
make test-db   # requires Postgres; prints test and skip counts
make lint
uv build
bash scripts/check-wheel.sh
```

The packaging check uses a fresh environment and exercises installed SDK/MCP
behavior. The [correctness workflow](.github/workflows/correctness.yml) is retained;
**GitHub Actions is disabled at the owner's request**, and Mnemo has so far been
tested on macOS only. Optional live-Ollama tests skip when their models are
unavailable.

`mnemo/` contains the async store, direct API, SDK, MCP servers, installer and the
parked extraction pipeline; `migrations/` holds the SQL; `web/`, `examples/`,
`scripts/` and `tests/` hold the UI, demos, eval harnesses and checks.
[The spec](PROJECT_SPEC.md) defines contracts; [the plan](docs/MASTER_PLAN.md) and
[status](PROJECT_STATUS.md) track delivery.

This is a local, single-user service. Scope columns do not provide authentication
or access control, and memories are stored unencrypted, so don't save secrets.
Identity is one current value per subject/predicate in a scope. Direct mutations
accept strings up to 8192 UTF-8 bytes and operate on active, durable, non-expiring
memories. Creation undo, grouped undo, branching, merging and multi-user access are
out of scope for now. The installed agents run the server from this checkout, so
keep it where it is (or rerun `make agents` after moving it).

## Architecture

```mermaid
flowchart LR
    Claude[Claude Code] --> Direct[Six direct MCP tools]
    Codex[Codex] --> Direct
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

## History: the automatic extraction pipeline (parked)

Mnemo began as a write-quality pipeline: `observe` records conversation turns, and a
worker extracts facts, checks them against their source, deduplicates, scores and
tiers them, and records each decision. It still works (`make mcp` starts the legacy
tools and worker, `make worker` a separate consumer; [setup](docs/EXTRACTION.md)),
but it is parked: coding agents save what matters themselves, and the experiments
below did not reach the quality target.

`make eval` is an **18-turn scripted regression**, currently gated precision/recall
**90.9% / 100%**, must-keep recall **100%**, and zero forbidden writes. It replays
labeled candidates; it is not real-conversation extraction accuracy, a LongMemEval
retrieval result, or a competitor comparison.

Small local models did not meet the memory-quality target. The recorded 200-turn
authored development run achieved **1.41% strict precision / 2.41% recall**; the
48-turn external holdout had **0/9 exact must-keep matches**. The later bounded
development baseline retrieved **16/23** complete distinct targets. Swapping only the
extraction model to Azure `gpt-5.6-luna` raised complete extraction to **20/23**
([v8](docs/quality-v8/README.md)), but one value per subject/predicate overwrote
four correct facts. Member/occurrence identities (spec §17) and optional
contradiction routing followed; routing stays off because the judge read "another
item" as "a correction" ([v9](docs/quality-v9/README.md),
[v12](docs/quality-v12/README.md)). Lasting tiering made important extracted facts
durable ([v11](docs/quality-v11/README.md)). See [v4](docs/quality-v4/README.md),
[v7](docs/quality-v7/README.md) and [current status](PROJECT_STATUS.md).

An earlier [host-agent pilot](docs/direct-pilot/README.md) let Qwen or Azure Luna
drive the direct tools, one attempt per scenario: both reached the intended end state
in 5/6 scenarios, and on an ambiguous "undo that" Luna reverted both memories
instead of asking. The revision guards block stale and racing writes but cannot know
whether a write was wanted; what they guarantee is that such a change is attributed,
visible in history and reversible.

## License

The project declares the **MIT license** in [pyproject.toml](pyproject.toml).
Bundled LongMemEval material comes from
[xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval) and its
[cleaned dataset](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned),
with the upstream [MIT license](mnemo/data/LongMemEval-LICENSE.txt) preserved.
Mnemo's atomic labels are provisional; see [data attribution](mnemo/data/README.md).
