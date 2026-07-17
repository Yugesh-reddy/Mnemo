"""Register Mnemo with Claude Code, Codex and OpenCode, and add the shared-memory policy.

    mnemo-install                 # show every change, then ask before applying
    mnemo-install --yes           # apply without asking
    mnemo-install --uninstall     # remove what it added (memories are kept)

Claude Code gets a user-scope MCP server (``claude mcp add-json --scope user``) and the
policy in ``~/.claude/CLAUDE.md``. Codex gets an ``[mcp_servers.mnemo]`` table with its
tools pre-approved in ``$CODEX_HOME/config.toml`` and the policy in
``$CODEX_HOME/AGENTS.md``. OpenCode (when installed) gets a ``mnemo`` server under ``mcp``
in its global ``opencode.json`` and the policy in its global ``AGENTS.md``, or in
``~/.claude/CLAUDE.md`` while OpenCode is reading that file instead. Text written into a
file sits between mnemo markers, so it can be updated or removed without touching the
rest of the file. Each agent gets its own MNEMO_ACTOR; all of them share one database,
namespace and user.

Before changing anything it checks that Postgres is reachable and, for the default
Ollama backend, that Ollama is running; a missing embedding model (Ollama's, or the
local backend's, which runs inside Mnemo) is downloaded after you confirm. It refuses
a backend or model whose vectors would not match the memories already stored.
Problems are reported with the command that fixes them.
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

import httpx

from mnemo import services
from mnemo.config import LOCAL_MODELS, Settings, get_settings
from mnemo.embedder import build_embedder, download_local_model, local_model_ready

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


def default_opencode_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode"


def opencode_detected(opencode_dir: Path) -> bool:
    return shutil.which("opencode") is not None or opencode_dir.is_dir()


def strip_jsonc(text: str) -> str:
    """JSON from JSONC: drop // and /* */ comments and trailing commas outside strings."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(char)
            i += 1
    # Trailing commas, now that strings are the only place a comma can hide.
    result: list[str] = []
    for piece in out:
        if piece[0] in "}]" and result:
            k = len(result) - 1
            while k >= 0 and result[k].isspace():
                k -= 1
            if k >= 0 and result[k] == ",":
                del result[k]
        result.append(piece)
    return "".join(result)


def opencode_entry(command: str, env: dict[str, str]) -> dict:
    return {"type": "local", "command": [command], "environment": env}


def _ours(entry: object) -> bool:
    """A server entry mnemo-install wrote: it runs a mnemo-mcp-direct."""
    command = entry.get("command") if isinstance(entry, dict) else None
    return (
        isinstance(command, list)
        and bool(command)
        and Path(str(command[0])).name == "mnemo-mcp-direct"
    )


def opencode_config(text: str, path: Path, entry: dict | None) -> str:
    """The config with the mnemo MCP server set to ``entry`` (None removes it).

    The entry goes directly under "mcp", the layout every OpenCode version reads,
    unless the file already groups its servers under "mcp.servers" (the newer layout),
    in which case it goes there. Plain JSON is rewritten with its other settings kept.
    A JSONC file with comments or trailing commas would lose them, so it is only left
    alone when it already says the same; otherwise the error shows the entry to paste.
    """
    plain = True
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        plain = False
        try:
            data = json.loads(strip_jsonc(text))
        except json.JSONDecodeError as exc:
            raise InstallError(f"{path} is not valid JSON or JSONC ({exc})") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object")
    mcp = data.get("mcp", {})
    if not isinstance(mcp, dict):
        raise InstallError(f'"mcp" in {path} is not an object; fix it first')
    grouped = mcp.get("servers")
    # An object without "type" is the newer grouping, not a server named "servers".
    nested = isinstance(grouped, dict) and "type" not in grouped
    places = [mcp, grouped] if nested else [mcp]
    found = [place["mnemo"] for place in places if "mnemo" in place]
    if not all(_ours(existing) for existing in found):
        raise InstallError(
            f"{path} already defines an MCP server named mnemo that mnemo-install did "
            "not write; remove or rename it first"
        )
    home = grouped if nested else mcp
    if found == ([] if entry is None else [entry]) and (entry is None or "mnemo" in home):
        return text
    if not plain:
        where = '"mcp" > "servers"' if nested else '"mcp"'
        snippet = {"mcp": {"servers": {"mnemo": entry}} if nested else {"mnemo": entry}}
        action = (
            f"add this to it:\n{json.dumps(snippet, indent=2)}"
            if entry is not None
            else f'delete "mnemo" under {where}'
        )
        raise InstallError(
            f"{path} has comments or trailing commas, which rewriting would lose; {action}"
        )
    for place in places:
        place.pop("mnemo", None)
    if entry is not None:
        home["mnemo"] = entry
        data["mcp"] = mcp
    else:
        if nested and not grouped:
            mcp.pop("servers")
        if not mcp:
            data.pop("mcp", None)
    return json.dumps(data, indent=2) + "\n"


