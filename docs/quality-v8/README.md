# v8: one bounded stronger-extractor measurement

**Decision: failure under the frozen rule, but not because of extraction.** Azure
`gpt-5.6-luna` completely extracted **20/23** distinct development targets versus
qwen's 16/23, with **zero unsupported writes**. All 20 were written to the store.
Only **16/23** were still current and retrievable: storage identity overwrote four
of them, so retrieved coverage tied the baseline and the rule (more than 16/23 and
more than the control) failed.

The protocol below was committed in `4830c55` before any Azure call. The blind
reviews were committed in `170b6f9` before any replay existed.

## Result

| Measurement | v6 baseline (qwen) | Same-code control (qwen) | v8 Luna |
| --- | ---: | ---: | ---: |
| Complete distinct targets: candidate | 16/23 | 16/23 | **20/23** |
| … historical (written at some point) | 16/23 | 16/23 | **20/23** |
| … current | 16/23 | 16/23 | 16/23 |
| … visible | 16/23 | 16/23 | 16/23 |
| … retrieved (decision metric) | 16/23 | 16/23 | 16/23 |
| Complete source-local occurrences | 16/24 | 16/24 | 21/24 |
| Gated historical writes: supported / unsupported / ambiguous | 50 / 0 / 1 | 49 / 0 / 1 | 56 / 0 / 4 |
| Candidates: supported / unsupported / ambiguous | 60 reviewed (v6) | same | 65 / 0 / 4 of 69 |
| Extraction requests / tokens / wall time | 26 / 31,318 / 299 s (v7 qwen) | – | 21 / 22,046 / 55 s |

The control replays the frozen v5 qwen candidates under today's code. It
reproduces 16/23 exactly, so code changes since v6 do not explain anything here.
It wrote 50 events where v6 wrote 51; the fallback verifier is a local LLM and
isn't byte-deterministic. See [the decision](decision.json), [Luna score](score.json)
and [control score](control-score.json).

### Where the four targets went

Every loss is `overwrite_or_visibility`: the store keeps one current value per
scope + subject + predicate, so a later assertion under the same predicate supersedes
an earlier, different one. The earlier event stays in history, but current reads
and retrieval stop returning it.

| Lost target (earlier turn) | Superseded by (later turn) | Kind |
| --- | --- | --- |
| `learned_to_make`: sauerkraut and kimchi | `learned_to_make`: vegan lasagna with cashew ricotta | Two distinct true facts, one slot |
| `attended_workshop`: fermentation workshop at a local food co-op | `attended_workshop`: a fermentation workshop | A vaguer restatement replaced the specific one |
| `working_on_project`: solo project for the Data Mining class | `working_on_project`: customer-data trends project | Two projects, or one; the source doesn't say (same as v7) |
| `attended_class`: vegan cuisine class (turn 1:0) | the same proposition restated (turn 1:8) | Same fact re-stored; the blind review gave no credit for restatements |

In the first three, true information disappears from current state. They are the case the deferred
[member/occurrence identity proposal](../quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md)
addresses: two facts that must coexist under one subject/predicate. The historical
stage (20/23) is the ceiling that change could reach on this set.

### Sensitivity to one review convention (post hoc)

Before any replay, the blind review chose **no** alternative-group credit, to
match the v6 baseline review. v7 had credited the vegan-class target through its
later restatement. Applying only that v7 alternative gives **17/23** retrieved,
which would pass the rule. That number is computed after seeing results and is
**not** the outcome; it is recorded in
[sensitivity-v7-alternative.json](sensitivity-v7-alternative.json) because the
verdict depends on it. The robust findings don't depend on it: extraction rose
from 16 to 20, unsupported writes stayed at zero, and identity became the
binding loss.

### Other observations

- **Ambiguous writes rose from 1 to 4.** They are "Data Mining techniques" (an
  unstated referent), `considered_overmixing` (could read as contemplating the
  action), `planned_dietary_change` (a desire upgraded to a plan) and kimchi
  "based on" a workshop (at vs. learned from). The qwen fallback verifier accepted
  all four. It rejected nine other Luna candidates, all of which the review
  judged supported; only one (stand mixer) belonged to a target, which was partial anyway.
- **Every write in both arms was demoted to the session tier** (qwen 50/50,
  Luna 60/60): no write scored above the durable cutoff. That predates v8 and
  appears in the control. Under the current gate, even correct facts expire after
  the session TTL, which this set doesn't measure.
