# Evidence-grounded extraction and realistic evaluation

September 12–14, 2026. **Memory quality remains below acceptance.** The previous
frozen results remain in [quality-v3](../quality-v3/README.md). Numeric thresholds,
score weights and NLI policy are unchanged. Consolidation remains deferred.

## Implemented

Commit `3aaaed1` adds evidence-first extraction, nonempty values, exact source
quotes, and at most two corrective retries carrying the rejected output and
validator feedback. The worker derives each quote's offsets from trusted queued
input and verifies the entire original turn. Custom extractors may omit the
optional quote, but cannot supply fabricated evidence. An exact quote alone does
not establish assertion entailment.

Evaluation retains failed turns and worker error decisions. Every target remains
in recall denominators; both arms record `turn_errors`. The CLI saves the complete
report before returning nonzero for failures. Conflicting replay candidates for
identical source text cannot silently overwrite each other.

Commit `c393c98` fixes failure attribution: a reviewed paraphrase applies only to
an explicitly mapped target, not every missing target in the same source turn.
The audit also distinguishes reports with retrieval probes from older baselines.

The serial benchmark now follows worker retries/backoff through terminal status,
records failed jobs and warmups, retains their measured usage, and separates
successful-only latency/throughput from measurements that include failures. Failed
jobs cause a nonzero CLI exit after the complete report is written.

## Continuous 200-turn development conversation

The authored Harbor Voices conversation spans ten sessions, with corrections,
permissions, unfamiliar relations, multiple people and context-dependent events.
Labels were written before model output by the same Codex author/reviewer. This
is synthetic development data with provisional labels, not independent human gold.

The [complete real-model report](naturalistic-real.json) scores all 200 turns.
Five source turns exhaust quote-validation retries, including the May 19 preview
date correction. Its status is `scored_with_errors`; it is not a successful
pipeline run. Cleanup succeeded. Shared extraction failures are replayed to both
arms; gated worker retries here do not measure live model recovery.

Gated strict final precision/recall is **1.41% / 2.41%**, historical
precision/recall **1.38% / 2.15%**, and must-keep recovery **2/32**. Naive final
precision/recall is **0.95% / 2.41%**, historical **0.93% / 2.15%**, and must-keep
also **2/32**. These are complete normalized-assertion matches against frozen
labels; source-reviewed paraphrases are reported separately.

[All 145 gated writes](naturalistic-review.json) have individual source-linked
judgments: **125 source-supported, 13 unsupported and seven ambiguous**. The
supported group includes 44 lossy assertions and two components of compound
labels; it does not mean 125 complete or useful memories. Zero matches to the
explicit forbidden list therefore does **not** mean zero false writes.

The [30 missed must-keep targets](naturalistic-mustkeep-review.json) are individually
traced through candidates, decisions, current state and saved retrieval probes:

- 16 have a source-supported equivalent returned by search under different wording.
- Six lose event/context bindings; two combine wrong actors and lost bindings;
  one has an ambiguous encoding.
- Two lose facts at verification; one combines extraction actor error and verifier
  rejection; one combines verification rejection with session scope.
- One is lost through a complete extraction failure.

The configured verifier makes 174 fallback requests and only three NLI calls on
this development run; unknown relations route to structured fallback. That fallback
sometimes identifies Priya or Omar as “the user.” It also rejects
scheduled events because they have not occurred, and a stated request because it
cannot confirm the requested action was completed. Two correct entailment verdicts
at 0.95 fail the unchanged 0.99 threshold. These require development work, not
confidence-only tuning.

The May 19 correction survives in raw session cache but has no atomic write; May 16
remains in administrative current state. Different predicates also leave the old
$100 and new $150 budgets, and the April 10 booking and April 13 correction,
coexisting as current assertions. Correct same-session retrieval does not prove
cross-session durability or expiry behavior. No elapsed-TTL experiment was run.

[Post-run label issues](label-followups.json) include interview duration being
labeled audio duration and a compound usability outcome. Frozen labels and scores
remain unchanged; these findings require a reviewed future dataset version.

Allocated tables and indexes grow from **2,392,064 bytes naive to 3,743,744 gated**,
while events fall from 216 to 145. Shared extraction takes 941,396 ms and reports
210 requests, 187,870 input tokens and 15,210 output tokens. Arm times are 12,053 ms
naive and 1,138,533 ms gated. These are workstation diagnostics, not capacity
measurements or evidence of total-storage savings. Actual monetary costs remain
unknown without explicit prices.

## Development extraction probe

The 20-source probe contains 24 per-turn must-keep targets (23 final identities)
and 45 truth/must-keep targets. Source review finds 4 complete targets in saved
v5.1 candidates, 14 under the first evidence prompt, 16 under v6.1 and 15 under
v6.2. The current probe has five partial targets and four targets lost through
three failed source turns. All attempts and judgments are retained.

Strict matching remains 0/24 and 0/45. These are post-run candidate diagnostics,
not stored-memory recall. v6.2 does not improve every intermediate result; local
generation is not assumed perfectly deterministic.

## Frozen external validation and checks