def opencode_config_path(opencode_dir: Path) -> Path:
    for name in ("opencode.jsonc", "opencode.json"):
        if (opencode_dir / name).exists():
            return opencode_dir / name
    return opencode_dir / "opencode.json"


def opencode_rules_path(opencode_dir: Path, claude_dir: Path) -> Path:
    """The global rules file OpenCode reads: its own AGENTS.md, else ~/.claude/CLAUDE.md
    when that exists and Claude Code support is not disabled."""
    own = opencode_dir / "AGENTS.md"
    disabled = any(
        os.environ.get(name) == "1"
        for name in ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
    )
    if own.exists() or disabled or not (claude_dir / "CLAUDE.md").exists():
        return own
    return claude_dir / "CLAUDE.md"


def server_env(settings: Settings, actor: str, backend: str) -> dict[str, str]:
    env = {
        "MNEMO_DSN": settings.dsn,
        "MNEMO_BACKEND": backend,
        "MNEMO_EMBED_DIM": str(settings.embed_dim),
        "MNEMO_NAMESPACE": settings.namespace,
        "MNEMO_USER_ID": settings.user_id,
        "MNEMO_ACTOR": actor,
    }
    if backend == "local":
        # Pinned so a later default can't silently change the vectors of new writes.
        env["MNEMO_EMBED_MODEL"] = settings.embed_model
        env["MNEMO_MODEL_DIR"] = str(settings.model_dir)
    return env


def server_command() -> str:
    """The mnemo-mcp-direct next to the running interpreter (this installation)."""
    return str(Path(sys.executable).parent / "mnemo-mcp-direct")


def running_from_checkout() -> bool:
    """True when this code runs from a source checkout rather than an installed package."""
    return "site-packages" not in Path(__file__).resolve().parts


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
    remove_if_empty: bool = False
    """Delete the file rather than leave it empty (its mere existence means something)."""

    def apply(self) -> None:
        if self.after == "" and self.remove_if_empty:
            self.path.unlink(missing_ok=True)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(self.after)

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


def claude_settings(text: str, auto_memory: str) -> str:
    """settings.json with autoMemoryEnabled false ("off") or the key removed ("on")."""
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise InstallError(f"~/.claude/settings.json is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise InstallError("~/.claude/settings.json is not a JSON object")
    if auto_memory == "off":
        data["autoMemoryEnabled"] = False
    else:
        data.pop("autoMemoryEnabled", None)
    updated = json.dumps(data, indent=2) + "\n"
    return text if json.loads(text or "{}") == data else updated


def _auto_memory_off(text: str) -> bool:
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and data.get("autoMemoryEnabled") is False


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
    claude_auto_memory: str | None = None,
    opencode_dir: Path | None = None,
    opencode_optional: bool = False,
) -> tuple[list[Command], list[FileChange], list[str]]:
    """Commands, file changes and notes for the agents. ``opencode_optional``: OpenCode
    was detected rather than requested, so a config mnemo-install must not rewrite
    skips OpenCode with a note instead of stopping the other agents."""
    commands: list[Command] = []
    notes: list[str] = []
    policy = policy_text()
    texts: dict[Path, tuple[str, str]] = {}  # path -> (on disk, planned)
    remove_if_empty: set[Path] = set()

    def edit(path: Path, change: Callable[[str], str]) -> None:
        before, current = texts.get(path, (_read(path), _read(path)))
        texts[path] = (before, change(current))

    def policy_block(text: str) -> str:
        if uninstall:
            return remove_block(text, MD_BEGIN, MD_END)
        return upsert_block(text, policy, MD_BEGIN, MD_END)

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
        edit(claude_dir / "CLAUDE.md", policy_block)
        settings_path = claude_dir / "settings.json"
        current = _read(settings_path)
        if claude_auto_memory:
            edit(settings_path, lambda text: claude_settings(text, claude_auto_memory))
        elif uninstall:
            if _auto_memory_off(current):
                notes.append(
                    "Claude Code's auto-memory is still off; add --claude-auto-memory on "
                    "to turn it back on."
                )
        elif not _auto_memory_off(current):
            notes.append(
                "Claude Code's own auto-memory stays on (with the policy, Claude still saved "
                "to Mnemo in the eval). --claude-auto-memory off turns it off."
            )
    if "codex" in agents:
        config_path = codex_home / "config.toml"
        if uninstall:
            edit(config_path, lambda text: remove_block(text, TOML_BEGIN, TOML_END))
        else:
            env = server_env(settings, "codex", backend)
            edit(config_path, lambda text: codex_config(text, command, env))
        edit(codex_home / "AGENTS.md", policy_block)
    if "opencode" in agents:
        opencode_dir = opencode_dir or default_opencode_dir()
        config_path = opencode_config_path(opencode_dir)
        entry = (
            None
            if uninstall
            else opencode_entry(command, server_env(settings, "opencode", backend))
        )
        try:
            edit(config_path, lambda text: opencode_config(text, config_path, entry))
        except InstallError as exc:
            if not opencode_optional:
                raise
            notes.append(f"Skipped OpenCode: {exc}")
        else:
            rules = opencode_rules_path(opencode_dir, claude_dir)
            if rules != claude_dir / "CLAUDE.md":
                edit(rules, policy_block)
                # An empty AGENTS.md would still stop OpenCode reading ~/.claude/CLAUDE.md.
                remove_if_empty.add(rules)
            elif not uninstall:
                notes.append(
                    f"OpenCode reads {rules} (it has no AGENTS.md of its own), so the policy "
                    "for OpenCode goes there."
                )
                edit(rules, policy_block)
            elif "claude" not in agents:
                notes.append(f"The policy stays in {rules}, which Claude Code also reads.")
    changes = [
        FileChange(path, before, after, path in remove_if_empty)
        for path, (before, after) in texts.items()
    ]
    return commands, [c for c in changes if c.before != c.after], notes


