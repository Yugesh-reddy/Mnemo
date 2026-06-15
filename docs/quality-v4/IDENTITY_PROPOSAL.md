# Fact identity and simultaneous values: review proposal

Status: design only. No schema, public API or storage identity changes are included
in the current extraction milestone. The default remains one HEAD per scoped
subject/predicate. This proposal needs approval under AGENTS.md rule 6 before
implementation.

## The observed problem

A user can discuss learning several dishes, participating in several workshops,
or keeping several project goals. An extractor may faithfully produce the same
predicate for each independent event. The store currently interprets the later
value as an update to one fact. History survives, but earlier concurrent values
leave current memory. Giving every item an improvised predicate disguises the
problem and damages consistent corrections.

A new identity policy must distinguish a replacement (budget 100 to 150), a
concurrent member (another project goal), and another occurrence (a second
interview). Embedding similarity cannot establish any of these operations.

## Proposed contract

Keep existing add(subject, predicate, value) behavior and all existing identities.
Add an explicit, opt-in identity mode to a separate reviewed write request:

- **Attribute:** one value, one HEAD. Explicit corrections append UPDATE events.
  Budget, emergency contact and meeting-free day use this mode.
- **Collection member:** one fact per stable member identity within a collection.
  Additions preserve other active members. Removal archives only the identified
  member; it does not remove the collection. Member keys cannot depend on mutable
  display wording or embeddings.
- **Occurrence:** one fact per explicit occurrence identity. Correcting the date
  or duration of an existing interview updates that interview's HEAD. Another
  interview gets a different identity even if its description is similar.

The identity request is derived by application policy from trusted context and
verified evidence. A model can suggest a mode and reference, but cannot arbitrarily
choose a stored fact ID or overwrite another scope. Missing or ambiguous references
must remain unresolved session observations with a retained decision reason. They
must not silently become attribute replacements or permanent duplicate occurrences.

SDK calls using the existing API retain attribute behavior. Any new optional
identity fields require documented validation and migration behavior. A concrete
SQL migration and API diff should be reviewed before enabling another mode.

## Implementation sequence after approval

1. Freeze development identity labels: replacement, duplicate, concurrent member,
   new occurrence, correction to occurrence, and unresolved reference. Include
   same text in different sessions and late-arriving corrections.
2. Add explicit identity fields and constraints while preserving old attribute
   keys. Backfill only the attribute mode; do not reclassify historical rows using
   a model. Namespaces, user/agent boundaries and row/scope locks still apply.
3. Keep append-only mutation paths. Validate occurrence/member targets under the
   same lock as the HEAD update. Revert restores the selected identity's prior
   payload and embedding; it cannot cross into a sibling occurrence.
4. Extend retrieval and blame to expose each active member/occurrence once, with
   its complete evidence lineage. Keep session and valid-time filters unchanged.
5. Run development identity tests, then a fresh disjoint holdout. Publish both
   historical support and current coverage. Adopt only if concurrent-fact recall
   improves without false replacements, cross-scope writes or duplicate inflation.

## Required acceptance cases

- 100 becomes 150 on one budget identity; 100 remains in immutable history.
- A Friday-to-Monday correction does not create two active meeting-free days.
- Adding a fermentation workshop preserves an earlier Indian cuisine class.
- Two interviews with Lena on distinct dates coexist; fixing the second date
  changes only its occurrence and preserves its duration and interviewee binding.
- Concurrent retries of the same occurrence create one visible fact.
- Ambiguous “move that one to Monday” produces a retained unresolved decision.
- Reverting one collection member/occurrence cannot restore a sibling's event.
- Archive/TTL/session boundaries and as-of queries remain consistent in every mode.

Independent label adjudication, not a lower NLI threshold, is the prerequisite for
choosing and evaluating the identity resolver. Consolidation remains deferred.
