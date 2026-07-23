"""The store's SQLite file: open it, and talk to it the way the code talked to asyncpg.

One ``Connection`` wraps one ``sqlite3`` connection. Statements run in a worker
thread (``asyncio.to_thread``) so a call waiting on another process's write lock does
not stall the event loop. The wrapper keeps asyncpg's shape (``fetch``, ``fetchrow``,
``fetchval``, ``execute``, ``transaction()``) so call sites change their SQL and
little else.

Values cross the boundary as Python types: IDs as ``UUID``, timestamps as aware
``datetime``, ``object_number`` as ``Decimal``. The columns that carry them are
named below, because SQLite stores all of them as text and a view or an alias has
no declared type to go by.

Transactions: the outermost one is ``BEGIN IMMEDIATE`` (it takes the file's single
write lock at once, so it never fails half way when it starts writing), nested ones
are savepoints, and a ``readonly`` one is a plain ``BEGIN`` (a WAL snapshot that
never blocks writers). ``now()`` in SQL returns the time the transaction began.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar
from uuid import UUID

T = TypeVar("T")

BUSY_TIMEOUT_MS = 30_000

UUID_COLUMNS = frozenset(
    {
        "fact_id",
        "event_id",
        "current_event_id",
        "superseded_by",
        "parent_event_id",
        "commit_id",
        "parent_commit_id",
        "receipt_id",
        "request_id",
        "previous_event_id",
        "restored_from_event_id",
        "identity_ref",
    }
)
TIME_COLUMNS = frozenset(
    {
        "valid_from",
        "valid_to",
        "expires_at",
        "recorded_at",
        "superseded_at",
        "last_used",
        "created_at",
        "applied_at",
    }
)
DECIMAL_COLUMNS = frozenset({"object_number"})


class StoreUnavailable(Exception):
    """The file cannot be opened as a Mnemo store; the message names the fix."""


# ---- timestamps ------------------------------------------------------------------


def format_ts(value: datetime) -> str:
    """The one stored form: fixed width, UTC, microseconds, so text order is time order."""
    if value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class _Clock:
    """Wall time that never runs backwards inside this process."""

    def __init__(self) -> None:
        self._last = datetime.min.replace(tzinfo=UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            now = datetime.now(tz=UTC)
            if now <= self._last:
                # Two reads within one microsecond: keep events strictly ordered.
                now = self._last + timedelta(microseconds=1)
            self._last = now
            return now


clock = _Clock()


# ---- values ------------------------------------------------------------------------


def to_param(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_ts(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True)
    return value


def _from_column(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in UUID_COLUMNS and isinstance(value, str):
        return UUID(value)
    if name in TIME_COLUMNS and isinstance(value, str):
        return parse_ts(value)
    if name in DECIMAL_COLUMNS and isinstance(value, (str, int, float)):
        return Decimal(str(value))
    return value


class Record(dict):
    """A row as asyncpg gave one: ``row["name"]``, ``row[0]``, and iteration over values.

    ``dict(row)`` and ``row.items()`` still give names and values.
    """

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

    def __iter__(self) -> Iterator[Any]:  # type: ignore[override]
        return iter(self.values())


def _records(cursor: sqlite3.Cursor, rows: Iterable[tuple]) -> list[Record]:
    names = [column[0] for column in cursor.description or ()]
    return [
        Record((name, _from_column(name, value)) for name, value in zip(names, row, strict=True))
        for row in rows
    ]


# ---- the connection ----------------------------------------------------------------


class Connection:
    def __init__(self, raw: sqlite3.Connection, path: Path) -> None:
        self.raw = raw
        self.path = path
        self.vector_functions = "sqlite-vec"
        """"python" when this Python's SQLite cannot load sqlite-vec."""
        self._lock = threading.Lock()
        self._depth = 0
        self._tx_now: datetime | None = None
        self._stmt_now: datetime | None = None
        raw.create_function("now", 0, self._now_text)

    # ---- time --------------------------------------------------------------------

    def now(self) -> datetime:
        """The current transaction's start time, else the current statement's."""
        return self._tx_now or self._stmt_now or clock()

    def _now_text(self) -> str:
        return format_ts(self.now())

    # ---- running statements ------------------------------------------------------

    async def _call(self, fn: Callable[[], T]) -> T:
        def run() -> T:
            with self._lock:
                if self._depth == 0:
                    self._stmt_now = clock()
                try:
                    return fn()
                finally:
                    self._stmt_now = None

        return await asyncio.to_thread(run)

    async def execute(self, sql: str, *args: Any) -> str:
        def run() -> str:
            cursor = self.raw.execute(sql, [to_param(a) for a in args])
            return f"{sql.split(None, 1)[0].upper()} {cursor.rowcount}"

        return await self._call(run)

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        params = [[to_param(a) for a in row] for row in rows]
        await self._call(lambda: self.raw.executemany(sql, params))

    async def fetch(self, sql: str, *args: Any) -> list[Record]:
        def run() -> list[Record]:
            cursor = self.raw.execute(sql, [to_param(a) for a in args])
            return _records(cursor, cursor.fetchall())

        return await self._call(run)

    async def fetchrow(self, sql: str, *args: Any) -> Record | None:
        def run() -> Record | None:
            cursor = self.raw.execute(sql, [to_param(a) for a in args])
            row = cursor.fetchone()
            records = [] if row is None else _records(cursor, [row])
            cursor.close()
            return records[0] if records else None

        return await self._call(run)

    async def fetchval(self, sql: str, *args: Any, column: int = 0) -> Any:
        row = await self.fetchrow(sql, *args)
        return None if row is None else row[column]

    # ---- transactions ------------------------------------------------------------

    def is_in_transaction(self) -> bool:
        return self._depth > 0

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> AsyncIterator[None]:
        """Outermost: BEGIN IMMEDIATE (or BEGIN when ``readonly``); nested: a savepoint."""
        depth = self._depth
        name = f"mnemo_sp_{depth}"

        def begin() -> None:
            if depth == 0:
                self.raw.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
                self._tx_now = clock()
            else:
                self.raw.execute(f"SAVEPOINT {name}")

        def end(ok: bool) -> None:
            try:
                if depth == 0:
                    self.raw.execute("COMMIT" if ok else "ROLLBACK")
                elif ok:
                    self.raw.execute(f"RELEASE {name}")
                else:
                    self.raw.execute(f"ROLLBACK TO {name}")
                    self.raw.execute(f"RELEASE {name}")
            finally:
                if depth == 0:
                    self._tx_now = None

        await asyncio.to_thread(self._locked, begin)
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            await asyncio.to_thread(self._locked, lambda: end(False))
            raise
        self._depth -= 1
        try:
            await asyncio.to_thread(self._locked, lambda: end(True))
        except BaseException:
            if depth == 0 and self.raw.in_transaction:
                await asyncio.to_thread(self._locked, lambda: self.raw.execute("ROLLBACK"))
            raise

    def _locked(self, fn: Callable[[], T]) -> T:
        with self._lock:
            return fn()

    async def close(self) -> None:
        await asyncio.to_thread(self._locked, self.raw.close)

    def is_closed(self) -> bool:
        try:
            self.raw.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            return True
        return False


