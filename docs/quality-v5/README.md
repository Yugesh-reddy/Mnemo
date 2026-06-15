# Partial extraction recovery

Development cycle continued September 15, 2026 from Codex thread
`01a0a2e7-8c71-7903-b8a3-b73d9825a87e` and the user's exported handoff.

## Recovered context

The checkout began at `689e111` without the prior thread's uncommitted changes or
its `docs/quality-v5` outputs. The local transcript supplied the original patches
for extraction recovery, decision replay and report handling. Those changes were
restored and tested again; the model measurements in this directory are new runs.
[Handoff evidence](handoff-evidence.json) records the transcript fingerprint and
the original verifier experiment's printed regression results.

The previous component-check verifier was rejected. Its final 41 targeted cases
had five false accepts and five false rejects; the older 103 cases had three false
accepts and five false rejects. Its improvements on some actor cases did not
survive the broader regression check. Production `quality.py` retains the policy
from `689e111`; these historical counts are not measurements of this continuation.
The old raw experiment reports were unavailable and have not been reconstructed
as if they were complete evidence.

The original disjoint 48-turn reservation, `b46e15ed`, was restored mechanically
from the checksum-verified upstream source. The payload has its original hash.
It remains unopened, unlabeled and unevaluated. See [reservation.json](reservation.json).

## Fix

Previously, one malformed member made the entire extraction response fail, even
when other facts had valid values and exact source quotes. Both providers still
request a complete corrected response within the existing retry limit. If the
final response contains valid siblings, they now return with separate raw
rejection records. Earlier attempts are never combined with the final response;
invalid final JSON and responses with no valid survivors still fail.

The normal quality gate verifies every survivor against the full turn. Malformed
members receive rejected decisions in the same atomic commit as the surviving
facts and cache reconciliation. Extracted writes retain low-trust
`agent_inference` provenance. Replay retains those rejection records, and report
tools exclude malformed members from candidate coverage without crashing.

No dependencies, schema, identity/cardinality rules, verifier thresholds or
write-score weights changed. This repairs a loss mechanism; it does not establish
general memory accuracy.

## Measurement

The development probe uses the existing 20 labeled source turns from the three
external development records. Source and label hashes are checked against the
saved baseline. All raw model replies, final survivors, rejections, errors and
usage are retained. The 24 per-turn must-keep labels remain in the denominator.

`measure_partial.py` compares strict whole-response parsing and partial recovery
on **identical final replies**. Only newly recovered batches enter its incremental
Postgres gate measurement. These are diagnostic comparisons, not fresh validation
or a full pipeline accuracy estimate. Earlier baseline extraction outputs can
differ because the model generated new replies and retry feedback now describes
all invalid members.

The [completed probe](extraction-partial-dev.json) returns 60 candidates across
20 sources, with three partial responses, four rejected members and zero failed
turns. Strict matching remains **0/45 targets and 0/24 must-keep**. On the same
final replies, whole-response parsing retains 54 candidates; partial recovery
retains 60. [Controlled comparison](partial-measurement.json).

The incremental gate stores **five new session-tier facts** from the six recovered
candidates, compared with zero for those three failed strict batches. Source
review covers all six candidates and all five writes: every stored assertion has
source support, including one complete must-keep equivalent and one incomplete
event. One supported denial is rejected because the verifier confuses baking
cookies with roasting vegetables. [All judgments](partial-source-review.json).
These are provisional judgments by one reviewer. The other 54 extracted candidates
were not replayed through the gate or reviewed in this incremental measurement.
No later-session or post-TTL recall improvement is established.

Reproduce from the repository root with an available local model:

```sh
export MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx
export MNEMO_VERIFIER_BACKEND=cross_encoder
export MNEMO_VERIFIER_FALLBACK_BACKEND=ollama
export HF_HUB_OFFLINE=1
.venv/bin/python scripts/probe_extraction_dev.py \
  --manifest mnemo/data/quality-v4/manifest.json \
  --baseline docs/quality-v3/external-dev.json \
  --output /tmp/mnemo-extraction-new.json
.venv/bin/python docs/quality-v5/measure_partial.py \
  --probe /tmp/mnemo-extraction-new.json \
  --output /tmp/mnemo-partial-new.json
```

The scripts refuse to overwrite existing evidence. The measurement requires the
same policy fingerprint as the probe and checks its final survivors and rejections.

## Validation

The new regression files reproduce 16 failures on the original implementation.
With the final changes, the required-Postgres suite passes **218 tests, zero
skipped**, including the installed local extractor. It covers malformed siblings,
semantic rejection after parser recovery, final-response-only recovery, both
evaluation modes, audit preservation and unchanged recall denominators.
[Test log](tests.log).

Ruff and Black pass, the rebuilt wheel passes installation checks from a clean
environment outside the checkout, and the scripted eval remains **90.9% precision
/ 100% recall / zero false writes**, versus naive **60% / 90%**.
[Lint](lint.log) · [wheel](wheel-check.log) · [eval](eval.log).

The real extraction and rollback demo passes with the available model:
`MNEMO_EXTRACTOR_MODEL=qwen3.5:4b-mlx make demo`.
The first default-model test run skipped one integration check and the first demo
failed because `llama3.2:3b` is absent on this workstation. Retrying with the
installed model resolves both; no model was installed or default changed.
[Demo log](demo.log).

## Remaining quality work

Actor attribution, modality, event binding and relation naming still need
development evidence. Strict label matches and source-supported equivalents must
stay separate. The proposed identity/cardinality contract and long-term retention
evaluation remain outstanding. Consolidation stays deferred. The reserved source
must not be used for tuning.
