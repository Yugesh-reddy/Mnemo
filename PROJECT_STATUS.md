# Mnemo — implementation status

Updated September 11, 2026. [Spec v4](PROJECT_SPEC.md) defines the contracts;
[the correctness plan](docs/CORRECTNESS_PLAN.md) records the infrastructure backlog.
The [quality reliability plan](docs/QUALITY_RELIABILITY_PLAN.md) and
[current evidence](docs/quality-v3/README.md) cover the follow-up work.
**The memory-quality acceptance target is still unmet.** Numeric thresholds remain
unchanged. Consolidation is deferred.

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

Three external development conversations supply 140 turns. A 42-turn holdout was
consumed by the prior cycle; a different 46-turn record was reserved before the
current changes and labeled only after the policy freeze. Including the original
36-turn record, coverage totals **264 external turns across six records**. This
still does not satisfy the separate single naturalistic 200-turn requirement.
LongMemEval includes simulated dialogue, not verified human conversations.

Both full-suite arms share real extracted candidates and use isolated stores.
Source/label/policy fingerprints, historical writes, must-keep retrieval probes,
usage and all allocated storage are recorded. The latest synthetic replay instead
uses saved user candidates to isolate verifier behavior; only its gated arm is
published. Neither evaluation format establishes a competitor comparison.

## Executed correctness validation

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

## Current quality results

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
finds false assertions in all three current evaluation scopes. Read the
[full evidence](docs/quality-v3/README.md) for sources, all write judgments, all
missed must-keep traces and limits. Both holdouts are now consumed.

## Storage, latency, cost and remote CI

Current external dev allocated bytes grow from **2,457,600 to 4,038,656**; holdout
bytes grow from **835,584 to 1,409,024** despite fewer events. These totals include
evidence, queue, audit records and indexes; no total-storage saving is established.

The controlled benchmark uses 25 observations after two warmups, one worker and
one outstanding request, after other model runs finish. The latest result is
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

1. Independently adjudicate provisional atomic labels and development equivalences;
   preserve original strict reports and keep source correctness separate from
   whether a fact is useful enough to retain.
2. Fix extraction's relation, type, event-binding and coverage errors on development
   sources. Unknown-relation rendering is repaired, but structured fallback and
   prompt instructions alone do not guarantee faithful assertions.
3. Prepare a concrete storage-identity/cardinality proposal before changing the
   one-HEAD-per-subject/predicate contract. Simultaneous plans and interests still
   overwrite one another. Measure durable access across sessions and TTL expiry.
4. Add one naturalistic 200-turn conversation and reserve another untouched holdout
   before further policy development. Require source-reviewed false-write and
   must-keep non-regression before declaring quality reliable.
5. Configure the intended repository's required `correctness` merge check and
   calculate monetary cost with actual prices.

Reflection remains disabled. Consolidation requires demonstrated benefit,
complete lineage, source-evidence verification and trust capped by its sources.
