# Mnemo — implementation status

Updated September 18, 2026. [Spec v4](PROJECT_SPEC.md) defines the contracts;
[the correctness plan](docs/CORRECTNESS_PLAN.md) records the infrastructure backlog.
The [quality reliability plan](docs/QUALITY_RELIABILITY_PLAN.md) and
[current evidence](docs/quality-v7/README.md) cover the follow-up work.
**The memory-quality acceptance target is still unmet.** Numeric thresholds remain
unchanged. Consolidation is deferred.

## Current milestone: bounded quote-feedback experiment rejected

The single v7 experiment added exact source-substring suggestions to retry errors
for invented terminal punctuation, while retaining strict validation and the
initial prompt. It recovered no candidates. With the same 20 development sources,
fixed queries and unchanged `equivalence-v2` scorer, complete retrieval fell from
**16/23 to 15/23**. Source-local coverage remained **16/24**; distinct candidate and
historical coverage remained **16/23**, while current/visible coverage fell to
**15/23**. All 49 gated historical writes were reviewed: **46 supported, zero
unsupported, three ambiguous**, versus 50/0/1 in the baseline. Frozen strict
coverage remains **0/24**; both original ambiguous targets stay unresolved and
in the denominator.

The solo Data Mining project was lost after a later, less specific project
assertion reused its subject/predicate identity. Four first-pass replies differed
before retry feedback applied, so this run does not establish that the diagnostic
caused the overwrite or ambiguity increase. No contract changes were made.
The experiment failed the retrieval threshold, retention and ambiguity criteria.
Production extraction is restored byte-for-byte to `09b9fa6`; the patch, tests,
reviews, raw replies and decision are preserved in [quality-v7](docs/quality-v7/README.md).
No second prompt iteration ran. The current production baseline remains 16/23.

Experimental correctness checks passed 239 tests; the restored checkout passed
231. Both deliberately deselected three live-model tests to preserve the run
budget; Ruff/Black passed. The one probe plus replay used 26 extraction requests,
59 verifier LLM requests and 175 embedding requests over **664.51 seconds** of
measured model phases. Monetary cost is unknown. Source judgments are provisional;
the independent code-review attempt failed at its usage limit before reviewing.
Generalization, full conversations and later-session/TTL retention remain
unmeasured. `b46e15ed` remains sealed. This completed failure authorizes no further
tuning cycle.

## Prior milestone: complete baseline; span experiment rejected

All 60 saved candidates from the 20-source development probe now have a full gate,
store and retrieval baseline. Two provisional agent reviews agree on **16/24
complete occurrences** and **16/23 complete distinct targets**. All 16 complete
targets survive the gate, production session visibility and fixed-query retrieval.
The 51 historical writes comprise **50 supported, zero unsupported and one
ambiguous** assertion. Ambiguity remains in the precision denominator (50/51).
Strict matching remains zero; original labels and evidence are preserved.

Evaluation normalization is frozen as `mnemo-strict-v1`, independently of storage
aliases. Existing IDs and temporal/session fields now link decisions to writes,
visibility and retrieval. Original must-keep queries remain present when later
truth labels reuse their predicate. The baseline milestone passed 228 tests with
zero skips; subsequent integrity and alternative-lineage regressions are included
in the final validation below.

The remaining distinct misses are three extraction binding/meaning losses, two
quote-validation losses and two annotation ambiguities. A bounded source-span
selection experiment was implemented and measured, then **excluded from the
production default**: complete source-local coverage rose to **17/24**, but
current/visible/retrieved coverage fell to **14/23**. Its 57 historical writes
comprise **56 supported, zero unsupported and one ambiguous** assertion.

The attempted repair recovered the plant-based goal and a repeated birthday-cake
mention, weakened the baguette event, and exposed two subject/predicate overwrite
losses. The full patch, raw replies, failed runs and independent review differences
are preserved. `equivalence-v2` fixes downstream scoring of explicitly reviewed
alternative assertions and later equivalent mentions; it does not inflate
source-local extraction or alter the frozen labels. Baseline remains 16/23 under
the same scorer. Thirteen scorer regressions and independent code review pass.

The unchanged verifier reproduces all 103 established decisions (37 true accepts,
63 true rejects, three false rejects, zero false accepts); eight targeted controls
pass (four positive, four negative). Production extraction is restored exactly
to `681a638`, and verifier policy is unchanged. No extraction/verifier repair
passed this cycle's adoption checks.

