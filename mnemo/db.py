"""Database helpers + a tiny forward-only migration runner.

Migrations are plain ``.sql`` files in ``migrations/`` applied in filename order.
Applied files are recorded in ``schema_migrations`` so re-runs are no-ops. The SQL
is the product (CLAUDE.md golden rule #6) — this runner stays deliberately thin.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg

from mnemo.config import get_settings

_PACKAGE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = (
    _PACKAGE_DIR / "migrations"
    if (_PACKAGE_DIR / "migrations").is_dir()
    else _PACKAGE_DIR.parent / "migrations"
)


def to_vector_literal(vec: list[float] | str | None) -> str | None:
    """Format a vector as a pgvector text literal.

    Idempotent: a string is assumed already-formatted and passed through. This also
    serves as the asyncpg encoder, which may receive either form.
    """
    if vec is None:
        return None
    if isinstance(vec, str):
        return vec
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _decode_vector(text: str) -> list[float]:
    text = text.strip()
    if text in ("", "[]"):
        return []
    return [float(x) for x in text[1:-1].split(",")]


async def register_vector(conn: asyncpg.Connection) -> None:
    """Teach a connection to read pgvector columns as ``list[float]``.

    Tolerant of the type not existing yet (e.g. before migrations run). Writes use
    ``$n::vector`` literals, so this is only needed for reading embeddings back.
    """
    try:
        await conn.set_type_codec(
            "vector",
            schema="public",
            encoder=to_vector_literal,
            decoder=_decode_vector,
            format="text",
        )
    except asyncpg.exceptions.UndefinedObjectError:
        pass


async def connect(dsn: str | None = None) -> asyncpg.Connection:
    """Open a single asyncpg connection (caller owns closing it)."""
    conn = await asyncpg.connect(dsn or get_settings().dsn)
    try:
        await register_vector(conn)
        await validate_embedding_dimension(conn)
    except Exception:
        await conn.close()
        raise
    return conn


async def validate_embedding_dimension(
    conn: asyncpg.Connection, expected: int | None = None
) -> None:
    """Fail before writing if the configured model dimension disagrees with storage."""
    expected = expected or get_settings().embed_dim
    for table in ("memory_event", "fast_cache"):
        dimension = await conn.fetchval(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid=to_regclass($1) AND attname='embedding' AND NOT attisdropped",
            table,
        )
        if dimension is not None and dimension != expected:
            raise ValueError(
                f"{table}.embedding uses {dimension} dimensions; "
                f"configured embed_dim is {expected}. "
                "Use the matching embedding model or explicitly migrate and re-embed the data."
            )


async def apply_migrations(
    conn: asyncpg.Connection, migrations_dir: Path = MIGRATIONS_DIR, *, embed_dim: int | None = None
) -> list[str]:
    """Apply any un-applied ``*.sql`` files in order. Returns the names applied."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        );
        """)

    dimension = get_settings().embed_dim if embed_dim is None else embed_dim
    if not 1 <= dimension <= 2000:
        raise ValueError("embedding dimension must be 1..2000 for vector HNSW")
    if not migrations_dir.is_dir() or not list(migrations_dir.glob("*.sql")):
        raise RuntimeError(f"Packaged migrations are missing: {migrations_dir}")
    applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        async with conn.transaction():
            # The lock also works for isolated evaluation schemas on a shared DB.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'mnemo:migrations:' || current_schema(), 0))"
            )
            already = await conn.fetchval(
                "SELECT 1 FROM schema_migrations WHERE filename = $1", path.name
            )
            if already:
                continue
            sql = path.read_text()
            # Render only the initial dimension for a fresh database. Applied SQL
            # is never rewritten, and existing embeddings are never silently recast.
            if path.name == "0001_init.sql":
                sql = re.sub(r"vector\(768\)", f"vector({dimension})", sql)
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
        applied.append(path.name)
    await validate_embedding_dimension(conn, dimension)
    return applied


def _validate_isolated_schema(schema: str) -> None:
    if not re.fullmatch(r"mnemo_(eval|worker)_[0-9a-f]{32}", schema):
        raise ValueError("isolated schemas require a generated Mnemo eval/worker UUID name")


async def prepare_isolated_schema(
    dsn: str, schema: str, *, embed_dim: int | None = None
) -> asyncpg.Connection:
    """Create and migrate a disposable namespace without reusing application tables."""
    _validate_isolated_schema(schema)
    conn = await asyncpg.connect(dsn)
    created = False
    try:
        await conn.execute("SET search_path TO public")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        created = True
        # Create this first with only the private schema visible. Once public is
        # added for extension types, it cannot shadow migration bookkeeping.
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute(
            "CREATE TABLE schema_migrations ("
            "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        await conn.execute(f'SET search_path TO "{schema}", public')
        await apply_migrations(conn, embed_dim=embed_dim)
        await register_vector(conn)
        return conn
    except Exception:
        try:
            if created:
                await drop_isolated_schema(conn, schema)
        finally:
            await conn.close()
        raise


async def drop_isolated_schema(conn: asyncpg.Connection, schema: str) -> None:
    """Remove only a generated test/evaluation namespace; bound lock retries."""
    _validate_isolated_schema(schema)
    await conn.execute("SET search_path TO public")
    await conn.execute("SET lock_timeout TO '5s'")
    for attempt in range(3):
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            return
        except (asyncpg.DeadlockDetectedError, asyncpg.LockNotAvailableError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.1)


async def _main() -> None:
    conn = await connect()
    try:
        applied = await apply_migrations(conn)
        if applied:
            print("Applied migrations:")
            for name in applied:
                print(f"  + {name}")
        else:
            print("No pending migrations; schema is up to date.")
    finally:
        await conn.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
