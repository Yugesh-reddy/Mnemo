"""Tamper evidence: a hash chain over the event log, and the check behind `mnemo verify`.

Each event's hash is SHA-256 over the previous hash and a ``json_array`` of its payload
columns, in ``seq`` order (migration 0002 writes it on every insert). Verifying
recomputes the chain from the events and also checks that every memory's HEAD is its
newest event, so an edit made around Mnemo's triggers shows up.

Someone who rewrites an event and every later hash leaves a self-consistent chain. To
catch that, ``mnemo verify`` keeps the head it last verified in a witness file outside
the store and checks that the chain still passes through it; keep a copy elsewhere
(``--witness``) for a check that does not rely on the same disk.

A memory's identity (its scope, subject, predicate and key) lives in memory_fact, not
in its events. Migration 0003 seals each fact the same way, in a second chain over
those columns, and the witness records that chain's head too.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

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


# The identity columns of memory_fact, in the order migration 0003's trigger seals them.
FACT_COLUMNS = (
    "fact_id",
    "namespace",
    "user_id",
    "agent_id",
    "session_id",
    "subject",
    "predicate",
    "fact_key",
    "kind",
    "created_at",
    "identity_mode",
    "identity_ref",
)


def sha256_hex(text: str | bytes | None) -> str:
    """Registered as sha256_hex() on every connection."""
    data = b"" if text is None else text.encode() if isinstance(text, str) else text
    return hashlib.sha256(data).hexdigest()


def payload_sql(alias: str, columns: tuple[str, ...] = CHAIN_COLUMNS) -> str:
    return "json_array(" + ", ".join(f"{alias}.{c}" for c in columns) + ")"


def backfill(raw: sqlite3.Connection) -> int:
    """Chain the events stored before migration 0002, inside its transaction."""
    rows = raw.execute(f"SELECT seq, {payload_sql('e')} FROM memory_event e ORDER BY seq")
    previous, links = "", []
    for seq, payload in rows:
        previous = sha256_hex(previous + payload)
        links.append((seq, previous))
    raw.executemany("INSERT INTO memory_event_chain (seq, hash) VALUES (?, ?)", links)
    return len(links)


def seal_existing_facts(raw: sqlite3.Connection) -> int:
    """Seal the facts stored before migration 0003, in the order they were stored."""
    rows = raw.execute(
        f"SELECT fact_id, {payload_sql('f', FACT_COLUMNS)} FROM memory_fact f ORDER BY rowid"
    )
    previous, seals = "", []
    for fact_id, payload in rows:
        previous = sha256_hex(previous + payload)
        seals.append((fact_id, previous))
    raw.executemany("INSERT INTO memory_fact_seal (fact_id, hash) VALUES (?, ?)", seals)
    return len(seals)


@dataclass(frozen=True)
class Witness:
    """A chain position verified earlier: the hash of the event at ``seq``, and the
    seal of the fact at ``fact_seq`` (absent from witnesses written before migration 0003)."""

    seq: int
    hash: str
    fact_seq: int | None = None
    fact_hash: str | None = None

    @classmethod
    def read(cls, path: Path) -> Witness | None:
        bad = f"{path} is not a Mnemo witness file (want 'SEQ HASH [FACT_SEQ FACT_HASH]')"
        try:
            fields = path.read_text().split()
            if len(fields) not in (2, 4):
                raise ValueError(bad)
            witness = cls(int(fields[0]), fields[1])
            if len(fields) == 4:
                witness = cls(witness.seq, witness.hash, int(fields[2]), fields[3])
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise ValueError(bad) from exc
        if len(witness.hash) != 64 or len(witness.fact_hash or "f" * 64) != 64:
            raise ValueError(bad)
        return witness

    def write(self, path: Path) -> None:
        """Replace the file atomically, readable only by its owner."""
        from mnemo.embedder import private_dirs

        private_dirs(path.parent)
        temporary = path.with_name(path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            facts = f" {self.fact_seq} {self.fact_hash}" if self.fact_hash else ""
            handle.write(f"{self.seq} {self.hash}{facts}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def witness_path(db: Path) -> Path:
    """Where `mnemo verify` keeps the head it verified: beside the store, not in it."""
    return db.with_name(db.name + ".verified")


@dataclass
class Verification:
    events: int = 0
    head: str | None = None
    """The newest event's hash: keep it to detect a rewrite of the whole chain."""
    head_seq: int | None = None
    facts: int = 0
    fact_head: str | None = None
    """The newest fact seal: the witness keeps it to detect a rewrite of the seals."""
    fact_head_seq: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def witness(self) -> Witness | None:
        if not (self.head and self.head_seq):
            return None
        return Witness(self.head_seq, self.head, self.fact_head_seq, self.fact_head)

    @property
    def ok(self) -> bool:
        return not self.problems