The [identity follow-up proposal](docs/quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md)
contains measured event links and a concrete SQL/API sketch. It is unapproved;
independent identity labels, unresolved-reference design and a persistent-store
collision audit remain prerequisites. No contract changes were applied.
The 140-turn conversation-wide check, human adjudication and later-session/TTL
tests remain unmeasured. `b46e15ed` remains sealed.
[Baseline, reviews and attribution](docs/quality-v6/README.md).

Final checkout validation: **234 tests passed, zero skipped**, with Postgres
required. Ruff/Black and clean-wheel build/installation checks pass. Packaging
checks verify held-out resource presence without opening held-out contents.
`make eval` and the live extraction/rollback demo also pass using the installed
model override. The synthetic smoke result does not establish real-data quality.

## Latest extraction milestone: partial batch recovery

Continued the linked quality-debugging thread by recovering its uncommitted parser,
audit and replay fixes from the local transcript. The unsuccessful component-check
verifier remains excluded: its final broader development run introduced three
false accepts on the older cases. [Recovery evidence](docs/quality-v5/handoff-evidence.json).

After bounded retries, both extraction providers preserve validated siblings from
the final response and keep malformed members as separate raw rejection records.
Every survivor still passes the normal semantic verifier. Worker commits preserve
low-trust provenance and atomic decisions/cache reconciliation. Replay and report
tools preserve rejection evidence without giving malformed output coverage credit.

The new 20-source development probe has three partial responses, four rejected
members and no failed turns. On identical final replies, partial recovery retains
**60 candidates versus 54** for whole-response parsing. The incremental gate stores
**five source-supported session facts** from six recovered candidates; one supported
denial is incorrectly rejected because the verifier conflates cookies and vegetables.
One write is a complete must-keep equivalent and another is incomplete. Strict
must-keep matching remains **0/24**, and this is not a full-pipeline accuracy result.
[Measurements and source review](docs/quality-v5/README.md).

Required-Postgres validation passes **218 tests, zero skipped**, with the installed
`qwen3.5:4b-mlx` extractor. Ruff/Black, clean-wheel installation, the scripted eval
and real extraction/rollback demo pass. The workstation's default `llama3.2:3b`
is unavailable, so the model integration check and demo require the documented
model override. The original disjoint 48-turn reservation `b46e15ed` has been
restored by checksum and remains unopened, unlabeled and unevaluated.

## Prior evidence-grounded extraction milestone

Commit `3aaaed1` adds evidence-first extraction, exact-quote and nonempty-value
validation, bounded retries with the failed response and validator feedback,
trusted per-candidate source offsets, and full-turn verification. Failed evaluation
turns retain their labels in recall and expose errors in saved reports. Conflicting
replayed candidates for repeated text are rejected instead of silently overwritten.

The v6.2 extraction probe covers 20 development sources with 24 per-turn must-keep
targets. Single-reviewer diagnosis finds 15 complete targets versus four in saved
v5.1 candidates; strict matching is still zero. Five targets are partial, and four
are lost through three failed source turns. This is candidate coverage, not memory
recall or verifier accuracy. All intermediate attempts and judgments are retained.

The continuous 200-turn fictional development evaluation is complete. Gated strict
final precision/recall is **1.41% / 2.41%**, with **2/32 must-keep**. All 145 writes
have source review: 125 supported (including 44 lossy and two component assertions),
13 unsupported and seven ambiguous. All 30 strict must-keep misses are reviewed;
16 have equivalents returned by search. Five extraction turns fail, including a
May 19 correction that leaves the earlier May 16 atomic assertion current.

The frozen **48-turn external holdout** has **0% strict final/history precision and
recall, 0/9 must-keep** in both arms. Review of all 25 gated writes finds 23 supported
(including seven lossy), one unsupported and one ambiguous. Four must-keep misses
have equivalents returned by search; other losses include an omitted appraisal goal,
a rejected figurine count, missing ownership, session scope and a failed extraction.
Both reports are complete with status `scored_with_errors`, not successful pipeline
runs. Source review does not rewrite frozen labels or establish independent accuracy.

Policy v6.2 remains unchanged after these outputs. The holdout is now consumed.
See [all evidence and source judgments](docs/quality-v4/README.md).

