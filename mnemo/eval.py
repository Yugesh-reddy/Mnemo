"""Precision/recall junk-rate eval — the north star (spec §7).

Feeds one labeled conversation through two pipelines and reports
precision / recall / F1 / false-fact count for each:
- naive: store every extracted candidate (what incumbent memory layers do)
- gated: the full observe() -> worker pipeline (the product)

Deterministic by construction: EvalExtractor is keyword-driven (mimicking a naive
LLM extractor, mistakes included) and the embedder is injected. Swap a labeled
LoCoMo/LongMemEval conversation + a real extractor for the headline number.

Run: make eval   (python -m mnemo.eval)
"""

from __future__ import annotations

import asyncio

import asyncpg

from mnemo.config import get_settings
from mnemo.core import MnemoStore
from mnemo.db import apply_migrations, register_vector
from mnemo.extraction import ExtractionWorker
from mnemo.models import ExtractedFact

# ---- the labeled conversation (turn_id, role, text) ----------------------
# Hand-labeled, LoCoMo-flavored: durable facts, transient noise, a negation trap,
# a hypothetical trap, assistant chatter, and repetition (dedup pressure).
EVAL_CONVERSATION: list[tuple[str, str, str]] = [
    ("t01", "user", "Hey, I'm Sai. I'm a data engineer and I use PostgreSQL for my main project."),
    ("t02", "assistant", "Nice! PostgreSQL is great. By the way it's a sunny 72F here today."),
    ("t03", "user", "Today I'm just debugging the auth service, nothing major."),
    ("t04", "user", "I don't use MongoDB, never liked it."),
    ("t05", "user", "What if I switched to a graph database someday?"),
    ("t06", "user", "My team lead is Priya and we ship on Fridays."),
    ("t07", "user", "Also I prefer Postgres, just confirming."),
    ("t08", "assistant", "Got it, logging that you prefer PostgreSQL."),
    ("t09", "user", "2 + 2 is 4 right?"),
    ("t10", "user", "I live in Austin and work remote."),
    ("t11", "user", "My main language is Python, though I dabble in Rust."),
    ("t12", "user", "Maybe I'll learn Go someday, who knows."),
    ("t13", "assistant", "The weather in Austin is 95F today!"),
    ("t14", "user", "We deploy with Docker Compose on a single VM."),
    ("t15", "user", "I don't work weekends anymore."),
    ("t16", "user", "My team lead is Priya, as I said."),
    ("t17", "user", "Right now I'm waiting for CI to finish."),
    ("t18", "user", "My timezone is US Central."),
]

GROUND_TRUTH: set[tuple[str, str]] = {
    ("name", "Sai"),
    ("role", "data engineer"),
    ("preferred_database", "PostgreSQL"),
    ("team_lead", "Priya"),
    ("ship_day", "Friday"),
    ("location", "Austin"),
    ("preferred_language", "Python"),
    ("deploy_method", "Docker Compose"),
    ("timezone", "US Central"),
}

FORBIDDEN: set[tuple[str, str]] = {
    ("uses_database", "MongoDB"),  # negation: "I don't use MongoDB"
    ("uses_database", "graph database"),  # hypothetical: "what if I switched"
    ("learning_language", "Go"),  # hypothetical: "maybe I'll learn Go"
}


