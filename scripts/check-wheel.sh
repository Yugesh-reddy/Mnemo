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
from mnemo.db import MIGRATIONS_DIR
from web.app import TEMPLATES
assert len(list(MIGRATIONS_DIR.glob('*.sql'))) >= 7
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
"$mnemo_wheel_env/venv/bin/mnemo-audit-eval" --help
"$mnemo_wheel_env/venv/bin/mnemo-eval-suite" --help
"$mnemo_wheel_env/venv/bin/mnemo-benchmark" --help
"$mnemo_wheel_env/venv/bin/mnemo-migrate"

# The source archive must support the same locked install as a fresh checkout.
mkdir "$mnemo_wheel_env/source"
tar -xzf "$mnemo_checkout"/dist/*.tar.gz -C "$mnemo_wheel_env/source" --strip-components=1
cd "$mnemo_wheel_env/source"
UV_PYTHON=3.12 make install
