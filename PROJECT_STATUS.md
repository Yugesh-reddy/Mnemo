# Mnemo — implementation status

Updated September 11, 2026. [Spec v4](PROJECT_SPEC.md) defines the contracts;
[the implementation plan](docs/CORRECTNESS_PLAN.md) records the backlog and acceptance
checks. The [quality reliability plan](docs/QUALITY_RELIABILITY_PLAN.md) and
[recorded validation](docs/quality-v2/README.md) cover the follow-up milestone.
Numeric thresholds remain unchanged; the current real-model results do not meet
the memory-quality acceptance target.

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

## Quality work implemented

- Source review of all 38 previously unmatched historical writes, the explicit
  emergency-contact false write and all eight missing must-keep targets, with
  report/source hashes and failure attribution. Review is by one Codex reviewer;
  independent adjudication remains outstanding.
- Three new external development conversations (140 turns), plus one reserved
  42-turn holdout evaluated after a code/settings freeze. Including the earlier
  36-turn example gives 218 external turns across five records. This is not a
  single naturalistic 200-turn conversation or verified human-chat corpus.
- Extraction prompt preserves precise relations, actor direction, use versus
  preference, type and past actions. Null/negative placeholders cannot overwrite
  affirmative fact identities. Matching denial invokes bounded fallback even at
  high NLI confidence. The emergency-contact regression passes.
- Source/label/policy fingerprints, resumable per-conversation results, retrieval
  probes without reinforcement, complete historical assertion snapshots, explicit
  API/local pricing support and a controlled serial workload command.
- Every new external gated write has a source-review judgment: 75 development
  and 29 holdout events. These diagnostic reviews do not replace frozen scores.

## Executed validation

- Required Postgres suite with local models: **162 passed, zero skipped**.
  Command: `MNEMO_REQUIRE_DB=1 MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx HF_HUB_OFFLINE=1
  .venv/bin/pytest -q -rs`. [Test log](docs/quality-v2/tests.log).
- Ruff/Black, built package and clean-wheel installation pass. The wheel contains
  all new data and runs the audit/suite/benchmark CLI help commands outside the
  checkout. [Validation logs](docs/quality-v2/README.md#controlled-workload-result-and-validation).
- All development, holdout and benchmark processes exit successfully; evaluation
  schema cleanup leaves zero temporary schemas.
- Prior infrastructure evidence remains valid: required-DB unavailability fails
  rather than skips; real MCP observation/retrieval and rollback demos pass;
  actual HNSW execution plans and the adversarial historical-neighbor retrieval
  fixture are retained under [docs/evaluation](docs/evaluation/README.md).

## Quality results: acceptance is still unmet

The current 18-turn **scripted** regression is **90.9% precision / 100% recall**,
versus naive **60% / 90%**, with zero explicitly false gated historical writes.
Version 2 corrects database-use and primary-language labels while keeping the
same source turns. Legacy version-1 labels and 90%/100% evidence are preserved.
These are regression fixtures, not real-model accuracy or competitor comparisons.

On 90 fixed **development verifier assertions**, false accepts fall from 11 to one
and false rejects from one to zero. The emergency-contact denial is rejected.
A manager-direction false accept remains at 0.9927 confidence.

The **100-turn synthetic development rerun** has gated final precision/recall
**20% / 20%**, must-keep recall **37.5% (3/8)** and zero explicitly forbidden
historical writes. Its historical precision/recall are **32% / 26.7%**, below the
original policy's development-cohort **37.9% / 36.7%**. Eliminating the explicit
false write did not preserve recall. The old 200-turn mixed dev/held-out final
result (25% / 16%) is retained, but is not a like-for-like comparison to this run.

The **140-turn external development suite** matches one of 51 final truth targets
and one of 23 must-keep targets across its three isolated stores. Those counts
are not a claim that only one stored assertion is supported: source review finds
64 supported equivalents/partial/lossy assertions, eight unsupported strengthenings
and three ambiguous relations among 75 writes. Frozen labels and identities still
mismatch many correct assertions, and broad identities overwrite simultaneous facts.

The **fresh 42-turn holdout** has strict final precision/recall **3.7% / 4.3%**,
historical precision/recall **3.4% / 4%**, and **0/10 must-keep matches**. All
must-keep probes report upstream absence or label mismatch rather than a matching
stored target missed by search. Review of all 29 writes finds 17 supported
assertions, six unsupported assertions and six ambiguous relations. A delivery
date becomes a purchase date; tentative considerations become definite plans.
The explicit forbidden-list metric is zero, but **actual source review still finds
false assertions**. The unchanged policy and holdout fingerprints are retained.
This holdout is now consumed validation data, not a fresh set for future tuning.

Read [cohort results and failure evidence](docs/quality-v2/README.md) for precise
scope. No result establishes independently adjudicated real-world accuracy.

## Storage, latency, cost and remote CI

Fewer semantic events do not imply smaller total storage: holdout events fall
from 50 naive to 29 gated, while allocated bytes grow from 909,312 to 1,474,560.
External development allocated bytes grow from 2,613,248 to 4,464,640. The totals
include evidence, queue, audit records and indexes.

A controlled serial workload of **25 observations after two warmups** measures
observe-through-worker **p50 2.70 s / p95 6.90 s**, observe-only **20.4 / 35.7 ms**,
and **0.265 observations/s** on the local workstation. It ran after other model
work completed. This is serial service time, not concurrent capacity or a
production SLO. [Request-level measurements](docs/quality-v2/benchmark.json).
Monetary costs remain **unknown**: actual prices were not supplied, and embedding
token usage is unmetered. Explicit-rate estimation is implemented and tested.

The `correctness` workflow exists and database tests fail when Postgres is absent.
**Required merge protection and a remote run remain pending.** This checkout has
no remote. The same-named repository found in the authenticated account contains
unrelated DSA submissions; it was not modified. The intended repository/branch
and actual compute rates have been requested from the user.

## Remaining work, in order

1. Independently adjudicate labels and define development-only equivalence and
   compound-fact matching, preserving the original strict reports.
2. Repair unknown-relation hypothesis rendering, role direction and tentative-plan
   verification. The development traces show malformed possessive hypotheses
   rejecting valid completed activities. Do not tune on consumed holdout data.
3. Preserve predicate identity through corrections and retain simultaneous values
   without unrelated overwrites. Evaluate durable cross-session retention and
   expiry, which current final same-session probes do not establish.
4. Add a single naturalistic 200-turn conversation and a new unopened holdout.
   Require source-reviewed historical false-write and must-keep non-regression
   before calling quality reliable. The [detailed plan](docs/QUALITY_RELIABILITY_PLAN.md)
   records concrete failure cases and acceptance checks.
5. Configure the intended repository's required `correctness` merge check and
   rerun monetary calculations with actual prices.

Consolidation remains deferred until evaluation demonstrates benefit. It still
requires reflection provenance, complete lineage, source-evidence verification
and source-capped trust; no reflection writer is enabled.
