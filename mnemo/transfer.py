"""Versioned JSON export/import of the versioned store.

An export carries facts (with their HEAD pointers), every event, commits and
mutation receipts for one scope or for the whole store, plus the applied
migrations. Rows are the stored column values exactly as the file holds them
(format version 2), so IDs, sequence numbers, timestamps and values round-trip
byte for byte. Vectors and the word index are derived data and are left out:
import rebuilds them (``reindex``) with the destination's embedding model.

Import restores into an **empty** store only, in one transaction. Before commit it
reads back what it wrote and rolls back unless that equals the document.

Import also reads version 1 documents, written by the Postgres releases (tag
``v0.0.1-postgres``): timestamps are put in the store's fixed form, JSON values
become text, numbers stay exact, and the vectors are dropped. A request recorded
before the upgrade still replays its receipt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from mnemo.config import Settings, get_settings
from mnemo.db import applied_migrations
from mnemo.sqlite import Connection, format_ts, parse_ts

FORMAT = "mnemo.export"
FORMAT_VERSION = 2
POSTGRES_VERSION = 1
POSTGRES_LAST_MIGRATION = "0012_retract_receipts.sql"
SECTIONS = ("facts", "events", "commits", "receipts")
TABLES = {
    "facts": "memory_fact",
    "events": "memory_event",
    "commits": "memory_commit",
    "receipts": "memory_mutation_receipt",
}
ORDER = {
    "facts": "f.created_at, f.fact_id",
    "events": "e.seq",
    "commits": "c.at_seq, c.created_at, c.commit_id",
    "receipts": "r.created_at, r.receipt_id",
}
ALIAS = {"facts": "f", "events": "e", "commits": "c", "receipts": "r"}
_SCOPE_KEYS = ("namespace", "user_id", "agent_id")

# How version 1 (Postgres) columns become stored values.
_TIME = {
    "created_at",
    "valid_from",
    "valid_to",
    "recorded_at",
    "superseded_at",
    "last_used",
    "expires_at",
}
_JSON_TEXT = {"object_json", "source_span", "request_payload", "result"}
_REAL = {"confidence", "write_score", "strength"}
_DROPPED = {"embedding"}


class TransferError(Exception):
    """An export document cannot be restored into this destination."""


async def _columns(conn: Connection, table: str) -> list[str]:
    return [row["name"] for row in await conn.fetch(f"PRAGMA table_info({table})")]


async def _raw_rows(conn: Connection, sql: str, *args: Any) -> list[dict[str, Any]]:
    """Rows exactly as stored (no conversion to UUID, datetime or Decimal)."""

    def run() -> list[dict[str, Any]]:
        cursor = conn.raw.execute(sql, args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    return await conn._call(run)


async def _sections(conn: Connection, scope: dict[str, str] | None) -> dict[str, list[dict]]:
    sections: dict[str, list[dict]] = {}
    for section in SECTIONS:
        table, alias = TABLES[section], ALIAS[section]
        columns = ", ".join(f"{alias}.{c}" for c in await _columns(conn, table))
        source = f"{table} {alias}"
        owner = alias
        if section == "events":
            source += " JOIN memory_fact f ON f.fact_id = e.fact_id"
            owner = "f"
        where = (
            ""
            if scope is None
            else f"WHERE {owner}.namespace=?1 AND {owner}.user_id=?2 AND {owner}.agent_id=?3"
        )
        args = () if scope is None else tuple(scope[k] for k in _SCOPE_KEYS)
        sections[section] = await _raw_rows(
            conn, f"SELECT {columns} FROM {source} {where} ORDER BY {ORDER[section]}", *args
        )
    return sections


async def export_store(
    conn: Connection,
    *,
    scope: dict[str, str] | None,
    settings: Settings | None = None,
) -> str:
    """Return a pretty-printed export document for ``scope`` (``None`` = whole store).

    Reads one snapshot when this call owns the transaction.
    """
    if scope is not None and set(scope) != set(_SCOPE_KEYS):
        raise ValueError(f"scope must have exactly the keys {_SCOPE_KEYS}")
    async with conn.transaction(readonly=not conn.is_in_transaction()):
        migrations = await applied_migrations(conn)
        sections = await _sections(conn, scope)
        exported_at = format_ts(conn.now())
    document = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "scope": scope,
        "schema_migrations": migrations,
        **sections,
        "exported_at": exported_at,
        "counts": {section: len(sections[section]) for section in SECTIONS},
    }
    return json.dumps(document, indent=2, ensure_ascii=False)


def _validate_document(doc: Any) -> None:
    """Checks that need no database: format, shape, counts and row scope."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise TransferError(f"not a {FORMAT} document")
    if doc.get("format_version") not in (FORMAT_VERSION, POSTGRES_VERSION):
        raise TransferError(
            f"unsupported format_version {doc.get('format_version')!r}; "
            f"this Mnemo reads versions {POSTGRES_VERSION} and {FORMAT_VERSION}"
        )
    counts = doc.get("counts") or {}
    for section in SECTIONS:
        rows = doc.get(section)
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TransferError(f"section {section!r} must be a list of rows")
        if counts.get(section) != len(rows):
            raise TransferError(
                f"section {section!r} has {len(rows)} rows; counts says {counts.get(section)!r}"
            )
    scope = doc.get("scope")
    if scope is not None:
        if not isinstance(scope, dict) or set(scope) != set(_SCOPE_KEYS):
            raise TransferError(f"scope must be null or have exactly the keys {_SCOPE_KEYS}")
        for section in ("facts", "commits", "receipts"):
            for row in doc[section]:
                if any(row.get(key) != scope[key] for key in _SCOPE_KEYS):
                    raise TransferError(f"a {section} row lies outside the document scope")
    fact_ids = {row.get("fact_id") for row in doc["facts"]}
    for section in ("events", "receipts"):
        if any(row.get("fact_id") not in fact_ids for row in doc[section]):
            raise TransferError(f"a {section} row references a fact missing from the document")