Current correctness validation: **200 passed, zero skipped**, with Postgres required.
Commit `703d028` also makes the controlled benchmark retain failed jobs, warmups,
worker retries and usage. Successful-only latency is separate from measurements
including terminal failures. The controlled 25-observation run finishes all jobs with **p50 6.29 s / p95 7.45 s**
end-to-end and **22.7 / 31.4 ms** observe-only, at **0.1575 observations/s**. It is
a serial workload of simple fixture facts, not concurrent capacity or quality
acceptance. Actual costs remain unknown. Ruff/Black and the rebuilt clean-wheel
checks pass; old-policy measurements below remain historical.

## Implemented and regression tested

- Scoped, serialized HEAD mutations; immutable event payloads; fact-owned revert
  targets with restored embeddings, JSON and lineage; transactional decay with
  stale-HEAD and last-recall checks.
- Renewable, fenced extraction claims; job-owned scope; retries/backoff; atomic
  event/decision/cache completion; cancellation recovery; standalone worker and
  MCP background startup; scheduled archival and graceful shutdown.
- Complete assertion verification, code-enforced assistant exclusion, low-trust
  extraction provenance and append-only decision evidence. Unknown predicates
  use bounded structured fallback; the extractor's claimed authority, confidence
  and importance are excluded from verifier input.
- Regressions for reversed manager direction, team membership versus job role,
  database/editor/shell types, tentative plans and denied-value overwrites.
  Known database names or explicit database evidence ground the type; unfamiliar
  names without a type cue conservatively abstain. This is not a general ontology.
- Meaningful corrections survive high embedding similarity. Duplicate retries
  remain idempotent; repetitions after intervening corrections are ingested.
- Session restrictions and TTL, legacy expiry overlays, valid-time filtering,
  recorded-time search, archive/expiry diffs and consistent cache/semantic ranking.
- Bounded vector and lexical candidates, HNSW iterative filtering and verified
  index plans; explicit embedding-dimension and pgvector-version checks.
- Queue health and decision evidence through CLI, MCP, JSON and the operations UI.
- Packaged migrations, data, templates and demos; required-Postgres CI and clean
  wheel installation. Remote merge protection is not yet configured.

## Labels and evaluation controls

All 38 original unmatched writes and eight missed must-keep targets have
source-linked review with report/source hashes. Reviews distinguish equivalent
wording, unsafe encodings, wrong relations and ambiguous source labels. They are
provisional judgments by one Codex reviewer, not independent human gold.

Three external development conversations supply 140 turns. Separate 42-, 46- and
48-turn holdouts are now consumed. Including the original 36-turn record, coverage
totals **312 external turns across seven records**. The continuous authored 200-turn
development conversation is separate. LongMemEval includes simulated dialogue, not
verified human conversations. All atomic labels remain provisional.

Both full-suite arms share real extracted candidates and use isolated stores.
Source/label/policy fingerprints, historical writes, must-keep retrieval probes,
usage and all allocated storage are recorded. The latest synthetic replay instead
uses saved user candidates to isolate verifier behavior; only its gated arm is
published. Neither evaluation format establishes a competitor comparison.

## Prior v5.1 correctness validation

The required-Postgres suite passes **180 tests, zero skipped**, including available
local-model integration checks. [Latest test log](docs/quality-v3/tests-validated.log).
Ruff/Black and the built wheel pass. The first clean-wheel attempt failed because
Docker/Postgres was down; restarting the project service allowed the unchanged
wheel checks to pass, including all v3 source/label assets.
[Wheel retry](docs/quality-v3/wheel-retry.log).

Prior infrastructure evidence remains under [docs/evaluation](docs/evaluation/README.md):
required-DB unavailability fails instead of skipping, real MCP observation and
rollback demos pass, and HNSW execution plans are preserved. Infrastructure
regressions do not prove real-model write accuracy.

## Prior v5.1 quality results

The 18-turn **scripted** regression remains **90.9% precision / 100% recall** versus
naive **60% / 90%**. Its source turns are unchanged; v2 labels correct database use
and primary language. Legacy v1 results remain available.

On **103 selected development verifier assertions**, false accepts fall from seven
to zero and false rejects from seven to three relative to cached v4 verdicts.
The emergency-contact denial and reversed manager regression pass. Remaining
misses concern requested name/timezone settings and a usual ratio. These selected
cases are not a general accuracy sample.