Policy v6.2 was frozen after the development probe and tests, before opening and
labeling the disjoint 48-turn external record e3038f8c. Its source has 24 user turns
across four sessions; labels include 17 final truth identities and nine must-keep
identities. Neither its outputs nor the 200-turn results change this frozen policy.
The first setup attempt failed before extraction when Docker/Postgres stopped;
the same policy restarted after restoring services. Logs are preserved.

The [complete 48-turn report](holdout.json) has **0% strict final/history precision
and recall, 0/9 must-keep** for both arms. One display/protection source turn fails
quote validation; status is `scored_with_errors`, with successful cleanup.
[All 25 gated writes](holdout-review.json) are reviewed: **23 supported** (including
seven lossy), **one unsupported and one ambiguous**. The unsupported assertion
strengthens thinking about organizing a club meeting into a definite plan.

[All nine must-keep misses](holdout-mustkeep-review.json) are traced: four retrieved
equivalents, one omitted appraisal goal, one ownership/relation loss plus rejection,
one verification rejection, one event/ownership binding and session-scope loss,
and one extraction failure. The 12-figurine candidate is rejected at 0.95 despite
an entailment verdict. An earlier 57-record collection count remains current but
is absent from the later session's probe. Neither finding changed the frozen policy.

All 32 holdout verifier requests use structured fallback; no NLI calls are made.
The holdout stores 30 naive events versus 25 gated, but allocated bytes increase
from **696,320 to 1,433,600**. Shared extraction takes 163,618 ms, with 50 requests,
57,311 input and 2,596 output tokens; arm times are 1,184 ms naive and 131,798 ms
gated. Local integration tests briefly overlapped this diagnostic run. These are
not controlled latency results. The holdout is now consumed.

Required-Postgres validation passes **200 tests, zero skips** in
[tests-with-benchmark.log](tests-with-benchmark.log), including mixed failures,
all-failed workloads, recovered retries and failed warmups. The rebuilt wheel passes installation/import, packaged-data, migration, UI-template
and CLI checks outside the source checkout; see [wheel-with-benchmark.log](wheel-with-benchmark.log).
Ruff/Black pass. No new runtime dependency or database schema was added.

## Controlled serial workload

The [current measurement](benchmark.json) completes **25/25 observations**, plus two
successful warmups, with one outstanding observation and one worker. End-to-end
latency is **p50 6.29 s / p95 7.45 s**; observe-only latency is **22.7 / 31.4 ms**.
Throughput is **0.1575 observations/s** across 158.7 measured seconds. All jobs finish
on their first worker attempt. Failed-job accounting is covered separately by
regressions; no failures were omitted from this workload.

The sources are the 25 initial factual turns in the scripted development fixture,
not the longer conversational corrections. Warmups repeat two of those sources
in different namespaces. Completion is not a write-quality judgment. This is
closed-loop serial service time, not saturation, concurrent capacity or a production
SLO. Other model evaluations and tests had finished; OS activity was not isolated.
The 16 GiB workstation had about 8,028 MiB of swap occupied in the pre-run sample;
occupancy alone does not prove paging caused latency. Do not attribute differences
from prior runs solely to policy changes.

Measured usage is 25 extraction requests (21,839 input / 1,350 output tokens),
25 fallback-verifier requests (9,775 / 2,128 tokens), and 50 embeddings with unmetered
tokens. This workload routes all assertions to structured fallback: zero NLI calls,
despite a configured/loaded cross-encoder. Warmup usage is reported separately.
API cost and local compute cost remain **unknown** without explicit actual rates.

## Reproduce and next work

The local setup uses qwen3.5:4b-mlx, nomic-embed-text and
cross-encoder/nli-deberta-v3-small with bounded Ollama fallback. The pinned NLI
revision is in [nli-model.json](nli-model.json); the policy hash is in
[policy.sha256](policy.sha256).

```bash
MNEMO_REQUIRE_DB=1 MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx \
  HF_HUB_OFFLINE=1 .venv/bin/pytest -q -rs

MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx \
  MNEMO_VERIFIER_BACKEND=cross_encoder \
  MNEMO_VERIFIER_FALLBACK_BACKEND=ollama \
  .venv/bin/python -m mnemo.eval --real --dataset naturalistic --split dev \
  --probe-retrieval --output naturalistic-new-run.json
```

Use fresh output paths and serialize model workloads. Do not modify policy, source
or labels during a run. Replay shares extracted candidates between arms; extraction
time/usage is separate from store/gate time. The controlled workload has one worker
and one outstanding observation; it is not concurrent capacity or a production SLO.
API and local compute prices must be explicit; missing costs remain unknown.

Next development work should address actor binding, planned versus completed
assertions, valid facts lost with an invalid batch member, and stable identities
for corrections. Reserve a new untouched holdout before changing policy again.
The [identity/cardinality proposal](IDENTITY_PROPOSAL.md) is design only; one HEAD
per scoped subject/predicate remains the current contract.

Independent adjudication, actual compute prices and the intended remote are still
missing. This checkout has no remote, so required remote merge protection is not
enabled. The unrelated same-named GitHub repository must not be modified.
