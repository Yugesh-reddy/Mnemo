# Mnemo — implementation status

Updated September 9, 2026. [Spec v4](PROJECT_SPEC.md) defines the contracts;
[the implementation plan](docs/CORRECTNESS_PLAN.md) records the backlog and acceptance
checks. This replaces the July status, which overstated completion and evaluation
coverage. Thresholds have not been tuned against the new results.

## Implemented and covered by regression tests

- Scoped, serialized HEAD mutations; immutable event payloads; fact-owned revert
  targets with restored embeddings, JSON and lineage; transactional decay with
  stale-HEAD and last-recall checks.
- Renewable, fenced extraction claims; job-owned scope; retries/backoff; atomic
  event/decision/cache completion; cancellation recovery; standalone worker and
  MCP background startup; scheduled archival and graceful shutdown.
- Complete typed assertion verification with optional local NLI and bounded LLM
  fallback, code-enforced assistant exclusion and low-trust extraction provenance.
  Rejected candidates and score components are retained in append-only decisions.
- Meaningful corrections survive high embedding similarity. Duplicate retries
  remain idempotent; repetitions after intervening corrections are ingested.
- Session restrictions and TTL, legacy expiry overlays, valid-time filtering,
  recorded-time search, archive/expiry diffs and consistent cache/semantic ranking.
- Bounded vector and lexical candidates, HNSW iterative filtering and verified
  index plans; explicit embedding-dimension and pgvector-version checks.
- Queue health and decision evidence through CLI, MCP, JSON and the operations UI.
- Packaged migrations, data, templates and demos; runtime dependencies; required
  Postgres CI and a clean-wheel installation script. CI workflow is provided;
  remote CI execution and branch-protection configuration have not been performed.

## Executed validation

- `MNEMO_REQUIRE_DB=1 MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx .venv/bin/pytest -q -rs`:
  **149 passed, zero skipped**, with Postgres 16, pgvector 0.8.3 and local models.
- `make lint`: ruff and Black pass.
- `uv build` and `bash scripts/check-wheel.sh`: pass in a new environment and
  temporary working directory, including runtime imports, packaged assets,
  installed evaluation CLI and migration command.
- Required-database negative check against an unavailable port exits 1 with ten
  database setup errors and zero skips, as intended.
- All three originally reported verifier failures pass with real NLI plus bounded
  fallback; [verdicts and evidence](docs/evaluation/verifier-probes.json) are retained.
- The [external MCP client](docs/evaluation/mcp-real-smoke.json) observes a real user
  statement and retrieves the resulting verified semantic fact. The
  [rollback demo](docs/evaluation/demo.txt) completes ADD → UPDATE → REVERT using
  real extraction and embeddings. [Validation details](docs/evaluation/validation.json)
  record the commands and outcomes.
- A 5,000-event fixture naturally selects the HNSW index; an adversarial fixture
  retrieves the live event past 500 nearer historical vectors. See the recorded
  [execution plans](docs/evaluation/retrieval-plan.json).

## Evaluation: infrastructure complete, quality still insufficient

The original scripted 18-turn regression remains **90% precision / 100% recall**
versus naive 60% / 100%, with zero gated false writes throughout history. These
are deterministic fixture results, not model accuracy or competitor measurements.

The real-model 200-turn synthetic run uses Qwen 3.5 4B extraction, Nomic embeddings
and DeBERTa NLI with Qwen fallback. Both arms receive identical extracted candidates.
Final gated precision is **25%**, recall **16%**, and must-keep recall **38.5%**;
naive precision is 10.9% and recall 10%. Historical explicitly false writes fall
from 53 to **1**, and semantic events from 175 to 55. Total allocated storage grows
from 2,531,328 to 3,129,344 bytes because gated storage includes raw evidence,
queue and decision records. This run does not support a total-storage saving claim.

The remaining explicit false write is `user / emergency_contact / Alex Chen`
against a denial. NLI accepted it at 0.9933 confidence. Full-assertion input fixes
the verification contract; model confidence does not guarantee entailment.
There are also 38 unmatched historical gated assertions, which may include
predicate/paraphrase mismatches and are not adjudicated hallucinations.

The corpus has 100 development and 100 held-out turns with disjoint scenarios.
Held-out historical precision/recall are 19.2% / 16.7%, with zero explicitly
forbidden writes. This is one project-authored synthetic corpus, not a natural
200-turn conversation. A separate complete 36-turn LongMemEval conversation is
included with provisional atomic labels, alongside an independent 18-turn real
pipeline run. See [recorded runs and methodology](docs/evaluation/README.md).

## Remaining work, in order

1. Independently adjudicate atomic labels and unmatched assertions, expand natural
   conversations beyond the single external example, and reserve a fresh holdout
   before subsequent tuning. Investigate extraction coverage and predicate fidelity.
2. Calibrate NLI/fallback on development evidence, including confidently wrong
   predictions and unfamiliar relations. Re-run final/history precision and
   must-keep recall; do not treat a threshold or a three-case probe as a guarantee.
3. Benchmark latency under controlled load and estimate cost with explicit prices
   and complete usage. Current timings are workstation diagnostics; cost is unknown.
4. Require the `correctness` job in repository branch protection and observe its
   first remote run. Record public demo media only with appropriately qualified claims.
5. Keep consolidation disabled until evaluation demonstrates a benefit. A future
   implementation needs reflection provenance, all source events, verification
   against evidence and trust capped by medium and the weakest source. Adding the
   enum alone does not complete that feature.
