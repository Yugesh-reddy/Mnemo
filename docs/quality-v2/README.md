# Memory quality reliability milestone

Labels and failure attribution precede changes to extraction or verification.
The original reports are preserved under `docs/evaluation`.

## Source review completed before model-policy changes

`adjudication.json` reviews all 38 unmatched gated writes, with the original report
and dataset hashes, verbatim source, complete candidate, category and rationale.
This is a **single Codex source review**, not independently adjudicated human gold.
The review separates 10 equivalent assertions, 5 supported but overly broad
relations, 11 unsupported relations, 10 unsafe negative-value encodings, one
ambiguous synthetic source and one incorrect label subject. Those judgments do
not silently replace the original strict scores.

`failure-breakdown.json` traces all eight strictly missing must-keep targets:
one label equivalence, one ambiguous synthetic source, one lost extraction
identity, four extraction-fidelity/matching failures, and one correct fact
subsequently overwritten by the false emergency contact. Saved reports do not
contain retrieval probes or simulated expiry, so these are not attributed to
retrieval failure or TTL.

Reproduce the report with:

```bash
python -m mnemo.eval_audit docs/evaluation/real-200.json \
  --review docs/quality-v2/adjudication.json --output /tmp/failure-breakdown.json
```

## Expanded source data and reserved holdout

`mnemo/data/quality-v2/manifest.json` selects four additional complete LongMemEval
oracle conversations by deterministic hash order, with disjoint source session
IDs. Three development records contain 140 turns; a fourth **42-turn fresh
holdout** was reserved and evaluated once after policy freeze. Together with the earlier 36-turn sample there are 218
external dialogue turns across five conversations. This is a multi-conversation
suite, not one natural 200-turn conversation. LongMemEval itself includes simulated
conversations; do not describe these as verified human chat logs.

All 140 development turns were source-reviewed before changing the policy.
Labels distinguish stated actions, preferences and explicit plans. Questions and
assistant turns are explicitly annotated with empty lists. Label coverage targets
memory-relevant assertions and is provisional, not exhaustive semantic gold.
The holdout content/output is not used for prompt or threshold development.
Each conversation is evaluated in a separate store to avoid merging unrelated
users' facts. Raw-source and label hashes are checked by `mnemo.eval_suite`.

