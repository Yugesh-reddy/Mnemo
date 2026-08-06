"""Read-only views of the store for a person: ``mnemo ls``, ``search``, ``show`` and ``log``.

They read the scopes an agent's memory server started in the current directory reads
(this project, then global), or every scope with ``--all``, and never write: the
connection is ``query_only``, and search leaves recall counters alone, as
``memory_search`` does. ``search`` runs the same search as ``memory_search``, so what
it shows is what the agents see.

Memory text is untrusted (an agent, a file or a web page may have written it), so
control characters, bidirectional overrides and line breaks are shown escaped: a value
cannot move the cursor, recolour the terminal or forge another row.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

from mnemo.config import Settings, get_settings
from mnemo.core import MnemoStore, _row_value
from mnemo.db import check_schema, connect
from mnemo.direct import DirectMemory
from mnemo.errors import ErrorCode, MnemoError
from mnemo.project import resolve_lanes
from mnemo.services import service_error, where
from mnemo.sqlite import Connection

SOURCE_NAMES = {
    "direct_user_statement": "user",
    "tool_output": "tool",
    "agent_inference": "agent",
    "human_review": "review",
}
LIST_VALUE_CHARS = 80
_BIDI = frozenset([0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)])
_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
_PREFIX = re.compile(r"^[0-9a-f-]{4,36}$")


def clean(text: Any, *, keep_newlines: bool = False) -> str:
    """``text`` safe to print: control characters (C0, DEL, C1), bidirectional controls,
    line/paragraph separators and invisible format characters (zero-width, tag
    characters; the emoji joiner stays) escaped; newlines too unless ``keep_newlines``."""
    out = []
    for char in str(text):
        code = ord(char)
        if char == "\n" and keep_newlines:
            out.append(char)
        elif char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif (
            code < 0x20
            or 0x7F <= code < 0xA0
            or code in _BIDI
            or unicodedata.category(char) in ("Zl", "Zp")
            or (unicodedata.category(char) == "Cf" and char != "\u200d")
        ):
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def _short(text: str, limit: int = LIST_VALUE_CHARS) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value: datetime | str | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _source(provenance: str, trust: str) -> str:
    return f"{SOURCE_NAMES.get(provenance, clean(provenance))}/{clean(trust)}"


def _value(row: Any) -> str:
    value = _row_value(row)
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _reason(reason: str | None) -> str | None:
    """A retraction's reason as the agent gave it (events store "retracted: <reason>")."""
    if reason in (None, "retracted", "invalidated"):
        return None
    return reason.removeprefix("retracted: ")


@dataclass
class Scopes:
    """The namespaces a command reads, each with the label it prints."""

    base: str
    lanes: dict[str, str]  # namespace -> label, in reading order; labels may repeat

    def label(self, namespace: str) -> str:
        if namespace in self.lanes:
            return self.lanes[namespace]
        if namespace == self.base:
            return "global"
        return self._project(namespace)

    def _project(self, namespace: str) -> str:
        prefix = f"{self.base}@"
        if namespace.startswith(prefix):
            return clean(namespace.removeprefix(prefix).removeprefix("path:"))
        return clean(namespace)

    @property
    def namespaces(self) -> list[str]:
        return list(self.lanes)

    def describe(self) -> str:
        return ", ".join(
            f"project ({self._project(namespace)})" if label == "project" else label
            for namespace, label in self.lanes.items()
        )


def _settings() -> Settings:
    return get_settings().model_copy(update={"search_reinforce": False})


@asynccontextmanager
async def reading(settings: Settings) -> AsyncIterator[Connection]:
    """The store, opened for reading only: any write fails."""
    conn = await connect(settings.db, create=False)
    try:
        await check_schema(conn)
        await conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        await conn.close()


async def _all_namespaces(conn: Connection, settings: Settings) -> list[str]:
    rows = await conn.fetch(
        "SELECT DISTINCT namespace FROM memory_fact WHERE user_id=?1 AND agent_id=?2 "
        "ORDER BY namespace",
        settings.user_id,
        settings.agent_id,
    )
    return [row["namespace"] for row in rows]


async def _scopes(
    conn: Connection, settings: Settings, cwd: Path, *, every: bool = False
) -> Scopes:
    lanes = resolve_lanes(settings.namespace, settings.project, cwd)
    scopes = Scopes(settings.namespace, {namespace: label for label, namespace in lanes.items()})
    if every:
        for namespace in await _all_namespaces(conn, settings):
            if namespace not in scopes.lanes:
                scopes.lanes[namespace] = scopes.label(namespace)
    return scopes


