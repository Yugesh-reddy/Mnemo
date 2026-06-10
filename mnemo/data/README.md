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
