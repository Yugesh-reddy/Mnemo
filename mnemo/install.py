"""Register Mnemo with Claude Code, Codex, OpenCode, Cursor and Claude Desktop, and add the
shared-memory policy.

    mnemo-install                 # show every change, then ask before applying
    mnemo-install --yes           # apply without asking
    mnemo-install --uninstall     # remove what it added (memories are kept)

Claude Code gets a user-scope MCP server (``claude mcp add-json --scope user``) and the
policy in ``~/.claude/CLAUDE.md``. Codex gets an ``[mcp_servers.mnemo]`` table with its
tools pre-approved in ``$CODEX_HOME/config.toml`` and the policy in
``$CODEX_HOME/AGENTS.md``. OpenCode (when installed) gets a ``mnemo`` server under ``mcp``
in its global ``opencode.json`` and the policy in its global ``AGENTS.md``, or in
``~/.claude/CLAUDE.md`` while OpenCode is reading that file instead. Cursor (when
installed) gets a ``mnemo`` server in ``~/.cursor/mcp.json``, told the open project
through ``${workspaceFolder}``, and the policy as the always-applied rule
``~/.cursor/rules/mnemo.mdc``. Claude Desktop, only when named with ``--agent
claude-desktop``, gets a server in ``claude_desktop_config.json``; it works outside any
project, so it uses global memories, and has no instructions file. Text written into a
file sits between mnemo markers, so it can be updated or removed without touching the
rest of the file. Each agent gets its own MNEMO_ACTOR; all of them share one store file
(MNEMO_DB, default ~/.mnemo/mnemo.db), namespace and user.

Before changing anything it checks that the store file is usable with this Python and,
for the Ollama backend, that Ollama is running; a missing embedding model (Ollama's,
or the local backend's, which runs inside Mnemo) is downloaded after you confirm. It
creates the store readable only by you, and embeds any stored memories that have no
vector from the chosen model. Problems are reported with the command that fixes them.
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
import sysconfig
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
    return files("mnemo").joinpath("memory_policy.md").read_text(encoding="utf-8")


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


def _toml_string(value: str) -> str:
    """TOML 1.0 basic string (escapes Windows backslashes; avoids UTF-16 surrogates)."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def codex_table(command: str, env: dict[str, str]) -> str:
    lines = [
        "[mcp_servers.mnemo]",
        f"command = {_toml_string(command)}",
        'default_tools_approval_mode = "approve"',
        "",
        "[mcp_servers.mnemo.env]",
        *(f"{key} = {_toml_string(value)}" for key, value in env.items()),
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
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode"


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


_SERVER_NAMES = frozenset(
    {
        "mnemo-mcp-direct",
        "mnemo-mcp-direct.exe",
        "mnemo-mcp-direct.cmd",
        "mnemo-mcp-direct.bat",
    }
)


def _ours(entry: object) -> bool:
    """A server entry mnemo-install wrote: it runs a mnemo-mcp-direct (OpenCode gives the
    command as a list, Cursor and Claude Desktop as a string)."""
    command = entry.get("command") if isinstance(entry, dict) else None
    if isinstance(command, list):
        command = command[0] if command else None
    if not isinstance(command, str) or not command:
        return False
    # Normalize backslashes too so Windows entries are recognized even on POSIX.
    name = Path(command.replace("\\", "/")).name.lower()
    return name in _SERVER_NAMES


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


def _indent(text: str) -> int | str:
    """The indent the file already uses, so a rewrite changes only what it must."""
    match = re.search(r"^([ \t]+)\S", text, re.M)
    if match is None:
        return 2
    return len(match[1]) if match[1].strip(" ") == "" else match[1]


def mcp_servers_config(text: str, path: Path, entry: dict | None) -> str:
    """A Cursor or Claude Desktop config with ``mcpServers.mnemo`` set to ``entry`` (None
    removes it), every other key kept. Like OpenCode's, a file with comments or trailing
    commas is never rewritten: the error says what to change by hand."""
    plain = True
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        plain = False
        try:
            data = json.loads(strip_jsonc(text))
        except json.JSONDecodeError as exc:
            raise InstallError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} is not a JSON object")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(f'"mcpServers" in {path} is not an object; fix it first')
    if "mnemo" in servers and not _ours(servers["mnemo"]):
        raise InstallError(
            f"{path} already defines an MCP server named mnemo that mnemo-install did "
            "not write; remove or rename it first"
        )
    if servers.get("mnemo") == entry:
        return text
    if not plain:
        action = (
            f'add this under "mcpServers":\n{json.dumps({"mnemo": entry}, indent=2)}'
            if entry is not None
            else 'delete "mnemo" under "mcpServers"'
        )
        raise InstallError(
            f"{path} has comments or trailing commas, which rewriting would lose; {action}"
        )
    if entry is None:
        servers.pop("mnemo", None)
        if not servers:
            data.pop("mcpServers", None)
    else:
        servers["mnemo"] = entry
        data["mcpServers"] = servers
    if not data and not text.strip():
        return text
    return json.dumps(data, indent=_indent(text), ensure_ascii=False) + "\n"