# ---- opening the file --------------------------------------------------------------


def _cosine_distance(a: bytes | None, b: bytes | None) -> float | None:
    """sqlite-vec's ``vec_distance_cosine`` for two float32 blobs, in Python."""
    if a is None or b is None:
        return None
    if len(a) != len(b):
        raise ValueError("vector dimensions differ")
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy comes with fastembed
        from array import array

        x, y = array("f", a), array("f", b)
        dot = sum(p * q for p, q in zip(x, y, strict=True))
        norms = sum(p * p for p in x) ** 0.5 * sum(q * q for q in y) ** 0.5
    else:
        x, y = np.frombuffer(a, dtype=np.float32), np.frombuffer(b, dtype=np.float32)
        dot = float(x @ y)
        norms = float(np.linalg.norm(x) * np.linalg.norm(y))
    return 1.0 - dot / norms if norms else 1.0


def _vector_functions(raw: sqlite3.Connection) -> str:
    """Load sqlite-vec, or register the same function in Python when this Python's
    SQLite cannot load extensions (python.org builds). Returns which one is in use."""
    try:
        import sqlite_vec

        raw.enable_load_extension(True)
        try:
            sqlite_vec.load(raw)
        finally:
            raw.enable_load_extension(False)
        return "sqlite-vec"
    except (AttributeError, sqlite3.OperationalError):
        raw.create_function("vec_distance_cosine", 2, _cosine_distance, deterministic=True)
        raw.create_function("vec_version", 0, lambda: "python", deterministic=True)
        return "python"


def capability_problem() -> str | None:
    """None when this Python's SQLite can run the store (FTS5 is required; vectors fall
    back to Python without sqlite-vec); else the fix."""
    raw = sqlite3.connect(":memory:")
    try:
        _vector_functions(raw)
        raw.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize = 'porter unicode61')")
    except sqlite3.OperationalError as exc:
        return f"this Python's SQLite lacks FTS5 ({exc}); install Mnemo with uv's Python 3.12"
    finally:
        raw.close()
    return None


def _ensure_file(path: Path) -> None:
    """Create the store private to this user: directory 0700, file 0600."""
    if path.exists():
        return
    from mnemo.embedder import private_dirs

    private_dirs(path.parent)
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


def open_sync(path: str | Path, *, create: bool = True) -> Connection:
    """Open the store file with Mnemo's pragmas, extension and ``now()``."""
    path = Path(path).expanduser()
    if create:
        _ensure_file(path)
    elif not path.is_file():
        raise StoreUnavailable(f"no Mnemo store at {path}; run `mnemo install`")
    raw = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    try:
        vectors = _vector_functions(raw)
        raw.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if raw.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            raw.execute("PRAGMA journal_mode = WAL")
        # FULL: a commit survives power loss, as it did on Postgres. Agents write
        # rarely, so the extra sync per commit costs little.
        raw.execute("PRAGMA synchronous = FULL")
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA recursive_triggers = ON")
    except sqlite3.DatabaseError as exc:
        raw.close()
        raise StoreUnavailable(f"{path} is not a readable Mnemo store ({exc})") from exc
    except BaseException:
        raw.close()
        raise
    conn = Connection(raw, path)
    conn.vector_functions = vectors
    return conn


async def connect(path: str | Path, *, create: bool = True) -> Connection:
    return await asyncio.to_thread(open_sync, path, create=create)
