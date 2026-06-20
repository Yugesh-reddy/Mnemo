# Optional extraction and evaluation

The direct SDK/MCP path does not run extraction. This guide preserves the
quality-pipeline setup, evaluation commands and recorded limitations.

For extraction and embeddings, start Ollama and install the configured models:

```bash
ollama pull nomic-embed-text
ollama pull llama3.2:3b
make worker     # extraction, retries, lease renewal and scheduled decay
make health     # queue age, failures, retries, latency and archival count
```

`MNEMO_EXTRACTOR_MODEL` can select another installed instruct model. Configuration
is read from `.env` / `MNEMO_*` variables; see [config.py](../mnemo/config.py).

`make mcp` starts the stdio MCP server **and its worker**. Set
`MNEMO_WORKER_ENABLED=false` when running dedicated workers. Multiple consumers are
safe: each job gets an ownership token and renewed lease. The global consumer uses
the job's namespace/user/agent/session, with atomic writes and cache reconciliation.
SIGINT/SIGTERM stop new claims; shutdown allows in-flight work to finish for ten
seconds, then requeues owned work. Synchronous HTTP requests have bounded timeouts
and may finish after cancellation before Python exits.

## Verification

The default `heuristic` verifier is a conservative **regression mode**, with limited
predicate coverage. For model-based assertion verification, install the optional NLI extra:

```bash
uv sync --extra dev --extra nli
export MNEMO_VERIFIER_BACKEND=cross_encoder
export MNEMO_VERIFIER_MODEL=cross-encoder/nli-deberta-v3-small
export MNEMO_VERIFIER_FALLBACK_BACKEND=ollama
# The fallback defaults to MNEMO_EXTRACTOR_MODEL.
make mcp
```

NLI checks the complete subject/relation/value assertion. High-confidence
entailment is accepted; matching denied evidence and ambiguous results can use the
bounded JSON LLM fallback. Confidence does not guarantee entailment.
Unknown labels, malformed responses and model failures do not become accepted
facts. `ollama` and `openai` verifier modes are also available. The OpenAI backend
uses the configured API key and model; embedding dimensions must match storage.
Changing backend on an existing database does not silently recast vectors.

Assistant turns are excluded in code. Extractor-supplied provenance is never
trusted: extracted writes carry `agent_inference` / low trust. Explicit SDK/MCP
`add()` remains a direct write API, with caller-supplied provenance; it bypasses
the observation quality pipeline.

## Evaluation and its limits

The current 18-turn scripted regression has **90.9% precision / 100% recall**,
versus naive **60% / 90%**, with deterministic embeddings. Version 2 corrects two
labels that conflated database use with preference and primary with preferred
language; the source turns are unchanged. The legacy 90%/100% result and its
version-1 labels remain available. Neither version measures real-model accuracy
or a competitor implementation.

```bash
# Broader, project-authored synthetic data: 100 dev + 100 held-out turns
uv run mnemo-eval --dataset benchmark --split all --output synthetic.json

# Real extraction + embeddings + selected entailment verifier, identical
# extracted candidates replayed into both arms to isolate the gate
uv run --extra nli mnemo-eval --real --dataset benchmark --split held_out \
  --verifier cross_encoder --output real-held-out.json

# One continuous authored 200-turn development conversation across ten sessions
uv run --extra nli mnemo-eval --real --dataset naturalistic --split dev \
  --verifier cross_encoder --probe-retrieval --output naturalistic.json

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
the evaluation CLI does not estimate local compute cost. Ollama's legacy embedding endpoint does not
report tokens. Evaluation creates private temporary schemas and never truncates
application tables.
Reports retain the actual assertion history for review. Cleanup lock conflicts
are retried; if cleanup still fails, scores are saved with error metadata and the
CLI exits unsuccessfully instead of losing the report. Failed extraction turns are
also retained with their errors and remain in recall denominators. A report with
failed turns has status `scored_with_errors`; it is not a successful pipeline run.

See [recorded evidence](evaluation), [annotation scope and attribution](../mnemo/data/README.md),
and [the master implementation plan](MASTER_PLAN.md).

The original real-model 200-turn synthetic run achieved **25% final precision and
16% recall**, with **one explicitly false historical write**. Total storage grew
after including evidence and audit records. The broader evaluation exposes
substantial remaining quality gaps; see [current status](../PROJECT_STATUS.md).

The [memory-quality audit](quality-v4/README.md) retains reviews of the original
38 unmatched writes and eight misses, and expands external coverage to **312 turns
across seven conversations**. Labels remain provisional single-reviewer annotations.
Evidence-grounded extraction now checks exact source quotes and retries invalid
output; evaluations keep failed turns in the scores and preserve their errors.

The continuous authored **200-turn development run** has **1.41% strict final
precision, 2.41% recall and 2/32 must-keep**. Review of all 145 gated writes finds
13 unsupported and seven ambiguous assertions; 16 strict must-keep misses have
source-supported equivalents returned by search. The frozen **48-turn holdout**
has **0/9 exact must-keep**, with four retrieved equivalents and one unsupported
assertion among 25 writes. Extraction failures remain in both complete reports.
Zero forbidden-list matches does not establish zero false memories.

**Run `make test-db` for the current test and skip counts with Postgres required.**
These tests establish regressions, while real-model memory reliability remains
unmet. The controlled benchmark retains failed jobs, retries, warmups and usage;
missing prices remain unknown. Consolidation stays deferred.

```bash
# Use the configured real extraction, embedding and verifier backends
mnemo-eval-suite --manifest mnemo/data/quality-v4/manifest.json \
  --split dev --output external-dev.json

# Serial workload; warmup excluded. Supply actual rates for a monetary estimate.
mnemo-benchmark --samples 25 --warmup 2 --output benchmark.json
# Optional: --prices prices.json or --local-usd-per-hour YOUR_RATE
```

Holdout evaluation requires a matching `--frozen-policy` fingerprint. The saved
holdout is now consumed validation data and cannot be reused as fresh evidence
after further development. `--probe-retrieval` in the evaluation CLI distinguishes
missing current assertions from failures to retrieve stored assertions, without
reinforcing memory. The benchmark reports separate API and local-compute
estimates; prices that have not been supplied remain unknown.
The [master plan](MASTER_PLAN.md) records acceptance gaps and the planned fixes.