class EvalExtractor:
    """Deterministic keyword extractor mimicking naive over-extraction, mistakes
    included — junk (weather/math/transient), negation and hypothetical traps."""

    RULES: list[tuple[str, str, str, int]] = [
        # (trigger substring in lowercase turn, predicate, object, importance)
        ("i'm sai", "name", "Sai", 8),
        ("data engineer", "role", "data engineer", 8),
        ("postgresql", "preferred_database", "PostgreSQL", 8),
        ("prefer postgres", "preferred_database", "PostgreSQL", 8),
        ("mongodb", "uses_database", "MongoDB", 6),  # WRONG: negated
        ("graph database", "uses_database", "graph database", 6),  # WRONG: hypothetical
        ("team lead is priya", "team_lead", "Priya", 7),
        ("ship on fridays", "ship_day", "Friday", 7),
        ("72f", "weather", "72F sunny", 2),  # junk
        ("95f", "weather", "95F", 2),  # junk
        ("debugging the auth service", "currently_debugging", "auth service", 3),
        ("live in austin", "location", "Austin", 8),
        ("language is python", "preferred_language", "Python", 8),
        ("learn go", "learning_language", "Go", 5),  # WRONG: hypothetical
        ("docker compose", "deploy_method", "Docker Compose", 7),
        ("waiting for ci", "current_activity", "waiting for CI", 2),  # transient junk
        ("timezone is us central", "timezone", "US Central", 7),
        ("2 + 2", "math_fact", "2 + 2 = 4", 1),  # noise
    ]

    def extract(self, text: str, role: str = "user") -> list[ExtractedFact]:
        t = text.lower()
        out: list[ExtractedFact] = []
        for trigger, predicate, obj, importance in self.RULES:
            if trigger in t:
                out.append(
                    ExtractedFact(
                        subject="user",
                        predicate=predicate,
                        object=obj,
                        confidence=0.9,
                        importance=importance,
                        assertion_type=(
                            "direct_user_statement" if role == "user" else "agent_inference"
                        ),
                    )
                )
        return out


def metrics(
    stored: set[tuple[str, str]],
    truth: set[tuple[str, str]],
    forbidden: set[tuple[str, str]],
) -> dict:
    tp = len(stored & truth)
    precision = tp / len(stored) if stored else 0.0
    recall = tp / len(truth) if truth else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "stored": len(stored),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "false": len(stored & forbidden),
    }


async def _stored_pairs(conn: asyncpg.Connection) -> set[tuple[str, str]]:
    rows = await conn.fetch("SELECT predicate, object_text FROM memory_current")
    return {(r["predicate"], r["object_text"]) for r in rows}


async def run_naive(conn: asyncpg.Connection, embedder) -> set[tuple[str, str]]:
    """Naive baseline: store everything the extractor emits, no gate."""
    store = MnemoStore(conn, embedder)
    extractor = EvalExtractor()
    for _turn_id, role, text in EVAL_CONVERSATION:
        for cand in extractor.extract(text, role):
            await store.add(
                cand.subject,
                cand.predicate,
                cand.object,
                provenance=("direct_user_statement" if role == "user" else "agent_inference"),
                confidence=cand.confidence,
                importance=cand.importance,
            )
    return await _stored_pairs(conn)


async def run_gated(conn: asyncpg.Connection, embedder) -> set[tuple[str, str]]:
    """The product: observe() -> extraction worker (gate included once built)."""
    store = MnemoStore(conn, embedder)
    worker = ExtractionWorker(conn, embedder, EvalExtractor())
    for turn_id, role, text in EVAL_CONVERSATION:
        await store.observe(turn_id, text, "eval-session", role=role)
    while await worker.process_one():
        pass
    return await _stored_pairs(conn)


async def _truncate(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "TRUNCATE memory_event, memory_fact, memory_commit, fast_cache, "
        "extraction_job RESTART IDENTITY CASCADE"
    )


async def evaluate(dsn: str | None = None) -> dict:
    """Run both pipelines on a clean DB and return {'naive': ..., 'gated': ...}."""
    from tests.conftest import FakeEmbedder  # deterministic; no model, no network

    settings = get_settings()
    conn = await asyncpg.connect(dsn or settings.test_dsn)
    await register_vector(conn)
    embedder = FakeEmbedder(settings.embed_dim)
    try:
        await apply_migrations(conn)
        await _truncate(conn)
        naive = metrics(await run_naive(conn, embedder), GROUND_TRUTH, FORBIDDEN)
        await _truncate(conn)
        gated = metrics(await run_gated(conn, embedder), GROUND_TRUTH, FORBIDDEN)
        return {"naive": naive, "gated": gated}
    finally:
        await conn.close()


def report(result: dict) -> None:
    def line(name: str, r: dict) -> str:
        return (
            f"  {name:<6}: stored {r['stored']:3d} | P {r['precision'] * 100:5.1f}% | "
            f"R {r['recall'] * 100:5.1f}% | F1 {r['f1'] * 100:5.1f}% | false {r['false']}"
        )

    print("PRECISION / RECALL JUNK-RATE EVAL (naive vs gated — the north star)")
    print(line("NAIVE", result["naive"]))
    print(line("GATED", result["gated"]))


if __name__ == "__main__":
    report(asyncio.run(evaluate()))
