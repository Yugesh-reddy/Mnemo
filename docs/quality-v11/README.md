# v11: lasting tiering with the noise band restored

**Status: protocol frozen; not yet run.** Results replace this line after the run.

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
