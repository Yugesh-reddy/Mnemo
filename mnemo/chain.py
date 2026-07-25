"""Tamper evidence: a hash chain over the event log, and the check behind `mnemo verify`.

Each event's hash is SHA-256 over the previous hash and a ``json_array`` of its payload
columns, in ``seq`` order (migration 0002 writes it on every insert). Verifying
recomputes the chain from the events and also checks that every memory's HEAD is its
newest event, so an edit made around Mnemo's triggers shows up. It does not catch
someone who also rewrites every later hash; compare the printed chain head with one
you kept for that.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from mnemo.sqlite import Connection

# Every payload column of memory_event (all but the supersession and recall
# bookkeeping), in the order migration 0002's trigger hashes them.
CHAIN_COLUMNS = (
    "seq",
    "event_id",
    "fact_id",
    "op",
    "object_text",
    "object_number",
    "object_json",
    "provenance",
    "actor",
    "confidence",
    "trust_level",
    "source_span",
    "valid_from",
    "valid_to",
    "recorded_at",
    "parent_event_id",
    "commit_id",
    "importance",
    "write_score",
    "tier",
    "reason",
    "session_id",
    "expires_at",
)


def sha256_hex(text: str | bytes | None) -> str:
    """Registered as sha256_hex() on every connection."""
    data = b"" if text is None else text.encode() if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def payload_sql(alias: str) -> str:
    return "json_array(" + ", ".join(f"{alias}.{c}" for c in CHAIN_COLUMNS) + ")"


def backfill(raw: sqlite3.Connection) -> int:
    """Chain the events stored before migration 0002, inside its transaction."""
    rows = raw.execute(f"SELECT seq, {payload_sql('e')} FROM memory_event e ORDER BY seq")
    previous, links = "", []
    for seq, payload in rows:
        previous = sha256_hex(previous + payload)
        links.append((seq, previous))
    raw.executemany("INSERT INTO memory_event_chain (seq, hash) VALUES (?, ?)", links)
    return len(links)


@dataclass
class Verification:
    events: int = 0
    head: str | None = None
    """The newest event's hash: keep it to detect a rewrite of the whole chain."""
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


async def verify(conn: Connection) -> Verification:
    """Recompute the chain and check HEADs, in one read snapshot."""
    result = Verification()
    async with conn.transaction(readonly=not conn.is_in_transaction()):

        def chain() -> None:
            rows = conn.raw.execute(
                f"SELECT e.seq, {payload_sql('e')}, c.hash FROM memory_event e "
                "LEFT JOIN memory_event_chain c ON c.seq = e.seq ORDER BY e.seq"
            )
            previous = ""
            for seq, payload, stored in rows:
                result.events += 1
                expected = sha256_hex(previous + payload)
                if stored is None:
                    result.problems.append(f"event seq {seq} has no chain entry")
                elif stored != expected and len(result.problems) < 20:
                    result.problems.append(
                        f"event seq {seq} does not match its hash: it or an earlier "
                        "event was changed, removed or inserted outside Mnemo"
                    )
                previous = stored or expected
            result.head = previous or None
            orphans = conn.raw.execute(
                "SELECT c.seq FROM memory_event_chain c "
                "LEFT JOIN memory_event e ON e.seq = c.seq WHERE e.seq IS NULL"
            ).fetchall()
            for (seq,) in orphans[:20]:
                result.problems.append(f"event seq {seq} was deleted (its hash remains)")
            heads = conn.raw.execute("""
                SELECT f.fact_id, f.status, f.current_event_id, last.event_id, last.op
                FROM memory_fact f
                LEFT JOIN memory_event last ON last.seq = (
                  SELECT max(seq) FROM memory_event WHERE fact_id = f.fact_id)
                """).fetchall()
            for fact_id, status, current, newest, op in heads:
                if current != newest:
                    result.problems.append(f"memory {fact_id}: HEAD is not its newest event")
                elif (status == "invalidated") != (op == "INVALIDATE"):
                    result.problems.append(
                        f"memory {fact_id}: status {status!r} disagrees with its HEAD ({op})"
                    )

        await conn._call(chain)
    return result
