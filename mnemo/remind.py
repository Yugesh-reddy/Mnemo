"""`mnemo remind --hook`: Claude Code's end-of-task reminder to save what lasts.

Agents are told to save what a later session would need as they work (the memory
policy), and in a long task they forget. ``mnemo install`` adds this as a Stop hook: when
Claude finishes a turn that did real work (at least one tool call that is not a memory
call) without writing a memory, it asks Claude, once, to save anything worth keeping
before it stops, or to finish without mentioning memory. It is quiet otherwise:

- never on the reminded turn itself (``stop_hook_active``), so it cannot loop;
- not after a turn that already saved, updated, retracted or restored a memory;
- not after a turn with no tool calls (a chat answer), which the policy covers;
- at most once every ``COOLDOWN_MINUTES`` per session, so a long session is not nagged
  after every reply and its context does not fill with reminders.

The turn is read from the session's transcript. When there is none (``claude -p
--no-session-persistence``), the turn cannot be judged and the reminder is given. Any
error prints nothing and exits 0: a reminder must never stop a session from ending.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO

COOLDOWN_MINUTES = 20
TAIL_BYTES = 2_000_000  # the end of the transcript is enough to find the last turn
MEMORY_TOOL = "mcp__mnemo__memory_"
WRITES = ("create", "update", "retract", "revert")

REMINDER = (
    "Before you finish (a reminder from the user's Mnemo memory setup): if this task "
    "turned up something a later session in this project would need, save it now with "
    "memory_create, or memory_update if a memory on that subject exists. That means a "
    "command that worked, a convention or rule, a decision and its reason, where "
    "something lives, a gotcha, a preference the user stated, or the unfinished work and "
    "next step. Skip small talk, one-off answers, the steps you took, status at a moment "
    "(tests passing now), anything already saved, and secrets. Don't repeat your "
    "answer: after saving, say in one short line what you saved; if nothing qualifies, "
    "reply only with 'Nothing new to remember.'"
)


def last_turn(transcript: Path) -> dict[str, int] | None:
    """Tool calls in the transcript's last turn (since the user's last message): work
    (not memory) and memory writes. None when there is no readable transcript."""
    try:
        with transcript.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    work = writes = 0
    for raw in reversed(lines):
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(line, dict) or line.get("isSidechain"):
            continue
        message = line.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if line.get("type") == "user" and not line.get("isMeta"):
            blocks = content if isinstance(content, list) else []
            if not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                break  # the user's message that started this turn
        if line.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                if name.startswith(MEMORY_TOOL):
                    writes += name.removeprefix(MEMORY_TOOL) in WRITES
                else:
                    work += 1
    return {"work": work, "writes": writes}


class Cooldown:
    """When each session was last reminded (``remind-state.json`` beside the store)."""

    def __init__(self, path: Path, now: float | None = None) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        now = now or time.time()
        # Forget sessions untouched for a day.
        self.data = {
            key: value
            for key, value in (data.items() if isinstance(data, dict) else [])
            if isinstance(value, (int, float)) and now - value < 86400
        }

    def recent(self, session: str, now: float) -> bool:
        last = self.data.get(session)
        return last is not None and now - last < COOLDOWN_MINUTES * 60

    def mark(self, session: str, now: float) -> None:
        self.data[session] = now
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)


def decide(payload: dict[str, Any], cooldown: Cooldown, now: float | None = None) -> bool:
    """Whether to remind Claude before it stops."""
    now = now or time.time()
    if payload.get("stop_hook_active"):
        return False
    session = str(payload.get("session_id") or "")
    if session and cooldown.recent(session, now):
        return False
    path = payload.get("transcript_path")
    turn = last_turn(Path(path)) if isinstance(path, str) and path else None
    if turn is not None and (turn["writes"] or not turn["work"]):
        return False
    if session:
        cooldown.mark(session, now)
    return True


def run(args: Any, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        payload = json.loads(stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        db = Path(args.db).expanduser() if args.db else Path.home() / ".mnemo" / "mnemo.db"
        if decide(payload, Cooldown(db.with_name("remind-state.json"))):
            print(json.dumps({"decision": "block", "reason": REMINDER}), file=stdout)
    except Exception as exc:  # noqa: BLE001 - fail open: a session must be able to stop
        if os.environ.get("MNEMO_REMIND_DEBUG"):
            raise
        print(f"mnemo remind: skipped ({type(exc).__name__})", file=sys.stderr)
    return 0
