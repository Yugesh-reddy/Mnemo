"""mnemo-install: managed blocks, Codex TOML, OpenCode JSON, Claude registration, dry run
and uninstall."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import httpx
import pytest

import mnemo.install as inst


@pytest.fixture(autouse=True)
def _no_real_opencode(tmp_path, monkeypatch):
    """Every test detects "opencode" on PATH; keep its default config dir in tmp."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for name in ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"):
        monkeypatch.delenv(name, raising=False)


EXISTING_TOML = """model = "gpt-test"

[mcp_servers.vercel]
url = "https://mcp.vercel.com"

[projects."/Users/me/code"]
trust_level = "trusted"
"""


def test_blocks_append_replace_in_place_and_remove_cleanly():
    text = "# My notes\n\nKeep answers short.\n"
    once = inst.upsert_block(text, "policy v1", inst.MD_BEGIN, inst.MD_END)
    assert once.startswith(text) and once.endswith(f"{inst.MD_BEGIN}\npolicy v1\n{inst.MD_END}\n")
    assert inst.upsert_block(once, "policy v1", inst.MD_BEGIN, inst.MD_END) == once
    later = once + "\nAdded later.\n"
    replaced = inst.upsert_block(later, "policy v2", inst.MD_BEGIN, inst.MD_END)
    assert "policy v1" not in replaced and replaced.endswith(
        "policy v2\n" + inst.MD_END + "\n\nAdded later.\n"
    )
    assert inst.remove_block(replaced, inst.MD_BEGIN, inst.MD_END) == (
        "# My notes\n\nKeep answers short.\n\nAdded later.\n"
    )
    assert inst.remove_block(once, inst.MD_BEGIN, inst.MD_END) == text
    assert (
        inst.upsert_block("", "p", inst.MD_BEGIN, inst.MD_END)
        == f"{inst.MD_BEGIN}\np\n{inst.MD_END}\n"
    )


ENV = {"MNEMO_DSN": "postgresql://u:p@localhost/mnemo", "MNEMO_ACTOR": "codex"}


def test_codex_config_keeps_other_settings_and_is_idempotent():
    updated = inst.codex_config(EXISTING_TOML, "/opt/mnemo/bin/mnemo-mcp-direct", ENV)
    parsed = tomllib.loads(updated)
    assert parsed["model"] == "gpt-test"
    assert parsed["mcp_servers"]["vercel"]["url"] == "https://mcp.vercel.com"
    assert parsed["projects"]["/Users/me/code"]["trust_level"] == "trusted"
    assert parsed["mcp_servers"]["mnemo"] == {
        "command": "/opt/mnemo/bin/mnemo-mcp-direct",
        "default_tools_approval_mode": "approve",
        "env": ENV,
    }
    assert inst.codex_config(updated, "/opt/mnemo/bin/mnemo-mcp-direct", ENV) == updated
    assert inst.remove_block(updated, inst.TOML_BEGIN, inst.TOML_END) == EXISTING_TOML


def test_codex_config_refuses_an_unmanaged_mnemo_server():
    foreign = EXISTING_TOML + '\n[mcp_servers.mnemo]\ncommand = "something-else"\n'
    with pytest.raises(inst.InstallError, match="outside mnemo-install"):
        inst.codex_config(foreign, "/x", ENV)


def test_codex_home_prefers_env_then_a_pinning_wrapper(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "explicit"))
    assert inst.default_codex_home() == tmp_path / "explicit"
    monkeypatch.delenv("CODEX_HOME")
    wrapper = tmp_path / "bin" / "codex"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/bin/sh\nexport CODEX_HOME="${CODEX_HOME:-$HOME/.codex-secondary}"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(wrapper.parent))
    assert inst.default_codex_home() == Path.home() / ".codex-secondary"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert inst.default_codex_home() == Path.home() / ".codex"


