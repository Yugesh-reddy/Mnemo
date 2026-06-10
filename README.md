# Mnemo

**Agent memory that stores less and remembers what matters.**

Mnemo verifies and scores extracted assertions before writing them to an append-only
Postgres event store. Every stored revision has provenance and evidence; `blame`
and `revert` make mistakes inspectable and reversible. Python SDK, MCP server and a
small FastAPI/HTMX UI are included.

## Run locally

Requires Python 3.12, Docker and [uv](https://docs.astral.sh/uv/).
The database needs Postgres with pgvector >= 0.8; the supplied Compose image includes it.

```bash
make install
make up
make migrate
make eval       # 18-turn deterministic regression, no model required
```

For extraction and embeddings, start Ollama and install the configured models:

```bash
ollama pull nomic-embed-text
ollama pull llama3.2:3b
make worker     # extraction, retries, lease renewal and scheduled decay
make health     # queue age, failures, retries, latency and archival count
```

`MNEMO_EXTRACTOR_MODEL` can select another installed instruct model. Configuration
is read from `.env` / `MNEMO_*` variables; see [config.py](mnemo/config.py).

`make mcp` starts the stdio MCP server **and its worker**. Set
`MNEMO_WORKER_ENABLED=false` when running dedicated workers. Multiple consumers are
safe: each job gets an ownership token and renewed lease. The global consumer uses
the job's namespace/user/agent/session, with atomic writes and cache reconciliation.
SIGINT/SIGTERM stop new claims; shutdown allows in-flight work to finish for ten
seconds, then requeues owned work. Synchronous HTTP requests have bounded timeouts
and may finish after cancellation before Python exits.

## Verification

The default `heuristic` verifier is a conservative **regression mode**, with limited
predicate coverage. For full semantic entailment, install the optional NLI extra:

```bash
uv sync --extra dev --extra nli
export MNEMO_VERIFIER_BACKEND=cross_encoder
export MNEMO_VERIFIER_MODEL=cross-encoder/nli-deberta-v3-small
export MNEMO_VERIFIER_FALLBACK_BACKEND=ollama
# The fallback defaults to MNEMO_EXTRACTOR_MODEL.
make mcp
```

NLI checks the complete subject/relation/value assertion. High-confidence
entailment is accepted; ambiguous results can use the bounded JSON LLM fallback.
Unknown labels, malformed responses and model failures do not become accepted
facts. `ollama` and `openai` verifier modes are also available. The OpenAI backend
uses the configured API key and model; embedding dimensions must match storage.
Changing backend on an existing database does not silently recast vectors.

Assistant turns are excluded in code. Extractor-supplied provenance is never
trusted: extracted writes carry `agent_inference` / low trust. Explicit SDK/MCP
`add()` remains a direct write API, with caller-supplied provenance; it bypasses
the observation quality pipeline.

## Evaluation and its limits

The familiar **90% precision / 100% recall** is the 18-turn scripted regression
with deterministic embeddings, not measured real-model accuracy. Its naive arm
has 60% precision and 100% recall. Neither arm is a competitor implementation.

```bash
# Broader, project-authored synthetic data: 100 dev + 100 held-out turns
uv run mnemo-eval --dataset benchmark --split all --output synthetic.json

# Real extraction + embeddings + selected entailment verifier, identical
# extracted candidates replayed into both arms to isolate the gate
uv run --extra nli mnemo-eval --real --dataset benchmark --split held_out \
  --verifier cross_encoder --output real-held-out.json

# Independent end-to-end runs of extraction through each arm
uv run mnemo-eval --real --mode pipeline --output pipeline.json

# Complete 36-turn LongMemEval record with provisional atomic annotations
uv run mnemo-eval --real --verifier ollama \
  --longmemeval mnemo/data/longmemeval-car.json \
  --labels mnemo/data/longmemeval-car-labels.json --output longmemeval.json
```

Reports include final-state precision/recall/F1, historical precision/recall,
explicit false writes, unmatched assertions, must-keep coverage, event/row counts,
allocated storage bytes including indexes and audit records, latency, measured
request/token counts and fingerprints. Historical judgments use each write's own
source labels, so later corrections cannot hide earlier mistakes. Unmatched
assertions can be paraphrases or unfamiliar predicate names; that count is not an
adjudicated hallucination count. Retention scores and thresholds remain priors.

Candidate preparation time and shared extraction tokens are separate from the
comparison-arm latency/cost in replay mode. `--prices prices.json` estimates API
cost from supplied USD-per-million `input` and `output` rates keyed by `extractor`,
`embedder`, and `verifier`. Cost is null when pricing or token usage is missing;
local compute cost is not estimated. Ollama's legacy embedding endpoint does not
report tokens. Evaluation creates private temporary schemas and never truncates
application tables.
Reports retain the actual assertion history for review. Cleanup lock conflicts
are retried; if cleanup still fails, scores are saved with error metadata and the
CLI exits unsuccessfully instead of losing the report.

See [recorded evidence](docs/evaluation), [annotation scope and attribution](mnemo/data/README.md),
and [the detailed implementation plan](docs/CORRECTNESS_PLAN.md).

The recorded real-model 200-turn synthetic run achieved **25% final precision and
16% recall**, with **one explicitly false historical write**. Total storage grew
after including evidence and audit records. The broader evaluation exposes
substantial remaining quality gaps; see [current status](PROJECT_STATUS.md).

## Memory semantics

- Writes lock scope and HEAD before changing a fact. Payloads are immutable;
  supersession and recall counters are the explicitly mutable bookkeeping.
- Exact values and narrow spelling/predicate aliases deduplicate. Similar
  embeddings alone never suppress numeric, date or other meaningful corrections,
  or merge facts about different subjects.
- Session-tier facts require a session ID and expire after a configurable 24-hour
  TTL. Semantic session hits and raw cache hits are restricted to that session;
  durable facts are shared across sessions in the same scope. Legacy session
  events receive a read-time 24-hour TTL without changing their payloads.
- Current reads select active HEAD with `valid_from <= now < valid_to` and unexpired
  retention. `search(as_of=T, valid_at=V)` selects the latest revision recorded by T
  and filters its world-valid interval at V (default T). Both timestamps must be
  timezone-aware. Historical search excludes cache and never reinforces history.
- New future-effective HEAD revisions are hidden until valid_from; reads do not
  implicitly fall back to superseded revisions. Fact identity remains one HEAD per
  subject/predicate in a store scope, including session-tier updates.
- Revert checks fact/scope ownership, copies typed payload/embedding/source lineage,
  and starts a new belief interval. Session reverts retain session identity and
  renew TTL. Archival appends an event and rechecks HEAD and last recall under lock.
- Vector and lexical candidates are independently bounded, then scored together
  with unreconciled cache candidates before truncation. Live vector candidates use
  HNSW; historical snapshots use exact selection. HNSW is approximate and restrictive
  filters can return fewer than k matches. Iterative scans continue past filtered
  history, bounded by `MNEMO_SEARCH_HNSW_MAX_SCAN_TUPLES` (20,000 by default), following
  [pgvector's filtering guidance](https://github.com/pgvector/pgvector#iterative-index-scans).

## SDK, MCP and UI

```python
from datetime import datetime, timezone
from mnemo import Mnemo
from mnemo.embedder import build_embedder

memory = Mnemo("postgresql://mnemo:mnemo@localhost:5432/mnemo", build_embedder())
first = memory.add("user", "location", "Austin", provenance="direct_user_statement")
memory.add("user", "location", "Portland", provenance="agent_inference")
memory.blame(fact_id=first.fact_id)
memory.revert(first.fact_id, first.event_id)
memory.search("location", as_of=datetime.now(timezone.utc))
memory.observe("turn-1", "I use Postgres.", "session-1")  # run make worker for SDK observations
```

MCP tools: `memory_add`, `memory_observe`, `memory_search`, `memory_get`,
`memory_blame`, `memory_revert`, `memory_diff`, `memory_log`, `memory_decisions`,
`memory_health`. `memory_decisions` exposes rejection reasons, evidence, score
components, configuration snapshots and duplicate resolution.
The UI's **Operations** page shows the same queue health and decision evidence,
with a filter by source turn. JSON endpoints remain available at `/health` and
`/decisions`.

```bash
make demo   # real extraction → bad direct write → blame → revert; fresh namespace
make ui     # http://127.0.0.1:8000 — search → fact history → revert
```

The demo fails clearly if extraction yields no usable database fact. It does not
insert a fallback fact to disguise a failed pipeline and does not truncate memory.
Use `MNEMO_NAMESPACE` to select the same scope in MCP/UI and in diagnostic commands.

## Correctness and installation checks

```bash
make test-db   # fails when Postgres is unavailable, including in CI
make lint
uv build
bash scripts/check-wheel.sh  # new environment + cwd, no tests package
```

CI runs the required database suite against Postgres 16 + pgvector, lint, eval and
wheel checks. Optional local-model tests can skip when those models are absent;
required database tests cannot silently skip in CI. Repository branch protection
must be configured by the repository owner to require the `correctness` job.

Consolidation remains deferred until evaluation establishes a benefit. Reflection
would require complete source lineage, entailment verification and trust capped
at the least-trusted source and at medium; no reflection writer is enabled.