async def _elsewhere(conn: Connection, settings: Settings, scopes: Scopes) -> int:
    """Current memories in scopes this command did not read."""
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM memory_current WHERE user_id=?1 AND agent_id=?2 "
            "AND namespace NOT IN (SELECT value FROM json_each(?3))",
            settings.user_id,
            settings.agent_id,
            scopes.namespaces,
        )
    )


def _store(
    conn: Connection, settings: Settings, namespace: str, embedder: Any = None
) -> MnemoStore:
    return MnemoStore(
        conn,
        embedder,
        settings=settings,
        namespace=namespace,
        user_id=settings.user_id,
        agent_id=settings.agent_id,
    )


def _header(settings: Settings, scopes: Scopes, out: TextIO) -> None:
    print(f"store {where(settings.db)} · scopes: {scopes.describe()}", file=out)


def _nothing(what: str, elsewhere: int, out: TextIO, *, hint: str = "see them with --all") -> None:
    print(f"No {what}.", file=out)
    if elsewhere:
        print(
            f"{elsewhere} memor{'y' if elsewhere == 1 else 'ies'} in other scopes: {hint}.",
            file=out,
        )


# --- ls ----------------------------------------------------------------------------------


async def ls(
    *,
    every: bool = False,
    retracted: bool = False,
    limit: int = 50,
    as_json: bool = False,
    cwd: Path | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    settings = _settings()
    async with reading(settings) as conn:
        scopes = await _scopes(conn, settings, cwd or Path.cwd(), every=every)
        if retracted:
            items = []
            for namespace, label in scopes.lanes.items():
                found = await DirectMemory(_store(conn, settings, namespace)).retracted(limit=limit)
                items += [{**item.model_dump(mode="json"), "scope": label} for item in found]
            items.sort(key=lambda item: item["retracted_at"], reverse=True)
            items = items[:limit]
        else:
            rows = await conn.fetch(
                """
                SELECT fact_id, namespace, subject, predicate, object_text, object_number,
                       object_json, event_id, actor, provenance, trust_level, recorded_at
                FROM memory_current
                WHERE namespace IN (SELECT value FROM json_each(?1))
                  AND user_id = ?2 AND agent_id = ?3
                ORDER BY recorded_at DESC, seq DESC
                LIMIT ?4
                """,
                scopes.namespaces,
                settings.user_id,
                settings.agent_id,
                limit,
            )
            items = [
                {
                    "fact_id": str(row["fact_id"]),
                    "scope": scopes.label(row["namespace"]),
                    "subject": row["subject"],
                    "predicate": row["predicate"],
                    "value": _value(row),
                    "current_event_id": str(row["event_id"]),
                    "actor": row["actor"],
                    "provenance": row["provenance"],
                    "trust_level": row["trust_level"],
                    "recorded_at": _when(row["recorded_at"]),
                }
                for row in rows
            ]
        # A count of current memories elsewhere says nothing about retracted ones.
        quiet = every or retracted or bool(items)
        elsewhere = 0 if quiet else await _elsewhere(conn, settings, scopes)
    if as_json:
        print(json.dumps(items, indent=2), file=out)
        return 0
    _header(settings, scopes, out)
    if not items:
        _nothing("retracted memories" if retracted else "memories", elsewhere, out)
        return 0
    for item in items:
        when = item["retracted_at"] if retracted else item["recorded_at"]
        who = item["retracted_by"] if retracted else item["actor"]
        line = (
            f"{item['fact_id'][:8]}  {_when(when)}  {_short(who or '-', 14):<14}  "
            f"{_source(item['provenance'], item['trust_level']):<12}  "
            f"{_short(item['scope'], 24):<8}  "
            f"{_short(item['subject'], 40)} · {_short(item['predicate'], 40)} = "
            f"{_short(item['value'])}"
        )
        if retracted and item.get("reason"):
            line += f"  (reason: {_short(item['reason'], 60)})"
        print(line, file=out)
    return 0


# --- search ------------------------------------------------------------------------------


async def search(
    query: str,
    *,
    limit: int = 5,
    as_json: bool = False,
    cwd: Path | None = None,
    out: TextIO | None = None,
) -> int:
    """``memory_search``: the same scopes, search and order, embedding the query once."""
    out = out or sys.stdout
    from mnemo.embedder import build_embedder
    from mnemo.mcp_direct import _EmbedOnce

    settings = _settings()
    embedder = build_embedder(settings)
    try:
        async with reading(settings) as conn:
            scopes = await _scopes(conn, settings, cwd or Path.cwd())
            once = _EmbedOnce(embedder)
            hits: list[dict[str, Any]] = []
            for namespace, label in scopes.lanes.items():
                found = await DirectMemory(_store(conn, settings, namespace, once)).search(
                    query, limit=limit
                )
                hits += [{**hit.model_dump(mode="json"), "scope": label} for hit in found]
            hits.sort(key=lambda hit: hit["score"], reverse=True)  # stable: project first
            hits = hits[:limit]
            elsewhere = 0 if hits else await _elsewhere(conn, settings, scopes)
    finally:
        close = getattr(embedder, "close", None)
        if close:
            close()
    if as_json:
        print(json.dumps(hits, indent=2), file=out)
        return 0
    _header(settings, scopes, out)
    if not hits:
        # search reads the agents' scopes only, as memory_search does.
        _nothing("matching memories", elsewhere, out, hint="list them with `mnemo ls --all`")
        return 0
    for hit in hits:
        print(
            f"{hit['score']:.3f}  {hit['fact_id'][:8]}  {_when(hit['recorded_at'])}  "
            f"{_short(hit['actor'] or '-', 14):<14}  "
            f"{_source(hit['provenance'], hit['trust_level']):<12}  {hit['scope']:<8}  "
            f"{_short(hit['subject'], 40)} · {_short(hit['predicate'], 40)} = "
            f"{_short(hit['value'])}",
            file=out,
        )
    return 0


# --- show --------------------------------------------------------------------------------


async def _resolve(conn: Connection, settings: Settings, prefix: str) -> tuple[UUID, str]:
    """The one memory whose ID starts with ``prefix``, in any of the user's scopes."""
    prefix = prefix.strip().lower()
    if not _PREFIX.match(prefix):
        raise MnemoError(
            ErrorCode.INVALID_INPUT,
            "give a memory ID or at least its first 4 characters, as `mnemo ls` shows",
        )
    rows = await conn.fetch(
        "SELECT fact_id, namespace, subject, predicate FROM memory_fact "
        "WHERE user_id=?1 AND agent_id=?2 AND fact_id LIKE ?3 || '%' "
        "ORDER BY fact_id LIMIT 11",
        settings.user_id,
        settings.agent_id,
        prefix,
    )
    if not rows:
        raise MnemoError(ErrorCode.NOT_FOUND, f"no memory with an ID starting {prefix}")
    if len(rows) > 1:
        shown = "\n".join(
            f"  {row['fact_id']}  {_short(row['subject'], 40)} · {_short(row['predicate'], 40)}"
            for row in rows[:10]
        )
        more = "\n  …" if len(rows) > 10 else ""
        raise MnemoError(
            ErrorCode.INVALID_INPUT,
            f"{prefix} matches several memories; give more of the ID:\n{shown}{more}",
        )
    return UUID(str(rows[0]["fact_id"])), rows[0]["namespace"]


def _block(value: str, out: TextIO, indent: str = "  ") -> None:
    """A full value, each line behind a gutter so no line can pass for output."""
    for line in clean(value, keep_newlines=True).split("\n"):
        print(f"{indent}│ {line}", file=out)


async def show(
    memory_id: str, *, as_json: bool = False, cwd: Path | None = None, out: TextIO | None = None
) -> int:
    out = out or sys.stdout
    settings = _settings()
    async with reading(settings) as conn:
        fact_id, namespace = await _resolve(conn, settings, memory_id)
        scopes = await _scopes(conn, settings, cwd or Path.cwd())
        store = _store(conn, settings, namespace)
        fact = await conn.fetchrow(
            "SELECT subject, predicate, status, current_event_id FROM memory_fact "
            "WHERE fact_id = ?1",
            fact_id,
        )
        try:
            status = (await DirectMemory(store).get(fact_id)).status
        except MnemoError:
            status = fact["status"]  # a state the guarded tools do not serve
        events = await store.log(fact_id=fact_id, limit=10_000)
    history = [
        {
            "event_id": str(event.event_id),
            "seq": event.seq,
            "op": event.op,
            "value": "" if event.value is None else str(event.value),
            "actor": event.actor,
            "provenance": event.provenance,
            "trust_level": event.trust_level,
            "recorded_at": _when(event.recorded_at),
            "reason": _reason(event.reason),
            "restored_from_event_id": (
                str(event.parent_event_id)
                if event.op == "REVERT" and event.parent_event_id
                else None
            ),
        }
        for event in events
    ]
    current = next(
        (entry for entry in history if entry["event_id"] == str(fact["current_event_id"])), None
    )
    memory = {
        "fact_id": str(fact_id),
        "scope": scopes.label(namespace),
        "subject": fact["subject"],
        "predicate": fact["predicate"],
        "status": status,
        "current_event_id": str(fact["current_event_id"]),
        "value": current["value"] if current else None,
        "history": history,
    }
    if as_json:
        print(json.dumps(memory, indent=2), file=out)
        return 0
    print(f"memory  {fact_id}", file=out)
    print(f"scope   {memory['scope']}", file=out)
    print(f"status  {clean(status)}", file=out)
    print("subject", file=out)
    _block(fact["subject"], out)
    print("predicate", file=out)
    _block(fact["predicate"], out)
    if current is not None:
        label = "retracted value" if status == "retracted" else "value"
        print(f"{label}  (event {current['event_id'][:8]})", file=out)
        _block(current["value"], out)
    print(f"\nhistory, newest first ({len(history)} events)", file=out)
    for entry in history:
        print(
            f"  {entry['event_id'][:8]}  {entry['recorded_at']}  {entry['op']:<10}  "
            f"{_short(entry['actor'] or '-', 14):<14}  "
            f"{_source(entry['provenance'], entry['trust_level'])}",
            file=out,
        )
        if entry["restored_from_event_id"]:
            print(f"      restored from {entry['restored_from_event_id'][:8]}", file=out)
        if entry["op"] == "INVALIDATE":
            if entry["reason"]:
                print("      reason", file=out)
                _block(entry["reason"], out, indent="      ")
        else:
            _block(entry["value"], out, indent="      ")
    return 0


# --- log ---------------------------------------------------------------------------------


async def log(
    *,
    every: bool = False,
    limit: int = 20,
    as_json: bool = False,
    cwd: Path | None = None,
    out: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    settings = _settings()
    async with reading(settings) as conn:
        scopes = await _scopes(conn, settings, cwd or Path.cwd(), every=every)
        rows = await conn.fetch(
            """
            SELECT e.event_id, e.fact_id, e.op, e.object_text, e.object_number, e.object_json,
                   e.actor, e.provenance, e.trust_level, e.reason, e.recorded_at,
                   f.namespace, f.subject, f.predicate
            FROM memory_event e JOIN memory_fact f ON f.fact_id = e.fact_id
            WHERE f.namespace IN (SELECT value FROM json_each(?1))
              AND f.user_id = ?2 AND f.agent_id = ?3
            ORDER BY e.seq DESC
            LIMIT ?4
            """,
            scopes.namespaces,
            settings.user_id,
            settings.agent_id,
            limit,
        )
        elsewhere = 0 if every or rows else await _elsewhere(conn, settings, scopes)
    items = [
        {
            "event_id": str(row["event_id"]),
            "fact_id": str(row["fact_id"]),
            "op": row["op"],
            "scope": scopes.label(row["namespace"]),
            "subject": row["subject"],
            "predicate": row["predicate"],
            "value": _value(row),
            "actor": row["actor"],
            "provenance": row["provenance"],
            "trust_level": row["trust_level"],
            "reason": _reason(row["reason"]),
            "recorded_at": _when(row["recorded_at"]),
        }
        for row in rows
    ]
    if as_json:
        print(json.dumps(items, indent=2), file=out)
        return 0
    _header(settings, scopes, out)
    if not items:
        _nothing("changes", elsewhere, out)
        return 0
    for item in items:
        if item["op"] == "INVALIDATE":
            reason = item["reason"]
            what = f"retracted ({_short(reason, 60)})" if reason else "retracted"
        else:
            what = f"= {_short(item['value'])}"
        print(
            f"{item['recorded_at']}  {item['op']:<10}  {_short(item['actor'] or '-', 14):<14}  "
            f"{_source(item['provenance'], item['trust_level']):<12}  "
            f"{_short(item['scope'], 24):<8}  {item['fact_id'][:8]}  "
            f"{_short(item['subject'], 40)} · {_short(item['predicate'], 40)} {what}",
            file=out,
        )
    return 0


def run(command: str, args: Any) -> int:
    """Dispatch one of the read commands, turning store errors into a message."""
    import asyncio

    from mnemo.sqlite import StoreUnavailable

    calls = {
        "ls": lambda: ls(
            every=args.all, retracted=args.retracted, limit=args.limit, as_json=args.json
        ),
        "search": lambda: search(" ".join(args.query), limit=args.limit, as_json=args.json),
        "show": lambda: show(args.id, as_json=args.json),
        "log": lambda: log(every=args.all, limit=args.limit, as_json=args.json),
    }
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:  # memory text the console cannot encode prints escaped
        reconfigure(errors="backslashreplace")
    try:
        return asyncio.run(calls[command]())
    except (MnemoError, StoreUnavailable) as exc:
        message = exc.message if isinstance(exc, MnemoError) else str(exc)
    except Exception as exc:
        error = service_error(exc, get_settings())  # a model or embedding server down
        if error is None:
            raise
        message = error.message
    print(f"mnemo {command}: {message}", file=sys.stderr)
    return 1
