# Direct-memory host-agent pilot

Phase 5 adds a bounded Ollama tool-calling loop in
[`examples/agent_undo.py`](../../examples/agent_undo.py). The host chooses from the
six existing MCP tool schemas; the adapter executes its arguments through the
in-process `DirectMemory` API. This pilot does not exercise MCP transport, automatic
extraction, or semantic embeddings. Those are separate checks and measurements.

## Recorded result

The [September 22 run](run-2026-09-22/README.md) completed all six scenarios once
with `qwen3.5:4b-mlx`: **2/6 passed the frozen protocol, with zero unintended
mutations across six scenarios**. The model produced five intended events.
Both undo cases restored the requested value but omitted the required current
read. Ambiguous undo exhausted its eight-request budget without asking a question.
Conflict recovery read a specific historical revision rather than current state.
The [complete evidence](run-2026-09-22/results.json) retains these failures;
the prompt, scorer and results were not changed after the run.

## Protocol fixed before the run

Six independent scenarios run once each. Each starts with a fresh namespace and
fixture history; the model receives the prior context and latest user request,
but must discover fact/event IDs through tools. There are no demonstrations,
hidden target IDs, fallback writes, automatic repairs of model arguments, or
post-failure prompt iterations. The system prompt supplies the same read/guard/
ambiguity rules as the direct MCP server and names the two preference predicates.

| Scenario | Required behavior |
|---|---|
| Create | Remember PostgreSQL as the preferred database with one ADD. |
| Update | Search and read PostgreSQL, then write MySQL with its expected revision. |
| Undo | Search/read the MySQL memory, inspect history before restoring the original PostgreSQL event. |
| Targeted undo | With database and language changes present, restore only the database and retain Rust. |
| Ambiguous undo | Ask which change to undo; write neither an event nor a receipt. |
| Revision conflict | An actual SQLite revision is inserted immediately before the first update attempt. After the resulting conflict, re-read that fact before retrying MySQL or asking the user. |

The scorer checks **all new database events**, not only final HEAD. An unintended
mutation is an event outside the single permitted operation/fact/value/restore
source, or any extra event. Fixture writes and the explicitly recorded concurrent
injection are excluded. Failed requests are retained but are not state changes.
Read/search/history checks bind to the actual target and required ordering.
Clarification detection is a conservative textual screen; the published result
also includes a review of each final reply and complete tool trace. Merely adding
a question mark to a completion claim does not satisfy the ambiguity scenario.

The local model is `qwen3.5:4b-mlx`, installed as a 4.5B Qwen3.5 model in NVFP4
format. Metadata, digest and Ollama version are recorded with the run. The request
uses `think=false`, temperature 0, seed 7, `num_predict=512` and `num_ctx=8192`.
These settings do not guarantee identical generations on another runtime.

Each scenario permits at most eight model requests and twelve executed tool
calls, with a 45-second HTTP timeout and 180-second scenario deadline. There is
no automatic HTTP retry. Cloud model tags and non-local endpoints are rejected;
only already-installed models advertising tool support are allowed. No paid
provider is used. Hash vectors have the configured database dimension and are
explicitly non-semantic; keyword lookup is sufficient for these fixture facts.

## Run it

Start Postgres with `make up`, install the project, and ensure the chosen local
model is already installed. Run from the checkout:

```bash
uv run python -m examples.agent_undo \
  --model qwen3.5:4b-mlx \
  --output docs/direct-pilot/new-run
```

Use a new output directory on each deliberate run; existing evidence is never
overwritten. The runner uses the server in `MNEMO_TEST_DSN` to create and migrate
its own randomly named database, snapshots event/HEAD/receipt state, and drops
only that database. It never uses application memory or the extraction holdout.
A failed or interrupted case retains its partial transcript and best-effort audit
before cleanup; unattempted scenarios remain visible in the six-case denominator.
The report retains the temporary database name and cleanup outcome for recovery.

`results.json` records the exact system prompt, schemas, model requests/responses,
tool arguments/results, fixture and final snapshots, injected revision, per-case
wall time, errors, checks and whole-run wall time. Exit status is zero only when
all six automatic case checks pass and no background rows exist. A nonzero exit
is retained as a measured failure. Model responses are never used as evidence
that a write occurred without checking the store.

## Interpretation

This is one scripted host-model run per scenario, not a reliability benchmark,
extraction evaluation or evidence about the sealed `b46e15ed` holdout. The six-case
result and unintended-event count must always be reported with their denominators.
Even 6/6 would not establish behavior for arbitrary conversations or other models.
Extraction quality and numeric gate thresholds remain unchanged since v7.

Protocol references: [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling)
for the tool request/result loop, and [chat API](https://docs.ollama.com/api/chat)
for request options. The project uses its existing HTTP client dependency.
