"""Coding-agent eval harness: scenarios, CLI isolation, parsing and scoring. No CLI calls."""

from __future__ import annotations

import json
import tomllib

import pytest

from mnemo.project import detect_project
from scripts import agent_eval as ae


def test_the_committed_scenarios_are_valid_and_never_name_the_tools():
    scenarios = ae.load_scenarios()
    assert len(scenarios) == 10
    agents = [session.agent for s in scenarios for session in s.sessions]
    assert (agents.count("claude"), agents.count("codex")) == (11, 12)
    for scenario in scenarios:
        for session in scenario.sessions:
            assert "mnemo" not in session.prompt.lower()
            assert "tool" not in session.prompt.lower()


def one(*checks: ae.Check, sessions: int = 1) -> ae.Scenario:
    session = ae.Session(agent="claude", repo="alpha", prompt="p")
    return ae.Scenario(id="s", tests="t", sessions=[session] * sessions, checks=list(checks))


def record(answer: str = "", status: str = "ok", writes: int = 0) -> ae.SessionRecord:
    return ae.SessionRecord(
        agent="claude", repo="alpha", prompt="p", answer=answer, status=status, writes=writes
    )


@pytest.mark.parametrize(
    "check",
    [
        ae.Check(kind="answer", session=1, match="x"),
        ae.Check(kind="answer", session=0),
        ae.Check(kind="store", match="x"),
        ae.Check(kind="writes", session=0),
        ae.Check(kind="answer", session=0, match="("),
    ],
)
def test_inconsistent_checks_are_rejected(check):
    with pytest.raises(ValueError):
        one(check)


def test_answer_checks_are_case_insensitive_per_line_and_need_a_completed_session():
    thursday = ae.Check(kind="answer", session=0, match="^\\W*thursday\\W*$")
    leak = ae.Check(kind="answer", session=0, match="widget_stage_07", absent=True)
    scenario = one(thursday, leak)
    assert [c["passed"] for c in ae.evaluate(scenario, [record("Thursday.\n")], [])] == [
        True,
        True,
    ]
    assert [c["passed"] for c in ae.evaluate(scenario, [record("widget_stage_07")], [])] == [
        False,
        False,
    ]
    failed = ae.evaluate(scenario, [record("", status="error")], [])
    assert [c["passed"] for c in failed] == [False, False]


ROWS = [
    {"namespace": "ns@github.com/mnemo-eval/alpha", "subject": "project", "predicate": "deploy"}
    | {"value": "moved from Tuesday to Thursday"},
    {"namespace": "ns@github.com/mnemo-eval/alpha", "subject": "project", "predicate": "deploy"}
    | {"value": "every Tuesday"},
    {"namespace": "ns", "subject": "user", "predicate": "python packages", "value": "use uv"},
]


def test_store_checks_filter_by_pattern_exclusion_and_scope():
    stale = ae.Check(kind="store", match="tuesday", exclude="thursday", max=0)
    global_uv = ae.Check(kind="store", match="\\buv\\b", scope="global", min=1)
    project_uv = ae.Check(kind="store", match="\\buv\\b", scope="project", min=1)
    results = ae.evaluate(one(stale, global_uv, project_uv), [record()], ROWS)
    assert [c["passed"] for c in results] == [False, True, False]
    assert results[0]["observed"] == ["[project] project deploy every Tuesday"]


def test_write_checks_count_the_session_events():
    check = ae.Check(kind="writes", session=0, max=0)
    assert ae.evaluate(one(check), [record(writes=0)], [])[0]["passed"]
    assert not ae.evaluate(one(check), [record(writes=2)], [])[0]["passed"]


def test_claude_stream_yields_answer_tools_and_cost():
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-test"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "mcp__mnemo__memory_search",
                        "input": {"query": "deploy"},
                    },
                    {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "x"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": '{"hits": []}'}],
                    }
                ]
            },
        },
        {"type": "result", "result": "UNKNOWN", "total_cost_usd": 0.12, "usage": {"x": 1}},
    ]
    stdout = "\n".join([json.dumps(line) for line in lines] + ["not json"])
    parsed = ae.parse_claude(stdout)
    assert parsed["answer"] == "UNKNOWN" and parsed["cost_usd"] == 0.12
    assert parsed["model"] == "claude-test" and parsed["error"] is None
    assert [c["tool"] for c in parsed["tool_calls"]] == ["mnemo:memory_search", "Read"]
    assert "hits" in parsed["tool_calls"][0]["result"]


