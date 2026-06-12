# Structured assertion verification and source review

September 11, 2026. This development cycle follows commit `2381abb` and preserves
all [v4 results](../quality-v2/README.md). It improves selected verifier failures,
but **does not establish reliable memory quality**. Numeric thresholds and score
weights remain unchanged. Consolidation remains deferred.

## Implemented behavior

- NLI renders only known relations with faithful templates. Unknown predicates
  and structured objects go to the bounded structured fallback; without fallback,
  the verifier abstains. Past actions no longer become invented possessive facts
  such as `My presented poster is ...`.
- The fallback receives source evidence and `{subject, predicate, object}`. It
  does not receive extractor confidence, importance or claimed assertion authority.
- Job-role assertions lacking matching role evidence require fallback. Team
  membership cannot establish a job role. The reversed manager regression passes.
- Type guards reject the observed Linear-as-database and fish-as-editor cases;
  unfamiliar database names need an explicit database cue in the source. This
  conservative rule can miss real unfamiliar databases mentioned without a type.
  It is not a general ontology: other predicate names can still encode wrong types.
- Tentative consideration cannot establish a definite plan in the covered cases.
  Explicit later commitments defer to semantic verification, including anaphoric
  commitments. This guard does not prove all possible plan encodings correct.
- Extraction instructions preserve attribute identity through corrections and
  distinguish shells, issue trackers, editors, databases and tentative plans.
  Real-model extraction still violates these instructions; prompt compliance is
  measured, not assumed.

## Fixed development assertions

[verifier-final.json](verifier-final.json) contains 103 selected, source-reviewed
**development** assertions. Cached v4 verdicts have 33 true accepts, 56 true
rejects, seven false accepts and seven false rejects. The frozen v5.1 policy has
37 true accepts, 63 true rejects, **zero false accepts and three false rejects**.
The threshold is still 0.99; no numeric calibration was performed.

The three misses are a requested project-note name, a requested scheduling timezone,
and a usual sourdough ratio. Their legacy labels omit scope or qualifiers. These
require annotation review rather than blindly weakening verification to fit them.
The emergency-contact denial is rejected and Noah Chen remains stored.
These selected regressions do not establish general verifier accuracy.

## Synthetic development pipeline

[synthetic-dev-final.json](synthetic-dev-final.json) replays the saved **user**
candidates from [synthetic-dev.json](synthetic-dev.json) with real embeddings and
the current verifier. Extraction was not repeated; only the gated arm is published
because old reports do not preserve every assistant extraction. Do not treat it as
a new complete naive-versus-gated comparison.

Strict final precision/recall are **61.1% / 44%**; historical precision/recall are
**68.2% / 50%**; exact must-keep recall is **5/8 (62.5%)**. The three misses are
captions with video-call scope, peanut versus peanuts, and the ambiguous synthetic
name `Maya Chen revised`. [Failure attribution](synthetic-dev-failure-breakdown.json)
preserves each source, candidate, decision and target.

[Review of all 22 historical writes](synthetic-dev-review.json) finds 19 supported
assertions, two ambiguous assertions and **one unsupported strengthening**: listening
most often to jazz becomes a genre preference. The explicit forbidden-list metric
is zero, but source review still finds a false assertion. The use-versus-preference
problem is broader than the database predicate regressions.

## External development pipeline

[external-dev.json](external-dev.json) repeats **real extraction**, embeddings and
verification on three conversations totaling 140 turns. All three have zero strict
final/history precision, recall and must-keep recall against their unchanged labels.
This does not mean they contain no supported memories.

[Review of all 47 writes](external-dev-review.json) finds **37 supported**, **six
unsupported** and **four ambiguous** assertions. The six failures are:

- A time becomes the food object of `baked_bread`.
- Liking the idea of a fruit tart becomes liking the food.
- An oven convection setting becomes `editor_use`.
- A slower rise becomes an ingredient change.
- A failed past bread attempt acquires the future Italian recipe's identity.
- A restaurant becomes the consumed food.

[Every missed must-keep target](external-dev-failure-breakdown.json) is source-reviewed:
six scope losses, four relation errors, three type errors, one event-binding error,
three omissions, four wording/predicate equivalences and two compound-label
mismatches. These are primary diagnostic attributions, not independent gold.
The four supported equivalences are research topic, fermentation workshop,
vegan-class attendance and the two learned fermented foods. All 23 exact targets
are absent from current memory; there is no exact present-but-unretrieved target.

Simultaneously true values still overwrite one another: one presentation plan
replaces another; saag paneer replaces naan; one cooking goal replaces another.
Most unfamiliar assertions are demoted to session tier. Same-session retrieval
probes do not establish durable retention or safe expiry. Storage/cardinality
changes need a separate concrete contract proposal.

## Frozen 46-turn holdout

[holdout.json](holdout.json) was run once after the policy/label freeze. Strict
final precision/recall are **0% / 0%**, versus naive **4% / 4.3%**. Historical
precision/recall are **5% / 4.3%**, versus naive **5.3% / 8.7%**. Must-keep recall
is **0/11**, versus naive **1/11**. This holdout is now consumed validation data.

