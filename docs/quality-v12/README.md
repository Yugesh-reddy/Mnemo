# v12: identity routing with a stronger judge

**Decision: failure; routing stays off. The judge's question is wrong, not the
model.** With routing and v11's lasting tiering, Luna's development candidates
reach **20/23 retrieved under either judge**, the ceiling for this set (the other
three are the two locked-ambiguous targets and the partial stand-mixer target).
There were zero unsupported or forbidden writes and every must-keep target is
durable. The identity suite improved from 9 to **11 of 13** gate-clean cases with
Azure Luna as judge, but not to all 13. The protocol was committed in `b83250e`
before any run; the judge used 42 requests (16.7k input / 2.8k output tokens)
across both paid phases.

| Measurement | Local judge | **Azure Luna judge** |
| --- | ---: | ---: |
| Luna dev: retrieved complete targets | 20/23 | 20/23 |
| Luna dev: supported / unsupported / ambiguous writes | 51 / 0 / 4 | 52 / 0 / 4 |
| Luna dev routes: new member / restatement / correction | 2 / 4 / 0 | 4 / 2 / 0 |
| Suite: gate-clean cases passed | 9 / 13 | **11 / 13** |
| Suite: false replacements / extra values | 1 / 5 | 2 / 2 |
| Judge requests (tokens) | – | 28 suite + 14 dev (19.5k total) |

Location_move is gate-rejected in both arms ("moved to Denver") and excluded, as
declared.

### What Luna fixed and what it broke

Luna labelled the meeting-day, employer and diet corrections as contradictions at
0.99, so all three now replace the old value; the local judge had stopped at 0.95.
But it also labelled **"I learned how to make vegan lasagna"** as contradicting
"learned to make sauerkraut and kimchi" at 0.99 ("the source states lasagna, *not*
sauerkraut and kimchi"). Concurrent skills, which the local judge passed, became a
false replacement. The interview cases scored 0.98, just below the 0.99 threshold,
so "a second interview on May 10" coexisted by luck; correcting it to May 11
contradicted both interviews at 0.99 and became unresolved.

The routing checks reuse the write gate's question, "does this source support the
assertion (entailment, contradiction or neutral)?" For a relation that can hold
several values, a capable model treats "a different value is stated" as
contradiction, which is exactly the confusion routing must avoid. A stronger model
only makes that answer more confident. The development turns contain no genuine
correction, so they can't reveal this; the suite does.

### What this implies

Routing needs a question built for routing: does this turn say the stored value
**is no longer true** (corrected, changed, moved, replaced), does it **add another**
item alongside, or does it **restate** it? That is a new judge prompt and a new
frozen protocol. No threshold or prompt change follows from v12. See
[the decision](decision.json).

## Protocol (frozen before the run)

[v9](../quality-v9/README.md) showed that identity routing keeps coexisting facts,
but the local `qwen3.5:4b-mlx` judge rated real corrections and coexisting siblings
as contradictions at the same 0.95. So three corrections left stale values, and
the default stayed off.

v12 changes **only the judge** for routing's two checks ("does the new turn
contradict this stored value?", "does this stored value already state the
candidate?"). It uses Azure `gpt-5.6-luna` through an experiment-only adapter
that sends the unchanged verifier prompt. Routing is on in every arm. The write
gate keeps the local verifier, and tiering uses the v11 defaults. The candidates,
reviews, labels, 0.99 threshold and scorer are unchanged; the
[manifest](experiment-manifest.json) pins them.

| Phase | Judge | Data | Paid |
| --- | --- | --- | --- |
| `suite-local` | local verifier | [v9 identity cases](../quality-v9/identity-cases.json) (14) | no |
| `suite-azure` | Azure Luna | same | yes |
| `luna-local` | local verifier | frozen v8 Luna candidates (20 dev turns) | no |
| `luna-azure` | Azure Luna | same | yes |

**Budget.** Each paid phase is capped at 150 requests or 200,000 tokens, checked
before every request, and about 70 requests are expected in total. On
exhaustion the judge raises, the phase fails, and nothing is rerun.

**Suite scoring.** As in v9, with one change declared now. A case whose authored
candidate the write gate rejects (v9's "moved to Denver") is reported as
gate-rejected and excluded from the routing pass count, because v12 doesn't change
the gate. At least 12 of the 14 cases must be gate-clean.

**Decision rule.** Success requires:

1. every gate-clean suite case to pass with the Azure judge;
2. at least 12 gate-clean cases;
3. Luna with the Azure judge to retrieve more than 16/23 (v11 with routing off),
   not fewer than with the local judge, and still more than 16/23 without
   restatement-link-only credit;
4. zero unsupported and forbidden writes;
5. every written must-keep target to stay durable;
6. the judge to finish within budget.

Success shows routing works with an adequate judge. Enabling it in production then
needs a supported way to configure that judge, and the local-only default stays
off. Failure changes nothing, and no threshold or prompt tuning follows.
`b46e15ed` stays sealed.
