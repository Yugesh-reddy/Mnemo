#!/usr/bin/env bash
# End-to-end Linux installation check in a disposable Docker container.
#
# Builds a wheel from this checkout into a temporary directory, mounts only that
# directory read-only into a fresh Linux container (never touching the host HOME
# or checkout), installs the wheel with `uv tool install` as a non-root user,
# and verifies:
#   1. Clean-HOME install with no coding agents installed (`mnemo --help`,
#      `mnemo install --help`, `mnemo install --yes`, store dir 0700 / db 0600,
#      `mnemo migrate`, `mnemo verify`, `mnemo uninstall --yes`).
#   2. Install with fake `claude`/`codex` executables and pre-existing Claude,
#      Codex and OpenCode configs: verifies written MCP configs, policy blocks,
#      and idempotent reinstall.
#   3. Live `mnemo-mcp-direct` stdio round trip (create, search, update,
#      REVISION_CONFLICT, history, revert, get) using the exact command and env
#      written by the installer, plus `mnemo verify` witness creation (0600) and
#      re-verification.
#   4. `mnemo ui` startup and HTTP checks across all routes and static assets.
#   5. `mnemo uninstall --yes` restores every pre-existing config file and
#      removes the Claude registration while preserving the store file.
#
# Usage:
#   bash scripts/check-linux.sh            # fast check using --backend hash
#   bash scripts/check-linux.sh --local    # also downloads BAAI/bge-small-en-v1.5
#
# Environment overrides:
#   MNEMO_LINUX_IMAGE     Docker image (default: ghcr.io/astral-sh/uv:python3.12-bookworm-slim;
#                         python:3.12-slim also works and installs uv inside the container)
#   MNEMO_LINUX_PLATFORM  Optional docker --platform value (e.g. linux/amd64)
set -euo pipefail

backend="hash"
for arg in "$@"; do
  case "$arg" in
    --local) backend="local" ;;
    --hash) backend="hash" ;;
    -h|--help)
      awk 'NR > 1 { if (/^#/) { sub(/^# ?/, ""); print } else exit }' "$0"
      exit 0
      ;;
    *)
      echo "check-linux.sh: unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="${MNEMO_LINUX_IMAGE:-ghcr.io/astral-sh/uv:python3.12-bookworm-slim}"

command -v docker >/dev/null 2>&1 || { echo "check-linux.sh: docker is required" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "check-linux.sh: uv is required" >&2; exit 1; }

tmp_dist="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_dist"
}
trap cleanup EXIT

echo "==> Building wheel from $repo_root into temporary directory"
(cd "$repo_root" && uv build --wheel --out-dir "$tmp_dist")

docker_args=(run --rm -i)
if [ -n "${MNEMO_LINUX_PLATFORM:-}" ]; then
  docker_args+=(--platform "$MNEMO_LINUX_PLATFORM")
fi
docker_args+=(
  -e "MNEMO_CHECK_BACKEND=$backend"
  -v "$tmp_dist:/dist:ro"
  "$image"
  bash -euo pipefail -s
)

echo "==> Running Linux install check in $image (backend=$backend${MNEMO_LINUX_PLATFORM:+, platform=$MNEMO_LINUX_PLATFORM})"
docker "${docker_args[@]}" <<'CONTAINER_SCRIPT'
uname -a
if ! command -v uv >/dev/null 2>&1; then
  echo "==> Installing uv inside container"
  pip install --no-cache-dir --root-user-action=ignore -q uv
fi
uv --version

useradd -m -s /bin/bash tester

cat > /tmp/check-user.sh <<'USER_SCRIPT'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
backend="$MNEMO_CHECK_BACKEND"

echo "==> [1/5] uv tool install wheel and verify CLI + SQLite extensions"
uv tool install /dist/mnemohive-*.whl
mnemo --help >/dev/null
mnemo install --help >/dev/null

tool_py="$(head -n1 "$(command -v mnemo)" | sed 's/^#!//')"
"$tool_py" - <<'PY'
import sqlite3
from mnemo.sqlite import _vector_functions, capability_problem