def default_cursor_dir() -> Path:
    return Path.home() / ".cursor"


def cursor_detected(cursor_dir: Path) -> bool:
    """Cursor creates its user folder on first start."""
    return cursor_dir.is_dir()


def cursor_entry(command: str, env: dict[str, str]) -> dict:
    # Cursor starts global servers in the home folder; ${workspaceFolder} tells the server
    # which project is open (unexpanded or empty, the server uses no project).
    return {
        "type": "stdio",
        "command": command,
        "args": [],
        "env": {**env, "MNEMO_PROJECT_DIR": "${workspaceFolder}"},
    }


MSIX_CLAUDE = Path("Packages") / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"


def default_claude_desktop_config() -> Path:
    """claude_desktop_config.json: macOS Application Support, Windows %APPDATA% (or the
    Microsoft Store package's copy when only that exists), Linux ~/.config/Claude."""
    name = "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / name
    if _is_windows():
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        store = local / MSIX_CLAUDE
        if not (roaming / "Claude").is_dir() and store.is_dir():
            return store / name
        return roaming / "Claude" / name
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "Claude" / name


CURSOR_RULE_HEAD = """---
description: Shared memory with the other coding agents (Mnemo)
alwaysApply: true
---
"""


def cursor_rule(text: str, uninstall: bool) -> str:
    """~/.cursor/rules/mnemo.mdc: the policy as an always-applied rule. Uninstalling
    leaves the file empty (and so removed) when nothing else was added to it."""
    if uninstall:
        kept = remove_block(text, MD_BEGIN, MD_END)
        return "" if kept.strip() == CURSOR_RULE_HEAD.strip() else kept
    return upsert_block(text or CURSOR_RULE_HEAD, policy_text(), MD_BEGIN, MD_END)


