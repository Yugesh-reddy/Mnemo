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
holdout** is reserved. Together with the earlier 36-turn sample there are 218
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

Consolidation remains disabled.
