# Preserve concurrent facts: concrete follow-up for review

Status: **proposal only; not approved or implemented**. This supplements the
[v4 identity design](../quality-v4/IDENTITY_PROPOSAL.md). The source-span experiment
does not authorize a schema or identity-contract change. AGENTS.md rule 6 requires
confirmation before adding dependencies, changing schema, or altering write/read
semantics. Independent identity-label adjudication remains a prerequisite for an
automatic resolver; the current reviews concern assertion meaning, not identity.

## Measured evidence

The experimental [replay](span-replay-complete.json) preserves these disposable
evaluation fact IDs and immutable event links. They are evidence identifiers,
not IDs to migrate in a user's persistent database.

| Fact ID | Earlier event → later event | Observation |
|---|---|---|
| `fcafefef-0013-4e17-8e21-44ef6f8a1d37` | `de83c822-32fc-4d56-aae9-dda8dc406a44` → `ffdf7e2e-986b-4e36-9601-708125c52a0e` | `learned_to_make` sauerkraut/kimchi is replaced by vegan lasagna; no source retracts the earlier skill. |
| `9a72187a-3240-4389-b2a4-884950d84621` | `e21f5705-2652-4e8a-828e-d5ef64ced964` → `04688f73-b1a7-47b3-b3a1-b72c3018cf76` | A later `working_on_project` value loses the Data Mining course and solo qualification. Whether these describe the same project is unresolved. |
| `4c6f4f9f-2979-4bb0-b17b-18a809a27c4d` | `3c142553-af85-4175-a99a-de612c1f9e89` → `8d42125d-7ab6-4755-aaf4-4e259b9a1a46` | Vegan class is mentioned again. Replacing this value does not itself lose the unqualified class-attendance target; count the later equivalent. |

`core.canonicalize()` creates a subject/predicate key; `add()` finds its scoped
HEAD and `_apply_to_existing()` appends an UPDATE for a changed value. More
consistent predicates therefore expose collisions. Changing spelling aliases
cannot determine whether a new value is a correction, another member, or another
occurrence. Do not turn these three examples into predicate-specific exceptions.

## Proposed SQL boundary

The following is a reviewable migration sketch, deliberately outside `migrations/`.
It does not modify any `memory_event` payload or reclassify historical facts.

```sql
ALTER TABLE memory_fact
  ADD COLUMN identity_mode text NOT NULL DEFAULT 'attribute',
  ADD COLUMN identity_ref uuid NOT NULL
    DEFAULT '00000000-0000-0000-0000-000000000000',
  ADD CONSTRAINT memory_fact_identity_mode_check
    CHECK (identity_mode IN ('attribute', 'member', 'occurrence')),
  ADD CONSTRAINT memory_fact_identity_ref_check CHECK (
    (identity_mode = 'attribute') =
    (identity_ref = '00000000-0000-0000-0000-000000000000'::uuid)
  );

ALTER TABLE memory_fact
  DROP CONSTRAINT memory_fact_namespace_user_id_agent_id_fact_key_key,
  ADD CONSTRAINT memory_fact_scoped_identity_key
    UNIQUE (namespace, user_id, agent_id, fact_key, identity_mode, identity_ref);
```

Existing facts remain attribute identities, retaining their IDs, keys and HEADs.
New member/occurrence identities use a stable, caller-managed UUID scoped by the
same namespace/user/agent and canonical subject/predicate. Mutable values, dates,
display wording and embeddings never generate that identity. Existing aliases
remain unchanged; their collision audit must precede any separate alias proposal.
Do not backfill member IDs from model guesses or silently duplicate legacy rows.

## Proposed API and SQL changes

```diff
 async def add(self, subject, predicate, object, *,
+              identity_mode: Literal['attribute', 'member', 'occurrence'] = 'attribute',
+              identity_ref: UUID | None = None,
               ...):
```

Validate mode/reference before embedding or writing: attributes require `None`,
mapped internally to the zero UUID; other modes require a nonzero reference.
All three key lookups in `add()`/its race-recovery path and `_fact_id_for_key()`
must add `AND identity_mode=$5 AND identity_ref=$6`. `_insert_fact()` inserts both
fields; its existing savepoint and scoped lock remain. A conflict loser rereads
the complete six-part identity and follows the existing exact-value/no-op or
UPDATE behavior. Never select the first fact returned from an incomplete key.

Legacy `add()`, `log(subject, predicate)` and key-based lookups continue to address
only the attribute identity. Extend explicit identity lookups with both fields;
`fact_id` lookups remain scoped. Add the two fields to `Fact` and diagnostic
snapshots so callers can distinguish members. Search returns every eligible HEAD
once, using existing session/TTL/world-validity filters. Versioned views must
preserve their current column ordering when appending identity metadata.

Corrections reuse the same reference. New occurrences use different references.
Archive and revert continue to require a scoped fact ID; the target event must
belong to that same fact. Reverting one member cannot affect siblings.

The extraction worker must **not** forward arbitrary model IDs into these fields.
A separately reviewed resolver must bind trusted application references and
source evidence to replacement/duplicate/member/occurrence decisions. Ambiguous
references remain unresolved session observations with an audit reason, rather
than overwriting an attribute. The exact representation and expiry of such
unresolved observations need a further concrete worker/read-path design before
enabling automatic routing. SDK-only opt-in does not by itself repair extraction.

## Rollout and required acceptance

Deploy reader/writer support together before accepting non-attribute writes; old
binaries assume unique subject/predicate keys and cannot safely share a database
after new identities exist. Reverting application code is not a valid rollback
after that point. Preserve the expanded schema and disable new-mode writes; do
not delete or collapse member histories to recreate the old unique constraint.

Before approval for automatic routing:

1. Independently label concurrent skills, repeated class mentions, ambiguous
   projects, genuine corrections, and interview occurrences. Freeze disagreements.
2. Audit existing affected fact IDs and narrow aliases on the intended persistent
   store using read-only queries. The disposable IDs above are not that audit.
3. Produce the complete worker unresolved-observation design and API/SQL patch.
4. Verify budget replacement, two concurrent skills, two interviews, correction
   of only one interview, concurrent retries, cross-scope denial, archive/revert,
   historical reads, and later-session/TTL behavior.
5. Replay identical frozen development candidates before/after and report all
   historical false writes plus current/retrieved coverage. Keep `b46e15ed` sealed
   until a separate final evaluation protocol is frozen.

No approval is being inferred from this document. No migration was applied.