class FakeCli:
    def __init__(self, registered: bool = False):
        self.registered, self.calls = registered, []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        code = 0
        if argv[:3] == ["claude", "mcp", "get"]:
            code = 0 if self.registered else 1
        return subprocess.CompletedProcess(argv, code, "", "")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(inst.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text(EXISTING_TOML)
    return claude, codex


def args(claude: Path, codex: Path, *extra: str, checks: bool = False) -> list[str]:
    base = ["--claude-dir", str(claude), "--codex-home", str(codex), "--skip-migrate"]
    base += ["--opencode-dir", str(claude.parent / "opencode")]
    return base + ([] if checks else ["--skip-checks"]) + list(extra)


def test_dry_run_shows_every_change_and_writes_nothing(homes, capsys):
    claude, codex = homes
    cli = FakeCli()
    assert inst.main(args(claude, codex, "--dry-run"), runner=cli) == 0
    out = capsys.readouterr().out
    assert "Register the mnemo MCP server for Claude Code" in out
    assert "+[mcp_servers.mnemo]" in out and "+## Shared memory (Mnemo)" in out
    assert not (claude / "CLAUDE.md").exists() and not (codex / "AGENTS.md").exists()
    assert (codex / "config.toml").read_text() == EXISTING_TOML
    assert cli.calls == [["claude", "mcp", "get", "mnemo"]]


def test_declining_the_prompt_changes_nothing(homes):
    claude, codex = homes
    cli = FakeCli()
    assert inst.main(args(claude, codex), runner=cli, ask=lambda _: "n") == 1
    assert not (claude / "CLAUDE.md").exists()
    assert cli.calls == [["claude", "mcp", "get", "mnemo"]]


def test_install_then_uninstall_round_trips(homes, capsys):
    claude, codex = homes
    cli = FakeCli()
    assert inst.main(args(claude, codex, "--yes"), runner=cli) == 0
    policy = inst.policy_text().strip()
    assert policy in (claude / "CLAUDE.md").read_text()
    assert policy in (codex / "AGENTS.md").read_text()
    server = tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]["mnemo"]
    assert server["env"]["MNEMO_ACTOR"] == "codex"
    add = cli.calls[-1]
    assert add[:4] == ["claude", "mcp", "add-json", "mnemo"] and add[-2:] == ["--scope", "user"]
    registered = json.loads(add[4])
    assert registered["env"]["MNEMO_ACTOR"] == "claude-code"
    assert registered["command"] == server["command"]
    assert {k: v for k, v in registered["env"].items() if k != "MNEMO_ACTOR"} == {
        k: v for k, v in server["env"].items() if k != "MNEMO_ACTOR"
    }

    # Reinstalling re-registers Claude but leaves the files as they are.
    again = FakeCli(registered=True)
    before = {p: p.read_text() for p in (claude / "CLAUDE.md", codex / "AGENTS.md")}
    assert inst.main(args(claude, codex, "--yes"), runner=again) == 0
    assert [c[:3] for c in again.calls] == [
        ["claude", "mcp", "get"],
        ["claude", "mcp", "remove"],
        ["claude", "mcp", "add-json"],
    ]
    assert {p: p.read_text() for p in before} == before

    gone = FakeCli(registered=True)
    assert inst.main(args(claude, codex, "--uninstall", "--yes"), runner=gone) == 0
    assert gone.calls[-1] == ["claude", "mcp", "remove", "mnemo", "--scope", "user"]
    assert (codex / "config.toml").read_text() == EXISTING_TOML
    assert (claude / "CLAUDE.md").read_text() == "" and (codex / "AGENTS.md").read_text() == ""


def test_an_unmanaged_codex_server_stops_the_install(homes, capsys):
    claude, codex = homes
    (codex / "config.toml").write_text(EXISTING_TOML + '\n[mcp_servers.mnemo]\ncommand = "x"\n')
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 2
    assert "outside mnemo-install" in capsys.readouterr().err
    assert not (claude / "CLAUDE.md").exists()


async def test_migrate_is_idempotent_on_a_migrated_database(_disposable_test_db):
    assert await inst.migrate(_disposable_test_db, inst.get_settings().embed_dim) == []


