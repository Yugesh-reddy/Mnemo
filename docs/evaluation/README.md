# Recorded correctness and model evaluation

These artifacts were produced on September 9, 2026. They document implementation
checks and measured failures; they are not a production-quality certification or
a comparison with Mem0, Zep, Letta or other products. Model identifiers, digests,
NLI revision and package versions are in [environment.json](environment.json).

## Current runs

- [scripted-smoke.json](scripted-smoke.json): the original 18-turn deterministic
  fixture. Naive final precision/recall 60%/100%; gated 90%/100%. Historical
  explicitly false writes are five versus zero. This remains a regression test.
- [scripted-200.json](scripted-200.json): 200 synthetic turns, deterministic
  extraction/embeddings and heuristic verification. Gated recall is only 4%; the
  heuristic vocabulary does not generalize to the broader corpus.
- [real-200.json](real-200.json): all 200 synthetic turns with real extraction,
  embeddings, NLI and bounded fallback; identical candidates replayed into both
  arms. Naive final precision/recall 10.9%/10%; gated **25%/16%**. Must-keep recall
  is 38.5% in both arms. Historical precision/recall: 7.4%/21.7% versus 29.1%/26.7%.
  Explicitly false historical writes: **53 versus 1**; unmatched: 109 versus 38.
  Held-out gated historical precision/recall: 19.2%/16.7%, with zero explicitly
  false writes and 21 unmatched writes. This does not establish reliable recall.
- [real-longmemeval-car.json](real-longmemeval-car.json): one complete external
  36-turn, three-session conversation, with project-authored atomic labels. Naive
  final precision/recall 14.3%/3.6%; gated 33.3%/3.6%. Explicitly false historical
  writes: one versus zero. Six of seven gated events are unmatched to the labels.
- [real-smoke.json](real-smoke.json): independent real extraction runs through
  each arm of the original 18-turn fixture (`mode=pipeline`), exercising the
  production extraction path rather than sharing extracted candidates. Naive
  final precision/recall 41.7%/55.6%; gated 60%/66.7%. Explicitly false historical
  writes: one versus zero. These independent runs can differ in extracted candidates.
- [verifier-probes.json](verifier-probes.json): all three originally reported
  verifier failures, now checked with typed assertions and real NLI/fallback.
  Acceptance behavior passes in all three cases. NLI calls unsupported MongoDB
  against Austin a contradiction rather than neutral; rejection is correct, but
  that label distinction illustrates the model's limitations.
- [mcp-real-smoke.json](mcp-real-smoke.json): an external stdio client submits a
  real user statement, waits for the background worker and retrieves a verified
  semantic fact. It records health and the actual decision evidence.
- [retrieval-plan.json](retrieval-plan.json): EXPLAIN ANALYZE output from the live
  search candidate queries on 5,000 events. The planner selects `idx_event_embed`
  without forcing index use. A separate regression covers filtered history.
- [validation.json](validation.json), [test-results.txt](test-results.txt) and
  [demo.txt](demo.txt): executed correctness, installation and rollback checks.

Reports without an `initial` or `v2` suffix use pipeline version
`2026-09-09-v3`. They retain current assertions, every written assertion and
candidate decisions before their private schemas are removed. Completed current
runs have empty `metadata.cleanup_errors`.

## Known false write and annotation limits

The 200-turn run writes `user / emergency_contact / Alex Chen` from development
turn `dev-023c`: “I do not use Alex Chen; please don't infer that as my emergency
contact.” DeBERTa assigns entailment probability 0.9932956, above the unchanged
0.99 threshold. The gate writes it as a low-trust session fact, with score 0.64.
The event and verdict remain in `real-200.json`. A complete hypothesis prevents
object-only verification; it cannot make an imperfect classifier infallible.

The synthetic corpus contains 50 four-turn scenarios, split into 100 development
and 100 held-out turns with disjoint scenarios and multiple sessions. It exercises
corrections, unfamiliar predicates, denials and hypotheticals, but some phrasing
is artificial. It is not a natural 200-turn dialogue. Held-out results are now
visible, so subsequent tuning should use development labels and a fresh final holdout.

The LongMemEval sample and MIT license are packaged under `mnemo/data`. Its QA
evidence markers are not atomic-fact labels. The additional labels were authored
before inspecting model output, but have not been independently adjudicated.
Read [annotation scope and attribution](../../mnemo/data/README.md). The single
external conversation and synthetic corpus do not establish general accuracy.