def claude_desktop_installed(config: Path) -> bool:
    """Claude Desktop already has the server mnemo-install wrote."""
    try:
        data = json.loads(_read(config) or "{}")
    except (json.JSONDecodeError, OSError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and _ours(servers.get("mnemo"))


def claude_desktop_entry(command: str, env: dict[str, str]) -> dict:
    return {"command": command, "args": [], "env": env}


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
        "MNEMO_DB": str(settings.db),
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
    elif backend == "ollama":
        # Pinned so a non-default model or host follows the agents into every repo.
        env["MNEMO_EMBED_MODEL"] = settings.embed_model
        env["MNEMO_OLLAMA_HOST"] = settings.ollama_host
    return env


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform == "win32"


def server_command() -> str:
    """The mnemo-mcp-direct installed alongside the running interpreter."""
    exe_dir = Path(sys.executable).parent
    scripts = sysconfig.get_path("scripts")
    windows = _is_windows()
    candidates: list[str] = []
    extra_scripts = (
        str(exe_dir / "Scripts") if windows and exe_dir.name.lower() != "scripts" else None
    )
    for directory in (scripts, str(exe_dir), extra_scripts):
        if directory and directory not in candidates:
            candidates.append(directory)
    names = ("mnemo-mcp-direct.exe", "mnemo-mcp-direct") if windows else ("mnemo-mcp-direct",)
    for directory in candidates:
        for name in names:
            # Pass a qualified path so Windows shutil.which never prepends CWD via PATHEXT.
            found = shutil.which(os.path.join(directory, name))
            if found:
                return found
    fallback_name = "mnemo-mcp-direct.exe" if windows else "mnemo-mcp-direct"
    fallback_dir = Path(scripts) if windows and scripts else exe_dir
    return str(fallback_dir / fallback_name)


def running_from_checkout() -> bool:
    """True when this code runs from a source checkout rather than an installed package."""
    return not any(part.lower() == "site-packages" for part in Path(__file__).resolve().parts)


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


# The preview shows lines around each change, and OpenCode's config is rewritten whole,
# so other tools' credentials in these files would be printed. A setting whose name
# looks like a credential, and any value shaped like a known token, shows as <hidden>.
_SECRET_NAME = re.compile(
    r"token|secret|passw|api[_-]?key|access[_-]?key|private[_-]?key|credential|"
    r"auth|bearer|cookie|(?:^|[_-])pat(?:$|[_-])",
    re.I,
)
_SETTING = re.compile(
    r"""^(?P<lead>\s*["']?(?P<name>[\w.-]+)["']?\s*[:=]\s*)(?P<value>.*?)(?P<tail>,?\s*)$"""
)
_TOKEN = re.compile(
    r"\b(?:github_pat_|gh[pousr]_|glpat-|sk-|sk_live_|rk_live_|xox[abpr]-|hf_|m0-|AKIA|AIza)"
    r"[\w-]{8,}"
)
# A name: value or name=value pair anywhere in a line: minified JSON, TOML inline tables,
# --api-key=... arguments.
_PAIR = re.compile(
    r"""(?P<lead>(?P<q>["']?)(?P<name>[\w.-]+)(?P=q)\s*[:=]\s*)"""
    r"""(?P<value>"(?:[^"\\]|\\.)*"|'[^']*'|[^\s,;}\]"'{\[]+)"""
)
_URL_PASSWORD = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@\"']+:)[^\s/@\"']+@")
HIDDEN = "<hidden>"


def hide_secrets(line: str) -> str:
    """``line`` with credential-looking values replaced by ``<hidden>``."""
    match = _SETTING.match(line)
    if (
        match
        and _SECRET_NAME.search(match["name"])
        and match["value"]
        and match["value"][0] not in "{["
    ):
        line = f"{match['lead']}{HIDDEN}{match['tail']}"
    line = _PAIR.sub(_hide_pair, line)
    line = _URL_PASSWORD.sub(rf"\1{HIDDEN}@", line)
    return _TOKEN.sub(HIDDEN, line)


def _hide_pair(match: re.Match[str]) -> str:
    if not _SECRET_NAME.search(match["name"]):
        return match[0]
    value = match["value"]
    quote = value[0] if value[0] in "\"'" else ""
    return f"{match['lead']}{quote}{HIDDEN}{quote}"


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
            self.path.write_text(self.after, encoding="utf-8")

    def diff(self) -> str:
        """The preview of this change, with credentials hidden (the file gets the
        real values)."""
        lines = difflib.unified_diff(
            self.before.splitlines(keepends=True),
            self.after.splitlines(keepends=True),
            fromfile=str(self.path),
            tofile=str(self.path),
        )
        shown = []
        for line in lines:
            if not line.startswith(("---", "+++", "@@")):
                line = line[0] + hide_secrets(line[1:])
            # A last line without a newline would run into the next one.
            shown.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
        return "".join(shown)


