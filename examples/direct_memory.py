"""Scripted direct-write SDK demo; defaults to non-semantic hash embeddings.

Run ``make demo-direct`` without a model server, or ``python -m
examples.direct_memory`` to honor a backend explicitly configured in the
environment or .env. Uses the legacy SDK; guarded mutations are available via Mnemo.direct.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

import typer

from mnemo import Mnemo
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
    first = memory.add(
        "user", "preferred_database", "PostgreSQL", provenance="direct_user_statement", actor="user"
    )
    say(f"    {first.op}: {first.value} | event_id={first.event_id}")

    say('[2] Change: "Use MySQL instead."')
    changed = memory.add(
        "user", "preferred_database", "MySQL", provenance="direct_user_statement", actor="user"
    )
    say(f"    {changed.op}: {changed.value} | event_id={changed.event_id}")
    before = memory.get(first.fact_id)
    if before is None:
        raise RuntimeError("The changed memory is missing from HEAD")
    say(f"    Current value: {before.value}")

    say("[3] Inspect history to choose the revision to restore.")
    history = memory.blame(fact_id=first.fact_id)
    say(f"    {'event_id':<36} {'op':<7} {'value':<12} {'provenance':<22} trust")
    for event in history:
        say(
            f"    {event.event_id} {event.op:<7} {str(event.value):<12} "
            f"{event.provenance:<22} {event.trust_level}"
        )

    say("[4] Human review: restore the original PostgreSQL revision.")
    restored = memory.revert(first.fact_id, first.event_id, actor="demo-reviewer")
    say(f"    {restored.op}: {restored.value} | event_id={restored.event_id}")

    after = memory.get(first.fact_id)
    if after is None:
        raise RuntimeError("The restored memory is missing from HEAD")
    say(f"[5] Read back: {after.value} | event_id={after.event_id}")
    final = memory.blame(fact_id=first.fact_id)
    say("    History kept: " + " -> ".join(f"{event.op}({event.value})" for event in final))
    return {
        "fact_id": first.fact_id,
        "event_ids": [event.event_id for event in final],
        "ops": [event.op for event in final],
        "values": [event.value for event in final],
        "before": before.value,
        "after": after.value,
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
