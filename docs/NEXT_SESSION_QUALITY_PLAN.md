# Next session: measure real memory coverage, then repair the largest loss

Prepared September 15, 2026. Implementation baseline: `0cdb983`.
This is a development handoff; it does not implement or approve contract changes.

## Objective and first milestone

Improve the number of source-supported, important facts that remain correctly
retrievable. Preserve the original strict scores, measure semantic equivalents
separately, and account for false writes as carefully as missed facts.

The next session's first milestone is a complete, reproducible diagnosis of the
existing 20-source development probe. After that, implement one bounded repair
chosen from the measured losses, and validate it against both positive and negative
development cases. Do not assume that canonicalization is the next repair.

The later identity and retention work below is conditional follow-on work. It is
not a requirement to rush all milestones into one session or to consume the
holdout before the pipeline is ready.

## 1. Read the authoritative context and preserve the starting point

Read in this order:

1. `PROJECT_SPEC.md`, especially §14, then `AGENTS.md`.
2. `PROJECT_STATUS.md` and `docs/QUALITY_RELIABILITY_PLAN.md`.
3. `docs/quality-v5/README.md`.
4. `docs/quality-v5/extraction-partial-dev.json`.
5. `docs/quality-v5/partial-measurement.json` and
   `docs/quality-v5/partial-source-review.json`.
6. `docs/quality-v5/handoff-evidence.json` for the rejected verifier experiment.
7. `docs/quality-v4/IDENTITY_PROPOSAL.md` before proposing storage aliases.

Do not open `docs/quality-v5/reserved-holdout.json`. Metadata in
`docs/quality-v5/reservation.json` may be checked without exposing source text.
Exclude reserved and consumed holdout contents from development searches, prompts,
reviews, example selection and tuning. Read only explicitly selected development
files; do not recursively print every JSON file under `docs` or `mnemo/data`.

Starting facts:

- `0cdb983` preserves valid members of the final extraction response after bounded
  retries, audits malformed siblings, and preserves those rejections in replay.
- The latest probe covers 20 source turns from three external development records.
  It produced 60 candidates; whole-response validation of the same final replies
  would retain 54. Three partial responses recovered six candidates while four
  malformed members remained rejected.
- The incremental gate evaluated only those six recovered candidates and stored
  five source-supported session facts. The other 54 candidates have not been
  gated or source-reviewed in this latest incremental measurement.
- Candidate-stage strict matching is 0/45 labeled truth targets and 0/24 per-turn
  must-keep targets. The 24 must-keep occurrences represent 23 distinct
  case/subject/predicate targets: the same sister's birthday cake is mentioned twice.
- One recovered write completely covers a must-keep equivalent; another only
  partially preserves its event. Do not extrapolate these results to all targets.
- The component-check verifier experiment introduced three false accepts on the
  older 103 development cases. It is excluded from production; do not restore it
  as the default based on improvements on selected actor examples.
- Last validation: 218 tests passed, zero skipped, plus lint, a clean built wheel,
  scripted eval and the live extraction/rollback demo with the installed model.
- At handoff, `AGENTS.md` was an existing untracked user file. Preserve it and any
  subsequent user changes; do not stage unrelated files automatically.

Run `git status --short` and inspect new changes before work. Store new evidence
under `docs/quality-v6/`, with distinct before/after filenames and a run manifest.
Never overwrite v2–v5 reports, source labels, reviews or their recorded hashes.
Record the starting commit, policy hash, scoring version, dataset/review hashes,
non-secret settings, model identities and run scope. Keep individual incomplete
runs and errors identifiable.

## 2. Define reviewed equivalence before altering the pipeline

### 2.1 Freeze what "strict" means

`mnemo/eval.py:assertion_key()` calls `mnemo/core.py:canonicalize()`. Adding storage
aliases would currently change newly computed strict evaluation results too.
Separate and version evaluation normalization before changing any aliases.
Preserve the existing normalization behavior as the baseline version, including
its existing narrow aliases; do not simultaneously redesign strict matching.

Keep the original dataset and label files frozen. Add a separate equivalence
review layer. Change source labels only for an identified annotation error, in a
new version with a reason and old/new hashes. If labels change, compare both
baseline and candidate implementations under the same new label version while
retaining the original scores.

### 2.2 Give each target and candidate an attributable record