@dataclass
class Command:
    description: str
    argv: list[str]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


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
    cursor_dir: Path | None = None,
    cursor_optional: bool = False,
    claude_desktop_config: Path | None = None,
) -> tuple[list[Command], list[FileChange], list[str]]:
    """Commands, file changes and notes for the agents. ``opencode_optional`` and
    ``cursor_optional``: the client was detected rather than requested, so a config
    mnemo-install must not rewrite skips it with a note instead of stopping the others."""
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
    if "cursor" in agents:
        cursor_dir = cursor_dir or default_cursor_dir()
        config_path = cursor_dir / "mcp.json"
        entry = (
            None if uninstall else cursor_entry(command, server_env(settings, "cursor", backend))
        )
        try:
            edit(config_path, lambda text: mcp_servers_config(text, config_path, entry))
        except InstallError as exc:
            if not cursor_optional:
                raise
            notes.append(f"Skipped Cursor: {exc}")
        else:
            rule = cursor_dir / "rules" / "mnemo.mdc"
            edit(rule, lambda text: cursor_rule(text, uninstall))
            remove_if_empty.add(rule)
            if not uninstall:
                notes.append(
                    "Cursor asks before each Mnemo tool call until you allow them in its "
                    'settings. (An "mnemo:*" mcpAllowlist in ~/.cursor/permissions.json also '
                    "works, but that file replaces the allowlist set in Cursor's settings, "
                    "so mnemo-install does not create it.)"
                )
    if "claude-desktop" in agents:
        config_path = claude_desktop_config or default_claude_desktop_config()
        entry = (
            None
            if uninstall
            else claude_desktop_entry(command, server_env(settings, "claude-desktop", backend))
        )
        edit(config_path, lambda text: mcp_servers_config(text, config_path, entry))
        if not uninstall:
            notes.append(
                "Claude Desktop is not in a project, so it reads and writes global memories "
                "only, and it has no instructions file for the policy. Quit it fully and "
                "reopen it to load the server."
            )
    changes = [
        FileChange(path, before, after, path in remove_if_empty)
        for path, (before, after) in texts.items()
    ]
    return commands, [c for c in changes if c.before != c.after], notes


class Services:
    """The pre-flight probes; tests substitute fakes."""

    def store_problem(self, settings: Settings) -> str | None:
        return asyncio.run(services.store_problem(settings))

    def ollama_models(self, host: str) -> list[str]:
        return services.ollama_models(host)

    def pull(self, host: str, model: str) -> None:
        services.pull_model(host, model)

    def local_model_ready(self, settings: Settings) -> bool:
        return local_model_ready(settings.embed_model, settings.model_dir)

    def download_local(self, settings: Settings) -> None:
        download_local_model(settings.embed_model, settings.model_dir)

    def missing_vectors(self, settings: Settings) -> int:
        return asyncio.run(services.missing_vectors(settings, build_embedder(settings)))

    def reindex(self, settings: Settings) -> dict[str, int]:
        from mnemo.core import reindex
        from mnemo.db import connect

        async def run() -> dict[str, int]:
            embedder = build_embedder(settings)
            conn = await connect(settings.db, create=False)
            try:
                return await reindex(conn, embedder)
            finally:
                await conn.close()
                close = getattr(embedder, "close", None)
                if close:
                    close()

        return asyncio.run(run())


@dataclass
class Preflight:
    ready: list[str]
    problems: list[str]
    pull_model: bool = False


def preflight(settings: Settings, backend: str, probe: Services) -> Preflight:
    result = Preflight(ready=[], problems=[])
    problem = probe.store_problem(settings)
    if problem:
        result.problems.append(problem)
    else:
        result.ready.append(f"store at {services.where(settings.db)}")
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


async def migrate(path: Path) -> list[str]:
    """Create the store (directory 0700, file 0600) if needed and apply migrations."""
    from mnemo.db import apply_migrations, connect

    conn = await connect(path)
    try:
        return await apply_migrations(conn)
    finally:
        await conn.close()


def probe_or(probe: Services | None) -> Services:
    return probe or Services()


