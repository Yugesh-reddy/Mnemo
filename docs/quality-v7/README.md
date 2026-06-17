# One bounded experiment: exact-quote retry feedback

**Decision: reject.** Complete retrieved coverage fell from **16/23 to 15/23**;
no new target was retrieved. Ambiguous historical writes increased from one to
three. Production extraction is restored byte-for-byte to `09b9fa6`; the
[experimental patch and eight tests](experiment.patch) are preserved but inactive.
The single authorized experiment is complete. No second prompt iteration ran.

The hypothesis and adoption criteria were frozen in commit `1a253ab`, before
implementation or model execution. The production baseline is `09b9fa6` and its
[v6 reviewed baseline](../quality-v6/baseline-score-v2.json): 16/23 completely
retrieved targets, 50 supported historical writes, zero unsupported and one
ambiguous. Frozen strict source coverage is 0/24.

## Measured result

The unchanged `equivalence-v2` scorer compares the same 20 development sources,
24 source-local targets, 23 distinct targets and fixed retrieval queries.
[The score](score.json) includes all 49 gated historical writes, including
superseded writes; [the decision](decision.json) contains acceptance checks and
the lost fact's event lineage.

| Measurement | Baseline | v7 |
| --- | ---: | ---: |
| Complete source-local occurrences | 16/24 | 16/24 |
| Complete distinct candidate targets | 16/23 | 16/23 |
| Complete distinct historical targets | 16/23 | 16/23 |
| Complete distinct current targets | 16/23 | 15/23 |
| Complete distinct visible targets | 16/23 | 15/23 |
| Complete distinct retrieved targets | 16/23 | 15/23 |
| Supported gated historical writes | 50 | 46 |
| Unsupported gated historical writes | 0 | 0 |
| Ambiguous gated historical writes | 1 | 3 |
| Frozen strict source coverage | 0/24 | 0/24 |

The sole lost complete target is **the solo Data Mining class project**.
Its supported `user / working_on_project / solo project for Data Mining class`
write (`2986f6e7-63e7-45c5-87e6-61b1a71004a3`) was superseded by
`user / working_on_project / project involving analyzing customer data to identify
trends and patterns` (`eb92756c-c767-42de-af88-686b6ba26e04`). The second assertion
does not retain the solo/class qualifiers, and belongs to a different session.
The existing identity contract causes the loss before retrieval. No identity
change was attempted; the [v6 proposal](../quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md)
remains unapproved.

The three ambiguous writes are `thinks_feature_engineering_will_help / that`,
`found_results_unexpected / sourdough starter results`, and `will_try_out / that`.
The last was also ambiguous in the baseline; the first two are new. A quote does
not resolve the missing referent or distinguish starter results from bread results.
All 59 survivors have [source judgments](source-review.json): 55 supported, one
unsupported and three ambiguous. The unsupported assertion removed **maybe** from
the fermentation-time claim; the verifier rejected it. The naive arm stored 58
historical assertions: 54 supported, one unsupported and three ambiguous, all
linked to source judgments in the decision artifact. Explicit forbidden-label
hits were zero in both arms; those hits are separate from source-reviewed errors.

The [target review](mustkeep-review.json) keeps four partial occurrences, two
missing occurrences and the two original ambiguous targets unresolved. Neither
ambiguous target receives complete credit or leaves the denominator. Assertions
missing qualifiers or relationships receive no full credit, even when their
evidence quotes contain the missing detail.

The retry diagnostic recovered **zero candidates**. Three turns exhausted their
three attempts, retaining four invalid quotes. Convection and plant-based outputs
repeat the offending punctuation throughout. One restaurant sibling's valid quote
changes without repairing its invalid sibling. Four first responses differ from
the saved baseline **before feedback can apply**, including the project assertions.
Thus this single run does not identify the cause of first-pass variation or show
that the diagnostic caused the new overwrite and ambiguity. It does show no
measured benefit from the retry mechanism and failure of the frozen adoption
criteria. [Saved-attempt analysis](retry-mechanism-result.json).

## Diagnosis and intervention

[The source-bound diagnosis](diagnosis.json) covers all five clear misses:
team leadership loses its class binding; appliance use/acquisition loses ownership;
the restaurant assertion loses last week; convection and plant-based assertions
fail exact quote validation. Convection also loses first-use/date in its assertion,
so quote correction alone cannot earn complete credit. The strawberry comparison
class and kimchi workshop location remain ambiguous and in the denominator.

