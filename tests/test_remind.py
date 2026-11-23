"""`mnemo remind --hook`: Claude Code is reminded once to save what lasts after a turn
that did work and saved nothing; never twice in a row, never after a save or a chat
answer, at most once per cooldown per session, and never in a way that stops a session."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mnemo.install as inst
from mnemo import remind


def _line(kind: str, content, **more) -> str:
    return json.dumps({"type": kind, "message": {"role": kind, "content": content}, **more})


def _tool(name: str) -> str:
    return _line("assistant", [{"type": "tool_use", "id": "t", "name": name, "input": {}}])


RESULT = _line("user", [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}])
EARLIER = [
    _line("user", "first task"),
    _tool("Bash"),
    RESULT,
    _line("assistant", [{"type": "text", "text": "done"}]),
]


def _transcript(tmp_path: Path, turn: list[str], name: str = "t.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("\n".join([*EARLIER, _line("user", "next task"), *turn]) + "\n")
    return path


def _decide(tmp_path, transcript: Path | None, **payload) -> bool:
    cooldown = remind.Cooldown(tmp_path / "remind-state.json", now=1_000_000.0)
    body = {"session_id": "s1", "transcript_path": str(transcript) if transcript else None}
    return remind.decide({**body, **payload}, cooldown, now=1_000_000.0)


def test_a_turn_that_worked_and_saved_nothing_gets_one_reminder(tmp_path):
    turn = [
        _tool("Read"),
        RESULT,
        _tool("Bash"),
        RESULT,
        _line("assistant", [{"type": "text", "text": "x"}]),
    ]
    assert _decide(tmp_path, _transcript(tmp_path, turn))


@pytest.mark.parametrize(
    "turn",
    [
        [_line("assistant", [{"type": "text", "text": "a chat answer"}])],
        [_tool("mcp__mnemo__memory_search"), RESULT],  # only looked in memory
        [_tool("Edit"), RESULT, _tool("mcp__mnemo__memory_create"), RESULT],
        [_tool("Bash"), RESULT, _tool("mcp__mnemo__memory_update"), RESULT],
        [_tool("Bash"), RESULT, _tool("mcp__mnemo__memory_retract"), RESULT],
    ],
)
def test_no_reminder_after_a_save_or_a_turn_without_work(tmp_path, turn):
    assert not _decide(tmp_path, _transcript(tmp_path, turn))


def test_work_in_an_earlier_turn_does_not_count(tmp_path):
    # The earlier turn ran Bash; this one only answered.
    turn = [_line("assistant", [{"type": "text", "text": "sure"}])]
    assert not _decide(tmp_path, _transcript(tmp_path, turn))


def test_never_on_the_reminded_turn_itself(tmp_path):
    turn = [_tool("Bash"), RESULT]
    assert not _decide(tmp_path, _transcript(tmp_path, turn), stop_hook_active=True)


def test_at_most_once_per_cooldown_per_session(tmp_path):
    path = _transcript(tmp_path, [_tool("Bash"), RESULT])
    cooldown = remind.Cooldown(tmp_path / "remind-state.json", now=1_000_000.0)
    payload = {"session_id": "s1", "transcript_path": str(path)}
    assert remind.decide(payload, cooldown, now=1_000_000.0)
    later = remind.Cooldown(tmp_path / "remind-state.json", now=1_000_060.0)  # a new process
    assert not remind.decide(payload, later, now=1_000_000.0 + 60)
    assert remind.decide({**payload, "session_id": "s2"}, later, now=1_000_000.0 + 60)
    assert remind.decide(payload, later, now=1_000_000.0 + remind.COOLDOWN_MINUTES * 60 + 1)


def test_without_a_transcript_the_turn_cannot_be_judged_and_is_reminded(tmp_path):
    assert _decide(tmp_path, tmp_path / "missing.jsonl")
    assert _decide(tmp_path, None, session_id="other")


def test_the_hook_prints_claude_codes_block_decision(tmp_path):
    path = _transcript(tmp_path, [_tool("Bash"), RESULT])
    out = io.StringIO()
    args = SimpleNamespace(db=tmp_path / "mnemo.db", hook=True)
    payload = json.dumps(
        {"session_id": "s", "transcript_path": str(path), "stop_hook_active": False}
    )
    assert remind.run(args, io.StringIO(payload), out) == 0
    assert json.loads(out.getvalue()) == {"decision": "block", "reason": remind.REMINDER}
    quiet = io.StringIO()
    assert remind.run(args, io.StringIO(payload), quiet) == 0  # cooldown
    assert quiet.getvalue() == ""


@pytest.mark.parametrize("stdin", ["not json", "[]", ""])
def test_bad_input_never_blocks_a_session(tmp_path, stdin):
    out = io.StringIO()
    args = SimpleNamespace(db=tmp_path / "missing-dir" / "mnemo.db", hook=True)
    assert remind.run(args, io.StringIO(stdin), out) == 0
    assert out.getvalue() == "" or stdin == ""


def test_an_unwritable_state_folder_prints_nothing(tmp_path):
    out = io.StringIO()
    args = SimpleNamespace(db=tmp_path / "missing-dir" / "mnemo.db", hook=True)
    payload = json.dumps({"session_id": "s", "transcript_path": None})
    assert remind.run(args, io.StringIO(payload), out) == 0
    assert out.getvalue() == ""


def test_the_command_the_installer_writes_runs_end_to_end(tmp_path):
    path = _transcript(tmp_path, [_tool("Bash"), RESULT])
    payload = json.dumps({"session_id": "e2e", "transcript_path": str(path)})
    done = subprocess.run(
        [sys.executable, "-m", "mnemo.cli", "remind", "--hook", "--db", str(tmp_path / "m.db")],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(done.stdout)["decision"] == "block"


def test_the_installer_adds_and_removes_only_its_stop_hook():
    theirs = {"hooks": [{"type": "command", "command": "notify-send done"}]}
    text = json.dumps({"hooks": {"Stop": [theirs]}})
    added = json.loads(inst.claude_remind_hook(text, "/x/bin/mnemo remind --hook --db /s"))
    assert added["hooks"]["Stop"][0] == theirs
    assert added["hooks"]["Stop"][1]["hooks"][0]["command"].endswith("remind --hook --db /s")
    assert "matcher" not in added["hooks"]["Stop"][1]
    again = inst.claude_remind_hook(json.dumps(added), "/x/bin/mnemo remind --hook --db /s")
    assert json.loads(again) == added  # idempotent
    assert json.loads(inst.claude_remind_hook(json.dumps(added), None)) == {
        "hooks": {"Stop": [theirs]}
    }
