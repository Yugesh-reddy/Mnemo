# Mnemo — implementation status

Updated September 22, 2026. [Spec v4](PROJECT_SPEC.md) defines the contracts;
[the master plan](docs/MASTER_PLAN.md) records the backlog. Phases 0–4 are
implemented and locally verified. Phase 5's bounded pilots are complete:
**Qwen 2/6 with zero unintended mutations; Azure Luna 4/6 with two unintended
mutations**. Luna failed the zero-unintended-mutation requirement. Across both
runs the intended end state was reached in 10 of 12 scenarios and no existing
event was altered; the two unintended events are attributed, visible in history
and reversible with the same tools. Hosted CI and
merge protection were declined by the owner; they are not prerequisites for
this delivery.

## Shipped through Phase 5

- **Foundation and installation (Phase 0):** fresh-database vector registration,
  a tracked lockfile, packaged runtime resources and locked source installation.
- **Model-free lifecycle (Phase 1):** a deterministic, explicitly non-semantic
  hash embedder, disabled background work and external stdio lifecycle coverage.
- **Guarded SDK (Phase 2):** expected-event guards, append-only request receipts,
  durable retry replay, scoped reads/history and trust-preserving revert. Legacy
  APIs retain their contracts; [spec §15](PROJECT_SPEC.md#15-additive-guarded-memory-contract--september-22-2026)
  and the [SDK guide](docs/DIRECT_SDK.md) describe the additive API.
- **Direct MCP (Phase 3):** six tools over stdio, structured errors, a validated
  connection pool and no extraction, verification, decay or search reinforcement.
- **Web review, demo and documentation (Phase 4):** restore forms carry the
  revision the reviewer saw and a request UUID. Stale submissions return HTTP 409
  with refreshed state and no write; duplicate submissions replay their receipt.
  Restore preserves source trust. The separately labelled Manual correction form
  remains a legacy, unguarded human-review write. `make demo-direct` now demonstrates
  the guarded SDK, a stale-write conflict and an identical restore retry. The
  quick-start command waits for Postgres health before running migrations. The
  README covers setup, MCP clients, contracts and measured quality limits; detailed
  extraction instructions are in [the extraction guide](docs/EXTRACTION.md).
- **Local host-agent pilot (Phase 5):** a bounded Ollama tool loop uses the six
  published schemas through `DirectMemory`, with isolated fixture histories,
  actual revision-conflict injection, complete transcripts and event-level
  scoring. The [single six-scenario Qwen run](docs/direct-pilot/run-qwen/README.md)
  passed create and update. Both undo cases restored the requested value but
  skipped the required current read; ambiguous undo exhausted its request budget;
  conflict recovery read a specific revision instead of current state. All five
  model-written events were intended. This is a completed measurement with four
  protocol failures, not a claim of reliable host behavior.

The [Azure Luna rerun](docs/direct-pilot/run-luna/README.md) used the
same six scenarios once each and preserved tool-call IDs, the prompt and scorer.
Azure confirmed `gpt-5.6-luna-2026-07-09`. It passed create, update, simple undo
and conflict recovery. Targeted undo read a historical revision instead of current
state; ambiguous undo reverted both memories without clarification, producing
two unintended events. Those events carry `actor="pilot-host"` and the model's
request UUIDs, sit in `memory_history`, and are each undoable with one guarded
`memory_revert`; the guards bounded the damage but could not judge intent.
The 49.14-second run used 29 requests and reported 36,213
input / 1,691 completion tokens. Raw responses and snapshots are retained;
the temporary database was removed. These are bounded observations, not a
general reliability claim. No prompt or scorer tuning followed either run.

The quality pipeline and versioned event store remain one system. Direct writes
provide an explicit path into that store; they do not establish automatic
extraction accuracy.

## Phase 6: export/import

The owner chose export/import from the deferred Phase 6 list. `mnemo-transfer`
(`make export` / `make import`) writes a versioned JSON document of one scope or
the whole store and restores it into an empty, migrated store. It refuses
schema or embedding mismatches, rows outside the declared scope and non-empty
destinations. It keeps original IDs and sequence numbers and verifies the restored
rows against the document before commit ([spec §16](PROJECT_SPEC.md#16-exportimport--september-22-2026)).
Nine Postgres tests in `tests/test_transfer.py` cover the exact round trip, receipt
replay and sequence continuation after import, whole-store export, and every
refusal path. A manual CLI round trip between two scratch databases produced
byte-identical exports apart from `exported_at`. Pipeline tables (`fast_cache`,
`extraction_job`, `quality_decision`) are not exported. Merge-import stays out of
scope.

## Verification

`make test-db`: **360 passed, 1 live-Ollama skip** (including nine export/import
and nine v8 Azure-adapter tests). `make lint` passes Ruff and Black (87 Python files),
and `uv lock --check` passes. The pilot adds 23 regression cases and the Azure adapter adds 16
transport/configuration cases; two previously
skipped live embedding checks now pass. Pilot tests cover
argument validation, all six tools, actual conflict injection, event-level
scoring, current reads before recovery, bounded loops and interruption cleanup.
Offline replays reproduced both pilots' six verdicts without new host calls.
Both pilot databases' removal was independently confirmed.

Web tests exercise rendered
forms, stale conflicts without writes, replay after a later update, preserved
low trust, missing guards, unavailable targets and manual corrections. The demo
asserts exactly three revisions and three receipts, with no background work.

A browser check used two tabs against an invocation-owned database: a correction
in one tab made the other's restore form stale; the conflict banner showed the
new value; submitting the refreshed form restored PostgreSQL with its original
low trust and retained the complete history. The temporary server and database
were removed.

A fresh local clone with the Phase 4 changes and no `.env` also passes locked
installation, cold Postgres startup, migrations, the guarded demo and all 19
web/demo tests. The old detached startup returned while Postgres still rejected
connections; `make up` now uses Compose's `--wait` flag and returns with database
health confirmed. This check used a separate container, volume and dynamically
assigned local port; all temporary resources were removed.

`uv build` and `bash scripts/check-wheel.sh` pass in a clean environment outside
the checkout against an invocation-owned empty database. The installed wheel
passes resource and migration checks, the guarded demo and SDK lifecycle, receipt
replay, the installed pilot's `--help`, and real stdio MCP discovery/read/error
checks. Locked installation from
the source archive also passes. The wheel resolved MCP 1.30.0; the locked source
uses MCP 1.28.1. Packaging needed no model calls; the local host pilot and live
embedding tests are separate from extraction experiments.

The repository is [Yugesh-reddy/Mnemo](https://github.com/Yugesh-reddy/Mnemo).
**GitHub Actions remains disabled at the owner's request.** The workflow file is
retained, but there is no hosted green-CI result and no required merge check.
Independent agent review was unavailable after the earlier account-limit failure;
local code review, browser checks and executable verification were completed.

## Extraction quality and remaining scope

**v8 measured a stronger extractor; the frozen rule failed, and storage identity
is now the binding loss.** Production extraction is unchanged. With only the
extraction model swapped to Azure `gpt-5.6-luna`, complete extraction on the 20
development turns rose from **16/23 to 20/23** distinct targets, with zero
unsupported writes (ambiguous writes rose from 1 to 4). All 20 were written, but
one-value-per-subject/predicate identity overwrote four of them, so retrieved
coverage stayed **16/23**, tying the baseline; a same-code qwen control also gave 16/23.
Crediting one restatement as v7 did would give 17/23, a post-hoc figure that is not
the outcome. Every write in both arms landed in the session tier. See
[the v8 decision and evidence](docs/quality-v8/README.md) and
[v7](docs/quality-v7/README.md) (15/23, rejected). These are provisional, limited
measurements, not general reliability.
The 18-turn scripted regression remains gated precision/recall 90.9%/100%,
100% must-keep recall and zero false writes; it does not measure real extraction.
Numeric thresholds are unchanged. `b46e15ed` remains sealed. Consolidation stays
deferred. Phase 5 is complete with the limitations above. Export/import has
shipped from Phase 6.

**Pilot B (member/occurrence identity) shipped; its routing stays off.** Migration
0011 and [spec §17](PROJECT_SPEC.md#17-member-and-occurrence-identities-pilot-b--september-22-2026)
let several facts share a subject/predicate; legacy callers are unchanged.
Contradiction-gated extraction routing is behind `MNEMO_IDENTITY_ROUTING=off`. In
[v9](docs/quality-v9/README.md), routing raised Luna's retrieved coverage from 16/23
to **19/23** with zero unsupported writes, and `make eval` was unchanged. It passed
only 9/14 labeled identity cases (6/14 without routing), though. The local
verifier rated real corrections and coexisting interviews as contradictions at the
same 0.95, so three corrections left stale values. The frozen rule failed and the
default stays off. Grouped undo and consolidation remain deferred.

**Routing judge (v12) failed; routing stays off.** With lasting tiering, routing
brings Luna's development candidates to 20/23 retrieved (the set's ceiling) under
either judge, with zero unsupported writes. On the identity suite, Azure Luna as
judge passed 11/13 gate-clean cases (local 9/13): it fixed the three corrections
but turned two coexisting skills into a false replacement. The routing check
reuses the gate's entailment question, and a strong model reads "a different value"
as contradiction. The next step is a routing-specific question
([v12](docs/quality-v12/README.md)).

**Durability fixed in v11.** With the noise floor raised to 0.55, the lasting
policy passed every check ([v11](docs/quality-v11/README.md)): every written must-keep
target is durable (Luna 20/20, qwen 16/16), unsupported and forbidden writes are
zero, retrieval holds at 16/23, and `make eval` is unchanged. Extracted facts of
importance 5 or more now persist and fade through decay; 1–2 are dropped as noise,
3–4 stay in session.

**Durability (v10) failed on one check; legacy tiering stayed the default then.** Under
legacy scoring no extracted fact can become durable, so everything expires with
its session. The approved "lasting" policy made every written must-keep target
durable (Luna 20/20, qwen 16/16) with zero unsupported or forbidden writes and
no retrieval regression. But its weights lifted an importance-1 junk fact
above the noise floor, and `make eval` precision fell from 90.9% to 83.3%
([v10](docs/quality-v10/README.md)). The fix (a higher noise floor) needs its own
protocol.

## History

The records below preserve earlier checks and quality cycles. Their test counts,
commit hashes, next-step notes and remote/CI statements describe the time of each
record; the current delivery and owner decisions are above. Historical notes do
not authorize another extraction experiment or re-enabling CI.

### Master plan Phase 3: direct MCP profile

`make mcp-direct` / `mnemo-mcp-direct` now expose exactly six guarded tools:
create, get, search, update, history and revert. Scope and actor are configured
locally. The server owns a dimension-validated pool and embedder without importing
or starting extraction, verification or decay. Search does not reinforce memory.
Domain and argument-validation errors return JSON with MCP's error flag; the
adapter preserves structured errors despite FastMCP's exception message prefix.

Verification: `make test-db` reports **294 passed, 3 live-Ollama skips**;
`make lint` passes Ruff/Black (78 Python files); `uv lock --check` passes. Six
new tests cover schemas, isolated imports, disabled background work with hash and
Ollama configurations, and the external stdio lifecycle, errors, paging and replay
after process restart. An Ollama-configured server starts with an unreachable
model endpoint and answers non-embedding requests without background model calls.
Actual Ollama embedding calls remain untested in this model-free verification.

`uv build` and the extended `bash scripts/check-wheel.sh` pass in a fresh environment
outside the checkout, against an invocation-owned empty database. Checks include
the installed CLI, a real direct stdio client, six-tool discovery, current reads,
JSON input/conflict errors, guarded SDK replay, ten packaged migrations and locked
source archive installation. The wheel resolved MCP 1.30.0; the locked checkout and
source archive use MCP 1.28.1. Both paths passed. The existing CI command includes
both legacy and direct stdio tests, but hosted CI has not run: no remote exists.

Phase 4 (guarded web revert and the broader documentation/demo update) remains.

### Master plan Phase 2: guarded mutations and durable receipts

The user approved the additive receipt schema and guarded API contract.
`DirectMemory` and the synchronous `Mnemo.direct` wrapper now provide create,
update, revert, current/historical reads, stable history pagination and search.
Expected event IDs prevent stale overwrites, including A→B→A. Successful and
no-change mutations retain append-only receipts; identical retries replay the
original result without disturbing a later HEAD. Receipt failure rolls back the
event, supersession and HEAD together. Direct revert preserves source trust,
confidence and lineage. Legacy APIs retain their behavior.

Verification: `make test-db` reports **288 passed, 3 live-Ollama skips**;
`make lint` passes Ruff/Black (75 Python files). The direct test file has 36 cases,
covering acceptance cases 1–16, concurrent writers, concurrent identical requests,
reconnection, rollback, scope isolation, Unicode byte limits, typed legacy values
and malformed cursor rejection. Both SDK tests pass. Migration tests cover a
fresh database and an upgrade over populated migration 0009 without changed HEADs.
`uv build` and `bash scripts/check-wheel.sh` pass outside the checkout against an
invocation-owned empty database: all ten migrations, installed guarded SDK
lifecycle/conflict/replay, legacy demo, resource checks and locked source archive
installation. No model calls were needed.

One earlier legacy TTL run saw a 15-minute application/database clock difference;
after the clocks agreed, the unchanged test and full suite passed. An initial
packaging invocation incorrectly applied the hash backend to the quality eval;
the corrected invocation used heuristic verification for eval and hash only for
direct-memory checks. No quality thresholds or held-out data changed. Independent
review remains unavailable after the prior reviewer's account-limit failure;
local review and executable checks were completed.

The approved contract is recorded in spec §15, with a runnable
[SDK guide](docs/DIRECT_SDK.md). Phase 3's separate direct MCP profile is implemented
above.

### Master plan Phase 1: model-free lifecycle proof

`MNEMO_BACKEND=hash` supplies deterministic, normalized, non-semantic embeddings.
It uses the configured dimension, defaults background work off, rejects an
explicit enabled worker, and refuses extractor/verifier construction. Tests use
the same hash implementation; output matches the former fake embedder exactly.
Existing Ollama/OpenAI defaults and store semantics are unchanged.

The real stdio MCP test proves PostgreSQL → MySQL → history → revert → PostgreSQL,
then restarts the server and verifies the persisted HEAD and event chain. Runtime
tests reject any attempt to construct extraction/verification or schedule decay
with the worker disabled. `make demo-direct` runs the same lifecycle through the
synchronous SDK, prints revision IDs and provenance, and uses a fresh namespace.
The standalone module honors explicitly configured backends and otherwise uses
hash. Hash similarity does not establish semantic relevance.

Verification: `make test-db` reports **249 passed, 3 live-Ollama skips**; `make lint`
passes Ruff/Black (72 Python files). Nine demo tests cover the lifecycle, CLI,
backend selection and contradictory configuration. A clean local clone with no
`.env` and an invocation-owned empty database passes `make install`, `make migrate`,
`make demo-direct`, the external stdio lifecycle test, `uv build` and the extended
`bash scripts/check-wheel.sh`. The packaging check runs the demo from the installed
wheel and installs the source archive with its lockfile. `make eval` retains the
18-turn synthetic 90.9% precision / 100% recall / zero false writes result.
The independent reviewer hit its account usage limit before completing Phase 1;
local diff review and executable verification were completed.

Phase 2's approved receipt migration and guarded mutation contract are implemented
above; the Phase 1 demo and MCP lifecycle remain legacy compatibility checks.

### Master plan Phase 0: local fixes

Fresh-database connections now tolerate pgvector not being installed yet, so
migrations can create the extension. The regression test creates an empty
database, migrates twice, reconnects and verifies vector decoding. CI and
`make install` use `uv sync --locked --extra dev`; `uv.lock`, the documentation
and `AGENTS.md` are tracked. `CLAUDE.md` points to the operating guide. Broken
plan links, the missing demo-recording target and the stale README test count
are removed. Tests explicitly check must-keep recall and all ten MCP tools.

Verification: `MNEMO_REQUIRE_DB=1 uv run pytest tests/test_schema.py -v` passes
11 tests; the focused eval/MCP run passes 15. Final `make lint` passes Ruff and
Black (70 Python files), and `make test-db` reports **232 passed, 3 skipped**.
The skips require a running Ollama server or the `llama3.2` model. The new
fresh-database test first reproduced `ValueError: unknown type: public.vector`.

A clean local clone with no `.env`, using an invocation-owned empty database on
the existing Postgres service, passes `make install`, `make lint`, `make migrate`,
`make eval`, `uv build` and `bash scripts/check-wheel.sh`. All nine migrations
apply. The 18-turn synthetic smoke remains naive P/R 60%/90%, gated P/R
90.9%/100%, with gated must-keep recall 100% and zero false writes. This is not a
real-conversation extraction result.

Review caught a missing lockfile in the source distribution after the install
command changed. Its installation failure was reproduced, `uv.lock` was added to
the source manifest, and the packaging script now checks installation from the
extracted source archive as well as the wheel. The extended packaging check and
`uv lock --check` pass; follow-up review found no remaining issues. Historical
evaluation artifacts, including their raw log whitespace, are preserved.

Hosted CI and branch protection (Task 0.5) remain pending: no Git remote is
configured. The guarded mutation contract and product framing changes are not part of
Phase 0. Extraction quality is unchanged.

### v7 milestone: bounded quote-feedback experiment rejected

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

### Prior milestone: complete baseline; span experiment rejected

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

### v5 extraction milestone: partial batch recovery

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

### Prior evidence-grounded extraction milestone

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

### Implemented and regression tested

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

### Labels and evaluation controls

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

### Prior v5.1 correctness validation

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

### Prior v5.1 quality results

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

### Storage, latency, cost and remote CI

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

### Earlier quality backlog (requires a separately authorized cycle)

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
