"""Open the store file and apply its schema.

Migrations are plain ``.sql`` files in ``migrations/`` applied in filename order.
Applied files are recorded in ``schema_migrations`` so re-runs are no-ops. The SQL
is the product: this runner stays deliberately thin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from mnemo.config import get_settings
from mnemo.sqlite import Connection, StoreUnavailable
from mnemo.sqlite import connect as open_store

_PACKAGE_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = (
    _PACKAGE_DIR / "migrations"
    if (_PACKAGE_DIR / "migrations").is_dir()
    else _PACKAGE_DIR.parent / "migrations"
)


_HAS_TABLE = "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"


def migration_names(migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    names = sorted(path.name for path in migrations_dir.glob("*.sql"))
    if not names:
        raise RuntimeError(f"Packaged migrations are missing: {migrations_dir}")
    return names


async def connect(path: str | Path | None = None, *, create: bool = True) -> Connection:
    """Open the configured store file (caller owns closing it)."""
    return await open_store(path or get_settings().db, create=create)


async def applied_migrations(conn: Connection) -> list[str]:
    if not await conn.fetchval(_HAS_TABLE):
        return []
    rows = await conn.fetch("SELECT filename FROM schema_migrations ORDER BY filename")
    return [row["filename"] for row in rows]


async def check_schema(conn: Connection, migrations_dir: Path = MIGRATIONS_DIR) -> None:
    """Refuse a store this code cannot use: never migrated, or migrated by newer code."""
    applied = await applied_migrations(conn)
    known = migration_names(migrations_dir)
    if not applied:
        raise StoreUnavailable(f"{conn.path} has no Mnemo schema; run `mnemo install`")
    if applied != known:
        newer = sorted(set(applied) - set(known))
        if newer:
            raise StoreUnavailable(
                f"{conn.path} was migrated by a newer Mnemo ({', '.join(newer)}); "
                "upgrade Mnemo, then restart your agents"
            )
        raise StoreUnavailable(f"{conn.path} needs migrating; run `mnemo migrate`")


async def apply_migrations(conn: Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply any un-applied ``*.sql`` files in order. Returns the names applied."""
    applied: list[str] = []
    for name in migration_names(migrations_dir):
        sql = (migrations_dir / name).read_text()

        def run(sql: str = sql, name: str = name) -> bool:
            raw = conn.raw
            raw.execute("BEGIN IMMEDIATE")
            try:
                done = (
                    raw.execute(_HAS_TABLE).fetchone()
                    and raw.execute(
                        "SELECT 1 FROM schema_migrations WHERE filename=?", (name,)
                    ).fetchone()
                )
                if done:
                    raw.execute("ROLLBACK")
                    return False
                # executescript would commit first; run each statement in this transaction.
                for statement in _statements(sql):
                    raw.execute(statement)
                raw.execute("INSERT INTO schema_migrations (filename) VALUES (?)", (name,))
                raw.execute("COMMIT")
                return True
            except BaseException:
                raw.execute("ROLLBACK")
                raise

        if await conn._call(run):
            applied.append(name)
    return applied


def _statements(sql: str) -> list[str]:
    """Split a schema file into complete statements (triggers contain semicolons)."""
    import sqlite3

    statements: list[str] = []
    current = ""
    for line in sql.splitlines(keepends=True):
        current += line
        if sqlite3.complete_statement(current):
            if current.strip():
                statements.append(current.strip())
            current = ""
    if current.strip() and not all(
        part.strip().startswith("--") or not part.strip() for part in current.splitlines()
    ):
        raise ValueError(f"incomplete SQL statement at the end of the file: {current[:80]!r}")
    return statements


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
