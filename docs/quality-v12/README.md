# v12: identity routing with a stronger judge

**Status: protocol frozen; not yet run.** Results replace this line after the run.

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
