# Correctness and realistic evaluation milestone

This plan implements the September 8 roadmap in dependency order. The working
tree already contained partial implementations; those are retained and verified.
Thresholds stay at their existing priors. Consolidation remains conditional on
measured benefit and is deliberately outside the implementation below.

## 1. Transaction and rollback integrity

- Lock the scoped fact before reading HEAD. Serialize same-scope writes and
  checkpoints so concurrent writes form one event chain.
- Reject rollback targets from another fact or scope; copy typed payloads,
  embeddings and source lineage directly in SQL. Reactivate invalidated facts.
- Archive inside a transaction, recheck HEAD and retention after locking, and
  preserve JSON values without decoding/re-encoding them as strings.
- Protect meaningful corrections even when embeddings are identical. Only exact
  values with matching visibility metadata can be idempotent; canonical aliases
  still identify the same fact.
- Acceptance: lifecycle immutability, foreign-target rollback, concurrent adds
  and updates, stale decay selection, transactional failure, JSON round trip,
  Friday → Monday and 100 → 150 regression tests on Postgres.

## 2. Operational extraction pipeline

- Claim jobs with `FOR UPDATE SKIP LOCKED`, per-claim ownership tokens, bounded
  attempts, retry backoff and explicit leases. Renew during model calls; recheck
  ownership before any event/audit/cache commit.
- Derive scope exclusively from the claimed job. Identify the exact source cache
  row; keep reconciliation, events, decisions and completion atomic.
- Exclude assistant turns in code. Treat extracted candidates as low-trust agent
  inference regardless of the model's assertion_type field.
- Provide `make worker` / `mnemo-worker`, signals for graceful shutdown, scheduled
  decay and MCP lifespan startup/cleanup. Avoid synchronous model calls on the
  event loop and avoid long database transactions around network requests.
- Acceptance: two workers, slow calls longer than a lease, lost ownership,
  namespace/user/agent/session isolation, retry exhaustion, cancellation and
  two-tier handshake tests. Expose queue age, failures, latency and archive count.

## 3. Evaluation before gate changes

- Preserve the 18-turn deterministic fixture and its required precision/recall
  regressions. Label results as scripted; never present them as model accuracy.
- Add an approximately 200-turn conversation with atomic labels, corrections,
  unfamiliar relations, compound negation, mixed hypothetical/factual statements,
  multiple sessions and an explicit development/held-out split.
- Support replaying identical real extracted candidates into both arms, plus
  end-to-end production extraction/embedding/verification runs.
- Assess each write against its own source labels (subject, predicate, object),
  including writes later superseded. Report historical precision/false writes,
  must-keep coverage, final-state metrics, rows/bytes, latency and model usage.
  Cost is null with a reason unless supplied pricing can be applied to measured
  token usage. Record data/model/config fingerprints.
- Import LongMemEval conversations only with explicit atomic-fact annotations;
  QA evidence markers are not write-quality labels. Include attribution and
  distinguish native synthetic conversation results from external benchmarks.
- Acceptance: hidden false-write regression, correction labels, deterministic
  repeatability, real-model run artifact and installable CLI outside checkout.

## 4. Full-assertion entailment

- Pass the complete structured assertion to every verifier. Use clause-local
  evidence in heuristic mode and never accept mere object overlap. Exclude
  assistant input before any model call.
- Provide a local NLI cross encoder with labels read from model metadata; accept
  high-confidence entailment and delegate ambiguous cases to bounded JSON LLM
  verification. Fail closed on malformed responses and network failures.
- Keep heuristic mode explicitly labeled for deterministic regression. Unknown
  predicates require semantic verification rather than invented lexical rules.
- Preserve rejected candidates, reasons, evidence, model, confidence, score
  components and non-secret gate configuration in append-only decision records.
- Acceptance: all three reported verifier failures; wrong subject/relation,
  negation, unrelated hypothetical, malformed fallback and provenance spoofing.

## 5. Session, temporal and cache contract

- Session-tier events require a session_id and expire after configurable 24-hour
  TTL; durable facts are shared across sessions in the same store scope.
- Current state selects HEAD and filters active status, tier, valid_from,
  valid_to and expires_at. Valid intervals are half-open [from, to).
- `search(as_of=T, valid_at=V)` uses recorded time T to select the latest known
  revision and world time V (default T) to filter validity. Require aware
  datetimes. Historical search excludes fast cache and never reinforces history.
