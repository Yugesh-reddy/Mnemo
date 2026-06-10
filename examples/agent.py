"""Reference agent + the rollback demo (PROJECT_SPEC.md §10).

``run_scenario`` is the reusable, backend-agnostic core of the demo (it takes a store
+ worker, so tests drive it with a stub extractor). The Typer ``demo`` command wires it
to the real Ollama backend and prints the narrative; ``chat`` is a tiny interactive loop.

    make demo        # the scripted §10 rollback scenario, end to end
    python examples/agent.py chat
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import typer

from mnemo.core import MnemoStore
from mnemo.db import connect
from mnemo.embedder import build_embedder
from mnemo.extraction import ExtractionWorker, build_extractor

app = typer.Typer(add_completion=False, help="Mnemo reference agent + rollback demo.")

SESSION = "demo-session"


async def _drain(worker: ExtractionWorker) -> None:
    """Run the extraction worker until the queue is empty."""
    while await worker.process_one():
        pass


async def agent_answer(store: MnemoStore, fact_id: UUID, question: str) -> Any:
    """The agent answers from memory: search for context, read the relevant fact."""
    facts = await store.search("database", k=8, session_id=SESSION)
    match = next((f for f in facts if str(f.fact_id) == str(fact_id)), None)
    if match is None:
        match = await store.get(fact_id)
    return match.value if match else None


async def run_scenario(
    store: MnemoStore,
    worker: ExtractionWorker,
    *,
    say: Callable[[str], None] = lambda _s: None,
    stop_before_revert: bool = False,
) -> dict[str, Any]:
    """The §10 scenario. Returns key results so callers/tests can assert on them."""
    say("=== Mnemo demo — see → blame → revert → behavior change ===\n")

    # 1) The user states a fact. observe() caches it and the worker extracts it.
    say('[turn 1] User: "I use Postgres for my project."')
    await store.observe("t1", "I use Postgres for my project.", SESSION, role="user")
    await _drain(worker)

    facts = await store.search("database", session_id=SESSION)
    db = next(
        (f for f in facts if f.source == "semantic" and "postg" in str(f.value).lower()), None
    )
    if db is not None:
        fact_id, subject, predicate = db.fact_id, db.subject, db.predicate
        stored = db.value
    else:
        raise RuntimeError(
            "The extraction pipeline did not produce a database fact; "
            "inspect memory_decisions/health."
        )
    say(f"  extracted & stored: {subject} {predicate} = {stored} (trust={db.trust_level})\n")

    # 2) The agent mis-infers a switch to MongoDB (low-trust agent inference).
    say("[turn 2] The agent wrongly infers the user switched to MongoDB.")
    upd = await store.add(
        subject,
        predicate,
        "MongoDB",
        provenance="agent_inference",
        confidence=0.55,
        actor="agent",
    )
    say(f"  ✗ UPDATE: {predicate} = MongoDB  (agent_inference, trust={upd.trust_level})\n")

    # 3) Asked now, the agent answers from the (corrupted) memory.
    wrong = await agent_answer(store, fact_id, "What database do I use?")
    say('[turn 3] User: "What database do I use?"')
    say(f'  Agent: "You use {wrong}."   ← WRONG (from the bad inference)\n')

    # blame: where did each belief come from?
    history = await store.blame(fact_id=fact_id)
    say("--- blame (git blame for memory) ---")
    for e in history:
        say(f"  {e.op:<7} {str(e.value):<12} {e.provenance:<22} {e.trust_level:<6}")
    say("")

    if stop_before_revert:
        say("→ Open the web UI (make ui) to blame and revert this fact yourself.")
        return {
            "fact_id": fact_id,
            "ops": [e.op for e in history],
            "values": [e.value for e in history],
            "answer_wrong": wrong,
            "answer_fixed": None,
        }

    # 4) A human reverts to the original PostgreSQL belief.
    say("[human review] reverting to the original PostgreSQL belief…")
    add_event = history[0]
    await store.revert(fact_id, add_event.event_id)
    say("  ✓ REVERT applied\n")

    # 5) Same question, corrected answer — behavior visibly changed.
    fixed = await agent_answer(store, fact_id, "What database do I use?")
    say('[turn 4] User: "What database do I use?"')
    say(f'  Agent: "You use {fixed}."   ← FIXED (memory was corrected)\n')

    final = await store.blame(fact_id=fact_id)
    say("--- log (history intact) ---")
    say("  " + " → ".join(f"{e.op}({e.value})" for e in final))
    say(f"\n✓ Answer changed {wrong} → {fixed} after a human fixed the memory; history kept.")

    return {
        "fact_id": fact_id,
        "ops": [e.op for e in final],
        "values": [e.value for e in final],
        "answer_wrong": wrong,
        "answer_fixed": fixed,
    }


async def _scenario(stop_before_revert: bool) -> None:
    conn = await connect()
    try:
        embedder = build_embedder()
        extractor = build_extractor()
        store = MnemoStore(conn, embedder, namespace=f"demo-{uuid4().hex[:12]}")
        worker = ExtractionWorker(conn, embedder, extractor)
        print(f"Demo namespace: {store.namespace} (set MNEMO_NAMESPACE for the web UI)")
        await run_scenario(store, worker, say=print, stop_before_revert=stop_before_revert)
    finally:
        await conn.close()


async def _chat() -> None:
    conn = await connect()
    try:
        embedder = build_embedder()
        extractor = build_extractor()
        store = MnemoStore(conn, embedder)
        worker = ExtractionWorker(conn, embedder, extractor)
        print("Mnemo chat — type a message, or 'recall <query>' to search memory. Ctrl-D to exit.")
        turn = 0
        while True:
            try:
                text = input("you> ").strip()
            except EOFError:
                print()
                break
            if not text:
                continue
            if text.startswith("recall "):
                for f in await store.search(text[len("recall ") :], session_id="chat"):
                    print(
                        f"  • {f.subject} {f.predicate} = {f.value}  ({f.source}, {f.trust_level})"
                    )
                continue
            turn += 1
            await store.observe(f"c{turn}", text, "chat", role="user")
            await _drain(worker)
            print("  (remembered; extracted facts reconciled)")
    finally:
        await conn.close()


@app.command()
def demo() -> None:
    """Run the scripted §10 rollback scenario end to end."""
    asyncio.run(_scenario(stop_before_revert=False))


@app.command()
def setup() -> None:
    """Leave the corrupted (MongoDB) state so you can revert it in the web UI."""
    asyncio.run(_scenario(stop_before_revert=True))


@app.command()
def chat() -> None:
    """Interactive chat loop that remembers what you tell it."""
    asyncio.run(_chat())


if __name__ == "__main__":
    app()
