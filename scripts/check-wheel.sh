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
import mnemo.eval
import mnemo.worker
import mnemo.mcp_server
from mnemo.db import MIGRATIONS_DIR
from web.app import TEMPLATES
assert len(list(MIGRATIONS_DIR.glob('*.sql'))) >= 7
assert files('mnemo').joinpath('data/longmemeval-car.json').is_file()
assert TEMPLATES.env.get_template('list.html')
assert len(mnemo.eval.load_benchmark_dataset('all').turns) == 200
print('Installed wheel: runtime imports, migrations, data and templates verified')
PY
"$mnemo_wheel_env/venv/bin/mnemo-eval" --json > eval.json
"$mnemo_wheel_env/venv/bin/mnemo-worker" --help
"$mnemo_wheel_env/venv/bin/mnemo-migrate"
