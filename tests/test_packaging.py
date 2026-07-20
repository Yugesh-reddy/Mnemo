"""The packaging check must never touch the configured (real) store."""

from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "check-wheel.sh").read_text()


def test_wheel_check_points_every_store_step_at_a_scratch_file():
    export_at = SCRIPT.index("export MNEMO_DB")
    assert 'MNEMO_DB="$mnemo_wheel_env/' in SCRIPT
    for step in ('mnemo-migrate"', "examples.direct_memory", "mnemo-mcp-direct"):
        assert SCRIPT.index(step) > export_at, step
    assert "trap cleanup EXIT" in SCRIPT and 'rm -rf "$mnemo_wheel_env"' in SCRIPT
    assert "MNEMO_DSN" not in SCRIPT and "asyncpg" not in SCRIPT
