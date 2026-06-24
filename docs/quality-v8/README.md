# v8: one bounded stronger-extractor measurement

**Status: protocol frozen; not yet run.** This file is updated with results after
the single run. Everything below was fixed before any Azure call.

## Question

Every recorded extraction result so far comes from 3–4B local models. v6 set the
reviewed development baseline with `qwen3.5:4b-mlx`: **16/23** distinct must-keep
targets completely retrieved, with 50 supported, 0 unsupported and 1 ambiguous gated
historical writes ([baseline score](../quality-v6/baseline-score-v2.json)). v7's
retry-feedback experiment did not improve on it. The master plan allows new
extraction work only with a stronger model and a new frozen protocol.

v8 asks: if **only the extraction model** changes to Azure `gpt-5.6-luna`, does
complete retrieved coverage exceed 16/23 without an unsupported write?

## What changes and what does not

| Changes | Unchanged |
|---|---|
| Extractor model: Azure OpenAI v1 deployment `gpt-5.6-luna`, `reasoning_effort=none`, `max_completion_tokens=2048`, JSON output | System prompt (sha256 `680ea700…`, same as baseline), message construction, parser, exact-evidence validation, two validation retries |
| Transport: [experiment-only adapter](../../scripts/azure_extractor.py), not wired into production | Verifier: NLI cross-encoder with `qwen3.5:4b-mlx` fallback; `nomic-embed-text` embeddings |
| | Gate thresholds, trust, identity, tiering, retrieval queries, `equivalence-v2` scorer, the 20 frozen development turns and labels |

`temperature=0` is sent unless Azure rejects it with HTTP 400 on one synthetic
smoke call (a sentence that isn't in any dataset). If it does, temperature is
omitted for the whole run. The model also changes provider, serving stack and
determinism; one run cannot separate those effects.

## Budget

Only extraction is paid. The adapter checks a hard cap of **64 requests or 150,000
total tokens** before every request, including the smoke call, validation retries
and at most two infrastructure retries (HTTP 429/5xx) per request. On exhaustion,
the remaining turns record errors, the partial report is kept and nothing is rerun.
For scale, the v7 qwen run used 26 requests and about 31,000 tokens.

## Phases

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

## Decision rule

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
