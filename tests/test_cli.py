"""The `mnemo` command: database lifecycle, agent install passthrough, and the UI."""

from __future__ import annotations

from pathlib import Path

import pytest

import mnemo.cli as cli


class Recorder:
    def __init__(self, code: int = 0):
        self.code, self.calls = code, []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.code


@pytest.fixture
def no_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    return tmp_path


def test_compose_file_is_found_in_the_checkout_or_the_package():
    assert cli.COMPOSE_FILE.name == "docker-compose.yml" and cli.COMPOSE_FILE.is_file()
    assert "127.0.0.1:${MNEMO_DB_PORT:-5432}:5432" in cli.COMPOSE_FILE.read_text()


def test_up_and_down_run_compose_under_one_project(no_dotenv):
    run = Recorder()
    assert cli.main(["up"], runner=run) == 0
    assert cli.main(["down"], runner=run) == 0
    compose = ["docker", "compose", "-f", str(cli.COMPOSE_FILE), "-p", "mnemo"]
    assert run.calls == [compose + ["up", "-d", "--wait"], compose + ["down"]]


def test_up_reads_a_dotenv_in_the_working_directory(no_dotenv):
    (no_dotenv / ".env").write_text("MNEMO_DB_PORT=5433\n")
    run = Recorder()
    cli.main(["up"], runner=run)
    assert run.calls[0][-5:] == ["--env-file", str(no_dotenv / ".env"), "up", "-d", "--wait"]


def test_up_without_docker_says_what_to_install(no_dotenv, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    run = Recorder()
    assert cli.main(["up"], runner=run) == 2
    assert run.calls == [] and "Docker" in capsys.readouterr().err


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


def test_the_package_declares_the_command_and_ships_the_compose_file():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["mnemo"] == "mnemo.cli:main"
    included = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert included["docker-compose.yml"] == "mnemo/docker-compose.yml"