def _run_command(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    argv = list(cmd)
    if _is_windows() and argv:
        # On Windows, CreateProcess only resolves .exe; shutil.which finds .cmd/.bat shims too.
        resolved = shutil.which(argv[0])
        if resolved:
            argv[0] = resolved
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")


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
        choices=["claude", "codex", "opencode", "cursor", "claude-desktop"],
        action="append",
        help="default: claude, codex, and opencode and cursor when installed; "
        "claude-desktop when named, or already set up",
    )
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--yes", action="store_true", help="apply without asking")
    parser.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    parser.add_argument("--backend", choices=["ollama", "local", "hash", "openai"])
    parser.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-home", type=Path)
    parser.add_argument("--opencode-dir", type=Path, help="default: ~/.config/opencode")
    parser.add_argument("--cursor-dir", type=Path, help="default: ~/.cursor")
    parser.add_argument(
        "--claude-desktop-config",
        type=Path,
        metavar="FILE",
        help="default: the claude_desktop_config.json Claude Desktop reads on this system",
    )
    parser.add_argument("--skip-migrate", action="store_true")
    parser.add_argument("--skip-checks", action="store_true", help="don't probe the services")
    parser.add_argument(
        "--claude-auto-memory",
        choices=["off", "on"],
        help="turn Claude Code's own auto-memory off, or back on (default: leave it)",
    )
    args = parser.parse_args(argv)
    run = runner or _run_command
    settings = get_settings()
    backend = args.backend or settings.backend
    if backend != settings.backend:
        # Re-resolve backend-dependent defaults (model, dimension, search floor).
        settings = Settings(backend=backend)
    codex_home = args.codex_home or default_codex_home()
    opencode_dir = args.opencode_dir or default_opencode_dir()
    cursor_dir = args.cursor_dir or default_cursor_dir()
    claude_desktop = args.claude_desktop_config or default_claude_desktop_config()
    agents = args.agent or ["claude", "codex"] + (
        ["opencode"] if opencode_detected(opencode_dir) else []
    ) + (["cursor"] if cursor_detected(cursor_dir) else []) + (
        # Named once, then kept up to date (and removed by a plain uninstall).
        ["claude-desktop"]
        if claude_desktop_installed(claude_desktop)
        else []
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
            cursor_dir=cursor_dir,
            cursor_optional=not (args.agent and "cursor" in args.agent),
            claude_desktop_config=claude_desktop,
        )
    except InstallError as exc:
        print(f"mnemo-install: {exc}", file=sys.stderr)
        return 2
    migrating = not (args.uninstall or args.skip_migrate)
    print(f"Store: {services.where(settings.db)} (backend {backend})")
    print(f"Codex home: {codex_home}  (pass --codex-home if your codex uses another)")
    if "opencode" in agents:
        print(f"OpenCode config: {opencode_dir}  (pass --opencode-dir for another)")
    if "cursor" in agents:
        print(f"Cursor config: {cursor_dir}  (pass --cursor-dir for another)")
    if "claude-desktop" in agents:
        print(f"Claude Desktop config: {claude_desktop}  (pass --claude-desktop-config)")
    if not args.uninstall and running_from_checkout():
        print(
            f"Note: the agents will run {server_command()} from this checkout; moving or "
            "deleting it breaks them. `make agents` installs the mnemo tool and connects "
            "that instead."
        )
    for note in notes:
        print(f"Note: {note}")
    if not args.uninstall and (perm_warning := services.dir_permission_warning(settings.db)):
        print(f"Note: {perm_warning}")
    if checks.pull_model and backend == "local":
        size = LOCAL_MODELS.get(settings.embed_model, (0, 0, 0))[2]
        about = f" (about {size} MB)" if size else ""
        print(
            f"- Download the embedding model {settings.embed_model}{about} from Hugging Face "
            f"into {settings.model_dir}"
        )
    elif checks.pull_model:
        print(f"- Download the embedding model {settings.embed_model} into Ollama (about 270 MB)")
    checking = not (args.uninstall or args.skip_checks)
    missing = probe_or(probe).missing_vectors(settings) if checking else 0
    if migrating:
        print(
            "- Apply any pending schema migrations"
            if settings.db.exists()
            else f"- Create the store {services.where(settings.db)} (readable only by you)"
        )
    if missing:
        print(
            f"- Embed {missing} stored memories with {backend} {settings.embed_model} "
            "(search compares a question only with vectors from the same model)"
        )
    for step in commands:
        print(f"- {step.description}")
    for change in changes:
        print(change.diff(), end="")
    if not (commands or changes or migrating or checks.pull_model or missing):
        print("Nothing to change.")
        return 0
    if args.dry_run:
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
    if migrating:
        try:
            applied = asyncio.run(migrate(settings.db))
        except Exception as exc:
            error = services.service_error(exc, settings)
            print(
                f"mnemo-install: migration failed: {error.message if error else exc}",
                file=sys.stderr,
            )
            return 1
        print(f"Migrations applied: {', '.join(applied) or 'none pending'}")
    if missing:
        print(f"Embedding {missing} memories...", flush=True)
        try:
            counts = probe_or(probe).reindex(settings)
        except Exception as exc:
            error = services.service_error(exc, settings)
            print(
                f"mnemo-install: embedding failed: {error.message if error else exc}. "
                "Memories are still found by their words; run `mnemo reindex` later.",
                file=sys.stderr,
            )
        else:
            print(f"Embedded {counts['embedded']} memories.")
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