The two quote failures repeatedly replace a source comma/continuation with a
period. The experiment changes only validation-error feedback for this general
case: if removing one final `.`, `!` or `?` reveals an exact nonempty source
substring, show that literal option and ask for an evidence-only correction.
The malformed member is still rejected; only a new model reply can fix it.
No fuzzy matching, normalization, automatic rewriting or evidence relaxation occurs.
The initial prompt, output format, retry bounds, final-batch recovery, full-turn
verifier, thresholds, low trust, identity and tiering are unchanged.

This differs from v6's rejected span experiment, which changed first-pass prompts
and introduced an evidence-ID interface. The v7 intervention is confined to error
feedback after a specific exact-match failure. The [manifest](experiment-manifest.json)
and [implementation hashes](implementation-frozen.json) define the boundary.

## Budget and review

One implementation hypothesis, deterministic correctness checks, one regenerated
20-turn probe and one complete replay. No repeat of a completed run or second
prompt iteration is authorized. Both model phases are launched by
`run_experiment.py`, which refuses reused output paths and records wall time.
Its embedded extraction `before` rows come from the older v3 adapter; all v7
comparisons use the frozen v5 candidates and v6 complete baseline instead.

Eight focused mechanism tests include unseen wording, Unicode, a fabricated
positive quote against a negated source, unchanged first requests for both
providers, and a valid substring from a hypothetical purchase that still requires
full-turn verification. The first test run had a fixture mistake: `.` was itself
a literal substring of the negative-control source. The corrected pre-change run
has four expected failures; all 29 focused tests pass with the intervention.
Both original logs and corrected logs are preserved.

The deterministic project suite passes 239 tests; three live-model tests were
deliberately deselected to preserve the experimental model-call budget. Ruff and
Black pass. A code-review agent was attempted but failed at its usage limit before
providing a review. No independent review approval is claimed.

Source and target judgments were completed before inspecting gated replay results.
They are provisional primary-agent judgments, not human gold. The unchanged scorer
validated source/label hashes, raw rejections, candidate decisions, write links and
reviewed alternatives. Both phases completed with no turn or cleanup errors and
no infrastructure recovery. The failed review attempt and corrected test fixture
are recorded separately from experiment results.

After rejection, [restoration checks](restoration.json) verify production policy
and both scorers are byte-identical to the baseline, old evidence hashes match,
and the preserved patch applies cleanly. The restored checkout passes **231
tests**, with the same three live-model tests deliberately deselected; Ruff and
Black pass. See [restored tests](restored-tests.log) and
[restored lint](restored-lint.log). No production model rerun was performed.

## Calls, runtime and reproduction

| Phase | Measured calls | Wall time |
| --- | --- | ---: |
| Extraction | 26 LLM requests: 20 initial + 6 retries | 299.31 s |
| Replay, including both arms and retrieval | 59 verifier LLM requests; 175 embedding requests | 365.20 s |
| Total measured model phases | 85 LLM requests; 175 embedding requests | 664.51 s |

The verifier reports zero cross-encoder inference calls for these candidates;
the configured fallback handled them. Extraction reports 26,208 input and 5,110
output tokens; verification reports 27,069 input and 5,369 output tokens. Embedding
token counts are unmetered, not known zero. Times cover the recorded model phases,
not research, implementation, review or test time. The [extraction journal](extraction-run.json)
and [replay journal](replay-run.json) bind the commands to the experimental policy
and frozen manifest. No extra generation, replay or control run is authorized.

The completed evidence can be rescored offline with the frozen scorer, using a
fresh output path:

```bash
.venv/bin/python -m mnemo.eval_equivalence \
  --probe docs/quality-v7/extraction.json \
  --review docs/quality-v7/mustkeep-review.json \
  --support docs/quality-v7/source-review.json \
  --replay docs/quality-v7/replay.json \
  --output /tmp/mnemo-quality-v7-review.json
```

No holdout contents, including `b46e15ed`, were opened. Monetary cost is unknown
without supplied pricing. Development results cannot establish generalization,
full-conversation retention or later-session/post-TTL recall.