class FakeServices:
    def __init__(
        self,
        postgres=None,
        models=("nomic-embed-text:latest",),
        ollama_up=True,
        local_ready=True,
        mismatch=None,
    ):
        self.problem, self.models, self.up, self.pulled = postgres, list(models), ollama_up, []
        self.local_ready, self.mismatch, self.compared = local_ready, mismatch, []

    def postgres_problem(self, settings):
        return self.problem

    def ollama_models(self, host):
        if not self.up:
            raise httpx.ConnectError("refused")
        return self.models

    def pull(self, host, model):
        self.pulled.append((host, model))

    def local_model_ready(self, settings):
        return self.local_ready

    def download_local(self, settings):
        self.pulled.append(("local", settings.embed_model))
        self.local_ready = True

    def embedding_mismatch(self, settings, backend):
        self.compared.append((backend, settings.embed_model))
        return self.mismatch


@pytest.fixture
def ollama_settings(monkeypatch):
    for name in ("MNEMO_BACKEND", "MNEMO_EMBED_MODEL", "MNEMO_EMBED_DIM", "MNEMO_OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)
    inst.get_settings.cache_clear()
    yield
    inst.get_settings.cache_clear()


def test_postgres_down_stops_before_any_change(homes, ollama_settings, capsys):
    claude, codex = homes
    cli = FakeCli()
    probe = FakeServices(postgres="Can't reach Postgres at localhost:5432/mnemo. Run `make up`.")
    assert inst.main(args(claude, codex, "--yes", checks=True), runner=cli, probe=probe) == 2
    out = capsys.readouterr().out
    assert "FIX  Can't reach Postgres" in out and "run mnemo-install again" in out
    assert cli.calls == [] and not (claude / "CLAUDE.md").exists()


def test_ollama_down_suggests_starting_it_or_the_hash_backend(homes, ollama_settings, capsys):
    claude, codex = homes
    probe = FakeServices(ollama_up=False)
    assert (
        inst.main(args(claude, codex, "--dry-run", checks=True), runner=FakeCli(), probe=probe) == 2
    )
    out = capsys.readouterr().out
    assert "ollama serve" in out and "--backend hash" in out
    probe = FakeServices(ollama_up=False)
    assert (
        inst.main(
            args(claude, codex, "--dry-run", "--backend", "hash", checks=True),
            runner=FakeCli(),
            probe=probe,
        )
        == 0
    )


def test_a_missing_model_is_downloaded_only_after_confirmation(homes, ollama_settings, capsys):
    claude, codex = homes
    probe = FakeServices(models=["qwen3.5:4b-mlx"])
    assert (
        inst.main(
            args(claude, codex, checks=True), runner=FakeCli(), probe=probe, ask=lambda _: "n"
        )
        == 1
    )
    assert probe.pulled == []
    assert "Download the embedding model nomic-embed-text" in capsys.readouterr().out
    assert inst.main(args(claude, codex, "--yes", checks=True), runner=FakeCli(), probe=probe) == 0
    assert probe.pulled == [("http://localhost:11434", "nomic-embed-text")]
    assert "ok   Postgres at" in capsys.readouterr().out


def test_an_installed_model_is_not_downloaded_again(homes, ollama_settings):
    claude, codex = homes
    probe = FakeServices()
    assert inst.main(args(claude, codex, "--yes", checks=True), runner=FakeCli(), probe=probe) == 0
    assert probe.pulled == []


def test_local_backend_downloads_its_model_and_pins_it_for_the_agents(
    homes, ollama_settings, capsys
):
    claude, codex = homes
    probe = FakeServices(ollama_up=False, local_ready=False)  # no Ollama needed
    code = inst.main(
        args(claude, codex, "--backend", "local", checks=True),
        runner=FakeCli(),
        probe=probe,
        ask=lambda _: "n",
    )
    assert code == 1 and probe.pulled == []
    out = capsys.readouterr().out
    assert "Download the embedding model BAAI/bge-small-en-v1.5 (about 67 MB)" in out
    cli = FakeCli()
    code = inst.main(
        args(claude, codex, "--backend", "local", "--yes", checks=True), runner=cli, probe=probe
    )
    assert code == 0
    assert probe.pulled == [("local", "BAAI/bge-small-en-v1.5")]
    assert probe.compared == [("local", "BAAI/bge-small-en-v1.5")]
    env = tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]["mnemo"]["env"]
    assert env["MNEMO_BACKEND"] == "local" and env["MNEMO_EMBED_DIM"] == "384"
    assert env["MNEMO_EMBED_MODEL"] == "BAAI/bge-small-en-v1.5" and env["MNEMO_MODEL_DIR"]


