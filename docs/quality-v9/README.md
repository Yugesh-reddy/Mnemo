# v9: identity routing on frozen candidates

**Status: protocol frozen; not yet run.** Results replace this line after the run.

## Question

v8 showed that a stronger extractor wrote 20/23 development targets, but storage
identity (one current value per subject/predicate) overwrote four, so retrieval
stayed at 16/23. Pilot B added member/occurrence identities (migration 0011,
[spec §17](../../PROJECT_SPEC.md#17-member-and-occurrence-identities-pilot-b--september-22-2026))
and contradiction-gated routing behind `MNEMO_IDENTITY_ROUTING` (default `off`).

v9 asks whether turning routing on keeps more targets current and retrievable,
**without** false replacements, stale values, duplicates or new unsupported writes.
If it passes, the default flips to `contradiction`.

## Design

Only `MNEMO_IDENTITY_ROUTING` changes. Everything else is frozen by hash in the
[manifest](experiment-manifest.json): the v8 Luna candidates and reviews, the v5
qwen candidates and v6 reviews, the verifier (NLI plus `qwen3.5:4b-mlx` fallback),
embeddings, gate thresholds, retrieval queries and the `equivalence-v2` scorer.
With routing off, the gate configuration and fingerprint equal v8's exactly.
All phases are local; there are no paid calls.

| Phase | What it measures |
| --- | --- |
| `luna-off`, `luna-on` | The 20 development turns with Luna's frozen candidates, routing off and on |
| `qwen-on` | The same with qwen's frozen candidates (v8's control gave 16/23 with routing off) |
| `suite-off`, `suite-on` | [14 labeled identity cases](identity-cases.json) through the real worker, gate and verifier |
| `eval` | `make eval`'s scripted regression, routing off and on |

The development turns can show routing keeps facts, but not that it avoids stale
values after a real correction. The identity suite covers that. It has 14 authored
cases (29 turns), labeled before any run: six replacements (two through the
single-value registry), two sets of concurrent members, two interviews, correcting
one interview, ambiguous projects, two restatements and an exact repeat. Its
candidates are authored rather than extracted, so it isolates routing. A case
passes only if the predicate's current values equal its labeled set exactly.

A restatement is recorded as a duplicate linked to the existing event, and the
unchanged scorer can credit a target through that link. Targets credited only that
way are listed, and the rule requires the gain without them.

## Decision rule

Success requires all of:

1. Luna with routing retrieves **more than 16/23** complete targets (v8),
2. not fewer than Luna with routing off,
3. still more than 16/23 after removing restatement-link-only credit,
4. with zero unsupported gated historical writes;
5. qwen with routing retrieves at least 16/23 (v8's control);
6. **all 14 identity cases pass** with routing on;
7. `make eval`'s gated result is identical with routing off and on.

On success, the default flips to `contradiction` in a separate commit. On failure,
it stays `off`, the failing cases are recorded, and no threshold or prompt tuning
follows. Labels are provisional agent judgments; the suite is authored; the
fallback verifier is not byte-deterministic; `b46e15ed` stays sealed; later-session
and post-TTL recall are unmeasured.
