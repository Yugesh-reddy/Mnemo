"""The packaging check must never touch the configured (real) store."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "check-wheel.sh").read_text()


def test_wheel_check_points_every_store_step_at_a_scratch_file():
    export_at = SCRIPT.index("export MNEMO_DB")
    assert 'MNEMO_DB="$mnemo_wheel_env/' in SCRIPT
    for step in ('mnemo-migrate"', "examples/direct_memory.py", "mnemo-mcp-direct"):
        assert SCRIPT.index(step) > export_at, step
    assert "trap cleanup EXIT" in SCRIPT and 'rm -rf "$mnemo_wheel_env"' in SCRIPT
    assert "MNEMO_DSN" not in SCRIPT and "asyncpg" not in SCRIPT


def test_readme_links_work_on_pypi():
    """PyPI renders README.md without the repository, so relative links would 404."""
    import re

    readme = (ROOT / "README.md").read_text()
    links = re.findall(r"\]\(([^)\s]+)\)", readme)
    relative = [link for link in links if not re.match(r"(https?:|mailto:|#)", link)]
    assert not relative, relative
    base = "https://github.com/Yugesh-reddy/Mnemo/blob/main/"
    if not (ROOT / ".git").exists():
        return  # the source archive leaves out some linked files
    for link in links:
        if link.startswith(base):
            assert (ROOT / link.removeprefix(base).split("#")[0]).exists(), link
