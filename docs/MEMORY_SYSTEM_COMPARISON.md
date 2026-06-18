# What Mnemo should borrow from existing memory systems

Research date: September 19, 2026. Mnemo checkout: `a194786`.
“Congee” is interpreted as **Cognee** (`topoteretes/cognee`).

The [combined implementation plan](MEMORY_TOOL_IMPLEMENTATION_PLAN.md) turns
these findings into ordered milestones, contracts, tests and spending limits.

## Decision

**The caller-controlled write → history → restore workflow is feasible.**
Mnemo already implements its central database operation. The next investment
should make that operation reliable for an external agent, with a bounded pilot.
An internal extraction LLM is unnecessary for this workflow; the host LLM still
has to select the right memory and operation.

**Memory history and undo are existing capabilities elsewhere.** Mem0 exposes
history. Cognee implements graph export and pipeline rollback. Timescale's
Memory Engine is an especially close reference: PostgreSQL, MCP, history and
restoration into a new version, including an optional concurrent-change guard.
This supports technical feasibility, but removes “nobody else has memory undo”
as a defensible product claim. The sections below cite the implementations.

This is source inspection, not a comparative quality, performance or security
benchmark. Competitor test suites were not executed. No production code or
dependencies were changed; the frozen extraction results remain unchanged.

## 1. Mem0: borrow explicit memories and direct writes

