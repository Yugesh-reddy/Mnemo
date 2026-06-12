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
manifest = files('mnemo').joinpath('data/quality-v2/manifest.json')
assert len(mnemo.eval_suite.load_cases(manifest, 'dev')) == 3
assert len(mnemo.eval_suite.load_cases(manifest, 'holdout')) == 1
print('Installed wheel: runtime imports, migrations, data and templates verified')
PY
"$mnemo_wheel_env/venv/bin/mnemo-eval" --json > eval.json
"$mnemo_wheel_env/venv/bin/mnemo-worker" --help
"$mnemo_wheel_env/venv/bin/mnemo-audit-eval" --help
"$mnemo_wheel_env/venv/bin/mnemo-eval-suite" --help
"$mnemo_wheel_env/venv/bin/mnemo-benchmark" --help
"$mnemo_wheel_env/venv/bin/mnemo-migrate"
