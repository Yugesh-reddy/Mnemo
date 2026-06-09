# Mnemo implementation plan

Date: 2026-09-06; execution resumed 2026-09-08 with GPT-5.6 Sol subagents. Scope:
implement the architecture review and improvement roadmap
authorized in this conversation. `PROJECT_SPEC.md` remains the product map; this
plan records the concrete refinements needed to deliver its promises.

## Scope and contract decisions

- Keep PostgreSQL, readable SQL migrations, append-only fact events, MCP and the
  Python SDK. Existing event payloads are never rewritten. Supersession and recall
  counters remain the only mutable event bookkeeping.
- Ship additive migrations for retention expiry (separate from real-world
  `valid_to`), event session identity, job leases/retries/errors, scoped observation
  idempotency and an append-only quality-decision audit. Add reflection provenance
  only for the final consolidation milestone.
- Apply namespace/user/agent scope consistently to every public operation.
  Session memories require the matching session and expire after a configurable
  TTL. Durable memories remain available within the store scope. Historical reads
  distinguish recorded state from real-world validity and retention expiry.
- Exact or normalized equivalent values can deduplicate. Cosine similarity finds
  comparison candidates; it cannot by itself discard a changed number/date/value
  or merge unrelated subjects/predicates.
- The worker treats extraction output as untrusted. Assistant turns cannot create
  user facts. Model-supplied assertion labels cannot promote provenance or trust.
  A verifier evaluates the full assertion against source evidence, with typed
  entailment/contradiction/neutral verdicts and recorded reasons.
- Add required UI runtime dependencies and optional local NLI dependencies. Model
  downloads and real-model evaluations are explicit commands, separate from the
  deterministic regression suite. Never report mocked results as real-model data.
- Consolidation is built last, remains opt-in and requires measured evaluation
  evidence before automatic scheduling. Trust is medium at most and never above
  the weakest source. Preserve all source events and verify against their evidence.
- Keep schema/dependency/semantic refinements small and documented. The user's
  instruction to implement the reviewed roadmap authorizes these changes; avoid
  introducing unrelated services or product scope.

## M1 — Foundation integrity

1. Lock each fact before reading/changing HEAD; handle new-key concurrency safely.
2. Validate rollback target ownership and scope. Preserve embedding, JSON payload,
   source lineage and quality metadata across archive/revert transitions.
3. Make archival atomic and check the selected event is still HEAD before acting.
4. Scope blame/log/revert/invalidate/reinforce/diff, and preserve stable checkpoints
   under concurrent writes. Enforce payload immutability in SQL with documented
   exceptions for bookkeeping.
5. Add true two-connection races, cross-scope/foreign-event failures, semantic-only
   recall after rollback, JSON archival and failure-rollback regressions.

Acceptance: lifecycle and diff tests pass on real Postgres; racing updates produce
one coherent HEAD; no historical payload mutation occurs.

## M2 — Evaluation before gate changes

1. Move deterministic evaluation support into the installed package; remove
   imports of test fixtures from application modules.
2. Keep the 18-turn smoke fixture. Add a versioned, labeled approximately 200-turn
   corpus with development/held-out splits, critical facts and forbidden writes.
3. Inject conversations, extractors, verifiers and embeddings. Use identical
   extracted candidates for the naive/gated comparison, plus a complete pipeline
   mode for extraction quality.
4. Report precision/recall/F1, must-keep recall, current false facts, false writes
   over time, visible facts, total events/storage, processing latency and model
   usage/cost when available. Record data/model/config fingerprints.
5. Add a LongMemEval importer requiring explicit atomic-fact labels and preserving
   dates/session/source identifiers; document reproducible real-backend commands.
6. Use isolated evaluation databases/schemas and prevent evaluation/test cleanup
   from touching an application database. CI requires its database and fails if
   the required integration suite cannot run.

Acceptance: old regression remains interpretable; larger benchmark is reproducible
and never hides an earlier false write merely because it was later overwritten.

## M3 — Verification, extraction and reliable jobs

1. Add a typed full-assertion verifier, clause-aware prefilter, optional local NLI
   backend and bounded model fallback. Test unsupported objects, wrong relations,
   subject confusion, multiple negations, factual clauses beside hypotheticals,
   canonical values, sarcasm and malformed model responses.
