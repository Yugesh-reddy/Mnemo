# v9: identity routing on frozen candidates

**Decision: failure; `MNEMO_IDENTITY_ROUTING` stays `off`.** Routing did what it was
built for on the development turns: Luna's frozen candidates went from **16/23 to
19/23** retrieved complete targets, with zero unsupported writes and no credit
through restatement links. qwen's candidates stayed at 16/23, and `make eval` was
identical. The identity suite failed: **9/14 cases with routing versus 6/14
without**. Routing fixed every case that should keep several values, but it
left the old value current in three of four open-predicate corrections and in
the single-interview fix. The local verifier cannot tell a correction from a
coexisting sibling.

The protocol was committed in `f5d70ce` before any run. All phases were local.

## Result

| Measurement | Luna, off | **Luna, on** | qwen, on | (v8 qwen control) |
| --- | ---: | ---: | ---: | ---: |
| Complete targets: candidate / historical | 20 / 20 | 20 / 20 | 16 / 16 | 16 / 16 |
| … current | 16 | **20** | 16 | 16 |
| … visible / retrieved | 16 / 16 | **19 / 19** | 16 / 16 | 16 / 16 |
| Gated writes: supported / unsupported / ambiguous | 56 / 0 / 4 | 51 / 0 / 4 | 49 / 0 / 1 | 49 / 0 / 1 |
| Identity suite (14 cases) | 6 pass | **9 pass** | – | – |
| `make eval` gated | 90.9% / 100% | same | – | – |

Of Luna's 69 candidates, the gate rejected 10 (9 in v8; the fallback verifier
isn't deterministic), 52 took the attribute path, and routing changed only 7:
4 restatements, 2 new members and 1 exact repeat. That recovered all four v8
overwrite losses: the Data Mining project, the vegan class, the co-op workshop
and sauerkraut/kimchi. See
[the decision](decision.json) and the [Luna](luna-on-score.json) and
[qwen](qwen-on-score.json) scores.

### The lost chocolate cake: a routing bug, found here

Routing on lost one target that routing off kept. Turn `3:10` stored "baked a
chocolate cake for the sister's birthday" as a **session-tier** value in session
`_3`. Turn `1:0` said the same thing, so routing recorded a restatement and wrote
nothing. The retrieval probe runs in session `_1`, where session `_3`'s value
isn't visible, so the target was lost at the visible stage. The attribute path
already re-writes a value from another session instead of treating it as a
duplicate; the restatement route skipped that check. It is fixed in a separate
later commit and is **not** re-measured here: the number above is the frozen
result.

### Identity suite

| Case | Kind | Off | On | With routing on |
| --- | --- | :---: | :---: | --- |
| budget_replacement | replacement | ✓ | ✓ | correction |
| meeting_free_day_correction | replacement | ✓ | ✗ | Friday stays current |
| employer_change | replacement | ✓ | ✗ | Acme stays current |
| diet_change | replacement | ✓ | ✗ | vegetarian stays current |
| location_move | registry replacement | ✗ | ✗ | the gate rejects "moved to Denver" in both arms |
| role_change | registry replacement | ✓ | ✓ | attribute |
| concurrent_skills | members | ✗ | ✓ | new member |
| concurrent_instruments | members | ✗ | ✓ | new member |
| two_interviews | occurrences | ✗ | ✓ | new member |
| correct_one_interview | fix one occurrence | ✗ | ✗ | May 10 stays beside May 11 |
| ambiguous_projects | members | ✗ | ✓ | new member |
| repeated_class | restatement | ✗ | ✓ | restatement |
| vaguer_restatement | restatement | ✗ | ✓ | restatement |
| exact_repeat | repeat | ✓ | ✓ | same value |

Without routing: 8 false replacements and 3 extra values. With routing: 1 false
replacement (the gate-rejected Denver move, identical in both arms) and 5 extra
values, four of them stale values after a correction.

**Why corrections fail.** For open predicates the cross-encoder has no template, so
the `qwen3.5:4b-mlx` fallback judges contradiction. It labelled the Friday→Monday,
Acme→Globex and vegetarian→vegan turns as contradictions at probability **0.95**,
below the frozen 0.99 threshold, so routing kept both values. It gave the **same
0.95 contradiction** to "a second interview with Lena on May 10" against "an
interview with Lena on May 3", which should coexist. No threshold separates those:
at 0.95, two-interviews would become a false replacement, and correcting one
interview would contradict both and be marked unresolved. The routing signal needs
a better contradiction judge, not a different threshold. Per the protocol, no
threshold or prompt was changed.

The suite's routing-on run used 55 local verifier LLM requests (21,811 input /
4,799 output tokens) and 3 NLI calls. Phase wall times: Luna off 308 s, Luna on
384 s, qwen on 299 s, suite off 119 s, suite on 224 s.

### What this shows

Storage identity plus routing removes the v8 overwrite losses on real development
turns without new unsupported writes. It cannot yet ship as default, because a
wrong "coexist" leaves stale facts current after a correction, and this verifier
can't make that call reliably. The member/occurrence API (spec §17) is usable
now by callers that know their own identities. Suite cases are authored, labels
are provisional agent judgments, the fallback verifier isn't byte-deterministic,
and `b46e15ed` stayed sealed.

## Protocol (frozen before the run)

### Question

v8 showed that a stronger extractor wrote 20/23 development targets, but storage
identity (one current value per subject/predicate) overwrote four, so retrieval
stayed at 16/23. Pilot B added member/occurrence identities (migration 0011,
[spec §17](../../PROJECT_SPEC.md#17-member-and-occurrence-identities-pilot-b--september-22-2026))
and contradiction-gated routing behind `MNEMO_IDENTITY_ROUTING` (default `off`).

v9 asks whether turning routing on keeps more targets current and retrievable,
**without** false replacements, stale values, duplicates or new unsupported writes.
If it passes, the default flips to `contradiction`.

### Design

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

### Decision rule

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
