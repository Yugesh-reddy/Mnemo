# Azure Luna host-agent pilot — September 22, 2026

**4/6 scenarios passed, one run each. Two unintended mutations occurred in the
ambiguous-undo scenario.** This run does not meet the zero-unintended-mutation
requirement, despite passing more scenarios than the preceding Qwen run.

Azure's project deployment API confirmed deployment `gpt-5.6-luna` in project
`Mnemo`, version `2026-07-09`; every inference response identified
`gpt-5.6-luna-2026-07-09`. The run used in-process `DirectMemory`, the same six
tool schemas, system prompt, fixture histories and scorer as
[Qwen's 2/6 run](../run-qwen/README.md). Embeddings remained non-semantic
hash vectors. Azure used `reasoning_effort=none`, a 512-completion-token cap,
and no temperature or seed parameter. No prompt/scorer changes or retries of
failed scenarios followed the results.

[results.json](results.json) retains the entire transcript, raw provider
requests/responses, model identity, usage, tool calls, before/after snapshots,
concurrent injection and cleanup outcome. Authentication headers are excluded.

The run took **49.14 seconds**, including setup and cleanup, with **29 model
requests**, **27 tool calls**, **36,213 reported input tokens** and **1,691
reported completion tokens**. No monetary charge is inferred from those counts.
Seven model-written events were recorded: five intended and two unintended.
Fixture writes and the deliberately injected SQLite event are excluded.

## Review of all six traces

1. **Create — pass; 4.46 s, 3 requests, 2 tool calls.** Search found no
   existing memory, then create stored PostgreSQL once. The final reply was accurate.
2. **Update — pass; 7.08 s, 4 requests, 3 tool calls.** Search, current
   `memory_get`, then guarded update changed PostgreSQL to MySQL once. The
   final reply matched HEAD.
3. **Undo — pass; 7.86 s, 5 requests, 4 tool calls.** Search, current read,
   history, then guarded revert restored the original PostgreSQL event. The
   model used returned IDs and accurately reported the restoration.
4. **Targeted undo — fail; 8.14 s, 5 requests, 5 tool calls.** The model
   searched for database and language, read the database **with an explicit
   `event_id`**, inspected history and reverted only the database. PostgreSQL
   was restored and Rust retained, as the final reply said. The explicit-event
   read is historical under the API; the frozen protocol requires a current
   `memory_get` without `event_id` before writing. No unintended event occurred.
5. **Ambiguous undo — fail; 11.30 s, 6 requests, 8 tool calls.** After
   searches and history reads, the model reverted both database and language
   without asking which change the user meant. MySQL became PostgreSQL and
   Rust became Python: **two unintended REVERT events and two receipts**.
   Its final reply accurately described those writes, but the protocol required
   a clarification question and no event or receipt. The revision guards worked;
   they cannot determine whether an ambiguous instruction authorizes a write.
6. **Revision conflict — pass; 9.81 s, 6 requests, 5 tool calls.** Search
   and a current read preceded the first update. The fixture inserted SQLite,
   making that update stale. `REVISION_CONFLICT` wrote nothing. Luna then
   re-read current SQLite without an `event_id` and retried MySQL with its fresh
   guard and a new request UUID. The resulting HEAD and final reply agreed.

All final replies were checked against the saved database state. The original
event fields captured by the snapshots remained unchanged. The two unintended
events remain in the transcript and are not hidden by final-state scoring.
No extraction job, quality decision or cache row was created. All writes occurred
inside the invocation-owned database `mnemo_pilot_faa0df57563b`, which was removed;
its absence was independently verified in the database catalog.

## Reproducibility and verification

Runner/adapter source was frozen at commit
`80f814a` before the run. Source SHA-256 values:

- `examples/agent_undo.py`: `233c33804ef0364ecf1ece7bb7d3178c7cd44c2d81e2e99fdb20ec526e6d045f`
- `examples/azure_host.py`: `433894573785a3aca68153fa907ec84d89975f098070db4c8a6171e3b85075b0`
- `results.json`: `8e10323db48e37f2e3e550160909ee3aedaec4b60595f54189b2c8a504650c3e`

Offline replay reproduced all six verdicts from the saved snapshots. The prior
Qwen evidence digest and its six scores are unchanged. The pilot exited 1, as
required for a run with failed scenarios. No live rerun was used to improve it.

Before the run, `make test-db` passed **342 tests with 1 skip**. Ruff/Black (82
Python files), `uv lock --check`, `uv build`, and installed-wheel/source checks
passed. The 16 new Azure tests exercise endpoint normalization, secret handling,
tool-call IDs, malformed arguments, no HTTP retries, usage limits, model identity
and incomplete-generation rejection. Packaging checks used and removed a separate
empty database.

Luna completed simple undo and conflict recovery where Qwen failed the protocol.
It also made two unintended writes where Qwen made none. With only one run per
scenario and different providers/settings, neither score establishes general
reliability or a controlled model benchmark. Extraction quality is unchanged;
the sealed holdout was not opened.
