"""Database helpers + a tiny forward-only migration runner.

Migrations are plain ``.sql`` files in ``migrations/`` applied in filename order.
Applied files are recorded in ``schema_migrations`` so re-runs are no-ops. The SQL
is the product (CLAUDE.md golden rule #6) — this runner stays deliberately thin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg

from mnemo.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


async def connect(dsn: str | None = None) -> asyncpg.Connection:
    """Open a single asyncpg connection (caller owns closing it)."""
    return await asyncpg.connect(dsn or get_settings().dsn)


async def apply_migrations(
    conn: asyncpg.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply any un-applied ``*.sql`` files in order. Returns the names applied."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        );
        """)

    applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        already = await conn.fetchval(
            "SELECT 1 FROM schema_migrations WHERE filename = $1", path.name
        )
        if already:
            continue
        sql = path.read_text()
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", path.name)
        applied.append(path.name)
    return applied


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


if __name__ == "__main__":
    asyncio.run(_main())