- Luna needed no validation retries (qwen's v7 run needed 6) and ran about 5×
  faster. Azure accepted `temperature=0`. No monetary cost is inferred from token
  counts.
- The stand-mixer target stayed partial: use plus acquisition, without asserted
  ownership. The two locked targets stayed ambiguous.

### What this does and doesn't show

It shows that on these 20 turns a stronger extractor removes most extraction
misses without adding unsupported writes, and that the remaining retrieval loss
comes from storage identity (plus one review convention). It doesn't show
generalization: this is one run, 20 selected development turns and provisional
agent reviews, with no whole-conversation, later-session or post-TTL
measurement. `b46e15ed` stayed sealed. Following the protocol, no prompt tuning,
rerun or second model follows. Supporting Azure as a production extractor and
changing identity are separate decisions.

## Protocol (frozen before the run)

### Question

Every recorded extraction result so far comes from 3–4B local models. v6 set the
reviewed development baseline with `qwen3.5:4b-mlx`: **16/23** distinct must-keep
targets completely retrieved, with 50 supported, 0 unsupported and 1 ambiguous gated
historical writes ([baseline score](../quality-v6/baseline-score-v2.json)). v7's
retry-feedback experiment did not improve on it. The master plan allows new
extraction work only with a stronger model and a new frozen protocol.

v8 asks: if **only the extraction model** changes to Azure `gpt-5.6-luna`, does
complete retrieved coverage exceed 16/23 without an unsupported write?

### What changes and what does not

| Changes | Unchanged |
|---|---|
| Extractor model: Azure OpenAI v1 deployment `gpt-5.6-luna`, `reasoning_effort=none`, `max_completion_tokens=2048`, JSON output | System prompt (sha256 `680ea700…`, same as baseline), message construction, parser, exact-evidence validation, two validation retries |
| Transport: [experiment-only adapter](../../scripts/azure_extractor.py), not wired into production | Verifier: NLI cross-encoder with `qwen3.5:4b-mlx` fallback; `nomic-embed-text` embeddings |
| | Gate thresholds, trust, identity, tiering, retrieval queries, `equivalence-v2` scorer, the 20 frozen development turns and labels |

`temperature=0` is sent unless Azure rejects it with HTTP 400 on one synthetic
smoke call (a sentence that isn't in any dataset). If it does, temperature is
omitted for the whole run. The model also changes provider, serving stack and
determinism; one run cannot separate those effects.

### Budget

Only extraction is paid. The adapter checks a hard cap of **64 requests or 150,000
total tokens** before every request, including the smoke call, validation retries
and at most two infrastructure retries (HTTP 429/5xx) per request. On exhaustion,
the remaining turns record errors, the partial report is kept and nothing is rerun.
For scale, the v7 qwen run used 26 requests and about 31,000 tokens.

### Phases

`docs/quality-v8/run_experiment.py` runs each phase once. It checks the frozen file
hashes and gate configuration in the [manifest](experiment-manifest.json) first,
and refuses to overwrite earlier evidence.

1. `extraction`: the smoke call, then the unchanged development probe with Luna as
   the extractor. Records every raw exchange, usage and wall time.
2. Blind review: I judge every Luna candidate against its source, and every must-keep
   occurrence, using the v6/v7 rubrics, **before** any replay output exists. The
   runner refuses step 4 until both review files exist and records their hashes.
3. `control`: replays the frozen v5 qwen candidates under today's code. The policy
   fingerprint changed after v6 (direct API, hash guards, revert refactor; no gate
   logic), and this shows whether that drift affects the 16/23 baseline.
4. `replay`: gates, stores and retrieves the Luna candidates with the baseline
   configuration.
5. `score`: the unchanged scorer on both, then the frozen rule.

### Decision rule

**Success** requires all three:

- Luna completely retrieves **more than 16/23** distinct targets (the frozen baseline);
- and more than the same-code control;
- with **zero unsupported** gated historical writes (ambiguous writes are reported
  separately).

Either way the result is recorded. Success does not automatically change prompts,
thresholds or identity, and doesn't make Azure a production extractor; that
would be a separate decision. Failure authorizes no tuning, rerun or second model.
An incomplete extraction counts as failure, with its partial evidence kept.

`b46e15ed` stays sealed. Results cover 20 selected development turns, one run and
provisional agent reviews; they say nothing about whole conversations, later sessions
or post-TTL recall.