Create `docs/quality-v6/mustkeep-review.json`. Cover every one of the 24 per-turn
must-keep targets, including explicit missing entries. Preserve a grouping that
maps them onto the 23 distinct case-scoped logical targets. Do not merge separate
records into one user's memory merely because their subjects are named `user`.

Each review entry should contain:

- Stable target ID: case ID, turn ID and label index; plus its logical-target ID.
- Original source text, session ID and unmodified atomic label.
- Source/report hashes and a stable candidate reference. Prefer persisted
  decision/event/fact IDs where available; otherwise use a documented,
  source-bound snapshot reference rather than guessing IDs.
- The complete candidate assertion: subject, relation, value and qualifiers.
- Coverage: `complete`, `partial`, `none`, or `ambiguous`, with a specific reason.
- A separate source-support judgment. A candidate can cover a target while also
  adding unsupported detail; that candidate must not receive complete credit.
- Later gate outcome, event lineage, current visibility and retrieval evidence.
  Leave unmeasured stages explicitly unmeasured until replay provides evidence.
- Reviewer and review version. A second independent review is preferred; if it is
  unavailable, label the judgments provisional and preserve disagreements.

Seed mappings from v5's `mapped_targets`, but check their applicability to the
exact candidate and source. Do not infer equivalence from predicate similarity,
an embedding score, or the mere presence of information inside a source quote.

Complete coverage requires the correct actor, relation, value, negation, modality,
and material event/time qualifications. A source quote containing "last week"
does not compensate for a stored assertion that lost a date required by its target.
Several candidates may jointly cover a target only when their explicit relationship
and event binding justify the composition; document every participating assertion.
Never award multiple credits for duplicate candidates covering the same target.

### 2.3 Publish separate metrics

Use the 24 per-turn occurrences for source-local extraction coverage. Use the 23
distinct case-scoped targets for the selected probe's final unique-target coverage,
after explicitly specifying which targets should be current. Later corrections
must alter current expectations while preserving historical accountability.

Report, with explicit denominators:

1. Frozen strict candidate coverage and strict store metrics.
2. Reviewed complete candidate coverage; partial and ambiguous counts separately.
3. Source-supported historical writes and explicit false/ambiguous write counts.
4. Complete coverage among facts visible under production rules at the observation
   time. A raw `memory_current` snapshot alone does not establish session/TTL access.
5. Complete coverage retrieved by fixed queries in the declared session and time.
6. The same retrieval measures in a later session and after the relevant TTL,
   once the retention experiment is implemented.

For reviewed write precision, include every write, including superseded writes.
Keep ambiguous judgments in the total denominator and report their count. Report
forbidden-label hits separately: zero forbidden hits does not prove zero false
writes. Never turn unreviewed writes into presumed supported writes.

Define queries and matching rules before inspecting returned results. Start with
fixed regression queries and add realistic question wording for product-facing
retrieval; label these query sets separately. Use `reinforce=False` when measuring.

**Acceptance:** all targets have traceable judgments; partial coverage receives no
full credit; repeats do not inflate unique-target recall; changes to production
aliases cannot silently change the frozen strict scorer. No predicted score such
as "18–21/24" is used as an acceptance target.

## 3. Run a complete baseline through the existing gate and store

Use the 60 saved v5 candidates first. Do not regenerate extraction while measuring
the baseline, since that would confound gate/store behavior with model variability.

Build a small development-only runner, tentatively
`scripts/replay_extraction_probe.py` (this file does not exist yet). Reuse
`_MaterializedExtractor`, `ExtractionBatch`, `evaluate()` and the existing
development manifest loader rather than creating a second storage path.

Requirements:

- Validate source and label fingerprints against `mnemo/data/quality-v4/manifest.json`
  using only its `dev` records. Preserve the loader's source order, scope and exact
  session IDs. Run each external record in its own isolated evaluation scope.
- Replay all 60 final survivors and retain all four malformed rejection envelopes.
  Preserve failed turns as failures if a later input contains them; keep their
  targets in the appropriate denominators.
- Save every gate decision, historical write, current assertion and retrieval
  probe before the evaluator cleans up its disposable schemas.
- The current decision export omits decision/event/fact IDs. Extend diagnostic
  snapshots with existing IDs and temporal/session fields if required for review;
  no database schema change is necessary for exporting existing fields.