async def verify(conn: Connection, since: Witness | None = None) -> Verification:
    """Recompute the chain and check HEADs, in one read snapshot. With ``since``, also
    check that the chain still passes through that earlier verified head."""
    result = Verification()
    async with conn.transaction(readonly=not conn.is_in_transaction()):

        def chain() -> None:
            rows = conn.raw.execute(
                f"SELECT e.seq, {payload_sql('e')}, c.hash FROM memory_event e "
                "LEFT JOIN memory_event_chain c ON c.seq = e.seq ORDER BY e.seq"
            )
            previous, seen = "", None
            for seq, payload, stored in rows:
                result.events += 1
                result.head_seq = seq
                expected = sha256_hex(previous + payload)
                if stored is None:
                    result.problems.append(f"event seq {seq} has no chain entry")
                elif stored != expected and len(result.problems) < 20:
                    result.problems.append(
                        f"event seq {seq} does not match its hash: it or an earlier "
                        "event was changed, removed or inserted outside Mnemo"
                    )
                previous = stored or expected
                if since is not None and seq == since.seq:
                    seen = previous
            result.head = previous or None
            if since is not None and seen != since.hash:
                result.problems.append(
                    f"history was rewritten since it was last verified: the chain no longer "
                    f"passes through seq {since.seq} with hash {since.hash[:16]}…"
                    + (" (that event is gone)" if seen is None else "")
                )
            seals(conn.raw, result, since)
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


def seals(raw: sqlite3.Connection, result: Verification, since: Witness | None) -> None:
    """Recompute the fact seals: a memory's identity changed, deleted or inserted
    outside Mnemo breaks its seal, and a rewrite of every seal misses the witness."""
    rows = raw.execute(
        f"SELECT s.seq, s.fact_id, {payload_sql('f', FACT_COLUMNS)}, f.fact_id, s.hash "
        "FROM memory_fact_seal s LEFT JOIN memory_fact f ON f.fact_id = s.fact_id "
        "ORDER BY s.seq"
    )
    previous, seen, reported = "", None, 0

    def problem(text: str) -> None:
        nonlocal reported
        reported += 1
        if reported <= 20:
            result.problems.append(text)

    for seq, fact_id, payload, present, stored in rows:
        result.facts += 1
        result.fact_head_seq = seq
        if present is None:
            problem(f"memory {fact_id} was deleted outside Mnemo (its seal remains)")
        elif sha256_hex(previous + payload) != stored:
            problem(
                f"memory {fact_id}: its identity (scope, subject, predicate or key) was "
                "changed outside Mnemo, or an earlier seal was"
            )
        previous = stored
        if since is not None and since.fact_seq == seq:
            seen = previous
    result.fact_head = previous or None
    unsealed = raw.execute(
        "SELECT f.fact_id FROM memory_fact f "
        "LEFT JOIN memory_fact_seal s ON s.fact_id = f.fact_id WHERE s.fact_id IS NULL"
    ).fetchall()
    for (fact_id,) in unsealed:
        problem(f"memory {fact_id} has no seal: it was inserted outside Mnemo")
    if since is not None and since.fact_hash is not None and seen != since.fact_hash:
        result.problems.append(
            "memory identities were rewritten since they were last verified: the seals no "
            f"longer pass through seq {since.fact_seq} with hash {since.fact_hash[:16]}…"
            + (" (that seal is gone)" if seen is None else "")
        )