class Services:
    """The pre-flight probes; tests substitute fakes."""

    def postgres_problem(self, settings: Settings) -> str | None:
        return asyncio.run(services.postgres_problem(settings))

    def ollama_models(self, host: str) -> list[str]:
        return services.ollama_models(host)

    def pull(self, host: str, model: str) -> None:
        services.pull_model(host, model)

    def local_model_ready(self, settings: Settings) -> bool:
        return local_model_ready(settings.embed_model, settings.model_dir)

    def download_local(self, settings: Settings) -> None:
        download_local_model(settings.embed_model, settings.model_dir)

    def embedding_mismatch(self, settings: Settings, backend: str) -> str | None:
        embedder = build_embedder(settings)
        try:
            return asyncio.run(services.embedding_mismatch(settings, backend, embedder.embed))
        finally:
            close = getattr(embedder, "close", None)
            if close:
                close()


@dataclass
class Preflight:
    ready: list[str]
    problems: list[str]
    pull_model: bool = False


def preflight(settings: Settings, backend: str, probe: Services) -> Preflight:
    result = Preflight(ready=[], problems=[])
    problem = probe.postgres_problem(settings)
    location = services.where(settings.dsn)
    if problem:
        result.problems.append(problem)
    else:
        result.ready.append(f"Postgres at {location}")
    host, model = settings.ollama_host, settings.embed_model
    if backend == "ollama":
        try:
            installed = probe.ollama_models(host)
        except httpx.HTTPError:
            result.problems.append(
                f"Can't reach Ollama at {host}. Start the Ollama app (or run `ollama serve`), "
                "or install with --backend hash (search then matches exact words only)."
            )
        else:
            result.pull_model = not services.has_model(installed, model)
            result.ready.append(f"Ollama at {host}")
    elif backend == "local":
        result.pull_model = not probe.local_model_ready(settings)
        result.ready.append(f"local embeddings ({settings.embed_model}, no model server)")
    elif backend == "openai" and not settings.openai_api_key:
        result.problems.append("The openai backend needs OPENAI_API_KEY in the environment.")
    elif backend == "hash":
        result.ready.append("hash embeddings (no model server; search matches exact words)")
    return result


async def migrate(dsn: str, embed_dim: int) -> list[str]:
    import asyncpg

    from mnemo.db import apply_migrations, register_vector

    # Not mnemo.db.connect: it checks the dimension of the process-wide settings,
    # which differ from embed_dim when --backend selects another model.
    conn = await asyncpg.connect(dsn)
    try:
        await register_vector(conn)
        return await apply_migrations(conn, embed_dim=embed_dim)
    finally:
        await conn.close()


