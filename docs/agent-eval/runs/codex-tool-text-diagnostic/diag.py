"""Why does Codex not store facts? Two seed prompts x four conditions, Codex only."""

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg

ROOT = Path("/Users/yugesh/Desktop/Projects/Mnemo")
sys.path.insert(0, str(ROOT))
from mnemo.config import get_settings  # noqa: E402
from mnemo.db import apply_migrations  # noqa: E402
from scripts import agent_eval as ae  # noqa: E402

POLICY = """# Shared memory

You share a persistent memory with other coding agents through the `mnemo` MCP tools
(memory_search, memory_create, memory_get, memory_update).

- When the user tells you something worth keeping for later sessions (a convention,
  command, decision, handoff note, correction or preference), save it with
  memory_create, or memory_update if it changes an existing memory. Only say you
  saved it after the tool call succeeds.
- Before answering questions about this project's conventions, commands, decisions
  or the user's preferences, search memory first.
- Use scope 'global' only for things the user says apply to all projects.
"""

PROMPTS = {
    "explicit": "Please remember: the on-call handoff for this team happens on Fridays.",
    "handoff": (
        "I'm stopping for today. Note for whoever picks this up next: test_sync_timeout is "
        "flaky because of an upstream bug, so run the suite with `pytest -k 'not sync_timeout'` "
        "until the vendor ships a fix."
    ),
}
CONDITIONS = {
    "tool_text_only": {"sandbox": "read-only", "extra": [], "files": {}},
}


async def main(out: Path) -> None:
    settings = get_settings()
    token = uuid4().hex[:8]
    db = f"mnemo_codex_diag_{token}"
    parts = urlsplit(settings.test_dsn)
    admin_dsn = urlunsplit(parts._replace(path="/postgres"))
    dsn = urlunsplit(parts._replace(path="/" + db))
    admin = await asyncpg.connect(admin_dsn)
    await admin.execute(f'CREATE DATABASE "{db}"')
    await admin.close()
    conn = await asyncpg.connect(dsn)
    disable = ae.other_codex_servers()
    rows = []
    try:
        await apply_migrations(conn, embed_dim=settings.embed_dim)
        for cname, cond in CONDITIONS.items():
            for pname, prompt in PROMPTS.items():
                namespace = f"diag-{cname}-{pname}-{token}"
                work = Path(tempfile.mkdtemp(prefix="mnemo-diag-"))
                repo = ae.make_repo(work, "alpha", cond["files"])
                env = ae.mnemo_env(dsn, namespace, "codex", "ollama", settings.embed_dim)
                command = ae.codex_command(env, disable)
                command[command.index("-s") + 1] = cond["sandbox"]
                command = command[:-1] + cond["extra"] + ["-"]
                before = await ae.event_count(conn, namespace)
                status, stdout, stderr = ae.run_cli(command, prompt, repo, dict(__import__("os").environ))
                parsed = ae.parse_codex(stdout)
                writes = await ae.event_count(conn, namespace) - before
                (out / f"{cname}-{pname}.jsonl").write_text(stdout)
                row = {
                    "condition": cname,
                    "prompt": pname,
                    "status": status,
                    "writes": writes,
                    "mnemo_calls": [c["tool"] for c in parsed["tool_calls"] if c["tool"].startswith("mnemo:")],
                    "answer": parsed["answer"][:200],
                    "usage": parsed["usage"],
                }
                rows.append(row)
                print(json.dumps({k: row[k] for k in ("condition", "prompt", "status", "writes", "mnemo_calls")}), flush=True)
    finally:
        await conn.close()
        admin = await asyncpg.connect(admin_dsn)
        await admin.execute(f'DROP DATABASE "{db}" WITH (FORCE)')
        await admin.close()
    (out / "summary.json").write_text(json.dumps({"policy": POLICY, "rows": rows}, indent=2))


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=False)
    asyncio.run(main(out))
