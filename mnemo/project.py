"""Which project a memory server works in: the repository's remote, else its path.

Coding agents start their MCP servers inside the project they are working on, so the
server's working directory identifies the project. Remote URLs normalize to
``host/owner/repo`` without credentials, ports or ``.git``, so an SSH clone and an
HTTPS clone of one repository share memory. A repository without a usable remote
falls back to its top-level path; outside a repository there is no project.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# scp-like syntax: [user@]host:path (a leading "/", "\\" or "." means a local path instead).
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/\\].*)$")
_WIN_DRIVE = re.compile(r"^[A-Za-z]:")


def normalize_remote(url: str) -> str | None:
    """``host/owner/repo`` for a network remote; None for local or unparseable ones."""
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname, parts.path
    else:
        if _WIN_DRIVE.match(url):
            return None
        match = _SCP.match(url)
        if match is None:
            return None
        host, path = match["host"], match["path"]
    path = path.strip("/")
    path = path.removesuffix(".git")
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def normalize_project_path(path: Path | str) -> str:
    """Canonical path for ``path:`` lanes (stable drive-letter case and slash direction)."""
    text = str(path)
    windows_style = os.name == "nt" or text.startswith("\\\\") or bool(_WIN_DRIVE.match(text))
    if windows_style:
        if text[:8].upper() == "\\\\?\\UNC\\":
            text = "//" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
        text = text.replace("\\", "/")
        if _WIN_DRIVE.match(text):
            text = text[0].upper() + text[1:]
            if len(text) > 2:
                stripped = text.rstrip("/")
                return stripped + "/" if len(stripped) == 2 else stripped
            return text
    if len(text) > 1 and not (windows_style and text == "//"):
        text = text.rstrip("/")
    return text


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def detect_project(cwd: Path | str) -> str | None:
    """The project containing ``cwd``: origin (or another remote), else the repo path."""
    top = _git(Path(cwd), "rev-parse", "--show-toplevel")
    if top is None:
        return None
    remotes = (_git(Path(top), "remote") or "").split()
    for name in sorted(remotes, key=lambda remote: remote != "origin"):
        project = normalize_remote(_git(Path(top), "remote", "get-url", name) or "")
        if project:
            return project
    return f"path:{normalize_project_path(Path(top).resolve())}"
