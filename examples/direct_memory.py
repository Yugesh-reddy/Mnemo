"""Scripted direct-write SDK demo; defaults to non-semantic hash embeddings.

Run ``make demo-direct`` without a model server, or ``python -m
examples.direct_memory`` to honor a backend explicitly configured in the
environment or .env. Exercises guarded revisions and durable retries via Mnemo.direct.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import typer

from mnemo import ErrorCode, Mnemo, MnemoError
from mnemo.config import Settings
from mnemo.embedder import build_embedder

app = typer.Typer(add_completion=False, help="Direct write, history and rollback with the SDK.")


def demo_settings() -> Settings:
    """Honor an explicit backend; otherwise choose hash. No background work runs."""
    settings = Settings()
    if "backend" not in settings.model_fields_set:
        return Settings(backend="hash", worker_enabled=False)
    return settings.model_copy(update={"worker_enabled": False})


def run_scenario(
    memory: Mnemo, *, say: Callable[[str], None] = lambda _message: None
) -> dict[str, Any]:
    """Remember, change, inspect, undo and read back through the synchronous SDK."""
    say('[1] Remember: "My preferred database is PostgreSQL."')
    first = memory.direct.create(
        "user", "preferred_database", "PostgreSQL", request_id=uuid4(), actor="demo-agent"
    )
    say(f"    ADD: {first.value} | event_id={first.event_id}")

    say('[2] Change: "Use MySQL instead."')
    changed = memory.direct.update(
        first.fact_id,
        "MySQL",
        expected_event_id=first.event_id,
        request_id=uuid4(),
        actor="demo-agent",
    )
    say(f"    UPDATE: {changed.value} | event_id={changed.event_id}")
    before = memory.direct.get(first.fact_id)
    say(f"    Current value: {before.value}")

    try:
        memory.direct.update(
            first.fact_id,
            "SQLite",
            expected_event_id=first.event_id,
            request_id=uuid4(),
            actor="demo-agent",
        )
    except MnemoError as exc:
        if exc.code != ErrorCode.REVISION_CONFLICT:
            raise
        conflict_code = str(exc.code)
        say(f"    Stale update rejected: {conflict_code}; MySQL remains current.")
    else:
        raise RuntimeError("A stale revision unexpectedly overwrote the memory")

    say("[3] Inspect history to choose the revision to restore.")
    history = memory.direct.history(first.fact_id)
    say(f"    {'event_id':<36} {'op':<7} {'value':<12} {'provenance':<22} trust")
    for event in reversed(history.entries):
        say(
            f"    {event.event_id} {event.op:<7} {event.value_preview:<12} "
            f"{event.provenance:<22} {event.trust_level}"
        )

    say("[4] Guarded restore: return to the original PostgreSQL revision.")
    restore_request = uuid4()
    restore_args = dict(
        expected_event_id=history.current_event_id, request_id=restore_request, actor="demo-agent"
    )
    restored = memory.direct.revert(first.fact_id, first.event_id, **restore_args)
    say(f"    REVERT: {restored.value} | event_id={restored.event_id}")
    retry = memory.direct.revert(first.fact_id, first.event_id, **restore_args)
    if not retry.replayed or retry.event_id != restored.event_id:
        raise RuntimeError("An identical retry did not replay the saved result")
    say(f"    Identical retry replayed: {retry.replayed}; no additional revision.")

    after = memory.direct.get(first.fact_id)
    say(f"[5] Read back: {after.value} | event_id={after.current_event_id}")
    final = list(reversed(memory.direct.history(first.fact_id).entries))
    say("    History kept: " + " -> ".join(f"{event.op}({event.value_preview})" for event in final))
    say(f"    Source trust preserved: {after.provenance} / {after.trust_level}")
    return {
        "fact_id": first.fact_id,
        "event_ids": [event.event_id for event in final],
        "ops": [event.op for event in final],
        "values": [event.value_preview for event in final],
        "before": before.value,
        "after": after.value,
        "conflict_code": conflict_code,
        "replayed": retry.replayed,
    }


@app.command()
def demo() -> None:
    """Run a direct-memory lifecycle in a fresh namespace (no extraction)."""
    settings = demo_settings()
    embedder = build_embedder(settings)
    try:
        namespace = "demo-direct-" + uuid4().hex
        label = "hash (non-semantic)" if settings.backend == "hash" else settings.backend
        typer.echo("Mnemo direct-memory demo — scripted SDK calls, no host model")
        typer.echo(f"embeddings: {label}")
        typer.echo(f"namespace: {namespace}")
        memory = Mnemo(
            settings.dsn,
            embedder,
            namespace=namespace,
            user_id=settings.user_id,
            agent_id=settings.agent_id,
        )
        run_scenario(memory, say=typer.echo)
    finally:
        close = getattr(embedder, "close", None)
        if close:
            close()


if __name__ == "__main__":
    app()