## Metric definitions and comparison boundaries

- Final precision/recall compare normalized complete subject/predicate/value
  assertions with the final target facts. `must_keep_recall` uses the must-keep
  subset. Session facts are evaluated within the source sessions.
- Historical precision counts every appended write against allowed source-turn
  atomic labels, including legitimate candidates/transient facts; its denominator
  is all writes. Historical recall counts distinct labeled source assertions
  recovered across the conversation. Final and historical targets differ.
- `false_writes` means an explicitly forbidden source assertion, including events
  later superseded. `unsupported_writes` means unmatched to available labels.
  Paraphrases and unfamiliar predicate names can be unmatched; this is **not an
  adjudicated hallucination count**. Exact matching understates semantic matches.
- The naive baseline stores extracted candidates directly. Gated writes use the
  production observation, queue, verification and decision path. In replay mode,
  both arms share one extraction result per turn; shared extraction usage/time is
  reported in metadata, separately from arm usage/time.
- `storage_bytes` is allocated database storage including indexes for the arm's
  application tables. Gated includes raw cache, queue and audit records; naive
  includes its facts/events. It is not a payload-only estimate or a full database
  cluster/WAL/backup footprint. Page allocation makes small-run sizes coarse.

In the real 200-turn run, semantic events fall from 175 to 55, but allocated bytes
rise from **2,531,328 to 3,129,344** (23.6%). Preserving evidence has a storage cost;
this result does not support a claim that total storage is reduced.

Shared real-200 extraction takes 502.584 seconds (200 requests, 73,328 input and
11,042 output tokens). Naive arm processing takes 6.322 seconds; gated takes
327.955 seconds, including 147 NLI calls and 86 fallback requests. LongMemEval
shared extraction takes 128.596 seconds, then 1.019 seconds naive / 119.445 seconds
gated. These are workstation wall-clock diagnostics, with overlapping local
activity, not isolated load or throughput benchmarks.

No token prices were supplied, so `cost.usd` is null with an explicit reason.
Qwen token usage is recorded; Ollama's legacy embedding endpoint reports no token
counts. Local compute/electricity cost is unmeasured. The CLI accepts explicit
USD-per-million input/output prices per component; it does not invent a zero cost.

## Reproduction

Run from the repository after `make up`, `make migrate`, installing the NLI extra
and downloading the named local models:

```bash
uv sync --extra dev --extra nli
export MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx
export MNEMO_VERIFIER_BACKEND=cross_encoder
export MNEMO_VERIFIER_FALLBACK_BACKEND=ollama
# HF_HUB_OFFLINE=1 may be set once the NLI model has been cached.
.venv/bin/python -m mnemo.eval --real --dataset benchmark --split all --output real-200.json
.venv/bin/python -m mnemo.eval --real --mode pipeline --output real-smoke.json
.venv/bin/python -m mnemo.eval --real \
  --longmemeval mnemo/data/longmemeval-car.json \
  --labels mnemo/data/longmemeval-car-labels.json --output real-longmemeval-car.json
MNEMO_VERIFIER_BACKEND=heuristic .venv/bin/python -m mnemo.eval --output scripted-smoke.json
MNEMO_VERIFIER_BACKEND=heuristic .venv/bin/python -m mnemo.eval \
  --dataset benchmark --split all --output scripted-200.json
MNEMO_REQUIRE_DB=1 .venv/bin/pytest -q -rs
MNEMO_PLAN_OUTPUT=/tmp/retrieval-plan.json .venv/bin/pytest tests/test_retrieval_plan.py -q
make lint
uv build
bash scripts/check-wheel.sh
```

Outputs will vary with model/runtime versions and execution order, even with
temperature zero. Fingerprints identify dataset and component/config snapshots;
the model digests in environment.json identify this run's local model files.

## Earlier diagnostic runs

Files ending in `-initial.json` or `-v2.json` preserve earlier diagnostic output;
they are not headline results or comparable benchmark repetitions. Initial runs
included object-to-hypothesis fixes and a direct LLM verifier. The v2 runs precede
the extractor's preference-versus-use prompt correction.

`real-200-initial.json` was recovered from completed writes after a diagnostic
reader conflicted with schema cleanup. Its missing latency/usage remain null;
those measurements were not reconstructed. Task-created schemas were cleaned.
The current harness retains assertion snapshots itself and retries cleanup, saves
error metadata if cleanup fails, and exits nonzero instead of discarding a report.
