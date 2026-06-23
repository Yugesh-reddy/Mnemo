# Direct-memory host-agent pilot

Phase 5 adds a bounded host-model tool-calling loop in
[`examples/agent_undo.py`](../../examples/agent_undo.py). The host chooses from the
six existing MCP tool schemas; the adapter executes its arguments through the
in-process `DirectMemory` API. This pilot does not exercise MCP transport, automatic
extraction, or semantic embeddings. Those are separate checks and measurements.

## Recorded result

The [Azure Luna run](run-luna/README.md) completed all six scenarios
once with `gpt-5.6-luna-2026-07-09`: **4/6 passed, with two unintended mutations**.
It restored both memories on an ambiguous undo request instead of asking which
change was intended. Targeted undo also used a historical read where the protocol
required a current read. This run fails the zero-unintended-mutation requirement.

The [Qwen run](run-qwen/README.md) completed all six scenarios once
with `qwen3.5:4b-mlx`: **2/6 passed the frozen protocol, with zero unintended
mutations across six scenarios**. The model produced five intended events.
Both undo cases restored the requested value but omitted the required current
read. Ambiguous undo exhausted its eight-request budget without asking a question.
Conflict recovery read a specific historical revision rather than current state.
The [complete evidence](run-qwen/results.json) retains these failures;
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
no automatic HTTP retry. For the Ollama host, cloud model tags and non-local endpoints are rejected;
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

## Azure Luna rerun

The optional Azure adapter uses the existing `httpx` dependency and the resource's
OpenAI v1 Chat Completions API. A project URL such as
`https://mnemo.services.ai.azure.com/api/projects/Mnemo` resolves to
`https://mnemo.services.ai.azure.com/openai/v1/`. The `model` parameter is the
**deployment name**, which can differ from the underlying model name. See
[Microsoft's endpoint guidance](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints).

Set these in the gitignored project-root `.env`:

```dotenv
MNEMO_AZURE_ENDPOINT=https://mnemo.services.ai.azure.com/api/projects/Mnemo
MNEMO_AZURE_DEPLOYMENT=gpt-5.6-luna
AZURE_OPENAI_API_KEY=your-resource-key
```

Then use a new evidence directory:

```bash
uv run python -m examples.agent_undo --host azure \
  --output docs/direct-pilot/new-luna-run
```

The Azure response must identify `gpt-5.6-luna` or a dated Luna version. A different
model stops further requests before any of its tool calls execute. Setup requires
an existing deployment and resource key; this command does not provision Azure
resources. Authentication headers are excluded from the saved evidence.

The scenarios, system prompt, six tool schemas, fixture isolation and scorer are
shared with the Qwen pilot. Azure uses `reasoning_effort=none` and
`max_completion_tokens=512`, including any reasoning tokens. It omits Ollama's
seed, context and temperature options. These provider differences are recorded;
this is not an identical-runtime comparison. See
[Luna's model documentation](https://developers.openai.com/api/docs/models/gpt-5.6-luna)
and [Azure's output-token limits](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning#control-costs).

The cloud run is bounded to **48 requests total**, **32 KiB of serialized request
body per request**, and **512 completion tokens per request** (at most 24,576
completion tokens). Oversized inputs stop instead of truncating the transcript;
failed requests consume the request budget and are never automatically retried.
These are usage limits, not a dollar estimate for an unverified Azure tariff.
Raw provider requests/responses, returned model/version, finish reasons and token
usage are retained along with the existing event/receipt audit. Incomplete
generations cannot execute tool calls. The original Qwen evidence is preserved.

## Interpretation

This is one scripted host-model run per scenario, not a reliability benchmark,
extraction evaluation or evidence about the sealed `b46e15ed` holdout. The six-case
result and unintended-event count must always be reported with their denominators.
Even 6/6 would not establish behavior for arbitrary conversations or other models.
Extraction quality and numeric gate thresholds remain unchanged since v7.

Protocol references: [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling)
for the tool request/result loop, and [chat API](https://docs.ollama.com/api/chat)
for request options. The project uses its existing HTTP client dependency.