def test_memories_from_another_model_stop_the_install(homes, ollama_settings, capsys):
    claude, codex = homes
    problem = "The memories in localhost:5432/mnemo were embedded by a different model"
    probe = FakeServices(mismatch=problem)
    cli = FakeCli()
    code = inst.main(args(claude, codex, "--yes", checks=True), runner=cli, probe=probe)
    assert code == 2 and f"FIX  {problem}" in capsys.readouterr().out
    assert [c[:3] for c in cli.calls] == [["claude", "mcp", "get"]]  # nothing registered
    assert not (claude / "CLAUDE.md").exists()
    assert (codex / "config.toml").read_text() == EXISTING_TOML
    assert inst.main(args(claude, codex, "--dry-run", checks=True), runner=cli, probe=probe) == 2


def test_claude_auto_memory_can_be_turned_off_and_back_on(homes, capsys):
    claude, codex = homes
    claude.mkdir()
    (claude / "settings.json").write_text('{\n  "theme": "dark",\n  "model": "opus"\n}\n')
    assert inst.main(args(claude, codex, "--dry-run"), runner=FakeCli()) == 0
    assert "--claude-auto-memory off turns it off" in capsys.readouterr().out
    assert (
        inst.main(args(claude, codex, "--yes", "--claude-auto-memory", "off"), runner=FakeCli())
        == 0
    )
    data = json.loads((claude / "settings.json").read_text())
    assert data == {"theme": "dark", "model": "opus", "autoMemoryEnabled": False}
    capsys.readouterr()
    assert inst.main(args(claude, codex, "--uninstall", "--dry-run"), runner=FakeCli()) == 0
    assert "--claude-auto-memory on" in capsys.readouterr().out
    assert (
        inst.main(
            args(claude, codex, "--uninstall", "--yes", "--claude-auto-memory", "on"),
            runner=FakeCli(),
        )
        == 0
    )
    assert json.loads((claude / "settings.json").read_text()) == {"theme": "dark", "model": "opus"}


def test_invalid_claude_settings_stop_the_install(homes, capsys):
    claude, codex = homes
    claude.mkdir()
    (claude / "settings.json").write_text("{not json")
    code = inst.main(args(claude, codex, "--yes", "--claude-auto-memory", "off"), runner=FakeCli())
    assert code == 2 and "not valid JSON" in capsys.readouterr().err
    assert (claude / "settings.json").read_text() == "{not json"


@pytest.mark.parametrize("checkout", [True, False])
def test_running_from_a_checkout_is_flagged(homes, monkeypatch, capsys, checkout):
    claude, codex = homes
    monkeypatch.setattr(inst, "running_from_checkout", lambda: checkout)
    assert inst.main(args(claude, codex, "--dry-run"), runner=FakeCli()) == 0
    assert ("moving or deleting it breaks them" in capsys.readouterr().out) is checkout


def test_this_test_run_is_from_the_checkout():
    assert inst.running_from_checkout()


async def test_migrate_checks_the_dimension_it_is_given_not_the_global_one(_disposable_test_db):
    # Reinstalling with --backend local (384 dimensions) while the process-wide settings
    # say 768 must migrate the 384-dimensional store, not reject it.
    from urllib.parse import urlsplit, urlunsplit
    from uuid import uuid4

    import asyncpg

    assert inst.get_settings().embed_dim == 768
    parts = urlsplit(_disposable_test_db)
    name = "mnemo_dim_" + uuid4().hex[:12]
    admin = await asyncpg.connect(urlunsplit(parts._replace(path="/postgres")))
    await admin.execute(f'CREATE DATABASE "{name}"')
    try:
        dsn = urlunsplit(parts._replace(path="/" + name))
        assert await inst.migrate(dsn, 384)  # fresh store, created at 384
        assert await inst.migrate(dsn, 384) == []
        with pytest.raises(ValueError, match="uses 384 dimensions; configured embed_dim is 768"):
            await inst.migrate(dsn, 768)
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


# ---- OpenCode ---------------------------------------------------------------

OPENCODE = {"model": "anthropic/claude-x"}


def opencode_server(opencode: Path) -> dict:
    return json.loads((opencode / "opencode.json").read_text())["mcp"]["mnemo"]


