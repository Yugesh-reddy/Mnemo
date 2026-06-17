# Memory quality reliability: implementation and acceptance plan

Updated September 18, 2026. This follows the infrastructure milestone in
[CORRECTNESS_PLAN.md](CORRECTNESS_PLAN.md). The goal remains reliable memory
quality; passing infrastructure tests does not meet that acceptance target.
The latest rejected experiment and source-review qualifications live in
[quality-v7/README.md](quality-v7/README.md); the complete baseline and rejected span
experiment are in [quality-v6/README.md](quality-v6/README.md); partial recovery is in
[quality-v5/README.md](quality-v5/README.md); the preceding extraction cycle is in
[quality-v4/README.md](quality-v4/README.md); prior cycles remain in
[quality-v3/README.md](quality-v3/README.md) and
[quality-v2/README.md](quality-v2/README.md).

The current selected-development baseline covers all 60 saved candidates and 51
historical writes. Reviewed complete coverage is 16/24 occurrences and 16/23
distinct targets, with all 16 returned by fixed originating-session queries.
Historical support is 50 supported, zero unsupported, one ambiguous; reviews are
provisional. Frozen strict matching remains zero. The source-span experiment is
complete and rejected: 17/24 complete source occurrences, but only 14/23 complete
retrieved targets, with 56 supported, zero unsupported and one ambiguous write.
Production extraction is restored; verifier policy never changed. The same v2
scorer credits reviewed downstream alternatives in both arms and keeps the
baseline at 16/23. All 103 older verifier decisions are reproduced and eight
targeted controls pass. Full conversations and later-session/TTL retention remain
outside this subset result; the reserved holdout stays sealed.

The single authorized v7 retry-feedback experiment is also complete and rejected.
Exact source-prefix hints recovered no candidates. Complete source occurrences
remained 16/24 and distinct candidate/history targets 16/23, but current, visible
and retrieved targets fell to 15/23. All 49 gated historical writes were reviewed:
46 supported, zero unsupported, three ambiguous. The solo Data Mining project was
overwritten by a less specific assertion sharing its subject/predicate identity.
Four first-pass outputs varied before feedback, so causation of that variance is
unmeasured. The frozen >=17/23, no-loss and no-increased-ambiguity criteria failed.
Production is restored byte-for-byte; the patch and evidence are preserved. Both
ambiguous targets remain unresolved, strict coverage stays 0/24 and `b46e15ed`
remains sealed. No second implementation or prompt iteration is authorized by this
completed failure. Restored offline validation passes 231 tests with three
live-model tests deliberately deselected, plus Ruff/Black. No infrastructure
failure occurred; independent code review was unavailable due to its usage limit.

The next bottlenecks are extraction binding/meaning and subject/predicate identity
collisions. The [concrete identity proposal](quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md)
is unapproved and includes remaining resolver, label-review and rollout work;
no SQL migration or write/read contract change has been enabled.

## 1. Establish attributable labels before policy changes

Completed: preserve the original reports; bind reviews to report and dataset
hashes; source-review all 38 unmatched writes, the explicit emergency-contact
false write, and all eight missing must-keep targets. Separate unsupported
relations, unsafe encodings, equivalent wording, lossy identities, source
ambiguity and label errors. Do not silently replace the historical scores.

Completed: deterministically select three additional development conversations
(140 turns) and reserve a disjoint fourth conversation (42 turns). Freeze source
and label hashes. Label development sources before changing the prompt. Freeze
the final code/settings policy before labeling and evaluating the fresh holdout.

Acceptance limitation: this is one Codex source review, not independent human
adjudication. The expanded external corpus totals 218 turns across five records,
including the earlier 36-turn record. It does not satisfy the separate target of
one naturalistic 200-turn conversation. A subsequent 46-turn holdout was reserved
before v5.1 policy work and labeled only after its policy freeze. Labels are provisional and are not an
exhaustive catalog of every entailed paraphrase, plan or compound fact.

## 2. Repair extraction fidelity and denied-value overwrites

Implemented and regression tested: preserve use versus preference, database type,
actor direction, unfamiliar relation names and actual correction values. Ask for
relevant completed events as well as ongoing facts. Reject null and negative
placeholder values before they overwrite a positive identity. Keep rejection
evidence in the append-only decision log.

Acceptance: the emergency-contact denial cannot replace Noah Chen with Alex Chen;
assistant content cannot become user evidence; editor/language use cannot become
database preference. Focused regressions pass. Development model runs must still
demonstrate recall preservation; the current results do not meet that requirement.