- Save baseline reports to new files, with their exact selected-turn scope. The
  existing `docs/quality-v5/measure_partial.py` is incremental-only and cannot serve
  as this full 60-candidate baseline without a scope change.
- Review every stored write and every missing target, not only exact mismatches
  or previously known failures.
- Preserve both arms when they received the same complete candidate input. Where
  saved evidence lacks one arm's inputs, publish only the defensible comparison.

The selected 20-turn replay measures a diagnostic subset. It cannot reveal every
overwrite caused by omitted turns. Before claiming conversation-wide improvement,
expand to the complete three development conversations (140 turns), with the same
model/policy and independently preserved outputs. Capture complete source-bound
candidate batches so later gate experiments can replay identical inputs. Do not
silently splice old-policy and new-policy candidates into one baseline.

**Acceptance:** a complete selected-probe baseline exists with reviewed stored
precision, historical support, current equivalent coverage and fixed-query
retrieval. Reports declare subset scope, preserve errors and have successful
schema cleanup. No independent validation claim is made.

## 4. Attribute losses and select one repair

Create `docs/quality-v6/loss-attribution.json`, linking every missing target to the
earliest stage that lost it, with later contributing failures recorded separately:

- Extraction omission or malformed source evidence.
- Wrong actor, relation/type, value, modality, or missing event/time binding.
- Supported assertion rejected by representation checks or semantic verification.
- Accepted fact overwritten by an unrelated fact sharing its identity.
- Accepted fact inaccessible because of session scope, TTL or world validity.
- Visible fact missed by retrieval.
- Annotation ambiguity or a strict wording mismatch with complete equivalent memory.

Rank repair candidates by affected distinct must-keep targets and verified false
writes, not by how easy it is to raise an aggregate score. Keep source-specific
review disagreements visible.

### Candidate repair A: extraction fidelity

Use development failures to test wrong actor assignment, lost dates and event
bindings, modality strengthening, and malformed quotes. Preserve valid siblings
and full-turn verification. If testing an alternate representation or evidence
selection method, retain the original source and model reply and measure both
coverage and false assertions. Do not silently rewrite invented quotes to pass
validation, drop material qualifiers, or insert label-preferred predicate names.

General meaning-preservation instructions may be tested on development examples;
prompting the extractor to mimic this dataset's label wording is not a quality fix.

### Candidate repair B: verifier relation/event confusion

A concrete v5 case states that the user baked cookies with convection and has not
roasted vegetables with convection. The verifier incorrectly treats the first
activity as contradicting the denial of the second. Build paired development
cases that vary activity, actor, timing and negation. Include genuine contradictions
as negative controls and source-supported assertions as positive controls.

Use this case as a regression anchor, not a predicate-specific "cookies" patch.
Another known failure class assigns Priya's actions to the user; include actor-
repaired pairs if the new baseline confirms it remains material.

Keep the original verifier as a baseline. Test one change at a time. If researching
an alternative verifier/extraction technique, tie the research to the measured
failure, consult primary sources, and require a local comparison before adoption.
Do not assume that enabling model reasoning or adding component-check JSON fixes
semantics; the prior thread already observed regressions and a stalled diagnostic.

### Adoption checks for either repair

- Reproduce the failure before changing implementation.
- Use meaningful deterministic regressions for the mechanism and a real-model
  before/after development comparison for semantic behavior.
- Run the older 103 verifier cases plus the new targeted cases; report groups
  separately. Reconstruct missing case adapters from tracked development evidence
  and verify their contents rather than claiming absent v5 files exist.
- No new false accepts on established negative controls and no new unsupported
  writes in the evaluated pipeline sample. Existing critical false writes remain
  defects even if their count does not increase.
- Check recall non-regression on supported controls and current must-keep memory.
  Investigate changed verdicts if model variability could explain the result;
  preserve planned repeats rather than choosing the most favorable run.
- Demonstrate a real candidate, stored-memory or retrieval improvement at its
  measured stage. A metric-only reclassification must be reported as such.

**Acceptance:** one bounded mechanism is repaired and measured without the tested
precision/recall regressions. If it fails this check, preserve it as experimental
evidence and keep it out of the production default.

## 5. Treat aliases and identity as a separate decision

Do not implement broad canonicalization merely because an equivalence mapping
exists. A source-specific paraphrase match is not a universal predicate synonym.