def test_opencode_gets_a_server_and_its_own_policy_file(homes, tmp_path):
    claude, codex = homes
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    (opencode / "opencode.json").write_text(json.dumps(OPENCODE))
    (opencode / "AGENTS.md").write_text("# Mine\n")
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 0
    server = opencode_server(opencode)
    assert server["type"] == "local" and server["command"] == [inst.server_command()]
    assert server["environment"]["MNEMO_ACTOR"] == "opencode"
    assert json.loads((opencode / "opencode.json").read_text())["model"] == "anthropic/claude-x"
    assert inst.policy_text().strip() in (opencode / "AGENTS.md").read_text()
    assert not (claude / "CLAUDE.md").exists()

    before = (opencode / "opencode.json").read_text()
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 0
    assert (opencode / "opencode.json").read_text() == before

    assert (
        inst.main(
            args(claude, codex, "--agent", "opencode", "--uninstall", "--yes"), runner=FakeCli()
        )
        == 0
    )
    assert json.loads((opencode / "opencode.json").read_text()) == OPENCODE
    assert (opencode / "AGENTS.md").read_text() == "# Mine\n"


def test_opencode_reading_claude_md_gets_the_policy_there(homes, tmp_path, capsys):
    """Creating OpenCode's AGENTS.md would stop it reading ~/.claude/CLAUDE.md."""
    claude, codex = homes
    claude.mkdir()
    (claude / "CLAUDE.md").write_text("# Claude rules OpenCode also follows\n")
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 0
    opencode = tmp_path / "opencode"
    assert not (opencode / "AGENTS.md").exists()
    assert inst.policy_text().strip() in (claude / "CLAUDE.md").read_text()
    assert "OpenCode reads" in capsys.readouterr().out
    assert opencode_server(opencode)["environment"]["MNEMO_ACTOR"] == "opencode"


def test_opencode_is_installed_by_default_only_when_detected(homes, tmp_path, monkeypatch):
    claude, codex = homes
    monkeypatch.setattr(
        inst.shutil, "which", lambda name: None if name == "opencode" else f"/bin/{name}"
    )
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    assert not (tmp_path / "opencode").exists()
    (tmp_path / "opencode").mkdir()
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    assert opencode_server(tmp_path / "opencode")["environment"]["MNEMO_ACTOR"] == "opencode"


def test_opencode_jsonc_with_comments_is_never_rewritten(homes, tmp_path, capsys):
    claude, codex = homes
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    jsonc = '{\n  // my theme\n  "theme": "dark",\n}\n'
    (opencode / "opencode.jsonc").write_text(jsonc)
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 2
    err = capsys.readouterr().err
    assert "comments or trailing commas" in err and '"mnemo"' in err
    assert (opencode / "opencode.jsonc").read_text() == jsonc
    assert not (opencode / "opencode.json").exists()

    # Once the user pastes the entry, a reinstall leaves the file alone.
    entry = inst.opencode_entry(
        inst.server_command(), inst.server_env(inst.get_settings(), "opencode", "ollama")
    )
    pasted = jsonc.replace(
        '"theme": "dark",',
        '"theme": "dark",\n  "mcp": ' + json.dumps({"servers": {"mnemo": entry}}) + ",",
    )
    (opencode / "opencode.jsonc").write_text(pasted)
    assert (
        inst.main(
            args(claude, codex, "--agent", "opencode", "--backend", "ollama", "--yes"),
            runner=FakeCli(),
        )
        == 0
    )
    assert (opencode / "opencode.jsonc").read_text() == pasted


@pytest.mark.parametrize(
    "config",
    [
        {"mcp": {"servers": {"mnemo": {"type": "local", "command": ["other-server"]}}}},
        {"mcp": {"mnemo": {"type": "local", "command": ["something-else"]}}},
    ],
)
def test_an_unmanaged_opencode_server_stops_the_install(homes, tmp_path, capsys, config):
    claude, codex = homes
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    (opencode / "opencode.json").write_text(json.dumps(config))
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 2
    assert "did not write" in capsys.readouterr().err
    assert json.loads((opencode / "opencode.json").read_text()) == config


