"""The `mnemo` command: run the database, connect your agents, open the review UI.

    mnemo up          # Postgres + pgvector in Docker, localhost only (MNEMO_DB_PORT)
    mnemo install     # connect Claude Code and Codex; shows every change, asks first
    mnemo uninstall   # undo `mnemo install`; memories are kept
    mnemo migrate     # apply pending database migrations
    mnemo ui          # web review UI on http://127.0.0.1:8000
    mnemo down        # stop Postgres; data is kept

Installed as a tool (``uv tool install git+https://github.com/Yugesh-reddy/Mnemo``),
this is all a user needs, and the agents run the tool's own server rather than a
checkout. `mnemo install --help` lists the installer's options.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from mnemo.install import main as install_main

_PACKAGE_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = (
    _PACKAGE_DIR / "docker-compose.yml"
    if (_PACKAGE_DIR / "docker-compose.yml").is_file()
    else _PACKAGE_DIR.parent / "docker-compose.yml"
)
PROJECT = "mnemo"
"""One Compose project for the installed tool, shared with a checkout named Mnemo."""

Runner = Callable[[Sequence[str]], int]


def compose(*action: str) -> list[str]:
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), "-p", PROJECT]
    env_file = Path.cwd() / ".env"
    if env_file.is_file():  # the same .env the settings read, e.g. for MNEMO_DB_PORT
        command += ["--env-file", str(env_file)]
    return command + list(action)


def serve_ui(port: int) -> int:
    import uvicorn

    uvicorn.run("web.app:app", host="127.0.0.1", port=port)
    return 0


def main(argv: Sequence[str] | None = None, *, runner: Runner = subprocess.call) -> int:
    parser = argparse.ArgumentParser(
        prog="mnemo", description="Shared, versioned memory for your coding agents."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("up", help="start Postgres in Docker (localhost only)")
    commands.add_parser("down", help="stop Postgres; data is kept")
    commands.add_parser("install", help="connect Claude Code and Codex", add_help=False)
    commands.add_parser("uninstall", help="undo `mnemo install`", add_help=False)
    commands.add_parser("migrate", help="apply pending database migrations")
    ui = commands.add_parser("ui", help="web review UI on 127.0.0.1")
    ui.add_argument("--port", type=int, default=8000)
    args, rest = parser.parse_known_args(argv)
    if args.command in ("install", "uninstall"):
        return install_main((["--uninstall"] if args.command == "uninstall" else []) + rest)
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.command in ("up", "down"):
        if shutil.which("docker") is None:
            print(
                "mnemo: Docker is not installed or not on PATH. Install Docker Desktop "
                "(or another Docker engine) and start it.",
                file=sys.stderr,
            )
            return 2
        return runner(compose("up", "-d", "--wait") if args.command == "up" else compose("down"))
    if args.command == "migrate":
        from mnemo.db import main as migrate

        migrate()
        return 0
    return serve_ui(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