problem = capability_problem()
assert problem is None, f"capability_problem: {problem}"
conn = sqlite3.connect(":memory:")
vec_mode = _vector_functions(conn)
conn.close()
print(f"Linux Python SQLite {sqlite3.sqlite_version}: FTS5 ok, vector_functions={vec_mode}")
assert vec_mode == "sqlite-vec", f"expected sqlite-vec, got {vec_mode}"
PY

echo "==> [2/5] Clean-HOME install with no coding agents installed (backend=$backend)"
install_out="$(mnemo install --yes --backend "$backend")"
printf '%s\n' "$install_out" | grep -q "Claude Code CLI not found; skipped its MCP registration."
test ! -e "$HOME/.config/opencode"
test "$(stat -c %a "$HOME/.mnemo")" = "700"
test "$(stat -c %a "$HOME/.mnemo/mnemo.db")" = "600"
if [ "$backend" = "local" ]; then
  test "$(stat -c %a "$HOME/.mnemo/models")" = "700"
fi

mnemo migrate | grep -q "No pending migrations"
mnemo verify | grep -q "0 events, history intact, HEADs consistent, 0 memory identities sealed"

mnemo uninstall --yes >/dev/null
test -f "$HOME/.mnemo/mnemo.db"
test "$(wc -c < "$HOME/.claude/CLAUDE.md")" -eq 0
test "$(wc -c < "$HOME/.codex/config.toml")" -eq 0
test "$(wc -c < "$HOME/.codex/AGENTS.md")" -eq 0
echo "Clean-HOME install/verify/uninstall OK (modes: .mnemo=700, mnemo.db=600)"

echo "==> [3/5] Install with fake claude/codex stubs and pre-existing agent configs"
mkdir -p "$HOME/bin" "$HOME/.claude" "$HOME/.codex" "$HOME/.config/opencode"
export PATH="$HOME/bin:$PATH"

cat > "$HOME/bin/claude" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
state="$HOME/.fake-claude-mcp-state"
log="$HOME/.fake-claude-calls.log"
printf '%s\n' "$*" >> "$log"
if [ "${1:-}" = "mcp" ] && [ "${2:-}" = "get" ] && [ "${3:-}" = "mnemo" ]; then
  [ -f "$state" ] && exit 0 || exit 1
elif [ "${1:-}" = "mcp" ] && [ "${2:-}" = "add-json" ] && [ "${3:-}" = "mnemo" ]; then
  printf '%s\n' "${4:-}" > "$state"
  exit 0
elif [ "${1:-}" = "mcp" ] && [ "${2:-}" = "remove" ] && [ "${3:-}" = "mnemo" ]; then
  rm -f "$state"
  exit 0
fi
echo "unexpected fake claude invocation: $*" >&2
exit 2
SH
chmod +x "$HOME/bin/claude"

cat > "$HOME/bin/codex" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$HOME/bin/codex"

cat > "$HOME/.claude/CLAUDE.md" <<'MD'
# Existing Claude notes
MD
cat > "$HOME/.codex/config.toml" <<'TOML'
model = "gpt-5"

[mcp_servers.other]
url = "https://example.test/mcp"
TOML
cat > "$HOME/.codex/AGENTS.md" <<'MD'
# Existing Codex notes
Keep diffs small.
MD
cat > "$HOME/.config/opencode/opencode.json" <<'JSON'
{
  "model": "anthropic/claude-sonnet"
}
JSON
cat > "$HOME/.config/opencode/AGENTS.md" <<'MD'
# Existing OpenCode notes
MD

mkdir -p "$HOME/.orig"
cp "$HOME/.claude/CLAUDE.md" "$HOME/.orig/claude.md"
cp "$HOME/.codex/config.toml" "$HOME/.orig/codex.toml"
cp "$HOME/.codex/AGENTS.md" "$HOME/.orig/codex-agents.md"
cp "$HOME/.config/opencode/opencode.json" "$HOME/.orig/opencode.json"
cp "$HOME/.config/opencode/AGENTS.md" "$HOME/.orig/opencode-agents.md"

