"""Retrieval parity: a backend must find memories about as well as Ollama does.

40 memories a coding project would hold, 28 questions worded differently from the
stored text and 8 off-topic questions (tests/data/retrieval_set.json), searched through
the direct API exactly as the MCP tools search, limit 3. Live: each backend skips when
its model is unavailable. The set was written by the agent that added it, not
independent gold, and 28 questions are too few to separate backends by one or two.

Measured November 14, 2026 (ranked first / in the top 3 / off-topic questions that
returned a memory):

- Ollama nomic-embed-text, floor 0.50: 20-21 / 24 / 1 (ties between equal scores
  are ordered by random fact IDs, so the first column moves by one between runs)
- local bge-small-en-v1.5, floor 0.60: 19 / 25 / 3
- local bge-base-en-v1.5, floor 0.55: 19 / 26 / 2
- local nomic-embed-text-v1.5-Q, floor 0.50: 17 / 24 / 2 (not offered)

The bar below is Ollama's top-3 result, with ranked-first and off-topic allowing the
local default's measured result. Keep it there: the agent eval decides the default.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from mnemo.config import Settings
from mnemo.core import MnemoStore
from mnemo.db import drop_isolated_schema, prepare_isolated_schema
from mnemo.direct import DirectMemory
from mnemo.embedder import build_embedder, local_model_ready

SET = json.loads((Path(__file__).parent / "data" / "retrieval_set.json").read_text())
FIRST, TOP3, OFF_TOPIC_HITS = 19, 24, 3


def _available(backend: str) -> bool:
    settings = Settings(backend=backend)
    if backend == "local":
        return local_model_ready(settings.embed_model, settings.model_dir)
    try:
        tags = httpx.get(f"{settings.ollama_host}/api/tags", timeout=2.0).json()
    except Exception:
        return False
    return any(m["name"].split(":")[0] == settings.embed_model for m in tags.get("models", []))


async def _measure(dsn: str, backend: str) -> dict:
    settings = Settings(backend=backend, search_reinforce=False, worker_enabled=False)
    schema = "mnemo_eval_" + uuid4().hex  # its own schema, at this model's dimension
    db = await prepare_isolated_schema(dsn, schema, embed_dim=settings.embed_dim)
    embedder = build_embedder(settings)
    try:
        memory = DirectMemory(MnemoStore(db, embedder, settings=settings, namespace="parity"))
        fact_ids = []
        for row in SET["memories"]:
            created = await memory.create(
                row["subject"], row["predicate"], row["value"], request_id=uuid4(), actor="test"
            )
            fact_ids.append(created.fact_id)
        first = top3 = 0
        misses = []
        for question in SET["questions"]:
            hits = [hit.fact_id for hit in await memory.search(question["query"], limit=3)]
            want = fact_ids[question["answer"]]
            first += bool(hits) and hits[0] == want
            top3 += want in hits
            if want not in hits:
                misses.append(question["query"])
        off_topic = sum([bool(await memory.search(q, limit=3)) for q in SET["off_topic"]])
        return {"first": first, "top3": top3, "off_topic_hits": off_topic, "misses": misses}
    finally:
        close = getattr(embedder, "close", None)
        if close:
            close()
        await drop_isolated_schema(db, schema)
        await db.close()


@pytest.mark.parametrize("backend", ["ollama", "local"])
async def test_backend_finds_memories_about_as_well_as_ollama(_disposable_test_db, backend):
    if not _available(backend):
        pytest.skip(f"the {backend} embedding model is not available")
    result = await _measure(_disposable_test_db, backend)
    print(f"{backend}: {result}")
    total = len(SET["questions"])
    assert result["first"] >= FIRST, f"ranked first {result['first']}/{total}: {result}"
    assert result["top3"] >= TOP3, f"in the top 3 {result['top3']}/{total}: {result}"
    assert result["off_topic_hits"] <= OFF_TOPIC_HITS, result
