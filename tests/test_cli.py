"""The `mnemo` command: agent install passthrough, the UI, and keeping the store."""

from __future__ import annotations

from pathlib import Path

import pytest

import mnemo.cli as cli


@pytest.mark.parametrize("gone", ["up", "down"])
def test_up_and_down_explain_that_docker_is_no_longer_needed(gone, capsys):
    assert cli.main([gone]) == 0
    err = capsys.readouterr().err
    assert "no longer" in err and "Postgres" in err


def test_migrate_reindex_export_and_import_work_on_the_configured_file(
    tmp_path, monkeypatch, capsys
):
    from mnemo.config import get_settings

    path = tmp_path / "store" / "mnemo.db"
    monkeypatch.setenv("MNEMO_DB", str(path))
    monkeypatch.setenv("MNEMO_BACKEND", "hash")
    monkeypatch.setenv("MNEMO_NAMESPACE", "cli")
    get_settings.cache_clear()
    try:
        assert cli.main(["migrate"]) == 0
        assert "0001_sqlite_init.sql" in capsys.readouterr().out
        assert cli.main(["reindex"]) == 0
        assert "Embedded 0 of 0 events" in capsys.readouterr().out
        backup = tmp_path / "backup.json"
        assert cli.main(["export", "--out", str(backup)]) == 0
        assert '"format_version": 2' in backup.read_text()
        monkeypatch.setenv("MNEMO_DB", str(tmp_path / "restored.db"))
        get_settings.cache_clear()
        assert cli.main(["import", str(backup)]) == 0
        out = capsys.readouterr().out
        assert "Imported 0 facts" in out and "mnemo install" in out
    finally:
        get_settings.cache_clear()


def test_install_and_uninstall_pass_their_options_through(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "install_main", lambda argv: seen.append(list(argv)) or 0)
    assert cli.main(["install", "--dry-run", "--agent", "codex"]) == 0
    assert cli.main(["uninstall", "--yes"]) == 0
    assert cli.main(["install", "--help"]) == 0
    assert seen == [["--dry-run", "--agent", "codex"], ["--uninstall", "--yes"], ["--help"]]


def test_ui_serves_on_localhost(monkeypatch):
    served = {}
    monkeypatch.setattr(cli, "serve_ui", lambda port: served.setdefault("port", port) and 0)
    cli.main(["ui", "--port", "8123"])
    assert served == {"port": 8123}


def test_the_package_declares_the_command():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["mnemo"] == "mnemo.cli:main"
    included = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert "docker-compose.yml" not in included
    assert not (root / "docker-compose.yml").exists()


def test_help_lists_only_current_commands(capsys):
    import pytest

    import mnemo.cli as cli

    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "verify" in out and "SUPPRESS" not in out
    assert not [line for line in out.splitlines() if line.split()[:1] in (["up"], ["down"])]


def test_opencode_dir_expands_a_tilde_in_xdg_config_home(monkeypatch):
    from pathlib import Path

    import mnemo.install as inst

    monkeypatch.setenv("XDG_CONFIG_HOME", "~/conf")
    assert inst.default_opencode_dir() == Path.home() / "conf" / "opencode"
