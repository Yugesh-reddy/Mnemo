"""Versioned deterministic evaluation corpora.

The 18-turn fixture is a synthetic smoke test.  The 200-turn corpus is also
synthetic, but contains distinct user facts, sources, corrections, temporal
statements, negations, hypotheticals, and assistant-attribution traps.  It is a
regression benchmark; it must never be presented as a real-world model result.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mnemo.eval_support import AtomicLabel, EvalDataset, EvalTurn

_SMOKE_ROWS = (
    (
        "t01",
        "user",
        "Hey, I'm Sai. I'm a data engineer and I use PostgreSQL for my main project.",
        (
            ("name", "Sai", "must_keep", 8),
            ("role", "data engineer", "must_keep", 8),
            ("preferred_database", "PostgreSQL", "must_keep", 8),
        ),
    ),
    (
        "t02",
        "assistant",
        "Nice! PostgreSQL is great. By the way it's a sunny 72F here today.",
        (("weather", "72F sunny", "candidate", 2),),
    ),
    (
        "t03",
        "user",
        "Today I'm just debugging the auth service, nothing major.",
        (("currently_debugging", "auth service", "candidate", 3),),
    ),
    (
        "t04",
        "user",
        "I don't use MongoDB, never liked it.",
        (("uses_database", "MongoDB", "forbidden", 6),),
    ),
    (
        "t05",
        "user",
        "What if I switched to a graph database someday?",
        (("uses_database", "graph database", "forbidden", 6),),
    ),
    (
        "t06",
        "user",
        "My team lead is Priya and we ship on Fridays.",
        (("team_lead", "Priya", "must_keep", 7), ("ship_day", "Friday", "must_keep", 7)),
    ),
    (
        "t07",
        "user",
        "Also I prefer Postgres, just confirming.",
        (("preferred_database", "PostgreSQL", "must_keep", 8),),
    ),
    ("t08", "assistant", "Got it, logging that you prefer PostgreSQL.", ()),
    ("t09", "user", "2 + 2 is 4 right?", (("math_fact", "2 + 2 = 4", "candidate", 1),)),
    ("t10", "user", "I live in Austin and work remote.", (("location", "Austin", "must_keep", 8),)),
    (
        "t11",
        "user",
        "My main language is Python, though I dabble in Rust.",
        (("preferred_language", "Python", "must_keep", 8),),
    ),
    (
        "t12",
        "user",
        "Maybe I'll learn Go someday, who knows.",
        (("learning_language", "Go", "forbidden", 5),),
    ),
    (
        "t13",
        "assistant",
        "The weather in Austin is 95F today!",
        (("weather", "95F", "candidate", 2),),
    ),
    (
        "t14",
        "user",
        "We deploy with Docker Compose on a single VM.",
        (("deploy_method", "Docker Compose", "must_keep", 7),),
    ),
    ("t15", "user", "I don't work weekends anymore.", ()),
    ("t16", "user", "My team lead is Priya, as I said.", (("team_lead", "Priya", "must_keep", 7),)),
    (
        "t17",
        "user",
        "Right now I'm waiting for CI to finish.",
        (("current_activity", "waiting for CI", "candidate", 2),),
    ),
    ("t18", "user", "My timezone is US Central.", (("timezone", "US Central", "must_keep", 7),)),
)


def load_smoke_dataset(*, version: str = "2.0.0") -> EvalDataset:
    if version not in {"1.0.0", "2.0.0"}:
        raise ValueError("unknown smoke label version")
    base = datetime(2026, 1, 5, 15, tzinfo=UTC)
    turns = []
    for index, (turn_id, role, text, labels) in enumerate(_SMOKE_ROWS):
        # Preserve the source fixture and legacy labels for old report audits.
        # Use and primary language do not entail preference.
        if version == "2.0.0":
            replacements = {"t01": "uses_database", "t11": "primary_language"}
            labels = tuple(
                (
                    (replacements.get(turn_id, predicate), value, disposition, importance)
                    if predicate in {"preferred_database", "preferred_language"}
                    else (predicate, value, disposition, importance)
                )
                for predicate, value, disposition, importance in labels
            )
        turns.append(
            EvalTurn(
                turn_id=turn_id,
                role=role,
                text=text,
                session_id="smoke-session",
                timestamp=base + timedelta(minutes=index * 3),
                source_id=f"synthetic-smoke:{turn_id}",
                labels=tuple(AtomicLabel(p, v, d, importance=i) for p, v, d, i in labels),
            )
        )
    return EvalDataset(
        name="mnemo-synthetic-smoke",
        version=version,
        split="smoke",
        turns=tuple(turns),
        metadata={"synthetic": True, "license": "project-authored regression fixture"},
    )


# Each scenario contributes four different conversational events.  Values and
# predicates are intentionally broad so the corpus exercises entity, preference,
# workflow, accessibility, health, family, travel, temporal, and source slices.
_SCENARIOS = (
    ("name", "Maya Chen", "Call me Maya Chen in project notes.", "Mina Chen"),
    (
        "role",
        "site reliability engineer",
        "I work as a site reliability engineer.",
        "product designer",
    ),
    ("location", "Portland", "My home base is Portland.", "Seattle"),
    ("timezone", "Pacific Time", "Please schedule me in Pacific Time.", "Eastern Time"),
    (
        "preferred_language",
        "TypeScript",
        "TypeScript is my preferred programming language.",
        "Java",
    ),
    ("preferred_database", "PostgreSQL", "For durable services I prefer PostgreSQL.", "MongoDB"),
    ("editor", "Neovim", "I use Neovim as my everyday editor.", "Emacs"),
    ("shell", "fish", "My interactive shell is fish.", "PowerShell"),
    ("team", "Reliability Platform", "I belong to the Reliability Platform team.", "Payments"),
    ("manager", "Elena Ruiz", "My manager is Elena Ruiz.", "Jordan Lee"),
    ("on_call_day", "Tuesday", "My regular on-call handoff is Tuesday.", "Saturday"),
    ("deploy_method", "Kubernetes", "We deploy the API on Kubernetes.", "bare metal"),
    ("cloud_provider", "Google Cloud", "Our production account is on Google Cloud.", "Azure"),
    ("issue_tracker", "Linear", "Our team tracks issues in Linear.", "Jira"),
    ("documentation_tool", "Google Docs", "We write design proposals in Google Docs.", "Notion"),
    ("meeting_free_day", "Wednesday", "Wednesday is my meeting-free day.", "Monday"),
    ("work_start", "08:30", "I normally start work at 08:30.", "06:00"),
    ("pronouns", "she/her", "My pronouns are she/her.", "they/them"),
    ("spoken_language", "Mandarin", "I can speak Mandarin fluently.", "German"),
    (
        "accessibility_need",
        "captions",
        "I need captions enabled for video calls.",
        "screen magnification",
    ),
    ("diet", "vegetarian", "I follow a vegetarian diet.", "keto"),
    ("allergy", "peanuts", "I have a peanut allergy.", "shellfish"),
    ("emergency_contact", "Noah Chen", "My emergency contact is Noah Chen.", "Alex Chen"),
    ("pet", "cat named Pixel", "I have a cat named Pixel.", "dog named Luna"),
    ("favorite_music", "jazz", "Jazz is the music I listen to most.", "country"),
    ("hobby", "ceramics", "I spend weekends making ceramics.", "skydiving"),
    ("running_goal", "half marathon", "My training goal is a half marathon.", "ultramarathon"),
    ("learning_goal", "Japanese", "I am actively learning Japanese.", "Icelandic"),
    ("notification_preference", "email", "Send important account alerts by email.", "SMS"),
    (
        "contact_window",
        "before 18:00",
        "Please contact me before 18:00 local time.",
        "after midnight",
    ),
    ("date_format", "YYYY-MM-DD", "I prefer dates formatted as YYYY-MM-DD.", "MM/DD/YY"),
    ("temperature_unit", "Celsius", "Show weather temperatures in Celsius.", "Fahrenheit"),
    ("currency", "USD", "Use USD in my expense reports.", "EUR"),
    ("flight_seat", "aisle", "I prefer an aisle seat when flying.", "middle"),
    ("hotel_need", "quiet room", "For hotels, please request a quiet room.", "party floor"),
    ("home_airport", "PDX", "PDX is my home airport.", "SFO"),
    ("project_codename", "Juniper", "The current migration is codenamed Juniper.", "Cedar"),
    ("repository_host", "GitHub", "We host the project repository on GitHub.", "Bitbucket"),
    ("backup_schedule", "daily at 02:00 UTC", "Database backups run daily at 02:00 UTC.", "weekly"),
    ("retention_period", "30 days", "Application logs are retained for 30 days.", "forever"),
    (
        "service_owner",
        "Core Infrastructure",
        "Core Infrastructure owns the gateway service.",
        "Marketing",
    ),
    ("incident_channel", "#incidents", "We coordinate outages in #incidents.", "#random"),
    ("release_day", "Thursday", "Our normal release day is Thursday.", "Sunday"),
    ("test_framework", "pytest", "This Python service uses pytest.", "JUnit"),
    ("package_manager", "uv", "We manage Python environments with uv.", "conda"),
    ("api_style", "REST", "The public integration uses a REST API.", "SOAP"),
    ("data_region", "us-central1", "Customer backups stay in us-central1.", "europe-west1"),
    ("support_tier", "enterprise", "Our account has enterprise support.", "free"),
    ("invoice_day", "15th", "Invoices should be issued on the 15th.", "last day"),
    ("fiscal_year_start", "July", "Our fiscal year starts in July.", "January"),
)


def _scenario_turns(index: int, row: tuple[str, str, str, str]) -> tuple[EvalTurn, ...]:
    predicate, value, assertion, trap_value = row
    split = "dev" if index < 25 else "held_out"
    session = f"{split}-session-{index // 5 + 1:02d}"
    started = datetime(2026, 2, 1, 14, tzinfo=UTC) + timedelta(days=index)
    prefix = f"{split}-{index + 1:03d}"
    importance = 9 if predicate in {"allergy", "accessibility_need", "emergency_contact"} else 7
    disposition = "must_keep" if importance == 9 or index % 5 == 0 else "truth"
    correction = index % 5 == 0
    revised = f"{value} revised" if predicate == "name" else trap_value
    return (
        EvalTurn(
            prefix + "a",
            "user",
            assertion,
            session,
            started,
            prefix + ":user",
            (AtomicLabel(predicate, value, disposition, importance=importance),),
        ),
        EvalTurn(
            prefix + "b",
            "assistant",
            f"I recorded {trap_value} as your {predicate.replace('_', ' ')}.",
            session,
            started + timedelta(minutes=2),
            prefix + ":assistant",
            (AtomicLabel(predicate, trap_value, "forbidden", importance=7),),
        ),
        EvalTurn(
            prefix + "c",
            "user",
            f"I do not use {trap_value}; please don't infer that as my "
            f"{predicate.replace('_', ' ')}.",
            session,
            started + timedelta(minutes=8),
            prefix + ":negation",
            (AtomicLabel(predicate, trap_value, "forbidden", importance=7),),
        ),
        EvalTurn(
            prefix + "d",
            "user",
            (
                f"Correction to what I said earlier: my {predicate.replace('_', ' ')} is {revised}."
                if correction
                else f"What if someday I changed my {predicate.replace('_', ' ')} to {trap_value}? "
                "For now this is only hypothetical."
            ),
            session + "-followup" if correction else session,
            started + timedelta(minutes=15),
            prefix + (":correction" if correction else ":hypothetical"),
            (
                AtomicLabel(
                    predicate,
                    revised if correction else trap_value,
                    "must_keep" if correction else "forbidden",
                    importance=8,
                ),
            ),
        ),
    )


def load_benchmark_dataset(split: str = "held_out") -> EvalDataset:
    if split not in {"dev", "held_out", "all"}:
        raise ValueError("split must be 'dev', 'held_out', or 'all'")
    turns = tuple(
        turn
        for index, row in enumerate(_SCENARIOS)
        if split == "all" or (split == "dev") == (index < 25)
        for turn in _scenario_turns(index, row)
    )
    return EvalDataset(
        name="mnemo-synthetic-quality-corpus",
        version="1.0.0",
        split=split,
        turns=turns,
        metadata={
            "synthetic": True,
            "scenario_count": len(turns) // 4,
            "slices": [
                "source-role",
                "negation",
                "hypothetical",
                "must-keep",
                "correction",
                "multiple-sessions",
                "unfamiliar-predicates",
            ],
        },
    )


def load_longmemeval(
    source: str | Path | Iterable[dict[str, Any]],
    *,
    labels: dict[str, list[dict[str, Any]]],
    split: str = "external",
) -> EvalDataset:
    """Adapt LongMemEval-style sessions without pretending QA answers are facts.

    Explicit atomic labels keyed by source/turn id are mandatory.  LongMemEval's
    question answers are retrieval labels and cannot safely be converted into
    memory-write labels automatically.
    """

    if not labels:
        raise ValueError("LongMemEval import requires explicit atomic labels")
    if isinstance(source, (str, Path)):
        with Path(source).open(encoding="utf-8") as handle:
            records = json.load(handle)
    else:
        records = list(source)
    if isinstance(records, dict):
        records = records.get("data", records.get("records", []))

    turns: list[EvalTurn] = []
    for record_index, record in enumerate(records):
        sessions = record.get("haystack_sessions") or record.get("sessions") or []
        session_ids = record.get("haystack_session_ids") or []
        dates = record.get("haystack_dates") or []
        for session_index, session in enumerate(sessions):
            session_id = str(
                session_ids[session_index]
                if session_index < len(session_ids)
                else f"record-{record_index}-session-{session_index}"
            )
            session_date = dates[session_index] if session_index < len(dates) else None
            for turn_index, raw_turn in enumerate(session):
                source_id = str(
                    raw_turn.get("turn_id") or raw_turn.get("id") or f"{session_id}:{turn_index}"
                )
                raw_labels = labels.get(source_id)
                if raw_labels is None:
                    raise ValueError(f"missing explicit atomic labels for {source_id}")
                timestamp_raw = raw_turn.get("timestamp") or raw_turn.get("date") or session_date
                timestamp = None
                if timestamp_raw:
                    try:
                        timestamp = datetime.fromisoformat(
                            str(timestamp_raw).replace("Z", "+00:00")
                        )
                    except ValueError:
                        timestamp = datetime.strptime(str(timestamp_raw), "%Y/%m/%d (%a) %H:%M")
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=UTC)
                turns.append(
                    EvalTurn(
                        turn_id=source_id,
                        role=str(raw_turn.get("role", "user")),
                        text=str(raw_turn.get("content", raw_turn.get("text", ""))),
                        session_id=session_id,
                        timestamp=timestamp,
                        source_id=source_id,
                        labels=tuple(AtomicLabel(**item) for item in raw_labels),
                    )
                )
    return EvalDataset(
        name="longmemeval-adapter",
        version="1.0.0",
        split=split,
        turns=tuple(sorted(turns, key=lambda t: t.timestamp or datetime.min.replace(tzinfo=UTC))),
        metadata={"synthetic": False, "requires_external_attribution": True},
    )