The **100-turn synthetic development replay** reaches strict final precision/recall
**61.1% / 44%**, historical precision/recall **68.2% / 50%**, and **5/8 must-keep**.
The three exact misses are qualified captions, peanut versus peanuts, and the
ambiguous name `Maya Chen revised`. Source review of all 22 writes still finds
one false assertion: listening most often to jazz becomes a genre preference.

The **140-turn external development suite** misses all **23 exact must-keep**
targets. Strict final/history precision and recall are zero in all three cases.
Review of all 47 writes finds **37 supported, six unsupported and four ambiguous**.
All must-keep misses are source-reviewed: six scope losses, four relation errors,
three type errors, one event-binding error, three omissions, four equivalents and
two compound-label mismatches. Examples include Python/R as editors, strawberry
preference as allergy, and a past bread attempt acquiring a future recipe identity.

The frozen **46-turn holdout** has **0% final precision/recall and 0/11 must-keep**;
historical precision/recall are **5% / 4.3%**. Naive retains one exact must-keep.
Review of all 20 gated writes finds **17 supported, one unsupported and two
ambiguous**. The false assertion strengthens a museum's possible events into
definite hosting. A current Los Angeles location is rejected because the NLI
hypothesis asserts residence; a correct volunteering date is rejected at 0.95.
The equivalent lecture assertion is retrieved despite its exact label miss.
These held-out findings did not change the frozen policy.

**Zero forbidden-list matches does not mean zero false memories.** Source review
finds false assertions in all three preceding v5.1 evaluation scopes. Read the
[full evidence](docs/quality-v3/README.md) for sources, all write judgments, all
missed must-keep traces and limits. Those two holdouts are consumed.

## Storage, latency, cost and remote CI

Current v6.2 allocated bytes rise from **2,392,064 to 3,743,744** on the 200-turn
development run and **696,320 to 1,433,600** on the 48-turn holdout. Fewer events
do not establish a total-storage saving. The [current controlled benchmark](docs/quality-v4/benchmark.json)
is measured separately from diagnostic evaluation times. No monetary estimate is
claimed without actual rates. The paragraphs below preserve v5.1 measurements.

Prior v5.1 external dev allocated bytes grow from **2,457,600 to 4,038,656**; holdout
bytes grow from **835,584 to 1,409,024** despite fewer events. These totals include
evidence, queue, audit records and indexes; no total-storage saving is established.

The controlled benchmark uses 25 observations after two warmups, one worker and
one outstanding request, after other model runs finish. That prior result is
**p50 7.49 s / p95 12.73 s**, observe-only **36.2 / 94.0 ms**, and **0.129
observations/s**. The workstation had about 12 GB of swap occupied during a host
sample; OS pressure is not isolated, and occupancy alone does not prove paging
caused the timings. It measures serial service time, not concurrent capacity or a
production SLO. [Request-level measurements](docs/quality-v3/benchmark.json). Explicit-rate
cost estimation is implemented, but actual prices are missing and embedding
usage is unmetered, so monetary costs remain unknown.

The `correctness` workflow exists and database tests fail when Postgres is absent.
Required merge protection and a remote run remain pending. This checkout has no
remote; the same-named repository in the authenticated account contains unrelated
DSA submissions and was not modified. The intended repository/branch and actual
compute rates have been requested from the user.

## Remaining work, in order

1. Independently adjudicate provisional labels and explicit equivalences. Review
   the new interview-versus-audio duration issue and compound usability label;
   preserve the frozen reports before any dataset version change.
2. Use development sources to fix actor attribution, lost event bindings and
   verification that demands completed actions for schedules or requests. Partial
   batch recovery is implemented and measured; false rejections that conflate
   different activities and malformed source quotes still lose supported facts.
   Do not tune confidence thresholds on holdout outputs.
3. Review the prepared [identity/cardinality proposal](docs/quality-v4/IDENTITY_PROPOSAL.md)
   before changing one-HEAD semantics. Different relation names leave old and new
   budget/date values current; simultaneous plans can also overwrite one another.
4. Measure important-fact availability across sessions and elapsed TTL, and preserve
   the unopened `b46e15ed` reservation for validation after a future policy freeze.
   Source-supported historical
   writes and same-session retrieval do not establish durable correct memory.
5. Configure the intended repository's required `correctness` merge check and
   calculate monetary cost with actual prices.

Reflection remains disabled. Consolidation requires demonstrated benefit,
complete lineage, source-evidence verification and trust capped by its sources.