## 3. Validate complete-assertion verification on development evidence

Implemented: preserve database and role semantics in NLI hypotheses and route
matching denied clauses through bounded fallback despite high confidence.
Compare the previous and revised policies on 90 fixed development assertions.
Numeric thresholds and write-score weights remain unchanged. Record accepted and
rejected assertions, including high-confidence mistakes, instead of relying on
confidence alone.

Acceptance: rerun both historical false writes and must-keep coverage. A small
verifier-only improvement is insufficient to claim pipeline improvement. The
v5.1 development probe covers 103 assertions: false accepts fall from seven to
zero and false rejects from seven to three relative to cached v4 verdicts. Unknown
relations now use structured fallback, which excludes extractor confidence and
claimed authority. Role direction, type and tentative-plan regressions pass.
The full external pipeline still produces six source-reviewed false assertions
among 47 writes, including an oven setting as editor use and a restaurant as food.
Its 23 exact must-keep targets all miss; source review attributes four to equivalent
wording, two to compound labels and the remainder to extraction errors or omissions.

## 4. Freeze, validate, and preserve the holdout

Implemented: bind the policy fingerprint to code and non-secret runtime settings;
verify source/label fingerprints; require the frozen policy for holdout execution;
save each completed conversation; reject resumed reports from different policies,
data or unresolved cleanup failures. Both comparison arms receive the same real
extracted candidates in replay mode. Count superseded writes against their source.

Acceptance: publish strict final/history precision and recall, explicit forbidden
writes, must-keep coverage, all unmatched assertions and source-review limitations.
Record retrieval probes without reinforcing memory. A target absent from current
memory is an upstream/label issue; a present target missing from search requires
retrieval/session investigation. Do not tune against the held-out outputs. After
the run, this holdout is consumed validation data and cannot serve as fresh data
for another development cycle.

## 5. Measure operations and validate installation

Implemented: serial closed-loop workload with one outstanding observation and
one worker; fixed warmup; request-level observe/processing/total latency; nearest
rank p50/p95; throughput; model usage; explicit API and local compute rates.
Run it after other model work finishes. Unknown usage or prices stay unknown.
This measures serial service time, not concurrent saturation or production SLOs.

Acceptance: required-Postgres suite, lint and a clean built wheel pass. The wheel
must contain the new data and expose the audit, suite and benchmark commands.
Record all allocated bytes including evidence, queue, decisions and indexes; do
not infer total-storage savings from fewer semantic events.

Remote acceptance remains pending: configure `correctness` as a required merge
check on the intended repository/branch and observe a successful remote run.
This checkout has no remote. The same-named repository found in the authenticated
account contains unrelated DSA submissions and must not be changed. The intended
repository/branch and actual compute prices have been requested from the user.

## 6. Next development cycle, driven by the recorded failures

1. Independently review the provisional atomic labels and equivalence judgments.
   Define canonical relation aliases and compound-fact matching using development
   sources before rerunning scores. Keep original strict metrics separately.
2. Fix extraction coverage using the recorded development failures: Python/R
   become editors; strawberry preference becomes allergy; class attendance and
   kimchi-making experience are omitted; dates attach to the wrong bread attempt.
   Generic verb rendering and structured fallback are implemented, but a
   predicate-specific guard alone cannot cover arbitrary wrong relation names.
   Evaluate a relation-faithful candidate representation and source-span binding
   before changing thresholds. Preserve useful role/scope qualifiers rather than
   generating stronger assertions that the verifier must reject. Add the frequency
   versus preference case (listening most often to jazz) to the development audit.
3. Preserve identity across corrections (`meeting_free_day` must not become
   `free_day`) and distinguish simultaneously true values from replacements.
   Trace Python/R, naan/saag paneer and sauerkraut/kimchi losses. Prepare a concrete
   identity proposal against the existing one-HEAD-per-subject/predicate contract
   before changing storage semantics; include rollback and session implications.
4. Evaluate long-term retention separately: most unfamiliar relations are demoted
   to session tier. Test later-session must-keep access and expiry deliberately;
   final same-session probes alone cannot establish durable recall.
5. Add the separately requested naturalistic single 200-turn conversation. The
   upstream oracle file has no 180–220-turn record; use an appropriately attributed
   larger-source conversation or clearly label a project-authored naturalistic
   fixture. Do not relabel a collection of short records as one long conversation.
   Reserve another unopened holdout before the next policy cycle. Require
   source-reviewed false-write accounting and must-keep non-regression before
   calling memory quality reliable.