def _mismatch(probe: Services, settings: Settings, backend: str) -> bool:
    """Print and report a stored-vector mismatch; embedding failures are not one."""
    try:
        problem = probe.embedding_mismatch(settings, backend)
    except Exception as exc:  # the server reports embedding failures with their fix
        print(f"Note: could not compare stored memories with {backend} embeddings ({exc}).")
        return False
    if problem:
        print(f"FIX  {problem}")
        print("No agent configuration or database was changed.")
    return bool(problem)


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    ask: Callable[[str], str] = input,
    probe: Services | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Register Mnemo with Claude Code, Codex and OpenCode and add the memory policy."
    )
    parser.add_argument(
        "--agent",
        choices=["claude", "codex", "opencode"],
        action="append",
        help="default: claude, codex, and opencode when it is installed",
    )
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--yes", action="store_true", help="apply without asking")
    parser.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    parser.add_argument("--backend", choices=["ollama", "local", "hash", "openai"])
    parser.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--opencode-dir", type=Path, help="default: ~/.config/opencode")
    parser.add_argument("--skip-migrate", action="store_true")
    parser.add_argument("--skip-checks", action="store_true", help="don't probe the services")
    parser.add_argument(
        "--claude-auto-memory",
        choices=["off", "on"],
        help="turn Claude Code's own auto-memory off, or back on (default: leave it)",
    )
    args = parser.parse_args(argv)
    run = runner or (lambda cmd: subprocess.run(list(cmd), capture_output=True, text=True))
    settings = get_settings()
    backend = args.backend or settings.backend
    if backend != settings.backend:
        # Re-resolve backend-dependent defaults (model, dimension, search floor).
        settings = Settings(backend=backend)
    codex_home = args.codex_home or default_codex_home()
    opencode_dir = args.opencode_dir or default_opencode_dir()
    agents = args.agent or ["claude", "codex"] + (
        ["opencode"] if opencode_detected(opencode_dir) else []
    )
    checks = Preflight(ready=[], problems=[])
    if not (args.uninstall or args.skip_checks):
        checks = preflight(settings, backend, probe or Services())
        for line in checks.ready:
            print(f"ok   {line}")
        for problem in checks.problems:
            print(f"FIX  {problem}")
        if checks.problems:
            print("Fix the problems above, then run mnemo-install again.")
            return 2
    try:
        commands, changes, notes = plan(
            agents=agents,
            uninstall=args.uninstall,
            claude_dir=args.claude_dir,
            codex_home=codex_home,
            settings=settings,
            backend=backend,
            command=server_command(),
            runner=run,
            claude_auto_memory=args.claude_auto_memory,
            opencode_dir=opencode_dir,
            opencode_optional=not (args.agent and "opencode" in args.agent),
        )
    except InstallError as exc:
        print(f"mnemo-install: {exc}", file=sys.stderr)
        return 2
    migrating = not (args.uninstall or args.skip_migrate)
    print(f"Database: {services.where(settings.dsn)} (backend {backend})")
    print(f"Codex home: {codex_home}  (pass --codex-home if your codex uses another)")
    if "opencode" in agents:
        print(f"OpenCode config: {opencode_dir}  (pass --opencode-dir for another)")
    if not args.uninstall and running_from_checkout():
        print(
            f"Note: the agents will run {server_command()} from this checkout; moving or "
            "deleting it breaks them. `make agents` installs the mnemo tool and connects "
            "that instead."
        )
    for note in notes:
        print(f"Note: {note}")
    if checks.pull_model and backend == "local":
        size = LOCAL_MODELS.get(settings.embed_model, (0, 0, 0))[2]
        about = f" (about {size} MB)" if size else ""
        print(
            f"- Download the embedding model {settings.embed_model}{about} from Hugging Face "
            f"into {settings.model_dir}"
        )
    elif checks.pull_model:
        print(f"- Download the embedding model {settings.embed_model} into Ollama (about 270 MB)")
    if migrating:
        print("- Apply any pending database migrations")
    for step in commands:
        print(f"- {step.description}")
    for change in changes:
        print(change.diff(), end="")
    if not (commands or changes or migrating or checks.pull_model):
        print("Nothing to change.")
        return 0
    checking = not (args.uninstall or args.skip_checks)
    if args.dry_run:
        if checking and not checks.pull_model:
            return 2 if _mismatch(probe or Services(), settings, backend) else 0
        return 0
    if not args.yes and ask("Apply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return 1
    if checks.pull_model:
        print(f"Downloading {settings.embed_model}...", flush=True)
        try:
            if backend == "local":
                (probe or Services()).download_local(settings)
            else:
                (probe or Services()).pull(settings.ollama_host, settings.embed_model)
        except (httpx.HTTPError, RuntimeError, ValueError, OSError) as exc:
            retry = (
                "Check your internet connection"
                if backend == "local"
                else f"Run `ollama pull {settings.embed_model}`"
            )
            print(f"mnemo-install: download failed: {exc}. {retry} and try again.", file=sys.stderr)
            return 1
    if checking and _mismatch(probe or Services(), settings, backend):
        return 2
    if migrating:
        try:
            applied = asyncio.run(migrate(settings.dsn, settings.embed_dim))
        except Exception as exc:
            error = services.service_error(exc, settings)
            print(
                f"mnemo-install: migration failed: {error.message if error else exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Migrations applied: {', '.join(applied) or 'none pending'}")
    for step in commands:
        result = run(step.argv)
        if result.returncode != 0:
            print(f"mnemo-install: failed: {step.description}\n{result.stderr}", file=sys.stderr)
            return 1
    for change in changes:
        change.apply()
    print("Done. Start new agent sessions to pick up the changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