def test_strip_jsonc_keeps_strings_intact():
    text = '{ // c\n "a": "x,}//y /* z */", /* b */ "l": [1, 2,],\n}'
    assert json.loads(inst.strip_jsonc(text)) == {"a": "x,}//y /* z */", "l": [1, 2]}


def test_a_detected_opencode_config_it_cannot_rewrite_skips_only_opencode(homes, tmp_path, capsys):
    """Commented JSONC is common; it must not stop Claude Code and Codex being set up."""
    claude, codex = homes
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    jsonc = '{\n  // my theme\n  "theme": "dark",\n}\n'
    (opencode / "opencode.jsonc").write_text(jsonc)
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    out = capsys.readouterr().out
    assert "Skipped OpenCode" in out and '"mnemo"' in out
    assert (opencode / "opencode.jsonc").read_text() == jsonc
    assert not (opencode / "AGENTS.md").exists()
    assert inst.policy_text().strip() in (claude / "CLAUDE.md").read_text()
    assert "mnemo" in tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]

    # The same applies to uninstalling: the pasted entry is left with a note.
    entry = inst.opencode_entry(
        inst.server_command(), inst.server_env(inst.get_settings(), "opencode", "ollama")
    )
    pasted = '{\n  // my theme\n  "mcp": ' + json.dumps({"servers": {"mnemo": entry}}) + ",\n}\n"
    (opencode / "opencode.jsonc").write_text(pasted)
    assert inst.main(args(claude, codex, "--uninstall", "--yes"), runner=FakeCli()) == 0
    assert "Skipped OpenCode" in capsys.readouterr().out
    assert (opencode / "opencode.jsonc").read_text() == pasted
    assert (codex / "config.toml").read_text() == EXISTING_TOML


def test_opencode_entry_follows_the_layout_the_file_already_uses(homes, tmp_path):
    """Directly under "mcp" works on every OpenCode; "mcp.servers" only on newer ones."""
    claude, codex = homes
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    other = {"type": "local", "command": ["other-server"]}
    run = lambda *extra: inst.main(  # noqa: E731
        args(claude, codex, "--agent", "opencode", "--yes", *extra), runner=FakeCli()
    )

    flat = {"mcp": {"other": other, "servers": {"type": "local", "command": ["named-servers"]}}}
    (opencode / "opencode.json").write_text(json.dumps(flat))
    assert run() == 0
    written = json.loads((opencode / "opencode.json").read_text())["mcp"]
    assert written["mnemo"]["type"] == "local" and written["servers"] == flat["mcp"]["servers"]
    assert run("--uninstall") == 0
    assert json.loads((opencode / "opencode.json").read_text()) == flat

    nested = {"mcp": {"servers": {"other": other}, "timeout": {"startup": 1000}}}
    (opencode / "opencode.json").write_text(json.dumps(nested))
    assert run() == 0
    written = json.loads((opencode / "opencode.json").read_text())["mcp"]
    assert set(written["servers"]) == {"other", "mnemo"} and "mnemo" not in written
    assert run("--uninstall") == 0
    assert json.loads((opencode / "opencode.json").read_text()) == nested


def test_a_malformed_opencode_mcp_setting_stops_cleanly(homes, tmp_path, capsys):
    claude, codex = homes
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    (opencode / "opencode.json").write_text('{"mcp": []}')
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 2
    assert "is not an object" in capsys.readouterr().err
    assert (opencode / "opencode.json").read_text() == '{"mcp": []}'


def test_uninstall_removes_the_opencode_rules_file_it_would_leave_empty(homes, tmp_path):
    """An empty AGENTS.md would keep OpenCode from falling back to ~/.claude/CLAUDE.md."""
    claude, codex = homes
    opencode = tmp_path / "opencode"
    assert inst.main(args(claude, codex, "--agent", "opencode", "--yes"), runner=FakeCli()) == 0
    assert inst.policy_text().strip() in (opencode / "AGENTS.md").read_text()
    assert (
        inst.main(
            args(claude, codex, "--agent", "opencode", "--uninstall", "--yes"), runner=FakeCli()
        )
        == 0
    )
    assert not (opencode / "AGENTS.md").exists()
    assert json.loads((opencode / "opencode.json").read_text()) == {}