- Select bounded semantic and raw candidates under one repeatable-read snapshot,
  score both with the same formula, deduplicate source identity, then truncate.
- Revert restores the payload as a new current belief; session restoration renews
  TTL and preserves the originating session. Expiry is reversible via revert.
- Acceptance: future-dated facts, expired/cross-session facts, time travel after
  correction/invalidation, relevant raw cache with k=1, atomic reconciliation.

## 6. Retrieval, packaging and required CI

- Bound vector and lexical candidates independently, union event IDs, then
  rerank. Preserve ascending raw distance plus LIMIT in the vector query.
- Verify EXPLAIN plans on a realistically populated Postgres fixture; distinguish
  planner eligibility from a small-table planner's legitimate sequential scan.
- Continue iterative HNSW scans past filtered historical neighbors, with a bounded
  scan budget; require pgvector >= 0.8 and test an adversarial historical fixture.
- Package migrations, evaluation data, templates and demo assets. Keep tests out
  of runtime imports. Test a wheel in a fresh environment and temporary cwd.
- Add CI with Postgres 16 + pgvector. Required DB checks fail when unavailable;
  optional real-model tests are separate and explicitly labeled.
- Acceptance: complete DB suite, lint/format, deterministic eval, demo, MCP stdio
  client, UI search/blame/revert and clean-wheel smoke checks.

## 7. Consolidation remains last

No automatic reflection writes until evaluation shows a benefit. The existing
spec requires agent_reflection provenance, every source event ID, entailment
against sources, and trust no higher than medium or the least-trusted source.
Enum support alone does not imply a working consolidation pipeline.

## Delivery evidence

Record executed commands and actual outcomes in PROJECT_STATUS.md. Separate
implemented features from tested behavior and externally blocked validation.
Retain reviewable diffs; do not fold unrelated brand assets into milestone work.

Execution status on September 9: steps 1–6 are implemented, with 149 passing tests,
clean-wheel validation and real-model reports. Evaluation data remains provisional
and model quality is insufficient: the 200-turn run has 25% final precision, 16%
recall and one explicitly false historical write. Step 7 remains deliberately
deferred. [Current status](../PROJECT_STATUS.md) and
[measurement evidence](evaluation/README.md) distinguish these outcomes from the
original acceptance targets and list the follow-up work.

References: [project spec](../PROJECT_SPEC.md),
[LongMemEval dataset format](https://github.com/xiaowu0162/LongMemEval#-dataset-format),
[pgvector index requirements](https://github.com/pgvector/pgvector#why-isnt-a-query-using-an-index).

## Additional regressions found while implementing

- Repeating a statement after an intervening correction must be ingested again.
  Permanent text-hash dedup lost Austin → Portland → Austin; dedup now distinguishes
  retries by turn identity and suppresses only adjacent identical observations.
- A fresh installation selected MCP 2.x and failed importing FastMCP. Constrain the
  existing dependency to supported 1.x until an explicit migration is implemented.
- The distance ORDER BY alone did not yield an HNSW plan through the HEAD join.
  The tested vector subquery now drives the indexed event scan and checks HEAD
  membership with a correlated primary-key lookup. Lexical candidates remain
  independently bounded; historical snapshots use exact candidate selection.
- Legacy session facts get a 24-hour expiry overlay from recorded_at on reads;
  migrations never backfill or rewrite immutable historical payloads.
- Reasserting a future-effective value for now must append a visible revision;
  exact value equality does not make an invisible future event a valid no-op.
- NLI hypothesis generation must preserve preference versus use. Natural-language
  rendering cannot weaken the structured relation being verified.
- A diagnostic reader conflicted with evaluation schema cleanup. Cleanup now has
  bounded retries, always closes both connections, retains scores/history with
  cleanup error metadata, and makes the CLI exit unsuccessfully if cleanup fails.
  Future reports contain their own assertion evidence, eliminating that reader.


## September 11 quality follow-up

The [quality reliability plan](QUALITY_RELIABILITY_PLAN.md) now records the next
milestone and its unmet acceptance targets. Source review, predicate/denial fixes,
frozen external validation and controlled serial measurement are implemented;
162 tests pass. The holdout exposes remaining semantic and recall failures, so
this does not mark memory quality complete. See [the saved results](quality-v2/README.md).
