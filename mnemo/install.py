"""Register Mnemo with Claude Code and Codex, and add the shared-memory policy.

    mnemo-install                 # show every change, then ask before applying
    mnemo-install --yes           # apply without asking
    mnemo-install --uninstall     # remove what it added (memories are kept)

Claude Code gets a user-scope MCP server (``claude mcp add-json --scope user``) and the
policy in ``~/.claude/CLAUDE.md``. Codex gets an ``[mcp_servers.mnemo]`` table with its
tools pre-approved in ``$CODEX_HOME/config.toml`` and the policy in
``$CODEX_HOME/AGENTS.md``. Text written into a file sits between mnemo markers, so it
can be updated or removed without touching the rest of the file. Each agent gets its
own MNEMO_ACTOR; all of them share one database, namespace and user.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from mnemo.config import Settings, get_settings

MD_BEGIN = "<!-- mnemo:begin (managed by mnemo-install; edits inside are replaced) -->"
MD_END = "<!-- mnemo:end -->"
TOML_BEGIN = "# mnemo:begin (managed by mnemo-install; edits inside are replaced)"
TOML_END = "# mnemo:end"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class InstallError(RuntimeError):
    """A change that would overwrite something mnemo-install does not manage."""


def policy_text() -> str:
    return files("mnemo").joinpath("memory_policy.md").read_text()


def _block(begin: str, end: str) -> re.Pattern[str]:
    return re.compile(r"\n*" + re.escape(begin) + r".*?" + re.escape(end) + r"\n*", re.S)


def upsert_block(text: str, body: str, begin: str, end: str) -> str:
    """Replace the managed block where it is, or append it after the existing content."""
    block = f"{begin}\n{body.strip()}\n{end}\n"
    match = _block(begin, end).search(text)
    if match is None:
        return (text.rstrip("\n") + "\n\n" if text.strip() else "") + block
    before, after = text[: match.start()].rstrip("\n"), text[match.end() :].lstrip("\n")
    return (before + "\n\n" if before else "") + block + ("\n" + after if after else "")


def remove_block(text: str, begin: str, end: str) -> str:
    kept = _block(begin, end).sub("\n\n", text).strip("\n")
    return kept + "\n" if kept else ""


def codex_table(command: str, env: dict[str, str]) -> str:
    lines = [
        "[mcp_servers.mnemo]",
        f"command = {json.dumps(command)}",
        'default_tools_approval_mode = "approve"',
        "",
        "[mcp_servers.mnemo.env]",
        *(f"{key} = {json.dumps(value)}" for key, value in env.items()),
    ]
    return "\n".join(lines)


def codex_config(text: str, command: str, env: dict[str, str]) -> str:
    """config.toml with the managed mnemo server; refuses an unmanaged one."""
    outside = tomllib.loads(remove_block(text, TOML_BEGIN, TOML_END))
    if "mnemo" in outside.get("mcp_servers", {}):
        raise InstallError(
            "config.toml already defines [mcp_servers.mnemo] outside mnemo-install's "
            "markers; remove it or rename it first"
        )
    updated = upsert_block(text, codex_table(command, env), TOML_BEGIN, TOML_END)
    parsed = tomllib.loads(updated)["mcp_servers"]["mnemo"]
    if parsed["command"] != command or parsed["env"] != env:
        raise InstallError("the generated Codex configuration did not round-trip")
    return updated


def server_env(settings: Settings, actor: str, backend: str) -> dict[str, str]:
    return {
        "MNEMO_DSN": settings.dsn,
        "MNEMO_BACKEND": backend,
        "MNEMO_EMBED_DIM": str(settings.embed_dim),
        "MNEMO_WORKER_ENABLED": "false",
        "MNEMO_NAMESPACE": settings.namespace,
        "MNEMO_USER_ID": settings.user_id,
        "MNEMO_ACTOR": actor,
    }


def server_command() -> str:
    """The mnemo-mcp-direct next to the running interpreter (this installation)."""
    return str(Path(sys.executable).parent / "mnemo-mcp-direct")


def default_codex_home() -> Path:
    """CODEX_HOME, else the home a `codex` wrapper script pins, else ~/.codex."""
    if os.environ.get("CODEX_HOME"):
        return Path(os.environ["CODEX_HOME"]).expanduser()
    executable = shutil.which("codex")
    if executable:
        try:
            with open(executable, "rb") as handle:
                head = handle.read(4096).decode(errors="ignore")
        except OSError:
            head = ""
        match = re.search(r'CODEX_HOME="\$\{CODEX_HOME:-\$HOME/([^}"]+)\}"', head)
        if match:
            return Path.home() / match.group(1)
    return Path.home() / ".codex"


@dataclass
class FileChange:
    path: Path
    before: str
    after: str

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=str(self.path),
                tofile=str(self.path),
            )
        )


@dataclass
class Command:
    description: str
    argv: list[str]


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""


def plan(
    *,
    agents: Sequence[str],
    uninstall: bool,
    claude_dir: Path,
    codex_home: Path,
    settings: Settings,
    backend: str,
    command: str,
    runner: Runner,
) -> tuple[list[Command], list[FileChange], list[str]]:
    commands: list[Command] = []
    changes: list[FileChange] = []
    notes: list[str] = []
    policy = policy_text()
    if "claude" in agents:
        if shutil.which("claude") is None:
            notes.append("Claude Code CLI not found; skipped its MCP registration.")
        else:
            registered = runner(["claude", "mcp", "get", "mnemo"]).returncode == 0
            if registered:
                commands.append(
                    Command(
                        "Remove Claude Code's existing 'mnemo' MCP server",
                        ["claude", "mcp", "remove", "mnemo", "--scope", "user"],
                    )
                )
            if not uninstall:
                config = {
                    "type": "stdio",
                    "command": command,
                    "args": [],
                    "env": server_env(settings, "claude-code", backend),
                }
                commands.append(
                    Command(
                        "Register the mnemo MCP server for Claude Code (user scope)",
                        ["claude", "mcp", "add-json", "mnemo", json.dumps(config)]
                        + ["--scope", "user"],
                    )
                )
        path = claude_dir / "CLAUDE.md"
        before = _read(path)
        after = (
            remove_block(before, MD_BEGIN, MD_END)
            if uninstall
            else upsert_block(before, policy, MD_BEGIN, MD_END)
        )
        changes.append(FileChange(path, before, after))
    if "codex" in agents:
        config_path = codex_home / "config.toml"
        before = _read(config_path)
        if uninstall:
            after = remove_block(before, TOML_BEGIN, TOML_END)
        else:
            after = codex_config(before, command, server_env(settings, "codex", backend))
        changes.append(FileChange(config_path, before, after))
        path = codex_home / "AGENTS.md"
        before = _read(path)
        after = (
            remove_block(before, MD_BEGIN, MD_END)
            if uninstall
            else upsert_block(before, policy, MD_BEGIN, MD_END)
        )
        changes.append(FileChange(path, before, after))
    return commands, [c for c in changes if c.before != c.after], notes


async def migrate(dsn: str, embed_dim: int) -> list[str]:
    from mnemo.db import apply_migrations, connect

    conn = await connect(dsn)
    try:
        return await apply_migrations(conn, embed_dim=embed_dim)
    finally:
        await conn.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    ask: Callable[[str], str] = input,
) -> int:
    parser = argparse.ArgumentParser(
        description="Register Mnemo with Claude Code and Codex and add the memory policy."
    )
    parser.add_argument("--agent", choices=["claude", "codex"], action="append")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--yes", action="store_true", help="apply without asking")
    parser.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    parser.add_argument("--backend", choices=["ollama", "hash", "openai"])
    parser.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--skip-migrate", action="store_true")
    args = parser.parse_args(argv)
    run = runner or (lambda cmd: subprocess.run(list(cmd), capture_output=True, text=True))
    settings = get_settings()
    backend = args.backend or settings.backend
    codex_home = args.codex_home or default_codex_home()
    try:
        commands, changes, notes = plan(
            agents=args.agent or ["claude", "codex"],
            uninstall=args.uninstall,
            claude_dir=args.claude_dir,
            codex_home=codex_home,
            settings=settings,
            backend=backend,
            command=server_command(),
            runner=run,
        )
    except InstallError as exc:
        print(f"mnemo-install: {exc}", file=sys.stderr)
        return 2
    migrating = not (args.uninstall or args.skip_migrate)
    print(f"Database: {settings.dsn.rsplit('@', 1)[-1]} (backend {backend})")
    print(f"Codex home: {codex_home}  (pass --codex-home if your codex uses another)")
    for note in notes:
        print(f"Note: {note}")
    if migrating:
        print("- Apply any pending database migrations")
    for step in commands:
        print(f"- {step.description}")
    for change in changes:
        print(change.diff(), end="")
    if not (commands or changes or migrating):
        print("Nothing to change.")
        return 0
    if args.dry_run:
        return 0
    if not args.yes and ask("Apply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return 1
    if migrating:
        applied = asyncio.run(migrate(settings.dsn, settings.embed_dim))
        print(f"Migrations applied: {', '.join(applied) or 'none pending'}")
    for step in commands:
        result = run(step.argv)
        if result.returncode != 0:
            print(f"mnemo-install: failed: {step.description}\n{result.stderr}", file=sys.stderr)
            return 1
    for change in changes:
        change.path.parent.mkdir(parents=True, exist_ok=True)
        change.path.write_text(change.after)
    print("Done. Start new Claude Code and Codex sessions to pick up the changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