Consolidation remains deferred. Reflection requires measured benefit, complete
source lineage, verification against those sources and source-capped trust.

## 7. Evidence-grounded extraction cycle

Implemented and committed as 3aaaed1: evidence-first extraction with source-checked
quotes, per-candidate trusted offsets, full-turn verification, nonempty values and
bounded corrective retries. The v6.2 development probe still has three failed
source turns covering four must-keep targets. Source review finds 15 complete
candidate targets versus four in the saved v5.1 baseline; strict matching stays
zero. This is a diagnostic improvement, not evidence of reliable stored memory.

Implemented: the evaluator saves failed-turn errors while retaining every target
in the recall denominator, and rejects conflicting replay candidates for repeated
text. Required-Postgres tests pass with zero skips.

One continuous 200-turn fictional project conversation is authored and labeled
before model output. It supplies ten sessions and explicit corrections, permissions,
relation types, dates and context-dependent references. It remains provisional
same-author development data. The complete run scores 200 turns with five failed
extractions retained. All 145 writes and 30 strict must-keep misses are reviewed.
Strict must-keep is 2/32; 16 misses have retrieved equivalents, while actor, event,
verification and session losses remain. Source review finds 13 unsupported writes.

Policy v6.2 is frozen after the extraction development probe and tests. The next
disjoint 48-turn external holdout, e3038f8c, was labeled after that freeze and before
its model outputs. Its complete frozen result has 0/9 exact must-keep and one failed
extraction. All 25 writes and nine misses are source-reviewed: four misses have
retrieved equivalents; one write is unsupported and one ambiguous. These outputs
did not change policy. The holdout is consumed; reserve another before further
policy development. All original reports remain available.

A concrete [identity/cardinality design](quality-v4/IDENTITY_PROPOSAL.md) is prepared
for review before any schema or one-HEAD contract change. Consolidation remains last.


Commit `703d028` repairs controlled measurement: failed extractions are retried by
the real worker through terminal status, and failed/warmup observations keep their
usage and timings. Separate successful-only metrics prevent failures from being
presented as successful capacity. The required-Postgres suite now passes 200 tests
with zero skips, including failed-job, all-failed and warmup regressions. The
controlled 25-observation run completes all jobs: p50 6.29 s / p95 7.45 s end-to-end,
22.7 / 31.4 ms observe-only, 0.1575 observations/s. It measures simple fixture facts
under serial load, not concurrent capacity. Explicit prices remain unavailable.

Next bounded development priorities, from the completed source review:

1. Preserve actors and event references before verification; third-party actions
   must not become user actions merely because the turn was user-authored.
2. Verify the asserted modality: schedules do not require completed interviews,
   requests do not require completed actions, and past actions do not require
   permanent present state.
3. Recover valid batch members after a different candidate exhausts evidence
   validation; measure recall and false writes before enabling that behavior.
4. Review stable correction identities and cross-session availability alongside
   the prepared contract proposal. Do not use embedding similarity as proof of
   equivalence or silently merge distinct events.

## 8. Partial batch recovery

Implemented September 15: after the existing bounded retries, recover validated
members of the final extraction response and audit each malformed sibling. Never
combine attempts or revive an earlier response after final JSON/provider failure.
The normal verifier and scoring still apply, and replay/report tooling preserves
raw rejections without awarding candidate coverage. No schema, dependency,
identity contract or numeric threshold changes were needed.

The 20-source development rerun retains 60 candidates versus 54 when the identical
final replies are parsed with whole-response rejection. Three partial batches
recover six candidates; four malformed members remain rejected. The incremental
gate stores five source-supported session facts and falsely rejects one supported
denial. Source review covers all six recovered candidates and all five writes.
Strict must-keep remains 0/24; one recovered write is an equivalent must-keep and
another only partially preserves its event. This fixes batch loss, without meeting
the broader quality acceptance target or demonstrating durable recall.

Validation passes 218 required-Postgres tests with zero skips using the installed
model, lint/format, a clean built wheel, `make eval`, and the rollback demo with
the available model override. Full evidence and reproduction commands are in
[quality-v5](quality-v5/README.md). The linked thread's failed verifier experiment
is recorded separately and excluded from production. The original next holdout,
`b46e15ed`, is restored and remains unopened, unlabeled and unevaluated.