Do not globally merge:

- `owns_appliance`, `uses_appliance`, `acquired_appliance`.
- `favorite_food` and `favorite_fruit`.
- `baked_chocolate_cake` and `baked_for_sister_birthday`.
- `learned_to_make` and `learned_fermented_foods`.

These differ in relation, scope, temporal meaning, or concurrent-value behavior.
Genuine spelling/wording aliases can be proposed only with complete-assertion
equivalence and compatible identity/cardinality. Even a true synonym needs an
existing-data collision review before formerly separate HEAD slots share a key.

Create a proposal recording definitions, allowed aliases, counterexamples,
expected cardinality, existing affected fact IDs and conflict handling. Distinct
classes, dishes, appliances and interviews may require collection/occurrence
identity even when their predicates look attribute-like.

If the baseline confirms substantial overwrite losses, prepare a concrete SQL/API
diff implementing the reviewed attribute/collection-member/occurrence proposal.
Include legacy behavior, migration/backfill boundaries, concurrency, ambiguous
references, archive/revert and existing alias collisions. Prepare this reviewable
design before requesting approval; do not enable the contract change beforehand.

The identity proposal also requires independent label adjudication before choosing
and evaluating its resolver. Another review by the same agent is not independent;
record whether that prerequisite has actually been met.

The governing instruction is `AGENTS.md` rule 6: "Confirm before adding a
dependency, changing the §3 schema, or altering §4/§5 semantics." Such changes can
require review even without a SQL migration. This plan does not provide that
approval. Continue independent evaluation work while a concrete proposal awaits
the necessary user decision.

Identity acceptance examples include:

- A second workshop preserves the earlier Indian cuisine class.
- Two interviews on different dates coexist; a correction updates only its event.
- A budget correction replaces the current value while keeping immutable history.
- Concurrent duplicate observations do not multiply current facts.
- Revert and archive affect only the intended occurrence/member and preserve scope.

## 6. Measure retention before recalibrating tiers

Inspect the actual importance, specificity, novelty and transient components for
supported writes. Alias normalization in `core.canonicalize()` changes identity;
it does not automatically change the predicate passed to `specificity()`.

With current weights, changing specificity from 0.2 to 1.0 adds 0.24. Holding other
inputs fixed, the five v5 recovered writes would score approximately 0.583–0.690,
all below the 0.70 durable cutoff. Treat any vocabulary expansion that changes
tiering as a policy intervention, with its own comparison and review as required.

Measure the unchanged baseline in at least three situations:

1. Immediately after reconciliation, in the originating session.
2. In a different session in the same authorized namespace/user/agent scope.
3. Just before and after the relevant TTL boundary, with explicit observation time.

Avoid wall-clock sleeps and changes to immutable event payloads. Prefer a controlled
clock or deliberately timestamped test data. Clearly distinguish current-read
experiments from historical `as_of` searches, which follow a different contract.
Use the normal visibility/retrieval paths and disable reinforcement during probes.
Raw unreconciled cache hits must not count as durable semantic-memory success.

Only propose cutoff/weight changes when measured retention losses justify them.
Compare frozen settings on development data, measuring retained important facts,
unsupported durable writes, duplicate/collision behavior, storage and model usage.
Do not optimize the durable fraction as an end in itself or promote an assertion
whose meaning is wrong. Unknown costs remain unknown without supplied prices.

## 7. Freeze and use the reserved holdout only after development is ready

`b46e15ed` is the reserved, unopened 48-turn source. The earlier 42-, 46- and
48-turn validation sources are consumed and cannot serve as fresh validation.

Before opening the reserved source:

- Freeze extraction, verifier, identity/alias policy, scoring/tiering, matching
  rules, evaluation code, model settings, retrieval query-generation protocol and success
  criteria. Hash the relevant implementation and scoring/review versions.
- Define annotation instructions on development data. Obtain independent
  adjudication where possible; otherwise explicitly retain provisional status.
- Finish required development checks and resolve the known failures relevant to
  the intended quality claim. The next session does not need to reach this step.

Then label the reserved source without seeing the candidate system's outputs,
freeze labels, and execute the predeclared validation protocol once. If stochastic
repetitions are part of that protocol, specify them before any output is inspected
and report them all. Review source support using the frozen rubric without tuning
the evaluated implementation afterward.

