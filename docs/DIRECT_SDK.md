# Guarded direct memory in Python

Apply all migrations with `make migrate`. `Mnemo.direct` provides create-only
writes, updates by fact ID, guarded revert, current/historical reads, paginated
history and search. The async equivalent is `DirectMemory(store)` from
`mnemo.direct`.

```python
from uuid import uuid4

from mnemo import Mnemo
from mnemo.config import get_settings
from mnemo.embedder import build_embedder

settings = get_settings()
embedder = build_embedder(settings)
memory = Mnemo(settings.dsn, embedder, namespace="example-" + uuid4().hex)

created = memory.direct.create(
    "user", "preferred_database", "PostgreSQL", request_id=uuid4()
)
changed = memory.direct.update(
    created.fact_id,
    "MySQL",
    expected_event_id=created.event_id,
    request_id=uuid4(),
)
restore_request = uuid4()
restored = memory.direct.revert(
    created.fact_id,
    created.event_id,
    expected_event_id=changed.event_id,
    request_id=restore_request,
    actor="my-agent",
)
assert memory.direct.get(created.fact_id).value == "PostgreSQL"
page = memory.direct.history(created.fact_id, limit=20)
hits = memory.direct.search_direct("PostgreSQL", limit=5)
```

For a model-free local check, set `MNEMO_BACKEND=hash` and
`MNEMO_WORKER_ENABLED=false` before running the example. Hash embeddings are
deterministic test vectors; their similarity is not semantic relevance. Existing
Ollama/OpenAI backends remain available.
Close an Ollama/OpenAI embedder with `embedder.close()` when its owner shuts down.

Generate and retain one `request_id` per intended mutation. If a response is lost,
retry with that ID and **all the same parameters**, including the expected event
and actor. The receipt returns the original result with `replayed=True`; it does
not move HEAD again. The result describes that mutation, so use `get` to learn the
current HEAD after a replay. Request IDs are scoped to namespace/user/agent.

Catch `MnemoError` and inspect `error.code` (an exported `ErrorCode`) or
`error.to_dict()`. On `REVISION_CONFLICT`, read current state and reconsider the
change. A deliberate new mutation needs a new request ID and the new expected
revision. Reusing an ID with different parameters raises `REQUEST_ID_REUSED`.

Exact-value updates and revert-to-current return `status="no_change"` and retain a
receipt. Spelling changes are real revisions. Create/update use low trust;
revert preserves the restored event's trust and provenance. Archived, invalidated,
session and expiring states cannot be revived through this API.

Use `get(fact_id, event_id)` for a full historical value and its `restorable` flag.
History previews stop at 256 characters and indicate truncation; pass
`page.next_cursor` to `history` for the next page. Concurrent writes do not enter
an already-started history traversal. To undo a revert, restore the
`previous_event_id` from its result while expecting the current HEAD. Undoing
creation is unsupported.

The synchronous client opens and closes a connection per call. Inside an async
application, bind `DirectMemory` to a `MnemoStore` using your own connection or
pool. The six-tool direct MCP profile is a later master-plan phase; the existing
MCP server retains its legacy behavior.