mnemo install --yes --backend "$backend" >/dev/null
# Reinstall must be idempotent for file contents and cleanly re-register Claude.
cp "$HOME/.codex/config.toml" "$HOME/.orig/codex-after-first.toml"
mnemo install --yes --backend "$backend" >/dev/null
diff -u "$HOME/.orig/codex-after-first.toml" "$HOME/.codex/config.toml"
grep -q "mcp remove mnemo --scope user" "$HOME/.fake-claude-calls.log"
grep -q "mcp add-json mnemo " "$HOME/.fake-claude-calls.log"
test -f "$HOME/.fake-claude-mcp-state"
test "$(stat -c %a "$HOME/.mnemo")" = "700"
test "$(stat -c %a "$HOME/.mnemo/mnemo.db")" = "600"

echo "==> [4/5] Drive mnemo-mcp-direct over stdio and run mnemo verify + mnemo ui"
"$tool_py" - "$backend" <<'PY'
import asyncio
import json
import os
import sys
import tomllib
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

backend = sys.argv[1]
home = Path.home()

codex_cfg = tomllib.loads((home / ".codex/config.toml").read_text())
assert codex_cfg["model"] == "gpt-5"
assert codex_cfg["mcp_servers"]["other"]["url"] == "https://example.test/mcp"
srv = codex_cfg["mcp_servers"]["mnemo"]
assert srv["env"]["MNEMO_ACTOR"] == "codex"
assert srv["env"]["MNEMO_BACKEND"] == backend
assert Path(srv["command"]).is_file() and os.access(srv["command"], os.X_OK)

claude_cfg = json.loads((home / ".fake-claude-mcp-state").read_text())
assert claude_cfg["command"] == srv["command"]
assert claude_cfg["env"]["MNEMO_ACTOR"] == "claude-code"

oc_cfg = json.loads((home / ".config/opencode/opencode.json").read_text())
assert oc_cfg["model"] == "anthropic/claude-sonnet"
assert oc_cfg["mcp"]["mnemo"]["command"] == [srv["command"]]
assert oc_cfg["mcp"]["mnemo"]["environment"]["MNEMO_ACTOR"] == "opencode"

for rel in (".claude/CLAUDE.md", ".codex/AGENTS.md", ".config/opencode/AGENTS.md"):
    text = (home / rel).read_text()
    assert "## Shared memory (Mnemo)" in text and "# Existing " in text, rel


async def round_trip() -> None:
    params = StdioServerParameters(
        command=srv["command"],
        env={**os.environ, **srv["env"]},
    )
    async with asyncio.timeout(60):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = [t.name for t in (await session.list_tools()).tools]
                assert len(tools) == 7, tools

                c = await session.call_tool(
                    "memory_create",
                    {
                        "subject": "linux-check",
                        "predicate": "test_command",
                        "value": "Run pytest via uv run pytest -q",
                        "source": "user",
                    },
                )
                assert not c.isError, c
                created = json.loads(c.content[0].text)
                fact_id, ev1 = created["fact_id"], created["event_id"]
                assert created["scope"] == "global"

                query = (
                    "how do we run unit tests here?"
                    if backend == "local"
                    else "linux-check test_command pytest"
                )
                s = await session.call_tool("memory_search", {"query": query})
                assert not s.isError, s
                hits = json.loads(s.content[0].text)["hits"]
                assert any(
                    h["fact_id"] == fact_id
                    and h["actor"] == "codex"
                    and h["trust_level"] == "high"
                    for h in hits
                ), hits

                u = await session.call_tool(
                    "memory_update",
                    {
                        "fact_id": fact_id,
                        "value": "Run pytest via make test",
                        "expected_event_id": ev1,
                        "source": "user",
                    },
                )
                assert not u.isError, u
                ev2 = json.loads(u.content[0].text)["event_id"]

                stale = await session.call_tool(
                    "memory_update",
                    {
                        "fact_id": fact_id,
                        "value": "stale write",
                        "expected_event_id": ev1,
                        "source": "user",
                    },
                )
                assert stale.isError
                assert json.loads(stale.content[0].text)["code"] == "REVISION_CONFLICT"

                h = await session.call_tool("memory_history", {"fact_id": fact_id})
                assert not h.isError, h
                entries = json.loads(h.content[0].text)["entries"]
                assert [e["op"] for e in entries] == ["UPDATE", "ADD"], entries

                r = await session.call_tool(
                    "memory_revert",
                    {"fact_id": fact_id, "to_event_id": ev1, "expected_event_id": ev2},
                )
                assert not r.isError, r

                g = await session.call_tool("memory_get", {"fact_id": fact_id})
                assert not g.isError, g
                assert json.loads(g.content[0].text)["value"] == "Run pytest via uv run pytest -q"

                (home / ".last-fact-id").write_text(fact_id)
                print(f"MCP stdio round trip OK (fact_id={fact_id})")