Mem0 documents `infer=False` for storing supplied messages without its fact
extraction step. Inference is a selectable behavior, rather than a prerequisite
for every memory write. Direct mode can retain duplicate entries, so bypassing
extraction does not resolve identity or duplication by itself.
[Add documentation](https://docs.mem0.ai/core-concepts/memory-operations/add).

It also exposes update by `memory_id`, separately from creation, and a history
API containing prior/new values. These are useful interface precedents for
Mnemo's caller-supplied facts.
[Update documentation](https://docs.mem0.ai/core-concepts/memory-operations/update),
[history documentation](https://docs.mem0.ai/api-reference/memory/history-memory).

In the inspected Python OSS implementation, `_update_memory` changes the vector
record and then calls the history store. These are separate storage calls, not
Mnemo's shared PostgreSQL transaction. History is backed by a SQLite manager.
This observation applies to that code path; it does not establish how the hosted
Mem0 service handles transactions. No first-class guarded historical restore
was found in the inspected Python `Memory` class and operation documentation.
[Pinned implementation](https://github.com/mem0ai/mem0/blob/a39a802bbc93e85b820078cd3c4dbaf53af25dbe/mem0/memory/main.py#L2038),
[history storage](https://github.com/mem0ai/mem0/blob/a39a802bbc93e85b820078cd3c4dbaf53af25dbe/mem0/memory/storage.py).

**Apply to Mnemo:** expose create and update-by-ID distinctly; make extraction
optional in the proposed direct profile. Keep Mnemo's event, HEAD and request
receipt changes in one transaction. Wrapping another system's update API would
not automatically give us that atomicity or recover versions it never retained.

## 2. Cognee: borrow provenance and portable snapshots

Cognee's inspected MCP package offers a minimal tool mode with `remember`,
`recall`, and `forget`. Its permanent-memory path runs `add` and `cognify`;
this is a broader processing system than a revision ledger. The small tool
surface is useful even when the underlying implementation is sophisticated.
[MCP package](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee-mcp/README.md),
[remember implementation](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee/api/v1/remember/remember.py).

Its `GraphSnapshot` preserves typed nodes and edges through serialization.
The implementation and regression tests address a practical import failure:
retaining text is insufficient if the fields needed to index that text are lost.
[Snapshot implementation](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee/modules/migration/snapshot.py),
[snapshot tests](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee/tests/unit/migration/test_snapshot.py).

Cognee also tracks dataset/document/chunk references and pipeline runs.
The inspected rollback handler removes a failed run's contributions while
protecting artifacts owned by other runs; a recovery option preserves documents
completed in the interrupted run. It can delete unowned derived artifacts.
This is pipeline recovery, not evidence of arbitrary per-memory, append-only
historical restoration. Snapshot export alone does not establish that either.
[Source references](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee/infrastructure/databases/provenance/source_refs.py),
[rollback implementation](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee/modules/cognify/rollback.py).

There is also a Mem0 import adapter that retains external IDs and metadata.
That establishes a migration route, not live transactional undo across two
independent stores.
[Adapter](https://github.com/topoteretes/cognee/blob/4294605f20324b250fd0edf8893cb69dc4215369/cognee/modules/migration/sources/mem0.py).

**Apply to Mnemo:** keep the agent-facing tool set small; record source/action
lineage; eventually test export/import for both exact data and successful
retrieval. Use the authenticated connection or trusted host for actor identity.
An LLM-provided `actor` or provenance string is descriptive input, not proof of
who acted. Graph extraction, self-improvement and multiple storage backends are
unnecessary for the proposed single-memory pilot.

## 3. Memory Engine: the closest implementation to study

Timescale's Memory Engine documents `me_memory_history` and
`me_memory_revert`. Revert restores a stored snapshot into a new forward version.
Its optional `expectedVersionHash` protects a live memory against a changed
current payload; callers may omit it to override deliberately. The documented
audit retention is 30 days, which limits available restore targets.
[Revert documentation](https://docs.memory.build/mcp/me_memory_revert/).

This is implemented, not just a proposal: the SQL reads a historical snapshot,
locks the current row, checks access and the supplied hash, and applies the
restoration. A database trigger records the mutation. A mutable current row
with transactional audit triggers can therefore provide versioned restoration.
[Pinned SQL](https://github.com/timescale/memory-engine/blob/2ef90da9385448e0bbb02ed1da82c04bd663602d/packages/database/space/migrate/idempotent/004_memory_event.sql#L200).

Its history interface has scoped queries, pagination, actor/cause fields and
an `operationId` grouping a bulk statement. These are useful precedents for a
history response an LLM can navigate without receiving an entire event log.
An operation correlation ID is not, by itself, a durable retry receipt.
[History documentation](https://docs.memory.build/mcp/me_memory_history/).

**Apply to Mnemo:** require `expected_event_id` for mutations in the proposed
agent profile. Use the existing unique revision identifier instead of introducing
a content hash. In Memory Engine's inspected SQL the hash covers payload fields,
not the monotonically increasing version. As a design inference, an A → B → A
sequence can return to the same hash; Mnemo's unique event IDs can detect that
intervening history. This is a choice of concurrency semantics, not a claim that
Memory Engine's documented state-comparison contract is broken.
[Hash computation](https://github.com/timescale/memory-engine/blob/2ef90da9385448e0bbb02ed1da82c04bd663602d/packages/database/space/migrate/idempotent/001_memory.sql#L5).

Keep retry receipts separate: if a successful restore response is lost and
another change occurs, retrying the original request must return its recorded
result without restoring again. Document retention and historical access scope
explicitly. Do not promise unlimited recovery from a store that discards history.

## 4. Concrete Mnemo improvements, in order

The detailed API/transaction proposal is in
[Direct memory writes and reliable undo](MEMORY_VERSIONING_MVP_RESEARCH.md).

1. **Prove the existing flow externally.** A real stdio MCP client creates a
   PostgreSQL preference, changes it to MySQL, reads history, restores the saved
   PostgreSQL event, and reads it back. Use an isolated scope and disable the
   extraction worker for that deployment. Verify that no extractor/verifier
   runs. Embeddings remain a dependency of the existing write/search path.
2. **Make targeting and concurrency explicit.** Create-only and update-by-ID
   calls; return `fact_id` and `current_event_id` from get/search; require expected
   revisions for update/revert. Lock, compare, append and move HEAD atomically.
3. **Make retries and attribution reliable.** A scoped `request_id` and saved
   receipt commit with the mutation. Identical retries return the original
   result; reused IDs with different parameters fail. Preserve source trust;
   an agent revert must not automatically become high-trust human review.
4. **Give the agent usable history.** Bounded pages with exact version IDs,
   current revision, action, actor and source references. Keep action grouping
   distinct from transport retries. Record both the restored target and the
   revision displaced by a revert so an undo can itself be undone accurately.
5. **Before general memory use, fix independent identities.** The preference
   pilot fits today's subject/predicate key. Two classes or projects may need
   two IDs. Create-only behavior prevents a silent overwrite but does not itself
   provide collection/occurrence identity. Use the existing
   [identity proposal](quality-v6/IDENTITY_FOLLOWUP_PROPOSAL.md) for that separate
   migration; do not merge event predicates to make labels match.

After those work, source-preserving import/export is a reasonable extension.
Whole-session undo, branches, merges and a universal Mem0/Cognee wrapper would
expand the reliability problem substantially; they are outside this pilot.

## 5. A bounded investment and success criteria

Set a **one-week engineering budget as a planning limit, not a completion
promise**. First complete the external lifecycle proof. Spend the remaining
budget on the smallest reviewed contract changes and their failure cases.
Separate code time, model calls and actual cost in the report. Do not restart
extractor prompt tuning or open the reserved holdout for this milestone.

Deterministic checks should cover exact restore and immutable earlier payloads;
wrong-memory and cross-scope targets; two writers using the same revision;
A → B → A stale requests; a lost response retried after another update; conflicting
request IDs; and an interrupted transaction. Also check current get/search after
restore, so a successful SQL operation is not mistaken for usable agent memory.

Then test the host LLM separately on a small predeclared task set: explicit undo,
ambiguous undo, two different memories, stale state and reconnect/retry. Record
correct action/target selection, unexpected writes, clarification behavior,
latency and cost. Scripted calls cannot demonstrate conversational reliability.
Do not blend these results with the old extraction recall denominator.

For a portfolio, a reproducible demo and these failure-case checks are a useful
engineering result. For a product, ask a few developers to try the workflow in
their actual agents and observe whether they use undo again. If the pilot reveals
no reason to choose Mnemo over an existing implementation, finish it as a bounded
portfolio project or adopt the existing tool. Business demand is not established
by this source review.

## 6. Positioning, reuse and evidence boundaries

The spec's assertion that incumbents with mutable stores cannot guarantee
auditable restoration is too broad given Memory Engine's implementation. Propose
correcting that wording when the user-facing product contract is revised; this
research does not silently rewrite the current spec. Python, Postgres, MCP and
history are not unique by themselves. A simpler integration or a better measured
restore experience is a hypothesis to test, not an achieved advantage.

Borrow interface patterns, invariants and regression scenarios first. All three
inspected repository root licenses identify Apache 2.0; before copying particular
files, check their notices and bundled third-party terms, and preserve required
attribution. This research did not copy competitor implementation into Mnemo.

Pinned revisions inspected:

- Mem0: `a39a802bbc93e85b820078cd3c4dbaf53af25dbe` (September 18).
- Cognee: `4294605f20324b250fd0edf8893cb69dc4215369` (September 18).
- Memory Engine: `2ef90da9385448e0bbb02ed1da82c04bd663602d` (August 13).

[Source manifest](memory-system-research-sources.json) records downloaded file
hashes and upstream URLs. Tests were read where relevant, not executed upstream.
The preceding local check remains **7 passed / 13 database setup errors** because
Postgres and the Docker daemon were unavailable. This comparison adds no new
runtime verification. No held-out source or labels were opened.
