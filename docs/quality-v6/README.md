# Reviewed development baseline and rejected span-selection experiment

Started September 17, 2026 at `bca6566`. This cycle follows
[the accepted handoff](../NEXT_SESSION_QUALITY_PLAN.md). Frozen v2–v5 evidence and
labels remain unchanged; reserved `b46e15ed` remains unopened. No contract,
dependency, storage identity, tiering or threshold change is part of this cycle.

## Outcome, September 18

**Do not enable the span-selection experiment.** It improves source-local
coverage by one occurrence but reduces complete retrieved targets from **16/23
to 14/23**. Production extraction is restored byte-for-byte to `681a638`; the
verifier never changed. Evaluation fixes and the complete evidence remain.

| Measurement | Saved baseline | Span experiment |
|---|---:|---:|
| Sources / candidate survivors / raw rejections | 20 / 60 / 4 | 20 / 66 / 2 |
| Strict candidate must-keep coverage | 0/24 | 0/24 |
| Reviewed complete source-local occurrences | 16/24 | 17/24 |
| Complete distinct candidate / historical targets | 16/23 / 16/23 | 16/23 / 16/23 |
| Complete current / visible / retrieved targets | 16/23 each | 14/23 each |
| Historical writes: supported / unsupported / ambiguous | 50 / 0 / 1 | 56 / 0 / 1 |
| Supported-write precision, ambiguity in denominator | 50/51 (98.04%) | 56/57 (98.25%) |

The comparison uses [baseline scoring v2](baseline-score-v2.json) and
[validated experimental scoring](span-score-validated.json). The extractor
probe's embedded `before` rows are older v3 diagnostics, **not** the v5 baseline
in this table. [The experiment manifest](span-experiment-manifest.json) records
the actual inputs, hashes, model, decision and preserved patch.

### Why the attempted repair fails adoption

Numbered spans recover the plant-based goal and the earlier birthday-cake
occurrence. The latter repeats an already-covered logical target. The new
baguette assertion weakens completed baking into `used_flour_for`, losing one
previously complete target. First-time convection use still loses "first", and
the restaurant assertion selects an out-of-range ID and is rejected. The other
invalid ID concerns a non-must-keep feature-engineering assertion. IDs prevent
quote fabrication when valid; they do not prevent omissions or semantic weakening.

Two additional complete targets disappear after accepted writes: the Data Mining
project qualification and the sauerkraut/kimchi learning assertion. The same
subject/predicate slots receive later values. The immutable histories retain
them, but current retrieval does not. No source retracts the learned foods;
whether the two project mentions identify one project is unresolved. The
[identity follow-up proposal](IDENTITY_FOLLOWUP_PROPOSAL.md) records exact event
links, an SQL/API sketch, legacy boundaries and remaining design prerequisites.
It is unapproved and was not applied.

The frozen adoption criterion required at least baseline retrieved coverage and
no new unsupported writes. Passing the false-write check cannot offset this
recall regression. This is one regenerated development run against saved outputs;
it does not isolate every effect of model variability or prove generalization.
No further prompt tuning was performed after this outcome.

### Review and scoring corrections

The [primary review](span-mustkeep-review.json), [independent review](span-source-review.json)
and [adjudication](span-adjudicated-targets.json) are preserved separately. The
conservative final source-local tally is 17 complete, five partial, one absent,
and one annotation-ambiguous occurrence. All 66 candidates were source-reviewed:
63 supported and three ambiguous. The gate wrote one of those ambiguous assertions,
an unqualified starter ratio. Human gold adjudication remains outstanding.

The first stage score, retained as `span-score.json`, undercounted two complete
equivalents by tracking only one chosen candidate per target. An accepted poster
assertion also covers the research topic; a later explicit vegan-class attendance
assertion covers the same undated target. A separate [source-only alternative
review](span-alternative-review.json) checked those links without gate/retrieval
outputs. This follow-up was nominated after diagnosis and is not a blinded initial
review. Its disclosure is preserved in [the final review](span-review-validated.json).