asyncio.run(round_trip())
PY

mnemo migrate | grep -q "No pending migrations"
mnemo verify | grep -q "3 events, history intact, HEADs consistent, 1 memory identities sealed"
test "$(stat -c %a "$HOME/.mnemo/mnemo.db.verified")" = "600"
mnemo verify | grep -q "still builds on the head verified at seq 3"
echo "mnemo verify + witness (0600) OK"

MNEMO_BACKEND="$backend" mnemo ui --port 18765 >"$HOME/ui.log" 2>&1 &
ui_pid=$!
stop_ui() {
  kill "$ui_pid" 2>/dev/null || true
  wait "$ui_pid" 2>/dev/null || true
}
trap stop_ui EXIT

fact_id="$(cat "$HOME/.last-fact-id")"
"$tool_py" - "$fact_id" <<'PY'
import sys
import time

import httpx

fact_id = sys.argv[1]
base = "http://127.0.0.1:18765"

for _ in range(60):
    try:
        resp = httpx.get(f"{base}/", timeout=1.0)
        if resp.status_code == 200:
            break
    except Exception:
        time.sleep(0.1)
else:
    raise SystemExit("mnemo ui did not become ready on 127.0.0.1:18765")

assert "linux-check" in resp.text and "uv run pytest -q" in resp.text
for path in (
    f"/fact/{fact_id}",
    "/rows?q=pytest",
    "/retracted",
    "/diff",
    "/static/app.css",
    "/static/htmx.js",
):
    r = httpx.get(f"{base}{path}", timeout=5.0)
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
print("mnemo ui HTTP checks OK across all routes and static assets")
PY

stop_ui
trap - EXIT

echo "==> [5/5] mnemo uninstall restores pre-existing configs and preserves memories"
mnemo uninstall --yes >/dev/null
test ! -e "$HOME/.fake-claude-mcp-state"
diff -u "$HOME/.orig/claude.md" "$HOME/.claude/CLAUDE.md"
diff -u "$HOME/.orig/codex.toml" "$HOME/.codex/config.toml"
diff -u "$HOME/.orig/codex-agents.md" "$HOME/.codex/AGENTS.md"
diff -u "$HOME/.orig/opencode-agents.md" "$HOME/.config/opencode/AGENTS.md"
"$tool_py" - <<'PY'
import json
from pathlib import Path

home = Path.home()
assert json.loads((home / ".config/opencode/opencode.json").read_text()) == json.loads(
    (home / ".orig/opencode.json").read_text()
)
PY
test -f "$HOME/.mnemo/mnemo.db"
mnemo verify | grep -q "3 events, history intact, HEADs consistent, 1 memory identities sealed"
echo "PASS: fresh Linux install end-to-end check succeeded (backend=$backend)"
USER_SCRIPT
chmod 0755 /tmp/check-user.sh
su - tester -c "MNEMO_CHECK_BACKEND='$MNEMO_CHECK_BACKEND' bash /tmp/check-user.sh"
CONTAINER_SCRIPT