[All 20 historical writes were reviewed](holdout-review.json): **17 supported**,
**two ambiguous** and **one unsupported strengthening**. A report that the Modern
Art Museum *might* host local-artist events becomes a definite `hosting_events`
assertion. The explicit forbidden-list metric is zero because its predicate and
subject differ from the pre-labeled forbidden example. This demonstrates why
source-reviewed history is necessary.

[All 11 must-keep misses are diagnosed](holdout-failure-breakdown.json). The
verifier turns current presence in Los Angeles into a residence claim and rejects
it; a supported volunteering date gets entailment at 0.95 and fails the unchanged
0.99 threshold. Other losses include omitted event dates, an art-history course
encoded as editor use, separate event/venue facts without binding, and predicate
mismatches. The lecture's equivalent assertion is retrieved despite its exact
label miss. Rachel Lee's work is retained in an earlier session under `interest`,
but the later session's probe does not return that session-scoped assertion.
No frozen-policy changes were made after these findings.

Total allocated bytes grow from **835,584 naive to 1,409,024 gated** while semantic
events fall from 38 to 20. External development bytes grow from **2,457,600 to
4,038,656**, with events falling from 112 to 47. These totals include tables and
indexes for evidence, queues and decisions. Fewer writes are not total-storage
savings. External coverage now totals 264 turns across six records, including the
prior 36-turn sample and consumed 42-turn holdout. The separate single naturalistic
200-turn conversation remains outstanding.

## Freeze and evidence controls

The next deterministic disjoint LongMemEval record, `2ce6a0f2` (46 turns), was
reserved before this cycle's policy changes. The prior 42-turn holdout is consumed
and was not used for this cycle's tuning. The current policy was frozen after dev
source review and **before reading or labeling the new holdout**. Its 44 atomic
labels include 11 must-keep targets and cover every user turn; assistant rows are
excluded by role. Source/label hashes and timestamps are in the
[manifest](../../mnemo/data/quality-v3/manifest.json).

Frozen policy: `cb2fafb6ae234ceb566fddc4f4c167080e99ffcdd5cb85c9ff64c392a5247ebb`.
All labels and post-run source judgments are by one Codex reviewer, provisional,
and not independently adjudicated human gold. Strict scores remain unchanged by
post-run review. LongMemEval is an external benchmark with simulated dialogue;
this is not a verified human-chat corpus or a competitor comparison.

Intermediate development trials are retained under distinct names, including the
initial/refined verifier and synthetic reports. They show why passing the 90-case
regression set or loosening the fallback prompt alone was insufficient. The final
103-case probe, real-extraction external run and saved-candidate synthetic replay
have matching frozen policy fingerprints. Do not combine their denominators.

## Controlled workload and validation

[benchmark.json](benchmark.json) records 25 observations after two warmups, one
outstanding observation and one worker. Other evaluation/model jobs had finished.
Observe-through-worker latency is **p50 7.49 s / p95 12.73 s**, observe-only
**36.2 ms / 94.0 ms**, and throughput **0.129 observations/s**. Percentiles use
nearest rank. The measured interval starts September 11 at 20:04:10 UTC and lasts
193.77 seconds; model load and warmup are excluded.

This controls request concurrency, not the workstation. A
[mid-run host sample](benchmark-host-sample.json) records a 16 GiB, eight-CPU Mac
with about 12 GB of swap occupied. Swap occupancy is not an active-paging trace or
proof of the latency cause. The earlier v4 2.70/6.90-second result is therefore
not an isolated policy-performance comparison or a production SLO.

The measured interval used 25 extraction requests (22,114 input / 1,397 output
tokens), 18 fallback requests (7,031 / 1,611 tokens), seven NLI predictions and
45 unmetered embedding calls. API and local-compute cost remain **unknown** without
actual rates. No zero-cost or total-storage-saving claim is made.

The [required-Postgres suite](tests-validated.log) passes **180 tests, zero skipped**.
[Lint](lint-validated.log), [build](build-final.log), and the
[clean-wheel retry](wheel-retry.log) pass. The original wheel check failed because
Docker/Postgres was down; its [failure log](wheel.log) is retained. The project
service was restored before database evaluation began, and the unchanged package
checks passed. Evaluation and benchmark cleanup leave zero temporary schemas.
[validation.json](validation.json) records models, digests, commands and limitations;
[results-summary.json](results-summary.json) provides a compact machine-readable
view. Implementation commit: `b8997c5`.

## Reproduction

Use the recorded model versions and explicit backend settings:

```bash
export MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx
export MNEMO_VERIFIER_BACKEND=cross_encoder
export MNEMO_VERIFIER_FALLBACK_BACKEND=ollama
export HF_HUB_OFFLINE=1
python scripts/probe_verifier_dev.py \
  --cases mnemo/data/quality-v3/verifier-dev-full.json --output NEW-verifier.json
python scripts/replay_gated_dev.py \
  --report docs/quality-v3/synthetic-dev.json --output NEW-replay.json
python -m mnemo.eval_suite --manifest mnemo/data/quality-v3/manifest.json \
  --split dev --output NEW-dev.json
```

Use new output paths to preserve evidence. A rerun of a consumed holdout is a
regression check, not fresh validation. No prices were supplied; monetary cost
must remain unknown. Required remote merge protection is pending the intended
repository/branch: this checkout has no remote, and the same-named account
repository contains unrelated DSA submissions and must not be modified.
