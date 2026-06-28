# v10: lasting tiering (durable by default, decay decides)

**Decision: failure; legacy tiering stays the default.** Every durability, safety and
retrieval check passed. Luna's 20 written must-keep targets and qwen's 16 were
all **durable** (legacy: none), with zero unsupported or forbidden writes and no
retrieval regression. The one failure is `make eval`: the junk turn "2 + 2 is 4
right?" (importance 1, labeled not to keep) became a session memory, so gated
precision fell from 90.9% to 83.3% (recall and must-keep stayed at 100%, false
writes at 0). The cause is the chosen weights, not the approved policy: identity
novelty is always 1.0, so every fact starts at 0.4 and importance 1 scores 0.46,
above the 0.45 noise floor. **Nothing is ever dropped as noise under these
settings**, which contradicts the approved "ephemeral noise is still dropped".

The protocol was committed in `d3693d0` before any run. All phases were local.

## Result

| Measurement | Luna legacy | **Luna lasting** | qwen lasting |
| --- | ---: | ---: | ---: |
| Written complete targets that are durable | 0 / 18 | **20 / 20** | **16 / 16** |
| Retrieved complete targets | 14/23 | 16/23 | 16/23 |
| Gated writes: durable / session | 0 / 51 | 52 / 7 | 46 / 4 |
| Gated writes: supported / unsupported / ambiguous | 47 / 0 / 4 | 55 / 0 / 4 | 49 / 0 / 1 |
| Strict forbidden-label writes | 0 | 0 | 0 |
| `make eval` gated | 90.9% P / 100% R | **83.3% P** / 100% R | – |

Luna's legacy arm is an infrastructure outlier: it took 746 s instead of about
300 s, and the fallback verifier rejected 18 candidates instead of the usual 9–10,
including two must-keep targets. Slow checks fail closed. No check compares
against that arm; v8 and v9 measured the same legacy policy at 16/23.
Retrieval under lasting equals v8's 16/23, because routing stays off and the
four identity overwrites remain (see v9).

**Labels (reported, not gated).** Of Luna's 69 candidates under lasting tiering:
43 of 54 lasting-labeled facts are durable (8 were rejected by the gate, 3 went to
session at importance 3–4). 9 of 15 transient-labeled facts are durable ("needs
help with feature engineering", "open to suggestions", "felt disappointed", …) and
would fade through decay if never recalled. 4 went to session and 2 were rejected.
Importance doesn't separate them, as the protocol expected.

**What failed and what it implies.** The approved rule has three bands: noise
(dropped), low importance (session) and importance ≥ 5 (durable). These settings
implemented only the top two. Keeping the noise band means raising the ephemeral
floor above the importance-2 score (0.52), for example to 0.55. That would drop
importance 1–2, keep 3–4 in session and leave 5+ durable. Per the protocol, no
weight change follows from v10; that correction would need its own frozen
protocol. See [the decision](decision.json).

## Protocol (frozen before the run)

### Why

In v8 and v9, every extracted fact landed in the session tier, which expires after
about 24 hours. Luna's best write scored 0.64 against the 0.70 durable cutoff. It's
structural:

- **Specificity** rewards predicates from a 14-word controlled vocabulary, but the
  extractor has been open-vocabulary since v5. All 55 of Luna's v9 writes scored
  0.2 on it.
- **Cosine novelty** compares "user predicate value" strings, which all look alike
  (median novelty 0.3). Repeats are now handled by fact identity anyway.
- The **transient** check flags whole turns containing "just". In the development
  data, "just" appears only in turns reporting completed events ("I just baked a
  chocolate cake for my sister's birthday"), all of them must-keep facts.
- **Importance doesn't separate what matters:** must-keep candidates were rated
  5–7 and everything else 3–7.

The owner approved Option A under rule 6: a verified, non-transient fact of
importance ≥ 5 is durable, and forgetting is left to decay. An unused durable fact
is archived (reversibly) after roughly 9–23 days depending on importance, and each
recall strengthens it.

### The change (settings only; defaults unchanged until this passes)

| Setting | Legacy | Lasting |
| --- | --- | --- |
| `w_imp` / `w_spec` / `w_nov` | 0.4 / 0.3 / 0.3 | 0.6 / 0.0 / 0.4 |
| `novelty_mode` | `cosine` | `identity` (1.0) |
| `transient_markers` | includes "just" | drops "just" |
| `transient_penalty`, cutoffs, verifier, embeddings, routing (off), decay | unchanged | unchanged |

Under lasting, a non-transient fact of importance ≥ 5 scores at least 0.70
(durable), and importance 1–4 is session. A transient-marked fact needs
importance ≥ 8 to be durable. The legacy gate fingerprint equals v8's.

### Phases (all local; no paid calls)

`luna-legacy` and `luna-lasting` replay the frozen v8 Luna candidates;
`qwen-lasting` replays the frozen v5 qwen candidates; `eval` runs `make eval`
under both policies; `score` applies the unchanged `equivalence-v2` scorer and
this rule. The [manifest](experiment-manifest.json) pins every input by hash.

Before any lasting-tier run, each of Luna's 69 candidates was labeled lasting (54)
or transient (15) in [durability-labels.json](durability-labels.json). Those
labels are **reported, not gated**. The approved policy accepts that some
transient facts become durable and then fade through decay; the labels measure
how many.

### Decision rule

Success requires all of:

1. Luna: every historically written complete must-keep target has a durable write;
2. qwen: the same;
3. zero unsupported gated historical writes in both;
4. zero strict forbidden-label writes in every arm;
5. no retrieval regression: Luna and qwen each at least 16/23;
6. `make eval`'s gated result identical under both policies.

On success, the lasting settings become the defaults in a separate commit, with
the scoring tests and spec §13 updated. On failure, the legacy defaults stay and
no further weight search follows from this protocol.

**Limits.** The policy was designed after inspecting these 20 development turns,
so success shows the rule fixes the diagnosed defect, not that it generalizes.
Labels are provisional agent judgments, the fallback verifier isn't
byte-deterministic, a replay doesn't exercise decay, and `b46e15ed` stays sealed.