2. Enforce source-role/provenance policy in the worker. Preserve high-importance
   unfamiliar facts through demotion; score accepted corrections as useful updates.
3. Introduce scoped, idempotent observation, leased job ownership, heartbeats,
   bounded exponential retry, recorded errors and atomic completion/reconciliation.
4. Add a worker CLI, graceful cancellation/shutdown, and an MCP lifecycle that
   actually processes observations. Network/model calls stay outside write locks.
5. Record candidate decisions (including rejection), source IDs, verdicts, score
   components and verifier/config versions in the append-only audit.

Acceptance: no assistant-derived false user fact or forged high-trust provenance;
slow/crashed workers cannot double-commit or overwrite a newer owner's work;
external MCP observe becomes semantic memory.

## M4 — Session/temporal behavior, retrieval and decay

1. Implement retention expiry and session-aware reads. Enforce `valid_from` and
   `valid_to`; implement as-of and historical-event search with explicit semantics.
2. Select bounded vector candidates using the distance-order/limit index shape,
   union lexical candidates, rerank and limit only after merging both tiers.
3. Use one consistent snapshot for semantic/cache selection and stable source
   deduplication. Test a busy cache, more than k existing hits, multiple facts per
   turn and reconciliation concurrent with search.
4. Make archive/expiry transitions visible in diffs. Preserve restore behavior for
   expired/session facts and make decay scheduling observable.
5. Distinguish diagnostic searches from reinforcing recall; keep normal SDK recall
   behavior compatible while UI browsing can opt out.

Acceptance: no cross-session ephemeral context, no future-valid fact at present,
stable historical reads, no cache handoff gap, and reversible archival.

## M5 — Distribution, observability and demos

1. Package migrations, evaluation data and web templates. Declare UI runtime
   dependencies and console entry points; build/install a wheel in a clean venv.
2. Validate model/dimension compatibility at startup; support dimension selection
   for fresh databases and require explicit migration for existing embeddings.
3. Expose queue lag, retries/failures and quality decisions in minimal UI/CLI views.
4. Add true MCP stdio tests including lifecycle/pool shutdown. Add CI Postgres,
   lint/format, deterministic evaluation and wheel smoke checks.
5. Measure candidate query plans against growing event history and record practical
   limits without claiming performance from tiny unit fixtures.
6. Update README, examples, configuration guidance and PROJECT_STATUS with actual
   commands/results; run both evaluation and rollback demos.

Acceptance: documented clean install and commands work; required DB tests run;
operators can inspect missing-memory decisions and failed extraction.

## M6 — Trust-safe consolidation, last

1. Add reflection provenance and complete source event lineage.
2. Provide pluggable synthesis with minimum cluster size, an explicit evaluation
   benefit prerequisite and an opt-in execution command.
3. Verify every proposed fact against all source evidence; cap trust at medium and
   the weakest source; reject invented or unsupported synthesis.
4. Recheck source HEADs before committing. Archive replaced source facts by new
   events only, and support source-level blame and reversible consolidation.
5. Evaluate bloat reduction, must-keep recall and false facts before enabling a
   scheduled policy; leave automatic consolidation disabled without that evidence.

Acceptance: failed or stale synthesis changes nothing; no trust laundering or
lost lineage; measured benefit and rollback are reproducible.

## Execution and validation

- Parallel ownership: core/store/retrieval; evaluation; verifier. The coordinating
  agent owns migrations, worker integration, config, packaging, UI and final
  integration. Reassign completed subagents to bounded follow-up tasks.
- Each agent runs database tests against its own named disposable database to
  avoid fixture teardown interfering with another agent's tests.
- Integrate in milestone order. Commit focused milestones once their relevant
  tests pass. Preserve existing untracked AGENTS.md and brand assets.
- Final checks: full required Postgres suite, lint/format, deterministic and
  larger-corpus evaluation, external MCP lifecycle, wheel installation, UI
  search → blame → revert, and documented live-backend results or explicit limits.

## Progress

- [x] Plan and contract refinements documented.
- [ ] M1 foundation integrity.
- [ ] M2 evaluation.
- [ ] M3 verification and worker reliability.
- [ ] M4 tiering, retrieval and decay.
- [ ] M5 distribution, observability and demos.
- [ ] M6 gated consolidation.
- [ ] Final integration and verified status report.
