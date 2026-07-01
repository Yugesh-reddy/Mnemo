"""Which project a memory server works in: the repository's remote, else its path.

Coding agents start their MCP servers inside the project they are working on, so the
server's working directory identifies the project. Remote URLs normalize to
``host/owner/repo`` without credentials, ports or ``.git``, so an SSH clone and an
HTTPS clone of one repository share memory. A repository without a usable remote
falls back to its top-level path; outside a repository there is no project.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

# scp-like syntax: [user@]host:path (a leading "/" or "." means a local path instead).
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*)$")


def normalize_remote(url: str) -> str | None:
    """``host/owner/repo`` for a network remote; None for local or unparseable ones."""
    url = url.strip()
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname, parts.path
    else:
        match = _SCP.match(url)
        if match is None:
            return None
        host, path = match["host"], match["path"]
    path = path.strip("/")
    path = path.removesuffix(".git")
    if not host or not path:
        return None
    return f"{host}/{path}".lower()


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
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
    return f"path:{Path(top).resolve()}"
