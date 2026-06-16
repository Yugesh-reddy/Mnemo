# Reviewed development baseline and bounded extraction repair

Started September 17, 2026 at `bca6566`. This cycle follows
[the accepted handoff](../NEXT_SESSION_QUALITY_PLAN.md). Frozen v2–v5 evidence and
labels remain unchanged; reserved `b46e15ed` remains unopened. No contract,
dependency, storage identity, tiering or threshold change is part of this cycle.

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
