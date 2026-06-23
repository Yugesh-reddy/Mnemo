"""Versioned JSON export/import of the versioned store (master plan Phase 6).

An export carries facts (with their HEAD pointers), every event, commits and
mutation receipts for one scope or for the whole store, plus the applied
migrations and the embedding backend/model/dimension that produced the vectors.
Rows are serialized by Postgres with ``to_jsonb`` and restored with
``jsonb_populate_recordset``, so values, IDs, sequence numbers and timestamps
round-trip exactly; the Python side only validates and moves text.

Import restores into an **empty** store only, in one transaction. Before commit
it re-exports what it wrote and rolls back unless that equals the document.
Pipeline working tables (fast cache, extraction jobs, quality decisions) are not
exported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg

from mnemo.config import Settings, get_settings

FORMAT = "mnemo.export"
FORMAT_VERSION = 1
SECTIONS = ("facts", "events", "commits", "receipts")
EXCLUDED_TABLES = ("fast_cache", "extraction_job", "quality_decision")
_SCOPE_KEYS = ("namespace", "user_id", "agent_id")
_STORE_TABLES = ("memory_fact", "memory_event", "memory_commit", "memory_mutation_receipt")

# $1..$3 = namespace/user/agent, or all NULL for the whole store.
_SECTIONS_SQL = """
SELECT jsonb_build_object(
  'facts', COALESCE((
    SELECT jsonb_agg(to_jsonb(f) ORDER BY f.created_at, f.fact_id)
    FROM memory_fact f
    WHERE $1::text IS NULL OR (f.namespace, f.user_id, f.agent_id) = ($1, $2, $3)
  ), '[]'::jsonb),
  'events', COALESCE((
    SELECT jsonb_agg(to_jsonb(e) ORDER BY e.seq)
    FROM memory_event e JOIN memory_fact f ON f.fact_id = e.fact_id
    WHERE $1::text IS NULL OR (f.namespace, f.user_id, f.agent_id) = ($1, $2, $3)
  ), '[]'::jsonb),
  'commits', COALESCE((
    SELECT jsonb_agg(to_jsonb(c) ORDER BY c.at_seq, c.created_at, c.commit_id)
    FROM memory_commit c
    WHERE $1::text IS NULL OR (c.namespace, c.user_id, c.agent_id) = ($1, $2, $3)
  ), '[]'::jsonb),
  'receipts', COALESCE((
    SELECT jsonb_agg(to_jsonb(r) ORDER BY r.created_at, r.receipt_id)
    FROM memory_mutation_receipt r
    WHERE $1::text IS NULL OR (r.namespace, r.user_id, r.agent_id) = ($1, $2, $3)
  ), '[]'::jsonb)
) AS sections
"""

# Facts go in without HEAD pointers (events don't exist yet); events keep their
# original seq; self-references inside one INSERT are checked at statement end.
_RESTORE_SQL = (
    """
    INSERT INTO memory_commit
    SELECT r.* FROM mnemo_import_doc d,
         jsonb_populate_recordset(NULL::memory_commit, d.doc->'commits') r
    """,
    """
    INSERT INTO memory_fact
    SELECT r.* FROM mnemo_import_doc d,
         jsonb_populate_recordset(NULL::memory_fact, (
           SELECT COALESCE(jsonb_agg(x - 'current_event_id'), '[]'::jsonb)
           FROM jsonb_array_elements(d.doc->'facts') x)) r
    """,
    """
    INSERT INTO memory_event OVERRIDING SYSTEM VALUE
    SELECT r.* FROM mnemo_import_doc d,
         jsonb_populate_recordset(NULL::memory_event, d.doc->'events') r
    """,
    """
    UPDATE memory_fact f SET current_event_id = r.current_event_id
    FROM mnemo_import_doc d,
         jsonb_populate_recordset(NULL::memory_fact, d.doc->'facts') r
    WHERE f.fact_id = r.fact_id AND r.current_event_id IS NOT NULL
    """,
    """
    INSERT INTO memory_mutation_receipt
    SELECT r.* FROM mnemo_import_doc d,
         jsonb_populate_recordset(NULL::memory_mutation_receipt, d.doc->'receipts') r
    """,
    """
    SELECT setval(pg_get_serial_sequence('memory_event', 'seq'), max(seq))
    FROM memory_event HAVING count(*) > 0
    """,
)


class TransferError(Exception):
    """An export document cannot be restored into this destination."""


def _embedding_identity(settings: Settings, dim: int | None) -> dict[str, Any]:
    # The hash backend ignores embed_model; don't let an unused setting block a restore.
    model = None if settings.backend == "hash" else settings.embed_model
    return {"backend": settings.backend, "model": model, "dim": dim}


async def _store_identity(conn: asyncpg.Connection) -> tuple[list[str], int | None]:
    migrations = await conn.fetchval(
        "SELECT COALESCE(array_agg(filename ORDER BY filename), '{}') FROM schema_migrations"
    )
    dim = await conn.fetchval(
        "SELECT atttypmod FROM pg_attribute "
        "WHERE attrelid=to_regclass('memory_event') AND attname='embedding' AND NOT attisdropped"
    )
    return list(migrations), dim


def _scope_args(scope: dict[str, str] | None) -> tuple[str | None, str | None, str | None]:
    if scope is None:
        return (None, None, None)
    return (scope["namespace"], scope["user_id"], scope["agent_id"])


async def export_store(
    conn: asyncpg.Connection,
    *,
    scope: dict[str, str] | None,
    settings: Settings | None = None,
) -> str:
    """Return a pretty-printed export document for ``scope`` (``None`` = whole store).

    Reads one repeatable-read snapshot when this call owns the transaction.
    Timestamps are rendered in UTC so documents compare byte-for-byte across hosts.
    """
    settings = settings or get_settings()
    if scope is not None and set(scope) != set(_SCOPE_KEYS):
        raise ValueError(f"scope must have exactly the keys {_SCOPE_KEYS}")
    transaction = (
        conn.transaction()
        if conn.is_in_transaction()
        else conn.transaction(isolation="repeatable_read", readonly=True)
    )
    async with transaction:
        await conn.execute("SET LOCAL TimeZone = 'UTC'")
        migrations, dim = await _store_identity(conn)
        header = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "scope": scope,
            "schema_migrations": migrations,
            "embedding": _embedding_identity(settings, dim),
            "excluded_tables": list(EXCLUDED_TABLES),
        }
        return await conn.fetchval(
            f"""
            WITH s AS ({_SECTIONS_SQL})
            SELECT jsonb_pretty(
              $4::jsonb || s.sections || jsonb_build_object(
                'exported_at', now(),
                'counts', jsonb_build_object(
                  'facts', jsonb_array_length(s.sections->'facts'),
                  'events', jsonb_array_length(s.sections->'events'),
                  'commits', jsonb_array_length(s.sections->'commits'),
                  'receipts', jsonb_array_length(s.sections->'receipts'))))
            FROM s
            """,
            *_scope_args(scope),
            json.dumps(header),
        )


def _validate_document(doc: Any) -> None:
    """Checks that need no database: format, shape, counts and row scope."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise TransferError(f"not a {FORMAT} document")
    if doc.get("format_version") != FORMAT_VERSION:
        raise TransferError(
            f"unsupported format_version {doc.get('format_version')!r}; "
            f"this Mnemo reads version {FORMAT_VERSION}"
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


async def import_store(
    conn: asyncpg.Connection, document: str, *, settings: Settings | None = None
) -> dict[str, int]:
    """Restore an export into an empty store; return the restored row counts.

    All-or-nothing: any refusal or verification mismatch writes nothing.
    """
    settings = settings or get_settings()
    try:
        # Decimal keeps numeric payloads exact during validation; the rows sent
        # to Postgres are the original text, never a Python re-serialization.
        doc = json.loads(document, parse_float=Decimal)
    except json.JSONDecodeError as exc:
        raise TransferError(f"document is not valid JSON: {exc}") from exc
    _validate_document(doc)

    async with conn.transaction():
        await conn.execute("SET LOCAL TimeZone = 'UTC'")
        # EXCLUSIVE blocks concurrent writers but not readers until commit.
        await conn.execute(f"LOCK TABLE {', '.join(_STORE_TABLES)} IN EXCLUSIVE MODE")

        migrations, dim = await _store_identity(conn)
        if doc.get("schema_migrations") != migrations:
            raise TransferError(
                "schema mismatch: the export was taken at migrations "
                f"{doc.get('schema_migrations')!r}; this store has {migrations!r}. "
                "Migrate both sides to the same version first."
            )
        expected = _embedding_identity(settings, dim)
        if doc.get("embedding") != expected:
            raise TransferError(
                f"embedding mismatch: export {doc.get('embedding')!r}, destination {expected!r}. "
                "Restore into a store configured for the same backend, model and dimension."
            )
        occupied = [
            table
            for table in _STORE_TABLES
            if await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {table})")
        ]
        if occupied:
            raise TransferError(
                f"destination is not empty ({', '.join(occupied)}); "
                "import restores into an empty store only"
            )

        await conn.execute(
            "CREATE TEMP TABLE mnemo_import_doc ON COMMIT DROP AS SELECT $1::jsonb AS doc",
            document,
        )
        for statement in _RESTORE_SQL:
            await conn.execute(statement)

        mismatched = await conn.fetch(
            f"""
            WITH s AS ({_SECTIONS_SQL})
            SELECT key FROM s, mnemo_import_doc d,
                 jsonb_each(s.sections)
            WHERE value IS DISTINCT FROM d.doc->key
            ORDER BY key
            """,
            *_scope_args(doc["scope"]),
        )
        if mismatched:
            raise TransferError(
                "restored rows differ from the document in "
                f"{', '.join(r['key'] for r in mismatched)}; nothing was written"
            )
        await conn.execute("DROP TABLE mnemo_import_doc")
    return {section: len(doc[section]) for section in SECTIONS}


async def _run(args: argparse.Namespace) -> None:
    from mnemo.db import connect

    settings = get_settings()
    conn = await connect()
    try:
        if args.command == "export":
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
            counts = await import_store(
                conn, Path(args.path).read_text(encoding="utf-8"), settings=settings
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
