# v11: lasting tiering with the noise band restored

**Decision: success; the lasting settings become the defaults.** Every check passed:
all written must-keep targets are durable (Luna 20/20, qwen 16/16; under legacy
tiering, none), with zero unsupported or forbidden writes, retrieval at 16/23 for
both, `make eval` byte-identical to legacy (90.9% / 100%, zero false writes), and
no candidate below importance 3 written. The protocol was committed in `463f6fb`
before any run. All phases were local.

| Measurement | Luna lasting | qwen lasting |
| --- | ---: | ---: |
| Written complete must-keep targets that are durable | **20 / 20** | **16 / 16** |
| Gated writes: durable / session | 52 / 8 | 46 / 4 |
| Gated writes: supported / unsupported / ambiguous | 56 / 0 / 4 | 49 / 0 / 1 |
| Retrieved complete targets | 16/23 | 16/23 |
| `make eval` gated (legacy → lasting) | 90.9% P / 100% R → **same** | – |

**Reported, not gated.** Of Luna's candidates, 43 of 54 lasting-labeled facts are
durable (7 rejected by the gate, 4 in session). 9 of 15 transient-labeled facts
are also durable ("needs help with feature engineering", "open to suggestions",
…), and would be archived by decay after roughly 11–15 days if never recalled.
4 went to session and 2 were rejected. That is the trade-off the owner accepted.
Retrieval stays at 16/23 because identity routing is off and the four overwrites
from v8 remain; routing is v12's question. The four ambiguous Luna writes are now
durable too; they were already stored in every earlier arm.

**Aborted first attempt.** The first run failed within seconds because Docker
(Postgres) and Ollama were down. Nothing was measured, and it is preserved in
[aborted-attempt-1](aborted-attempt-1/NOTE.md). The services were restarted and
the unchanged protocol was run again.

**Limits.** This is the second protocol on the same 20 development turns, so it
shows the approved three-band rule works here, not that it generalizes. Decay was
not exercised. See [the decision](decision.json).

## Protocol (frozen before the run)

[v10](../quality-v10/README.md) made every written must-keep target durable but
failed one check: its weights gave every fact a base score of 0.4, so the
importance-1 junk turn "2 + 2 is 4 right?" cleared the 0.45 noise floor and
`make eval` precision fell from 90.9% to 83.3%. The approved policy has three
bands (noise dropped, low importance session, importance ≥ 5 durable); v10
implemented two.

v11 changes one setting in the lasting policy: `ephemeral_floor` 0.45 → 0.55.

| Importance | 1–2 | 3–4 | 5–10 |
| --- | --- | --- | --- |
| Normal fact | dropped | session | durable |
| Transient-marked fact | dropped | session | session below 8, durable at 8+ |

Everything else is v10's: the same weights, identity novelty, transient markers
without "just", frozen v8 Luna and v5 qwen candidates, reviews, labels (reused
from v10 unchanged) and scorer. Routing stays off. All phases are local. The
[manifest](experiment-manifest.json) pins every input.

**Decision rule.** Success requires every written must-keep target to be durable
(Luna and qwen), zero unsupported and forbidden writes, retrieval of at least 16/23
for both, `make eval` identical under both policies, and no candidate of
importance below 3 written. On success, the lasting settings become the defaults
in a separate commit. On failure, the legacy defaults stay and no further weight
change follows.

**Limits.** This is the second protocol on the same 20 development turns, prompted
by v10's failure, so success shows the approved rule works here, not that it
generalizes. Labels are provisional, the fallback verifier can time out, decay
isn't exercised by a replay, and `b46e15ed` stays sealed.
