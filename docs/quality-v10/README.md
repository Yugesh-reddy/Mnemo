# v10: lasting tiering (durable by default, decay decides)

**Status: protocol frozen; not yet run.** Results replace this line after the run.

## Why

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

## The change (settings only; defaults unchanged until this passes)

| Setting | Legacy | Lasting |
| --- | --- | --- |
| `w_imp` / `w_spec` / `w_nov` | 0.4 / 0.3 / 0.3 | 0.6 / 0.0 / 0.4 |
| `novelty_mode` | `cosine` | `identity` (1.0) |
| `transient_markers` | includes "just" | drops "just" |
| `transient_penalty`, cutoffs, verifier, embeddings, routing (off), decay | unchanged | unchanged |

Under lasting, a non-transient fact of importance ≥ 5 scores at least 0.70
(durable), and importance 1–4 is session. A transient-marked fact needs
importance ≥ 8 to be durable. The legacy gate fingerprint equals v8's.

## Phases (all local; no paid calls)

`luna-legacy` and `luna-lasting` replay the frozen v8 Luna candidates;
`qwen-lasting` replays the frozen v5 qwen candidates; `eval` runs `make eval`
under both policies; `score` applies the unchanged `equivalence-v2` scorer and
this rule. The [manifest](experiment-manifest.json) pins every input by hash.

Before any lasting-tier run, each of Luna's 69 candidates was labeled lasting (54)
or transient (15) in [durability-labels.json](durability-labels.json). Those
labels are **reported, not gated**. The approved policy accepts that some
transient facts become durable and then fade through decay; the labels measure
how many.

## Decision rule

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