`equivalence-v2` accepts explicitly reviewed alternative groups at downstream
stages, with exact candidate snapshots, same-case scope, independent source support,
and actual decision/event/visibility/retrieval evidence. Joint groups require every
member; alternative groups require any complete group. They never retroactively
raise source-local extraction coverage. The corrected result is 14/23 rather than
12/23. Rescoring the baseline with the same code retains 16/23. Both versions and
all intermediate reviews remain available. Thirteen scorer tests pass, including
cross-case/tampered alternatives and the two restored downstream links.

### Controls and reproduction

The unchanged verifier reproduces all 103 saved acceptance decisions: **37 true
accepts, 63 true rejects, three false rejects, zero false accepts**. The adapter
uses v3's final `after` verdicts as its baseline, not the older embedded baseline.
[Full control results](verifier-controls-span.json). Eight additional constructed
parser/verifier controls yield **four true accepts and four true rejects**, covering
actor assignment, borrowing versus ownership, activity-specific negation and
intention versus completion. [Targeted results](span-targeted-results.json). These
controls hand-construct candidate assertions; they do not measure generated extraction.

The ten mechanism regressions fail before implementation; all 31 relevant parser,
batch and worker tests pass with the experiment. Code review found no parser
blocker. The experiment nevertheless fails the measured adoption criterion.

The [preserved patch](span-experiment.patch) contains the exact experimental
extractor and its ten tests. It applies cleanly to the restored checkout. To
reproduce in an isolated checkout, apply the patch, then use the existing
development probe/replay commands with fresh paths. `run_span_controls.py`
requires that experimental parser. It is evidence tooling, not a production
entry point. No dependency or runtime configuration was added.

The first replay (`span-replay.json`) and first final-suite log (`final-tests.log`)
record Postgres-unavailable failures. They remain distinct from successful runs.
All completed replay cases report successful schema cleanup. Full-conversation,
later-session and post-TTL quality remain unmeasured; `b46e15ed` remains sealed.

Final restored-checkout validation: **234 passed, zero skipped**, with Postgres
required ([test log](final-tests-db-ready.log)). Ruff/Black and the clean-wheel
build/install checks pass ([lint](final-lint.log), [build](final-build.log),
[wheel](final-wheel-check.log)). Wheel checks now verify held-out resource
presence from manifest metadata without opening held-out source or label contents.
The scripted [eval](final-eval.log) retains 100% must-keep recall and zero gated
false writes on its synthetic smoke fixture. The [live extraction/rollback demo](final-demo.log)
passes with the installed model override. These checks do not override the failed
development adoption result.

## Baseline

The 20 selected source turns contain 60 saved candidates and four raw rejections.
They represent three separately scoped development conversations, with 24
must-keep occurrences and 23 distinct case-scoped targets. This subset excludes
other turns that may cause additional interference. It is not independent validation.

Two provisional agent reviews independently agree on target coverage: **16
complete, four partial, two absent, two ambiguous**. Only complete, source-supported
assertions receive credit. Quotes cannot restore missing information in an assertion.
The primary [target review](mustkeep-review.json) and independent [all-candidate
review](independent-review.json) preserve reasons and exact source-bound assertions.
Both reviewers identify the comparison class for strawberries and the kimchi
workshop's location as ambiguous. Human adjudication remains outstanding.

[All 60 candidates were gated](baseline.json), resulting in 51 historical writes.
The existing retrieval diagnostic replaced one original diet-goal query with a
later concurrent goal sharing its label predicate. This gap was fixed with an
opt-in query set covering every original must-keep assertion. The [completed
baseline](baseline-complete.json) reuses the exact saved source/candidate verifier
verdicts and repeats embeddings/storage/retrieval in fresh disposable schemas;
it makes no new verifier calls. Original reports remain intact. The original
plant-based and later fermented-food goals are treated as concurrent because
neither source retracts the other. Later-session and post-TTL access are unmeasured.

