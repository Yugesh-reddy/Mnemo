# Memory quality reliability: implementation and acceptance plan

September 11, 2026. This follows the infrastructure milestone in
[CORRECTNESS_PLAN.md](CORRECTNESS_PLAN.md). The goal remains reliable memory
quality; passing infrastructure tests does not meet that acceptance target.
Executed measurements and source-review qualifications live in
[quality-v3/README.md](quality-v3/README.md); the prior cycle remains in
[quality-v2/README.md](quality-v2/README.md).

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