def _plain(value: Any) -> Any:
    """JSON from a Decimal-parsed document, with numbers as JSON numbers again."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _from_postgres(section: str, row: dict[str, Any]) -> dict[str, Any]:
    """One version 1 row as this store keeps it."""
    out: dict[str, Any] = {}
    for column, value in row.items():
        if column in _DROPPED:
            continue
        if value is None:
            out[column] = None
        elif column in _TIME:
            out[column] = format_ts(parse_ts(value))
        elif column in _JSON_TEXT:
            plain = _plain(value)
            sort = column in ("object_json", "request_payload")
            out[column] = json.dumps(plain, sort_keys=sort)
        elif column == "object_number":
            out[column] = str(value)
        elif column in _REAL:
            out[column] = float(value)
        elif isinstance(value, Decimal):
            out[column] = int(value)
        else:
            out[column] = value
    return out


async def import_store(
    conn: Connection,
    document: str,
    *,
    settings: Settings | None = None,
    embedder: Any | None = None,
) -> dict[str, int]:
    """Restore an export into an empty store; return the restored row counts.

    All-or-nothing: any refusal or verification mismatch writes nothing. With an
    ``embedder``, vectors are rebuilt afterwards (otherwise run ``mnemo reindex``).
    """
    try:
        # Decimal keeps version 1 numbers exact until they are converted.
        doc = json.loads(document, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise TransferError(f"document is not valid JSON: {exc}") from exc
    _validate_document(doc)
    postgres = doc["format_version"] == POSTGRES_VERSION
    if postgres:
        if POSTGRES_LAST_MIGRATION not in (doc.get("schema_migrations") or []):
            raise TransferError(
                "schema mismatch: this Postgres export predates "
                f"{POSTGRES_LAST_MIGRATION}. Export it again with Mnemo v0.0.1-postgres."
            )
        rows = {s: [_from_postgres(s, row) for row in doc[s]] for s in SECTIONS}
    else:
        rows = {s: [_plain(row) for row in doc[s]] for s in SECTIONS}

    async with conn.transaction():
        migrations = await applied_migrations(conn)
        if not postgres and doc.get("schema_migrations") != migrations:
            raise TransferError(
                "schema mismatch: the export was taken at migrations "
                f"{doc.get('schema_migrations')!r}; this store has {migrations!r}. "
                "Migrate both sides to the same version first."
            )
        occupied = [
            table
            for table in TABLES.values()
            if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {table})")
        ]
        if occupied:
            raise TransferError(
                f"destination is not empty ({', '.join(occupied)}); "
                "import restores into an empty store only"
            )
        # Events point at later events (superseded_by) and facts at events: check
        # every reference once, at commit.
        await conn.execute("PRAGMA defer_foreign_keys = ON")
        columns = {s: await _columns(conn, TABLES[s]) for s in SECTIONS}
        for section in ("commits", "facts", "events", "receipts"):
            await _insert(conn, section, columns[section], rows[section])
        heads = [
            (row["current_event_id"], row["fact_id"])
            for row in rows["facts"]
            if row.get("current_event_id") is not None
        ]
        await conn.executemany("UPDATE memory_fact SET current_event_id=?1 WHERE fact_id=?2", heads)

        restored = await _sections(conn, doc["scope"])
        mismatched = [s for s in SECTIONS if restored[s] != rows[s]]
        if mismatched:
            raise TransferError(
                f"restored rows differ from the document in {', '.join(mismatched)}; "
                "nothing was written"
            )
    if embedder is not None:
        from mnemo.core import reindex

        await reindex(conn, embedder)
    return {section: len(doc[section]) for section in SECTIONS}


async def _insert(conn: Connection, section: str, columns: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    unknown = set().union(*rows) - set(columns)
    if unknown:
        raise TransferError(
            f"restored rows differ from the document in {section}: "
            f"unknown columns {', '.join(sorted(unknown))}"
        )
    present = [c for c in columns if any(c in row for row in rows)]
    if section == "facts":
        present = [c for c in present if c != "current_event_id"]  # set once events exist
    marks = ", ".join(f"?{i}" for i in range(1, len(present) + 1))
    await conn.executemany(
        f"INSERT INTO {TABLES[section]} ({', '.join(present)}) VALUES ({marks})",
        [[row.get(c) for c in present] for row in rows],
    )


async def _run(args: argparse.Namespace) -> None:
    from mnemo.db import check_schema, connect
    from mnemo.embedder import build_embedder

    settings = get_settings()
    conn = await connect(create=args.command == "import")
    try:
        if args.command == "export":
            await check_schema(conn)
            scope = (
                None
                if args.all_scopes
                else {
                    "namespace": settings.namespace,
                    "user_id": settings.user_id,
                    "agent_id": settings.agent_id,
                }
            )
            document = await export_store(conn, scope=scope, settings=settings)
            if args.out == "-":
                sys.stdout.write(document + "\n")
            else:
                # "x": never overwrite an earlier backup.
                with open(args.out, "x", encoding="utf-8") as handle:
                    handle.write(document + "\n")
                print(f"Exported to {args.out}", file=sys.stderr)
        else:
            from mnemo.db import apply_migrations

            await apply_migrations(conn)
            counts = await import_store(
                conn,
                Path(args.path).read_text(encoding="utf-8"),
                settings=settings,
                embedder=build_embedder(settings),
            )
            print("Imported " + ", ".join(f"{n} {section}" for section, n in counts.items()))
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write the configured scope as JSON")
    export.add_argument("--out", default="-", help="file to create (default: stdout)")
    export.add_argument(
        "--all-scopes", action="store_true", help="export every namespace/user/agent"
    )
    restore = commands.add_parser("import", help="restore an export into an empty store")
    restore.add_argument("path")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (TransferError, FileExistsError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