| Measurement | Result |
|---|---:|
| Strict candidate must-keep matches | 0/24 |
| Complete reviewed candidate occurrences | 16/24 |
| Complete distinct candidate targets | 16/23 |
| Complete distinct historical/current/visible/retrieved targets | 16/23 at each stage |
| Historical writes | 51 |
| Source-supported / unsupported / ambiguous writes | 50 / 0 / 1 |
| Reviewed supported-write precision, ambiguity in denominator | 50/51 (98.04%) |

See [stage-by-stage evidence](baseline-score-final.json). All historical writes,
including superseded writes, are reviewed. Zero forbidden-label hits are reported
separately from source-reviewed false writes. The ambiguous write retains the
unresolved object `that`; it receives no complete-coverage credit. All 23 fixed
queries use original label text, the declared originating session, `k=5` and
`reinforce=False`. These are regression queries, not realistic question phrasing.

Code review found two integrity gaps in the scorer: it could overlook removed
raw rejection records or omitted historical writes. New checks require exact
rejection coverage and agreement between write IDs, decision event IDs and the
reported event count. [Revalidated scoring](baseline-score-validated.json) retains
the same totals; nine scorer tests pass, including both corruption regressions.

The frozen strict scorer copies the old aliases/value normalization into
`mnemo/eval_normalization.py` as `mnemo-strict-v1`. Changing production aliases can
no longer silently change this scoring version. Diagnostic exports now include
existing decision/event/fact IDs and production session/temporal visibility.

## Selected experiment

[Loss attribution](loss-attribution.json) finds three distinct targets missing
binding/meaning, two lost to quote validation, and two ambiguous. All 16 completely
extracted targets survive the current gate and fixed-query retrieval. This directs
the next repair toward extraction, rather than verifier threshold changes.

The bounded experiment addresses quote reproduction: supply numbered immutable
source spans, ask the model to select an ID, and construct the evidence quote from
the original text. Unknown IDs remain invalid; existing quote-only clients retain
strict exact-quote checks. Full-turn semantic verification still decides whether
the structured assertion is true. Source membership alone cannot prove truth.
Acceptance criteria were recorded before the experiment in [run-manifest.json](run-manifest.json).

Recent primary work supports separating content selection from free generation:
[Amar et al., TACL 2025](https://aclanthology.org/2025.tacl-1.74/) discusses failures
to copy source spans verbatim. [CHyD, September 2026 preprint](https://arxiv.org/abs/2609.10046)
constrains generation to source spans. Numbered span selection is a local design
inference for our existing JSON interface; it is not a reproduction of either
paper's training, decoding implementation or results.

[Claimify, ACL 2025](https://aclanthology.org/2025.acl-long.348/) evaluates claim
coverage and context preservation and handles unresolved ambiguity explicitly.
[VeriFact, EMNLP 2025](https://aclanthology.org/2025.emnlp-main.905/) studies incomplete
and missing relational facts and measures recall alongside precision. These
support our evaluation requirements; neither establishes that this local span
interface will improve memory. The measured comparison above rejects that claim.

## Baseline verification

228 tests passed, zero skipped, with Postgres required and the installed local
extractor configured. [Test log](baseline-tests.log). Regression tests cover frozen
normalization, decision-to-retrieval lineage, original goal queries, complete versus
partial credit, duplicate targets, all saved writes, and source/label tampering.
The initial service-unavailable test output is retained separately; the retry
passes. Schema cleanup succeeded for every completed replay case.

```sh
MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx MNEMO_VERIFIER_BACKEND=cross_encoder \
MNEMO_VERIFIER_FALLBACK_BACKEND=ollama HF_HUB_OFFLINE=1 \
.venv/bin/python scripts/replay_extraction_probe.py \
  --probe docs/quality-v5/extraction-partial-dev.json \
  --manifest mnemo/data/quality-v4/manifest.json --output /tmp/mnemo-dev-baseline.json
```

Use fresh output paths. `--verdicts-from` reuses a complete matching report's
source-bound verdicts. `python -m mnemo.eval_equivalence --help` lists scoring inputs.
The report hashes distinguish model policy, evaluation code, source candidates
and provisional judgments. Actual monetary cost remains unknown.