Source: [official LongMemEval](https://github.com/xiaowu0162/LongMemEval),
[cleaned oracle release](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416/longmemeval_oracle.json).
The packaged MIT license remains in `mnemo/data/LongMemEval-LICENSE.txt`.

## Implementation sequence

1. Freeze labels, split and baseline evidence; retain the original scores.
2. Fix extraction relation/direction/type fidelity and negative-value overwrites.
3. Preserve predicate semantics in NLI hypotheses; add the emergency-contact
   regression and evaluate conservative verification policies on development data.
4. Freeze the final policy, annotate/release the reserved holdout once, and rerun
   historical false-write and must-keep metrics. No tuning against holdout results.
5. Measure serial controlled workload latency and explicit priced costs; keep
   missing pricing/usage unknown. Configure required CI on the user-specified repo.

## Implemented fixes and regression evidence

The extraction prompt now asks for every relevant assertion while retaining its
precise relation, actor direction, type, corrected value and planned/completed
status. It no longer steers unfamiliar relations toward a seven-predicate list.
The worker rejects empty or negative placeholder values before they can replace
an affirmative identity. Those rejected candidates remain in the decision log.

NLI hypotheses retain database type and job-role semantics. Matching denied
evidence triggers bounded fallback even when NLI confidence exceeds 0.99; without
a fallback this case is rejected. Unrelated negative clauses do not trigger this
path. This is a conservative development policy, not a guarantee of entailment.
Score weights and numeric cutoffs are unchanged. The standard predicate vocabulary
adds database use and primary language as distinct relations alongside preference.

The heuristic backend also stops treating use as preference. This exposed two
legacy smoke-label errors. `load_smoke_dataset(version="1.0.0")` preserves the
original labels; version 2.0.0 keeps the same 18 source turns and corrects database
use and primary-language labels. Current scripted precision/recall are 90.9%/100%
gated and 60%/90% naive. The naive false update now visibly loses the earlier use
fact. This change is label repair, not evidence of model improvement.

`--probe-retrieval` records whether each target is absent from current memory or
present but not retrieved in its session, with queries and results. Probe usage
is separate from write usage, and probes never reinforce memory.

## Controlled measurement

Run `python -m mnemo.benchmark --samples 25 --warmup 2 --output benchmark.json`
with the chosen real backends after other model workloads finish. It uses a serial
closed-loop workload: one outstanding observation and one extraction worker, with
warmup excluded, separate observation/processing latency, nearest-rank p50/p95,
throughput and measured token usage. It is not a saturation/capacity benchmark.

Supply `--prices` with explicit input/output USD-per-million rates per component,
or `--local-usd-per-hour` with an explicit compute rate. Missing rates or token
usage remain unknown; zero prices are accepted only when explicitly supplied.
NaN, infinity, negative rates and incomplete API price entries cannot silently
produce a valid estimate. API and local-compute estimates remain separate.

Consolidation remains disabled.

## Completed development and holdout results

All real runs use Qwen 3.5 4B MLX extraction, Nomic embeddings and DeBERTa NLI
with bounded Qwen fallback. The frozen code/settings fingerprint is
`77bbf4e83088a4a740bc4b6da38a38bd214dc9528551f424eeada7f54280708a`.
The holdout matched this fingerprint and was not used to tune the policy.
All evaluation cleanup completed; no evaluation schemas remain.

[Verifier development calibration](verifier-dev-calibration.json) compares 90
fixed source-reviewed assertions at the unchanged 0.99 threshold. Before: 32 true
accepts, 46 true rejects, 11 false accepts and one false reject. After: 33 true
accepts, 56 true rejects, one false accept and no false rejects. The denied Alex
Chen emergency contact is rejected. The remaining false accept reverses the
manager relation: “My manager is Elena Ruiz” is accepted as the user being
“manager of Elena Ruiz” at 0.9927. This calibration is development evidence only.

[External development](external-dev-after.json) evaluates three isolated
conversations against labels frozen before prompt changes. The
[previous-policy run](external-dev-before.json) matched none of their strict final
or historical truth labels. The revised policy's strict final precision/recall:

- Data-science conversation, 44 turns: **0% / 0%**, with 0/5 must-keep matches.
- Baking conversation, 48 turns: **5.3% / 5.6%**, with 1/8 must-keep matches.
- Cooking conversation, 48 turns: **0% / 0%**, with 0/10 must-keep matches.

The revised policy writes 75 gated events, up from 19. Source review of
[all 75 writes](external-dev-after-review.json) finds 31 equivalent assertions,
16 supported but lossy assertions, 17 supported partial assertions, eight
unsupported strengthenings of tentative statements and three ambiguous relations.
The [19-write baseline review](external-dev-before-review.json) includes Python/R
and a stand mixer incorrectly represented as databases. These reviews explain
mismatches; they do not replace strict metrics or prove independent gold accuracy.
Explicit plans have `candidate` labels and do not count as strict truth matches.

[The 100-turn synthetic development rerun](synthetic-dev-after.json) has gated
final precision **20%**, recall **20%**, must-keep recall **37.5% (3/8)**, and zero
explicitly forbidden historical writes, compared with 29 in its naive arm.
Historical precision/recall are **32% / 26.7%**. In the original policy's same
development cohort, historical precision/recall were **37.9% / 36.7%**, with one
explicitly forbidden write. The safety fix therefore does **not** demonstrate
recall preservation. Do not compare this 100-turn final result directly with the
old mixed 200-turn final HEAD. The new emergency-contact fact remains Noah Chen
and is retrieved; the meeting-free correction changes predicate and misses its
canonical target. Peanut/peanuts and richer captions wording still fail strict
matching. The synthetic “Maya Chen revised” ambiguity remains unresolved.

[The fresh 42-turn holdout](holdout.json) has gated strict final precision
**3.7%**, recall **4.3% (1/23)**, must-keep recall **0% (0/10)**, and historical
precision/recall **3.4% / 4%**. Naive final precision/recall are **2.2% / 4.3%**.
All ten must-keep probes are classified `upstream_or_label_mismatch`, so this run
does not identify search failure of a matching stored target. Some assertions are
present under different predicates or subjects; zero strict matches does not mean
zero useful content. The source review of
[all 29 held-out writes](holdout-review.json) identifies 17 supported assertions
(including partial/lossy forms), six unsupported assertions and six ambiguous
relations. A concrete temporal false write stores “last Thursday” as purchase
time when the source establishes delivery time. Four tentative considerations
become definite plans, and modern room style becomes a preference. The explicit
forbidden-list metric is zero because it does not enumerate these assertions;
**zero forbidden matches is not zero false memories**.

[Machine-readable cohort summary](results-summary.json) retains exact counts and
per-conversation metrics. No post-hoc semantic review has been substituted for
frozen-label scoring. This holdout is now consumed validation data. Another
policy iteration requires a new unopened holdout.

## What the failure traces establish

The revised prompt recovers more candidate assertions, but important losses still
occur at extraction, verification and identity assignment. Development examples
show legitimate class participation and poster presentations rejected because
unknown predicates render as malformed hypotheses such as “My presented poster
is ...”. The structured assertion is already supplied to fallback, but the prompt
still centers the rendered hypothesis. Six external development candidates are
labeled entailment by fallback yet rejected below the unchanged confidence cutoff.
That is calibration evidence, not permission to lower the cutoff without controls.

Multiple simultaneous facts also collide at HEAD: Python is replaced by R,
sauerkraut by kimchi, naan by saag paneer, and one presentation plan by another.
Other corrections drift to different predicates, leaving stale values visible.
Most accepted unfamiliar relations are demoted to session tier. The recorded
same-session probes do not test durable cross-session recall or simulated expiry.
These require a separate development cycle, not tuning against this holdout.
See [the detailed follow-up plan](../QUALITY_RELIABILITY_PLAN.md).

Total storage still does not improve. In the external development run, naive
allocated bytes total **2,613,248**, versus **4,464,640** gated. In the holdout,
semantic events fall from **50 to 29**, while allocated storage grows from
**909,312 to 1,474,560 bytes**. These totals include indexes, raw evidence, queue
and audit decisions. Fewer semantic events are not a total-storage saving.

## Controlled workload result and validation

[The serial benchmark](benchmark.json) ran September 11 at 12:42 UTC on local
macOS arm64, after evaluation and test model calls finished. It contains 25
measured development observations after two warmups: one outstanding observation,
one worker, and no concurrent model workload launched by this task. Total
observe-through-worker latency was **p50 2.70 s, p95 6.90 s**, mean 3.77 s and max
7.34 s. Observe-only p50/p95 were **20.4 / 35.7 ms**. Measured throughput was
**0.265 observations/s** over 94.33 seconds. Percentiles use nearest rank. This is
a serial service-time measurement on a workstation, not a saturation benchmark,
concurrent-worker capacity estimate or production latency guarantee.

Measured usage excludes warmup: extraction 25 calls and 16,664 input / 1,412
output tokens; fallback eight calls and 1,742 input / 604 output tokens; NLI 25
calls; embeddings 45 calls with token counts unavailable. Monetary cost remains
**unknown** because no actual API prices or local USD/hour rate were supplied.
Explicit-price support is implemented and tested; unmetered embeddings also
prevent a complete token-priced estimate. No zero-cost assumption is made.

- Required Postgres correctness suite: **162 passed, zero skipped**; see [test log](tests.log).
- Ruff and Black: pass; see [lint log](lint.log).
- Built sdist/wheel and fresh-environment installation: pass; see [build](build.log)
  and [wheel](wheel.log) logs. New packaged data and all three new CLI commands are checked.
- Frozen holdout and serial benchmark exit successfully; temporary evaluation
  schema count is zero after cleanup.
- Branch protection is **not configured**. This checkout has no remote. The
  authenticated account's same-named `Yugesh-reddy/Mnemo` repository contains
  unrelated DSA submissions and has no correctness workflow. The intended
  repository and branch have been requested; that unrelated repository was not modified.

Reproduce with a new output path to preserve evidence:

```bash
export MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx
export MNEMO_VERIFIER_BACKEND=cross_encoder
export MNEMO_VERIFIER_FALLBACK_BACKEND=ollama
export HF_HUB_OFFLINE=1  # only when the NLI model is already cached
python -m mnemo.eval_suite --split dev --output external-dev.json
python -m mnemo.eval_suite --split holdout \
  --frozen-policy docs/quality-v2/frozen-policy.txt --output holdout-reproduction.json
python -m mnemo.benchmark --samples 25 --warmup 2 --output benchmark.json
```

A reproduction on consumed holdout data is a reproducibility check, not a new
independent validation. Baseline policy source is commit `01779e3`; fixed policy
source is `3a90983`. Model generation can vary across runs despite temperature
zero. The saved reports, labels and full assertion history remain the evidence.
