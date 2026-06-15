# Evaluation sources and annotation scope

`longmemeval-car.json` is the complete 36-turn, three-session record
`gpt4_2655b836` from LongMemEval's cleaned oracle split, downloaded September 8,
2026. Source: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
Repository: https://github.com/xiaowu0162/LongMemEval
The MIT license is included in `LongMemEval-LICENSE.txt`.

The sidecar contains project-authored atomic labels for each source turn, including
empty label lists for assistant replies and non-assertions. Labels distinguish
planned activities from completed actions. They were authored from the source
text before model results were inspected. They are **provisional annotations**,
not independently adjudicated gold labels. The retained upstream `has_answer`
markers are retrieval evidence, not atomic-fact labels. Exact normalized assertion
matching is conservative: unmatched writes are reported separately from explicit
forbidden writes because unfamiliar predicate names and paraphrases can also fail
matching. Do not call that unmatched count a hallucination count.

The upstream timestamps lack a timezone. The importer interprets them as UTC for
stable ordering, without claiming that was the speaker's timezone. Oracle sessions
are sorted by timestamp during import. This sample measures write quality; it is
not a LongMemEval retrieval score or a competitor comparison.

The project-authored 200-turn synthetic corpus is separate (`mnemo.eval_data`).
Its development and held-out partitions use disjoint scenarios, and include
corrections across sessions. Do not tune thresholds on held-out results.

The versioned `quality-v2` suite adds three source-reviewed development records
(140 turns) and a 42-turn holdout. The `quality-v3` suite reuses those development
sources and reserves a different 46-turn holdout before policy changes. Each
manifest pins source hashes, label hashes, disjoint session IDs and selection
rules; v3 also records policy-freeze and label-freeze timestamps. The policy was
frozen before its new holdout was read or labeled. After evaluation a holdout is
consumed validation data, never a fresh target for tuning. Both suites use the
same packaged upstream MIT license. These records broaden external coverage but
do not constitute one continuous naturalistic 200-turn conversation.

The v3 verifier case files are deliberately selected **development regressions**,
including cached baseline verdicts. They are not random accuracy samples or
held-out calibration data. Post-run source judgments live under `docs/quality-v3`
and do not silently change the frozen sidecar labels or strict scores.

`quality-v4/naturalistic-200.json` is a separate, continuous fictional Harbor Voices
project conversation with 100 user turns and 100 assistant turns over ten sessions.
It is authored development data, not a held-out or verified human conversation.
Every turn has an explicit sidecar label list. Source and label hashes are checked
by `load_naturalistic_dataset()`. Source review preceded model output and corrected
an interview topic-order label that incorrectly implied temporal order. Some labels
need preceding user context to resolve references; current extraction is turn-only.

The v4 external manifest reuses the three frozen development records and reserved
`e3038f8c` (48 turns, four disjoint sessions). Its atomic labels were written only
after the v6.2 policy freeze and before any output on that record. Its source-review
notes distinguish friend/grandmother vase references, tentative club/storage plans,
collection counts and an ambiguous new-record addition. The review does not infer
58 records from an explicitly repeated count of 57. Assistant inheritance claims
are not accepted as user evidence. Labels remain provisional single-reviewer work.

The v4 holdout is now evaluated and consumed. Both the authored 200-turn report
and the 48-turn holdout retain failed extraction turns and unchanged strict scores.
Every gated historical write and strict must-keep miss has a source-linked review
in `docs/quality-v4`. Post-run label issues remain separate from frozen labels.
