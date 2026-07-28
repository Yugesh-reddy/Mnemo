"""The `mnemo` command: connect your agents, open the review UI, keep the store.

    mnemo install     # connect Claude Code, Codex and OpenCode; shows every change, asks first
    mnemo uninstall   # undo `mnemo install`; memories are kept
    mnemo ui          # web review UI on http://127.0.0.1:8000
    mnemo migrate     # apply pending schema migrations to the store file
    mnemo reindex     # rebuild vectors and the word index (after a model change)
    mnemo verify      # check the history was not changed outside Mnemo since last time
    mnemo export      # write this scope's memories, with their history, as JSON
    mnemo import FILE # restore an export (or a Postgres-era export) into an empty store

Memory lives in one file, ``~/.mnemo/mnemo.db`` by default (MNEMO_DB). Installed as a
tool (``uv tool install mnemohive``), this is all a
user needs, and the agents run the tool's own server rather than a checkout.
`mnemo install --help` lists the installer's options.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from mnemo.install import main as install_main

GONE = {
    "up": "Mnemo no longer needs Docker or Postgres: memory is the file in MNEMO_DB "
    "(default ~/.mnemo/mnemo.db). Run `mnemo install` to create it.",
    "down": "Mnemo no longer runs Postgres, so there is nothing to stop. A Postgres "
    "container from an earlier version keeps running until you stop it with Docker.",
}


def serve_ui(port: int) -> int:
    import uvicorn

    uvicorn.run("mnemo.web.app:app", host="127.0.0.1", port=port)
    return 0


async def _verify(witness: Path | None) -> int:
    from mnemo.chain import Witness, verify, witness_path
    from mnemo.db import check_schema, connect
    from mnemo.services import where

    conn = await connect(create=False)
    store = where(conn.path)
    witness = witness or witness_path(conn.path)
    try:
        since = Witness.read(witness)
    except ValueError as exc:
        await conn.close()
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    try:
        await check_schema(conn)
        result = await verify(conn, since)
    finally:
        await conn.close()
    if not result.ok:
        print(f"FAIL {store}: {len(result.problems)} problem(s)", file=sys.stderr)
        for problem in result.problems:
            print(f"     {problem}", file=sys.stderr)
        print(f"     {where(witness)} was left unchanged.", file=sys.stderr)
        if any("since it was last verified" in problem for problem in result.problems):
            print(
                "     If you restored a backup or recreated the store yourself, delete "
                f"{where(witness)} and run mnemo verify again to record the new head.",
                file=sys.stderr,
            )
        return 1
    print(f"ok   {store}: {result.events} events, history intact, HEADs consistent")
    if since is not None:
        print(f"     still builds on the head verified at seq {since.seq}")
    print(f"     chain head {result.head or '(empty)'}")
    if result.witness is not None:
        result.witness.write(witness)
        print(f"     recorded in {where(witness)}; keep a copy elsewhere to catch a rewrite")
    return 0


async def _reindex() -> dict[str, int]:
    from mnemo.config import get_settings
    from mnemo.core import reindex
    from mnemo.db import check_schema, connect
    from mnemo.embedder import build_embedder

    settings = get_settings()
    embedder = build_embedder(settings)
    conn = await connect(create=False)
    try:
        await check_schema(conn)
        return await reindex(conn, embedder)
    finally:
        await conn.close()
        close = getattr(embedder, "close", None)
        if close:
            close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mnemo", description="Shared, versioned memory for your coding agents."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("install", help="connect Claude Code, Codex and OpenCode", add_help=False)
    commands.add_parser("uninstall", help="undo `mnemo install`", add_help=False)
    ui = commands.add_parser("ui", help="web review UI on 127.0.0.1")
    ui.add_argument("--port", type=int, default=8000)
    commands.add_parser("migrate", help="apply pending schema migrations")
    commands.add_parser("reindex", help="rebuild vectors and the word index")
    verify = commands.add_parser("verify", help="check the history was not changed outside Mnemo")
    verify.add_argument(
        "--witness",
        type=Path,
        metavar="FILE",
        help="the head verified last time (default: beside the store, MNEMO_DB.verified)",
    )
    commands.add_parser("export", help="write memories and history as JSON", add_help=False)
    commands.add_parser("import", help="restore an export into an empty store", add_help=False)
    for name in GONE:
        commands.add_parser(name, help=argparse.SUPPRESS)
    args, rest = parser.parse_known_args(argv)
    if args.command in ("install", "uninstall"):
        return install_main((["--uninstall"] if args.command == "uninstall" else []) + rest)
    if args.command in ("export", "import"):
        from mnemo.transfer import main as transfer_main

        transfer_main([args.command, *rest])
        return 0
    if rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    if args.command in GONE:
        print(f"mnemo {args.command}: {GONE[args.command]}", file=sys.stderr)
        return 0
    if args.command == "migrate":
        from mnemo.db import main as migrate

        migrate()
        return 0
    if args.command == "verify":
        return asyncio.run(_verify(args.witness))
    if args.command == "reindex":
        counts = asyncio.run(_reindex())
        print(f"Embedded {counts['embedded']} of {counts['events']} events; word index rebuilt.")
        return 0
    return serve_ui(args.port)


if __name__ == "__main__":
    raise SystemExit(main())