Publish strict and equivalent coverage, source-reviewed false writes, current and
later-session retrieval, errors, latency/usage and explicit limitations. A failed
result is a failed result; mark the holdout consumed, and reserve a new disjoint
source before another development cycle. Success on one small holdout alone does
not establish broad production reliability.

## 8. Commands and validation discipline

Use the installed local model rather than the unavailable `llama3.2:3b` default.
Check local model availability at session start; do not install a new dependency
or switch to an external paid backend without the applicable authorization.

Infrastructure and regression checks, kept separate from real-verifier settings:

```sh
docker compose ps
MNEMO_REQUIRE_DB=1 MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx \
  MNEMO_VERIFIER_BACKEND=heuristic MNEMO_VERIFIER_FALLBACK_BACKEND=none \
  HF_HUB_OFFLINE=1 .venv/bin/pytest -q -rs
make lint
MNEMO_VERIFIER_BACKEND=heuristic MNEMO_VERIFIER_FALLBACK_BACKEND=none make eval
MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx \
  MNEMO_VERIFIER_BACKEND=heuristic MNEMO_VERIFIER_FALLBACK_BACKEND=none make demo
```

These scripted/heuristic checks are not real-model semantic accuracy measurements.
Use `make up` if the existing project Postgres service needs starting. Investigate
missing required services instead of treating skipped tests as a completed check.

The existing command below runs the complete external **development** suite with
real extraction/verifier settings. Run it when reaching the full-conversation
baseline or validation step, after creating the output directory. It does not
replace the saved-candidate runner described in step 3.

```sh
MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx \
  MNEMO_VERIFIER_BACKEND=cross_encoder \
  MNEMO_VERIFIER_FALLBACK_BACKEND=ollama HF_HUB_OFFLINE=1 \
  .venv/bin/python -m mnemo.eval_suite \
  --manifest mnemo/data/quality-v4/manifest.json --split dev \
  --output docs/quality-v6/external-dev-baseline.json
```

Use new filenames for changed policies. Resume only when the tool confirms that
policy/data hashes match and cleanup succeeded. Avoid concurrent local model runs
when collecting comparative latency; correctness runs with concurrent load must
not be presented as controlled performance measurements.

Tests for new evaluation code must cover duplicate target mentions, partial and
ambiguous matches, wrong actor/modality/date, fabricated evidence, immutable strict
normalization, malformed rejection replay, superseded versus current facts,
session/TTL filtering and errors remaining in denominators. Select the subset
appropriate to the actual change, then run required project checks. Rebuild and
check a clean wheel when package/runtime files change:

```sh
uv build --wheel
bash scripts/check-wheel.sh
```

Commit each coherent validated milestone separately. Update `PROJECT_STATUS.md`
with measured results and unresolved limits. Do not claim reliable memory merely
because tests pass or equivalent matching raises the score.

## 9. Required handoff at the end of the next session

Provide:

- The reviewed target manifest and scoring specification, with 24 occurrence and
  23 unique-target accounting clearly separated.
- A complete selected-probe baseline, all write judgments and loss attribution.
- The chosen repair, before/after evidence, regression results and exact commit.
  If no repair passed adoption checks, state that and retain the experiment.
- The next measured bottleneck and any concrete contract proposal awaiting review.
- Full-conversation and cross-session results only if actually executed; otherwise
  identify them as outstanding.
- An explicit holdout status, plus source/policy/review hashes and report paths.

## Copy-paste prompt for the next session

> Continue Mnemo from its current checkout. Read PROJECT_SPEC.md first, then
> AGENTS.md, PROJECT_STATUS.md and docs/NEXT_SESSION_QUALITY_PLAN.md. Preserve the
> batch-recovery fix from 0cdb983 and all frozen evidence. Implement the
> development equivalence review/scoring and complete saved-candidate baseline
> described in steps 2–3, then use the measured losses to select and validate one
> bounded extraction or verifier repair. Report candidate, historical, current
> and retrieved coverage separately, alongside false writes. Do not assume
> canonicalization is the fix or rewrite labels to match model wording. Keep
> b46e15ed sealed. Prepare concrete proposals before any dependency, schema or
> write/read-contract change that needs approval. Continue ordinary debugging and
> required validation autonomously; commit coherent validated milestones and
> update the status with results and remaining limitations.
