#!/usr/bin/env bash
# Installs the built wheel into a fresh environment and exercises it against a
# throwaway store file inside that environment, removed on exit; it never touches the
# configured (real) store.
set -euo pipefail
mnemo_checkout="$(pwd)"
mnemo_wheel_env="$(mktemp -d)"

cleanup() {
  rm -rf "$mnemo_wheel_env"
}
trap cleanup EXIT
uv venv "$mnemo_wheel_env/venv" --python 3.12
uv pip install --python "$mnemo_wheel_env/venv/bin/python" "$mnemo_checkout"/dist/*.whl
cd "$mnemo_wheel_env"
MNEMO_DB="$mnemo_wheel_env/store/mnemo.db"
export MNEMO_DB
"$mnemo_wheel_env/venv/bin/python" - <<'PY'
from importlib.resources import files
import json
import mnemo.mcp_direct
from mnemo.db import MIGRATIONS_DIR
from web.app import TEMPLATES
assert MIGRATIONS_DIR.joinpath('0001_sqlite_init.sql').is_file()
assert TEMPLATES.env.get_template('list.html')
from web.app import STATIC
import sqlite_vec  # the vector extension loads from the installed wheel
assert STATIC.joinpath('app.css').is_file() and STATIC.joinpath('htmx.js').is_file()
assert 'site-packages' in MIGRATIONS_DIR.parts  # the packaged copy, not the checkout
assert files('mnemo').joinpath('memory_policy.md').is_file()
print('Installed wheel: runtime imports, migrations and templates verified')
PY
"$mnemo_wheel_env/venv/bin/mnemo-mcp-direct" --version
"$mnemo_wheel_env/venv/bin/mnemo" --help >/dev/null
"$mnemo_wheel_env/venv/bin/mnemo" install --help >/dev/null
"$mnemo_wheel_env/venv/bin/python" -m examples.agent_undo --help
"$mnemo_wheel_env/venv/bin/mnemo-migrate"
"$mnemo_wheel_env/venv/bin/python" -c "
import os, stat, sys
path = os.environ['MNEMO_DB']
modes = (stat.S_IMODE(os.stat(path).st_mode), stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode))
sys.exit(0 if modes == (0o600, 0o700) else f'store {path} modes {modes}, expected (0o600, 0o700)')"
MNEMO_BACKEND=hash \
  "$mnemo_wheel_env/venv/bin/python" -m examples.direct_memory
MNEMO_BACKEND=hash \
  "$mnemo_wheel_env/venv/bin/python" - <<'PY'
import asyncio
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mnemo import ErrorCode, Mnemo, MnemoError, MutationResult
from mnemo.config import get_settings
from mnemo.embedder import HashEmbedder

settings = get_settings()
namespace = 'wheel-direct-' + uuid4().hex
client = Mnemo(settings.db, HashEmbedder(settings.embed_dim), namespace=namespace)
first = client.direct.create('user', 'preferred_database', 'PostgreSQL', request_id=uuid4())
assert isinstance(first, MutationResult)
changed = client.direct.update(
    first.fact_id, 'MySQL', expected_event_id=first.event_id, request_id=uuid4())
try:
    client.direct.update(
        first.fact_id, 'SQLite', expected_event_id=first.event_id, request_id=uuid4())
except MnemoError as error:
    assert error.code == ErrorCode.REVISION_CONFLICT
else:
    raise AssertionError('A stale SDK mutation was accepted')
restored = client.direct.revert(
    first.fact_id, first.event_id, expected_event_id=changed.event_id, request_id=uuid4())
restarted = Mnemo(settings.db, HashEmbedder(settings.embed_dim), namespace=namespace)
retry = restarted.direct.revert(
    first.fact_id, first.event_id, expected_event_id=changed.event_id,
    request_id=restored.request_id)
assert retry.replayed and retry.event_id == restored.event_id
assert restarted.direct.get(first.fact_id).value == 'PostgreSQL'
assert [entry.op for entry in restarted.direct.history(first.fact_id).entries] == [
    'REVERT', 'UPDATE', 'ADD']
print('Installed wheel: guarded SDK lifecycle, revision conflict and receipt replay verified')

async def check_stdio():
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name('mnemo-mcp-direct')),
        env={**os.environ, 'MNEMO_NAMESPACE': namespace,
             'MNEMO_USER_ID': 'default', 'MNEMO_AGENT_ID': 'default'})
    async with asyncio.timeout(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 7
                got = await session.call_tool('memory_get', {'fact_id': str(first.fact_id)})
                assert not got.isError
                assert json.loads(got.content[0].text)['current_event_id'] == str(restored.event_id)
                bad = await session.call_tool('memory_get', {'fact_id': 'bad-uuid'})
                assert bad.isError and json.loads(bad.content[0].text)['code'] == 'INVALID_INPUT'
                stale = await session.call_tool('memory_update', {
                    'fact_id': str(first.fact_id), 'value': 'SQLite',
                    'expected_event_id': str(first.event_id), 'request_id': str(uuid4())})
                assert stale.isError
                assert json.loads(stale.content[0].text)['code'] == 'REVISION_CONFLICT'

asyncio.run(check_stdio())
print('Installed wheel: direct MCP console entry point, seven tools and JSON errors verified')
PY

# The source archive must support the same locked install as a fresh checkout.
mkdir "$mnemo_wheel_env/source"
tar -xzf "$mnemo_checkout"/dist/*.tar.gz -C "$mnemo_wheel_env/source" --strip-components=1
cd "$mnemo_wheel_env/source"
UV_PYTHON=3.12 make install
