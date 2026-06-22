# Host-agent undo pilot — September 22, 2026

**2/6 scenarios passed, one run per scenario. Zero unintended mutations across
all six scenarios; five intended model-written events.** The bounded pilot is
complete, but this host model did not satisfy the full interaction protocol.

The run used local `qwen3.5:4b-mlx` (4.5B, NVFP4) on Ollama 0.34.2, the six
published tool schemas and in-process `DirectMemory`. Embeddings were
non-semantic hash vectors. It made 31 model requests and 26 tool calls in
158.28 seconds, including setup and cleanup. No paid provider was used.

[results.json](results.json) is the original machine-readable transcript:
system prompt, schemas, every model request/response, tool arguments/results,
fixture and final event/HEAD/receipt snapshots, conflict injection and timings.
The [protocol](../README.md#protocol-fixed-before-the-run) was frozen before this
single run. No rerun, prompt adjustment or scorer change followed the results.

## Review of all six traces

1. **Create — pass; 12.72 s, 2 requests, 1 tool call.** `memory_create`
   wrote PostgreSQL once. The final reply accurately reported the stored value.
2. **Update — pass; 16.94 s, 4 requests, 3 tool calls.** Search found
   PostgreSQL, a current `memory_get` supplied the revision guard, and
   `memory_update` wrote MySQL once. The final reply matched the new HEAD.
3. **Undo — fail; 28.27 s, 5 requests, 4 tool calls.** An initial search
   returned no hits. A broader search and history read found the database and
   original event; guarded revert restored PostgreSQL once. The final reply
   was accurate, but the model never called current `memory_get` before writing.
   Correct final state does not satisfy the frozen read-before-mutation check.
4. **Targeted undo — fail; 43.84 s, 6 requests, 5 tool calls.** The model
   searched and inspected history for both database and language. It restored
   only PostgreSQL and retained Rust, exactly as its final reply stated, but
   again omitted current `memory_get` before the revert.
5. **Ambiguous undo — fail; 29.18 s, 8 requests, 8 tool calls.** Four
   searches returned no hits, a fifth found both memories, then the model read
   both current values and database history. It reached the eight-request limit
   without a final reply or clarification question. No event or receipt was
   written; MySQL and Rust remained current.
6. **Revision conflict — fail; 26.74 s, 6 requests, 5 tool calls.** After
   search and a current read, the fixture inserted SQLite before the first
   MySQL update. The stale update returned `REVISION_CONFLICT` without a write.
   The model then called `memory_get` **with SQLite's explicit event ID** and
   successfully retried MySQL. That historical read returned SQLite and the
   current event ID, but the frozen protocol requires a current read **without
   `event_id`** before recovery. The final reply matched the resulting MySQL
   HEAD. This is a protocol failure, not a failure of the revision guard.

Every model-written event matched its permitted fact, operation and value;
both reverts targeted the original database event. Five successful model
mutations produced five receipts. Fixture writes and the deliberately injected
SQLite event/receipt are excluded from the unintended-mutation count. The
snapshotted fields of all pre-existing events remained unchanged. No extraction
jobs, quality decisions or cache rows were created.

The model reused a patterned request UUID across the independent namespaces
and reused the failed request UUID for its conflict retry. The latter is allowed
by the API because failed operations leave no receipt. This run does not
establish request-ID discipline across a continuing conversation in one scope.

## Reproducibility and verification

The recorded source is commit `26a3ccc23f01750d0b258053e0886eaeaa9cf6f2`.
At the original review, `examples/agent_undo.py` matched its recorded SHA-256:
`9ce7357660a6786ee0e395faaa46f305f847734b19858b65db4f336bc52eed2f`.
The unmodified `results.json` SHA-256 is
`d90f927b5d23b75caf9ebc8a6b28d4400b21b6cb88be0c3215ae6c1c375995ae`.
The full model digest, generation settings and timing data are in the evidence.

Replaying `assess` offline over all six saved snapshots and traces reproduced
every score and failure reason. The runner reported cleanup success; a separate
database catalog check confirmed `mnemo_pilot_fd48aa11fd2a` no longer exists.
The pilot exited with status 1 because four scenarios failed their checks.

After the run, `make test-db` passed **326 tests with 1 skip**; `make lint` passed
Ruff and Black (80 Python files), and `uv lock --check` passed. `uv build` and
`bash scripts/check-wheel.sh` passed using an invocation-owned empty database
and an environment outside the checkout. Checks included the installed pilot's
`--help`, migrations, guarded SDK/demo, receipt replay, real direct MCP stdio
and locked source-archive installation. The packaging database was removed.

These six scripted observations do not establish general host-agent reliability,
semantic retrieval quality, MCP transport behavior or extraction accuracy.
Extraction remains unchanged since v7; the sealed holdout was not opened.
