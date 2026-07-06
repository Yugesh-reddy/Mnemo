"""Coding-agent memory eval: real Claude Code and Codex sessions sharing one Mnemo.

Each scenario in docs/agent-eval/scenarios.json runs a few sessions, one CLI
invocation each, in fresh throwaway git repositories against one scratch database.
Only Mnemo can carry information between sessions. Afterwards the final replies and
the stored memories are checked. Prompts, replies, tool calls, memory events, usage
and cost are recorded; existing evidence is never overwritten.

Isolation: Claude runs with auto-memory off (unless --claude-auto-memory on), no session
persistence, only project settings (the fixture's own, if an arm adds them) and only the
Mnemo MCP server. With auto-memory on, whatever Claude saves to its own per-project memory
folder is copied into the evidence and the fixture's folders are then removed.
Codex runs ephemeral and read-only, with hooks, plugins and every other configured
MCP server turned off. Neither changes the user's configuration.

    uv run python scripts/agent_eval.py --arm baseline --out docs/agent-eval/runs/NAME
    uv run python scripts/agent_eval.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
from pydantic import BaseModel, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mnemo.config import get_settings  # noqa: E402
from mnemo.db import apply_migrations  # noqa: E402

SCENARIOS = ROOT / "docs" / "agent-eval" / "scenarios.json"
SERVER = ROOT / ".venv" / "bin" / "mnemo-mcp-direct"
REMOTE = "https://github.com/mnemo-eval/{}.git"
SESSION_TIMEOUT = 300
PREVIEW = 600
CLAUDE_TOOL_PREFIX = "mcp__mnemo__"

Agent = Literal["claude", "codex"]


class Session(BaseModel):
    agent: Agent
    repo: Literal["alpha", "beta"]
    prompt: str


class Check(BaseModel):
    kind: Literal["answer", "store", "writes"]
    session: int | None = None
    match: str | None = None
    exclude: str | None = None
    absent: bool = False
    scope: Literal["project", "global", "any"] = "any"
    min: int | None = None
    max: int | None = None
    note: str = ""


class Scenario(BaseModel):
    id: str
    tests: str
    sessions: list[Session]
    checks: list[Check]

    @model_validator(mode="after")
    def _consistent(self) -> Scenario:
        if not self.sessions or not self.checks:
            raise ValueError(f"{self.id}: needs sessions and checks")
        for check in self.checks:
            if check.kind in ("answer", "writes") and (
                check.session is None or not 0 <= check.session < len(self.sessions)
            ):
                raise ValueError(f"{self.id}: check session out of range")
            if check.kind in ("answer", "store") and not check.match:
                raise ValueError(f"{self.id}: {check.kind} check needs a pattern")
            if check.kind != "answer" and check.min is None and check.max is None:
                raise ValueError(f"{self.id}: {check.kind} check needs min or max")
            for pattern in (check.match, check.exclude):
                try:
                    re.compile(pattern or "")
                except re.error as exc:
                    raise ValueError(f"{self.id}: bad pattern {pattern!r}: {exc}") from exc
        return self


class SessionRecord(BaseModel):
    agent: Agent
    repo: str
    prompt: str
    status: Literal["ok", "error", "timeout", "skipped"] = "ok"
    answer: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = None
    model: str | None = None
    seconds: float = 0.0
    writes: int = 0
    error: str | None = None


def load_scenarios(path: Path = SCENARIOS) -> list[Scenario]:
    scenarios = [Scenario.model_validate(s) for s in json.loads(path.read_text())["scenarios"]]
    ids = [scenario.id for scenario in scenarios]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario ids")
    return scenarios


# ---- CLI invocations ---------------------------------------------------------------


def mnemo_env(dsn: str, namespace: str, actor: str, backend: str, embed_dim: int) -> dict:
    return {
        "MNEMO_DSN": dsn,
        "MNEMO_BACKEND": backend,
        "MNEMO_EMBED_DIM": str(embed_dim),
        "MNEMO_WORKER_ENABLED": "false",
        "MNEMO_NAMESPACE": namespace,
        "MNEMO_USER_ID": "owner",
        "MNEMO_ACTOR": actor,
    }


def claude_env(auto_memory: bool) -> dict[str, str]:
    env = dict(os.environ)
    if not auto_memory:
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def claude_command(
    mcp_config: Path, max_usd: float, model: str | None = None, *, auto_memory: bool = False
) -> list[str]:
    """The prompt goes on stdin, so variadic flags cannot swallow it."""
    command = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--setting-sources",
        "project",
        "--no-session-persistence",
        "--max-budget-usd",
        f"{max_usd:.2f}",
    ]
    if model:
        command += ["--model", model]
    if auto_memory:
        command += ["--settings", json.dumps({"autoMemoryEnabled": True})]
    return command + ["--allowedTools", "mcp__mnemo"]


def claude_mcp_config(env: dict[str, str]) -> dict:
    return {"mcpServers": {"mnemo": {"type": "stdio", "command": str(SERVER), "env": env}}}


def codex_command(env: dict[str, str], disable: list[str], model: str | None = None) -> list[str]:
    inline = "{" + ", ".join(f"{key} = {json.dumps(value)}" for key, value in env.items()) + "}"
    command = [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "-s",
        "read-only",
        "--disable",
        "hooks",
        "--disable",
        "plugins",
        "-c",
        f"mcp_servers.mnemo.command={json.dumps(str(SERVER))}",
        "-c",
        f"mcp_servers.mnemo.env={inline}",
        "-c",
        'mcp_servers.mnemo.default_tools_approval_mode="approve"',
    ]
    for name in disable:
        command += ["-c", f"mcp_servers.{name}.enabled=false"]
    if model:
        command += ["-m", model]
    return command + ["-"]


def other_codex_servers() -> list[str]:
    """Configured Codex MCP servers other than mnemo, to turn off for each run."""
    result = subprocess.run(
        ["codex", "mcp", "list", "--json"], capture_output=True, text=True, check=True
    )
    return [server["name"] for server in json.loads(result.stdout) if server["name"] != "mnemo"]


def _preview(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:PREVIEW]


def parse_claude(stdout: str) -> dict[str, Any]:
    calls: dict[str, dict[str, Any]] = {}
    parsed: dict[str, Any] = {"answer": "", "usage": {}, "cost_usd": None, "model": None}
    parsed["error"] = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            parsed["model"] = event.get("model")
        elif kind in ("assistant", "user"):
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    if name.startswith(CLAUDE_TOOL_PREFIX):
                        name = "mnemo:" + name.removeprefix(CLAUDE_TOOL_PREFIX)
                    calls[block.get("id")] = {"tool": name, "arguments": block.get("input")}
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                    call = calls[block["tool_use_id"]]
                    call["result"] = _preview(block.get("content"))
                    call["is_error"] = bool(block.get("is_error"))
        elif kind == "result":
            parsed["answer"] = event.get("result") or ""
            parsed["usage"] = event.get("usage") or {}
            parsed["cost_usd"] = event.get("total_cost_usd")
            if event.get("is_error"):
                parsed["error"] = event.get("subtype") or "error"
    parsed["tool_calls"] = list(calls.values())
    return parsed


def parse_codex(stdout: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {"answer": "", "usage": {}, "cost_usd": None, "model": None}
    parsed["error"], parsed["tool_calls"] = None, []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "item.completed" and item.get("type") == "mcp_tool_call":
            parsed["tool_calls"].append(
                {
                    "tool": f"{item.get('server')}:{item.get('tool')}",
                    "arguments": item.get("arguments"),
                    "result": _preview(item.get("result") or item.get("error")),
                    "is_error": item.get("status") != "completed",
                }
            )
        elif kind == "item.completed" and item.get("type") == "command_execution":
            parsed["tool_calls"].append(
                {
                    "tool": "shell",
                    "arguments": item.get("command"),
                    "result": _preview(item.get("aggregated_output", "")),
                    "is_error": item.get("exit_code") not in (0, None),
                }
            )
        elif kind == "item.completed" and item.get("type") == "agent_message":
            parsed["answer"] = item.get("text", "")
        elif kind == "turn.completed":
            parsed["usage"] = event.get("usage") or {}
        elif kind in ("turn.failed", "error"):
            parsed["error"] = _preview(event.get("error") or event.get("message") or event)
    return parsed


def _text(output: str | bytes | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else output or ""


def run_cli(
    command: list[str], prompt: str, cwd: Path, env: dict[str, str]
) -> tuple[str, str, str]:
    """(status, stdout, stderr); never raises for the child's own failure."""
    try:
        done = subprocess.run(
            command,
            input=prompt,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=SESSION_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return "timeout", _text(exc.stdout), _text(exc.stderr)
    return ("ok" if done.returncode == 0 else "error"), done.stdout, done.stderr


# ---- store and scoring -------------------------------------------------------------

LANES = "(f.namespace = $1 OR starts_with(f.namespace, $1 || '@'))"


async def event_count(conn: asyncpg.Connection, namespace: str) -> int:
    return await conn.fetchval(
        f"SELECT count(*) FROM memory_event e JOIN memory_fact f USING (fact_id) WHERE {LANES}",
        namespace,
    )


async def store_state(conn: asyncpg.Connection, namespace: str) -> dict[str, list[dict]]:
    events = await conn.fetch(
        "SELECT e.seq, e.event_id::text, e.fact_id::text, f.namespace, f.subject, f.predicate, "
        "e.op::text, coalesce(e.object_text, e.object_json::text, e.object_number::text) "
        "AS value, e.actor, e.recorded_at::text FROM memory_event e "
        f"JOIN memory_fact f USING (fact_id) WHERE {LANES} ORDER BY e.seq",
        namespace,
    )
    current = await conn.fetch(
        "SELECT f.fact_id::text, f.namespace, f.subject, f.predicate, "
        "coalesce(f.object_text, f.object_json::text, f.object_number::text) AS value, "
        f"f.actor FROM memory_current f WHERE {LANES} ORDER BY f.recorded_at",
        namespace,
    )
    return {"events": [dict(row) for row in events], "current": [dict(row) for row in current]}


def scope_of(namespace: str) -> str:
    return "project" if "@" in namespace else "global"


def memory_text(row: dict) -> str:
    return f"{row['subject']} {row['predicate']} {row['value']}"


def _in_range(count: int, check: Check) -> bool:
    return (check.min is None or count >= check.min) and (check.max is None or count <= check.max)


def evaluate(
    scenario: Scenario, sessions: list[SessionRecord], current: list[dict]
) -> list[dict[str, Any]]:
    results = []
    for check in scenario.checks:
        if check.kind == "answer":
            record = sessions[check.session] if check.session < len(sessions) else None
            answer = record.answer if record else ""
            found = re.search(check.match, answer, re.I | re.M) is not None
            passed = record is not None and record.status == "ok" and found != check.absent
            observed: Any = answer[:200]
        elif check.kind == "store":
            rows = [
                row
                for row in current
                if check.scope in ("any", scope_of(row["namespace"]))
                and re.search(check.match, memory_text(row), re.I)
                and not (check.exclude and re.search(check.exclude, memory_text(row), re.I))
            ]
            passed = _in_range(len(rows), check)
            observed = [f"[{scope_of(row['namespace'])}] {memory_text(row)}" for row in rows]
        else:
            record = sessions[check.session] if check.session < len(sessions) else None
            passed = record is not None and record.status == "ok"
            passed = passed and _in_range(record.writes, check)
            observed = record.writes if record else None
        results.append(
            {**check.model_dump(exclude_defaults=True), "passed": passed, "observed": observed}
        )
    return results


def summarize(report: dict[str, Any]) -> dict[str, Any]:
    scenarios = report["scenarios"]
    sessions = [s for scenario in scenarios for s in scenario["sessions"]]
    mnemo_calls = [c for s in sessions for c in s["tool_calls"] if c["tool"].startswith("mnemo:")]
    return {
        "scenarios_passed": sum(all(c["passed"] for c in s["checks"]) for s in scenarios),
        "scenarios_run": len(scenarios),
        "checks_passed": sum(c["passed"] for s in scenarios for c in s["checks"]),
        "checks": sum(len(s["checks"]) for s in scenarios),
        "sessions_by_status": {
            status: sum(s["status"] == status for s in sessions)
            for status in ("ok", "error", "timeout", "skipped")
        },
        "sessions_with_mnemo_calls": sum(
            any(c["tool"].startswith("mnemo:") for c in s["tool_calls"]) for s in sessions
        ),
        "mnemo_calls_by_tool": {
            tool: sum(c["tool"] == tool for c in mnemo_calls)
            for tool in sorted({c["tool"] for c in mnemo_calls})
        },
        "memory_writes": sum(s["writes"] for s in sessions),
        "claude_auto_memory_files": sum(
            len(s.get("claude_auto_memory_files", [])) for s in scenarios
        ),
        "claude_cost_usd": round(sum(s["cost_usd"] or 0 for s in sessions), 4),
        "codex_tokens": {
            key: sum(s["usage"].get(key, 0) for s in sessions if s["agent"] == "codex")
            for key in ("input_tokens", "cached_input_tokens", "output_tokens")
        },
    }


# ---- orchestration -----------------------------------------------------------------


CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def auto_memory_dirs(work: Path, projects: Path = CLAUDE_PROJECTS) -> list[Path]:
    """Claude's per-project folders for the fixture repositories under ``work``.

    Claude names them after the project path with every other character turned into
    "-"; ``work``'s own name is unique (mkdtemp), so containment finds them whether
    the path was recorded through /var or /private/var.
    """
    marker = re.sub(r"[^A-Za-z0-9]", "-", work.name)
    if not projects.is_dir():
        return []
    return sorted(path for path in projects.iterdir() if path.is_dir() and marker in path.name)


def collect_auto_memory(work: Path, out: Path, projects: Path = CLAUDE_PROJECTS) -> list[str]:
    """Copy Claude's auto-memory for the fixtures into ``out``, then remove the folders."""
    saved: list[str] = []
    for folder in auto_memory_dirs(work, projects):
        memory = folder / "memory"
        if memory.is_dir():
            for file in sorted(memory.rglob("*")):
                if file.is_file():
                    target = out / folder.name / file.relative_to(memory)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(file, target)
                    saved.append(str(target.relative_to(out)))
        shutil.rmtree(folder)
    return saved


def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    (repo / "src").mkdir(parents=True)
    (repo / "README.md").write_text(f"# {name}\n\nA small internal service.\n")
    (repo / "src" / "app.py").write_text('def main() -> None:\n    print("hello")\n')
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    for command in (["git", "init", "-q"], ["git", "remote", "add", "origin", REMOTE.format(name)]):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    return repo


def _version(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True).stdout.strip()
    except OSError as exc:
        return f"unavailable: {exc}"


def _redact(command: list[str], dsn: str) -> list[str]:
    return [part.replace(dsn, "<dsn>") for part in command]


def run_session(
    session: Session,
    repo: Path,
    work: Path,
    env: dict[str, str],
    args: argparse.Namespace,
    codex_disable: list[str],
) -> tuple[SessionRecord, list[str], str, str]:
    if session.agent == "claude":
        config = work / f"claude-mcp-{uuid4().hex[:8]}.json"
        config.write_text(json.dumps(claude_mcp_config(env)))
        auto_memory = args.claude_auto_memory == "on"
        command = claude_command(
            config, args.claude_session_usd, args.claude_model, auto_memory=auto_memory
        )
        child_env = claude_env(auto_memory)
        parse: Callable[[str], dict[str, Any]] = parse_claude
    else:
        command = codex_command(env, codex_disable, args.codex_model)
        child_env = dict(os.environ)
        parse = parse_codex
    started = time.monotonic()
    status, stdout, stderr = run_cli(command, session.prompt, repo, child_env)
    parsed = parse(stdout)
    record = SessionRecord(
        agent=session.agent,
        repo=session.repo,
        prompt=session.prompt,
        status=status if not parsed["error"] or status != "ok" else "error",
        answer=parsed["answer"],
        tool_calls=parsed["tool_calls"],
        usage=parsed["usage"],
        cost_usd=parsed["cost_usd"],
        model=parsed["model"],
        seconds=round(time.monotonic() - started, 2),
        error=parsed["error"] or (stderr[-PREVIEW:] if status != "ok" else None),
    )
    return record, command, stdout, stderr


async def run(args: argparse.Namespace) -> int:
    scenarios = load_scenarios()
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s.id in wanted]
    files = {}
    for spec in args.repo_file:
        source, _, dest = spec.partition(":")
        files[dest or Path(source).name] = Path(source).read_text()
    settings = get_settings()
    codex_disable = other_codex_servers()
    if args.dry_run:
        env = mnemo_env("<dsn>", "eval-<scenario>", "<agent>", args.backend, settings.embed_dim)
        claude = claude_command(
            Path("<mcp.json>"),
            args.claude_session_usd,
            args.claude_model,
            auto_memory=args.claude_auto_memory == "on",
        )
        print("claude:", " ".join(claude))
        print("codex: ", " ".join(codex_command(env, codex_disable, args.codex_model)))
        print(f"{len(scenarios)} scenarios, {sum(len(s.sessions) for s in scenarios)} sessions")
        return 0
    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing evidence: {out}")
    (out / "raw").mkdir(parents=True)
    token = uuid4().hex[:8]
    db_name = f"mnemo_agent_eval_{token}"
    parts = urlsplit(settings.test_dsn)
    admin_dsn = urlunsplit(parts._replace(path="/postgres"))
    dsn = urlunsplit(parts._replace(path="/" + db_name))
    report: dict[str, Any] = {
        "arm": args.arm,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "environment": {
            "mnemo_commit": _version(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
            "mnemo_dirty": bool(_version(["git", "-C", str(ROOT), "status", "--porcelain"])),
            "claude": _version(["claude", "--version"]),
            "codex": _version(["codex", "--version"]),
            "backend": args.backend,
            "embed_model": settings.embed_model,
            "embed_dim": settings.embed_dim,
            "codex_servers_disabled": codex_disable,
            "repo_files": sorted(files),
            "claude_session_usd_cap": args.claude_session_usd,
            "claude_total_usd_cap": args.max_claude_usd,
            "claude_auto_memory": args.claude_auto_memory,
        },
        "database": db_name,
        "scenarios": [],
    }
    admin = await asyncpg.connect(admin_dsn, timeout=5)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(dsn)
    claude_spent = 0.0
    try:
        await apply_migrations(conn, embed_dim=settings.embed_dim)
        for number, scenario in enumerate(scenarios, 1):
            namespace = f"eval-{scenario.id}-{token}"
            work = Path(tempfile.mkdtemp(prefix=f"mnemo-eval-{scenario.id}-"))
            repos = {name: make_repo(work, name, files) for name in ("alpha", "beta")}
            records: list[SessionRecord] = []
            for index, session in enumerate(scenario.sessions):
                if session.agent == "claude" and claude_spent >= args.max_claude_usd:
                    records.append(
                        SessionRecord(
                            **session.model_dump(), status="skipped", error="Claude budget spent"
                        )
                    )
                    continue
                env = mnemo_env(
                    dsn,
                    namespace,
                    "claude-code" if session.agent == "claude" else "codex",
                    args.backend,
                    settings.embed_dim,
                )
                before = await event_count(conn, namespace)
                record, command, stdout, stderr = await asyncio.to_thread(
                    run_session, session, repos[session.repo], work, env, args, codex_disable
                )
                record.writes = await event_count(conn, namespace) - before
                claude_spent += record.cost_usd or 0.0
                raw = out / "raw" / f"{scenario.id}-{index}-{session.agent}"
                raw.with_suffix(".jsonl").write_text(stdout)
                if stderr.strip():
                    raw.with_suffix(".stderr.txt").write_text(stderr[-20000:])
                records.append(record)
                print(
                    f"[{number}/{len(scenarios)}] {scenario.id} #{index} {session.agent}: "
                    f"{record.status}, {record.seconds}s, writes={record.writes}, "
                    f"mnemo calls={sum(c['tool'].startswith('mnemo:') for c in record.tool_calls)}",
                    flush=True,
                )
                report.setdefault("commands", {}).setdefault(session.agent, _redact(command, dsn))
            state = await store_state(conn, namespace)
            checks = evaluate(scenario, records, state["current"])
            auto_memory = collect_auto_memory(work, out / "claude-auto-memory" / scenario.id)
            report["scenarios"].append(
                {
                    "id": scenario.id,
                    "tests": scenario.tests,
                    "passed": all(check["passed"] for check in checks),
                    "checks": checks,
                    "sessions": [record.model_dump(mode="json") for record in records],
                    "store": state,
                    "claude_auto_memory_files": auto_memory,
                }
            )
            shutil.rmtree(work, ignore_errors=True)
            report["summary"] = summarize(report)
            (out / "results.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    finally:
        await conn.close()
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        if args.keep_db:
            report["database_kept"] = True
        else:
            admin = await asyncpg.connect(admin_dsn, timeout=5)
            try:
                await admin.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
                report["database_removed"] = True
            finally:
                await admin.close()
        report["summary"] = summarize(report)
        (out / "results.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report["summary"], indent=2))
    for scenario in report["scenarios"]:
        failed = [c for c in scenario["checks"] if not c["passed"]]
        print(f"{'PASS' if not failed else 'FAIL'} {scenario['id']}")
        for check in failed:
            print(f"     {check['kind']}: {check.get('match')!r} observed {check['observed']!r}")
    return 0 if report["summary"]["scenarios_passed"] == len(scenarios) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", default="baseline", help="label recorded with the results")
    parser.add_argument("--out", help="new evidence directory (required unless --dry-run)")
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument(
        "--repo-file",
        action="append",
        default=[],
        metavar="SRC[:DEST]",
        help="copy a file into every fixture repository (e.g. AGENTS.md, .claude/settings.json)",
    )
    parser.add_argument("--backend", default="ollama", choices=["ollama", "hash"])
    parser.add_argument("--claude-model")
    parser.add_argument("--codex-model")
    parser.add_argument("--claude-session-usd", type=float, default=0.60)
    parser.add_argument("--max-claude-usd", type=float, default=6.00)
    parser.add_argument(
        "--claude-auto-memory",
        choices=["off", "on"],
        default="off",
        help="leave Claude Code's own auto-memory on (its saves are copied into the evidence)",
    )
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.out:
        parser.error("--out is required")
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
