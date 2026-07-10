"""The packaging check must never touch the configured (real) database."""

from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "check-wheel.sh").read_text()


def test_wheel_check_points_every_database_step_at_a_scratch_database():
    export_at = SCRIPT.index("export MNEMO_DSN")
    for step in ('mnemo-eval" --json', 'mnemo-migrate"', "examples.direct_memory"):
        assert SCRIPT.index(step) > export_at, step
    assert "trap cleanup EXIT" in SCRIPT and "DROP DATABASE IF EXISTS" in SCRIPT