def test_codex_events_yield_the_last_message_tools_and_usage():
    events = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Looking."}},
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "mnemo",
                "tool": "memory_create",
                "arguments": {"value": "v"},
                "result": {"content": []},
                "status": "completed",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "ls", "exit_code": 0},
        },
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    parsed = ae.parse_codex("\n".join(json.dumps(e) for e in events))
    assert parsed["answer"] == "Done." and parsed["usage"]["input_tokens"] == 10
    assert [(c["tool"], c["is_error"]) for c in parsed["tool_calls"]] == [
        ("mnemo:memory_create", False),
        ("shell", False),
    ]
    failed = ae.parse_codex(json.dumps({"type": "turn.failed", "error": {"message": "401"}}))
    assert "401" in failed["error"]


def test_cli_commands_isolate_the_agents_and_keep_the_prompt_off_argv(tmp_path):
    claude = ae.claude_command(tmp_path / "mcp.json", 0.6)
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--verbose"):
        assert flag in claude
    assert claude[claude.index("--setting-sources") + 1] == "project"
    assert claude[-2:] == ["--allowedTools", "mcp__mnemo"]

    env = ae.mnemo_env("postgresql://u:p@h/db", "eval-x", "codex", "ollama", 768)
    codex = ae.codex_command(env, ["vercel"])
    assert (
        codex[-1] == "-"
        and "--ephemeral" in codex
        and codex[codex.index("-s") + 1] == ("read-only")
    )
    assert codex.count("--disable") == 2 and "mcp_servers.vercel.enabled=false" in codex
    assert 'mcp_servers.mnemo.default_tools_approval_mode="approve"' in codex
    inline = next(c for c in codex if c.startswith("mcp_servers.mnemo.env="))
    assert tomllib.loads("env = " + inline.split("=", 1)[1])["env"] == env
    assert ae.claude_mcp_config(env)["mcpServers"]["mnemo"]["env"]["MNEMO_ACTOR"] == "codex"


def test_fixture_repositories_resolve_to_their_eval_project(tmp_path):
    repo = ae.make_repo(tmp_path, "alpha", {"AGENTS.md": "hello"})
    assert (repo / "AGENTS.md").read_text() == "hello"
    assert detect_project(repo / "src") == "github.com/mnemo-eval/alpha"


def test_summary_counts_passes_calls_and_spend():
    session = record("x").model_dump() | {
        "tool_calls": [{"tool": "mnemo:memory_search"}, {"tool": "Read"}],
        "cost_usd": 0.1,
        "writes": 1,
    }
    codex = record("y").model_dump() | {"agent": "codex", "usage": {"input_tokens": 7}}
    report = {
        "scenarios": [
            {"checks": [{"passed": True}], "sessions": [session]},
            {"checks": [{"passed": True}, {"passed": False}], "sessions": [codex]},
        ]
    }
    summary = ae.summarize(report)
    assert (summary["scenarios_passed"], summary["checks_passed"], summary["checks"]) == (1, 2, 3)
    assert summary["sessions_with_mnemo_calls"] == 1
    assert summary["mnemo_calls_by_tool"] == {"mnemo:memory_search": 1}
    assert summary["claude_cost_usd"] == 0.1 and summary["codex_tokens"]["input_tokens"] == 7


def test_auto_memory_on_keeps_claudes_memory_and_says_so(tmp_path):
    assert ae.claude_env(False)["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert "CLAUDE_CODE_DISABLE_AUTO_MEMORY" not in ae.claude_env(True)
    command = ae.claude_command(tmp_path / "mcp.json", 0.6, auto_memory=True)
    assert json.loads(command[command.index("--settings") + 1]) == {"autoMemoryEnabled": True}
    assert command[-2:] == ["--allowedTools", "mcp__mnemo"]
    assert "--settings" not in ae.claude_command(tmp_path / "mcp.json", 0.6)


def test_fixture_auto_memory_is_copied_then_removed_and_nothing_else_is_touched(tmp_path):
    projects = tmp_path / "projects"
    work = tmp_path / "var" / "mnemo-eval-handoff-ab_c12x"
    ours = projects / "-private-var-folders-T-mnemo-eval-handoff-ab-c12x-alpha"
    (ours / "memory").mkdir(parents=True)
    (ours / "memory" / "MEMORY.md").write_text("- [Tests](tests.md)\n")
    (ours / "memory" / "tests.md").write_text("run pytest -k 'not sync_timeout'\n")
    bare = projects / "-var-folders-T-mnemo-eval-handoff-ab-c12x-beta"
    bare.mkdir(parents=True)
    other = projects / "-Users-me-code-mnemo-eval-handoff-zzzzzzzz"
    (other / "memory").mkdir(parents=True)
    assert ae.auto_memory_dirs(work, projects) == [ours, bare]
    out = tmp_path / "evidence"
    saved = ae.collect_auto_memory(work, out, projects)
    assert saved == [f"{ours.name}/MEMORY.md", f"{ours.name}/tests.md"]
    assert (out / ours.name / "tests.md").read_text().startswith("run pytest")
    assert not ours.exists() and not bare.exists() and other.exists()
    assert ae.auto_memory_dirs(work, tmp_path / "missing") == []
