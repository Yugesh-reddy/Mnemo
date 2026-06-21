#!/usr/bin/env bash
set -euo pipefail
mnemo_checkout="$(pwd)"
mnemo_wheel_env="$(mktemp -d)"
trap 'rm -rf "$mnemo_wheel_env"' EXIT
uv venv "$mnemo_wheel_env/venv" --python 3.12
uv pip install --python "$mnemo_wheel_env/venv/bin/python" "$mnemo_checkout"/dist/*.whl
cd "$mnemo_wheel_env"
"$mnemo_wheel_env/venv/bin/python" - <<'PY'
from importlib.resources import files
import json
import mnemo.eval
import mnemo.eval_suite
import mnemo.benchmark
import mnemo.worker
import mnemo.mcp_server
import mnemo.mcp_direct
from mnemo.db import MIGRATIONS_DIR
from web.app import TEMPLATES
assert len(list(MIGRATIONS_DIR.glob('*.sql'))) >= 7
assert MIGRATIONS_DIR.joinpath('0010_mutation_receipts.sql').is_file()
assert files('mnemo').joinpath('data/longmemeval-car.json').is_file()
assert TEMPLATES.env.get_template('list.html')
assert len(mnemo.eval.load_benchmark_dataset('all').turns) == 200
assert len(mnemo.eval.load_naturalistic_dataset().turns) == 200
for version in ('v2', 'v3', 'v4'):
    directory = files('mnemo').joinpath(f'data/quality-{version}')
    manifest = directory.joinpath('manifest.json')
    assert len(mnemo.eval_suite.load_cases(manifest, 'dev')) == 3
    # Verify packaged resources without opening held-out source/label contents.
    for record in json.loads(manifest.read_text())['records']:
        assert directory.joinpath(record['path']).is_file()
        if 'labels' in record:
            assert directory.joinpath(record['labels']).is_file()
print('Installed wheel: runtime imports, migrations, data and templates verified')
PY
"$mnemo_wheel_env/venv/bin/mnemo-eval" --json > eval.json
"$mnemo_wheel_env/venv/bin/mnemo-worker" --help
"$mnemo_wheel_env/venv/bin/mnemo-mcp-direct" --version
"$mnemo_wheel_env/venv/bin/python" -m examples.agent_undo --help
"$mnemo_wheel_env/venv/bin/mnemo-audit-eval" --help
"$mnemo_wheel_env/venv/bin/mnemo-eval-suite" --help
"$mnemo_wheel_env/venv/bin/mnemo-benchmark" --help
"$mnemo_wheel_env/venv/bin/mnemo-migrate"
MNEMO_BACKEND=hash MNEMO_WORKER_ENABLED=false \
  "$mnemo_wheel_env/venv/bin/python" -m examples.direct_memory
MNEMO_BACKEND=hash MNEMO_WORKER_ENABLED=false \
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
client = Mnemo(settings.dsn, HashEmbedder(settings.embed_dim), namespace=namespace)
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
restarted = Mnemo(settings.dsn, HashEmbedder(settings.embed_dim), namespace=namespace)
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
                assert len((await session.list_tools()).tools) == 6
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
print('Installed wheel: direct MCP console entry point, six tools and JSON errors verified')
PY

# The source archive must support the same locked install as a fresh checkout.
mkdir "$mnemo_wheel_env/source"
tar -xzf "$mnemo_checkout"/dist/*.tar.gz -C "$mnemo_wheel_env/source" --strip-components=1
cd "$mnemo_wheel_env/source"
UV_PYTHON=3.12 make install
