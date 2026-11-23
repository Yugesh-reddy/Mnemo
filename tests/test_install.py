"""mnemo-install: managed blocks, Codex TOML, OpenCode JSON, Claude registration, dry run
and uninstall."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import httpx
import pytest

import mnemo.install as inst
from mnemo.db import migration_names


@pytest.fixture(autouse=True)
def _no_real_opencode(tmp_path, monkeypatch):
    """Every test detects "opencode" on PATH; keep its default config dir, and the
    store file, in tmp. HOME is a temporary folder too, so no default path (Cursor's,
    Claude Desktop's) can reach the real configs on the machine running the tests."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "home" / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "home" / "AppData" / "Local"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MNEMO_DB", str(tmp_path / "store" / "mnemo.db"))
    inst.get_settings.cache_clear()
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


ENV = {"MNEMO_DB": "/home/me/.mnemo/mnemo.db", "MNEMO_ACTOR": "codex"}


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


def test_codex_home_prefers_env_else_dot_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "explicit"))
    assert inst.default_codex_home() == tmp_path / "explicit"
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert inst.default_codex_home() == Path.home() / ".codex"


@pytest.mark.skipif(os.name != "posix", reason="POSIX shell wrapper")
def test_codex_home_detects_a_pinning_shell_wrapper(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    wrapper = tmp_path / "bin" / "codex"
    wrapper.parent.mkdir()
    wrapper.write_text('#!/bin/sh\nexport CODEX_HOME="${CODEX_HOME:-$HOME/.codex-secondary}"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(wrapper.parent))
    assert inst.default_codex_home() == Path.home() / ".codex-secondary"


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
    real_which = inst.shutil.which

    def fake_which(name: str, mode: int = os.F_OK | os.X_OK, path: str | None = None):
        if path is not None or os.path.dirname(name):
            return real_which(name, mode=mode, path=path)
        return f"/usr/local/bin/{name}"

    monkeypatch.setattr(inst.shutil, "which", fake_which)
    claude, codex = tmp_path / "claude", tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text(EXISTING_TOML, encoding="utf-8")
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


# Shaped like real credentials so both the name and the shape rules are exercised.
GITHUB_PAT = "github_pat_11AAAAAAA0exampleexampleexample"
MEM0_KEY = "m0-exampleexampleexample"
OTHER_KEY = "sk-proj-exampleexampleexample"


def test_the_preview_hides_credentials_and_the_files_keep_them(homes, tmp_path, capsys):
    claude, codex = homes
    toml = EXISTING_TOML + (
        "\n[mcp_servers.github.env]\n"
        f"GITHUB_PERSONAL_ACCESS_TOKEN = '{GITHUB_PAT}'\n"
        f"MEM0_API_KEY = '{MEM0_KEY}'\n"
    )
    (codex / "config.toml").write_text(toml, encoding="utf-8")
    opencode = tmp_path / "opencode"
    opencode.mkdir()
    other = {"type": "local", "command": ["x"], "environment": {"OPENAI_API_KEY": OTHER_KEY}}
    (opencode / "opencode.json").write_text(json.dumps({**OPENCODE, "mcp": {"other": other}}))

    assert (
        inst.main(
            args(claude, codex, "--dry-run", "--agent", "codex", "--agent", "opencode"),
            runner=FakeCli(),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "+[mcp_servers.mnemo]" in out and '"mnemo"' in out  # the changes still show
    assert " GITHUB_PERSONAL_ACCESS_TOKEN = <hidden>" in out and '"OPENAI_API_KEY": <hidden>' in out
    for secret in (GITHUB_PAT, MEM0_KEY, OTHER_KEY):
        assert secret not in out

    assert (
        inst.main(
            args(claude, codex, "--yes", "--agent", "codex", "--agent", "opencode"),
            runner=FakeCli(),
        )
        == 0
    )
    capsys.readouterr()
    assert GITHUB_PAT in (codex / "config.toml").read_text()
    assert json.loads((opencode / "opencode.json").read_text())["mcp"]["other"] == other


@pytest.mark.parametrize(
    "line, shown",
    [
        ("MNEMO_DB = '/home/me/.mnemo/mnemo.db'", "MNEMO_DB = '/home/me/.mnemo/mnemo.db'"),
        ('"headers": {', '"headers": {'),
        ('"Authorization": "Bearer abc.def",', '"Authorization": <hidden>,'),
        ('args = ["--token", "ghp_exampleexample1"]', 'args = ["--token", "<hidden>"]'),
        ("PASSWORD=hunter2", "PASSWORD=<hidden>"),
        ('url = "https://mcp.example.com"', 'url = "https://mcp.example.com"'),
    ],
)
def test_hide_secrets(line, shown):
    assert inst.hide_secrets(line) == shown


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


async def test_migrate_creates_a_private_store_once(tmp_path, _disposable_test_db):
    import stat

    path = tmp_path / "home" / ".mnemo" / "mnemo.db"
    assert await inst.migrate(path) == migration_names()
    assert await inst.migrate(path) == []
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert await inst.migrate(Path(_disposable_test_db)) == []


class FakeServices:
    def __init__(
        self,
        store=None,
        models=("nomic-embed-text:latest",),
        ollama_up=True,
        local_ready=True,
        missing=0,
    ):
        self.problem, self.models, self.up, self.pulled = store, list(models), ollama_up, []
        self.local_ready, self.missing, self.compared, self.reindexed = local_ready, missing, [], []

    def store_problem(self, settings):
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

    def missing_vectors(self, settings):
        self.compared.append((settings.backend, settings.embed_model))
        return self.missing

    def reindex(self, settings):
        self.reindexed.append((settings.backend, settings.embed_model))
        return {"events": self.missing, "embedded": self.missing}


@pytest.fixture
def ollama_settings(monkeypatch):
    for name in ("MNEMO_EMBED_MODEL", "MNEMO_EMBED_DIM", "MNEMO_OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MNEMO_BACKEND", "ollama")  # local is the default
    inst.get_settings.cache_clear()
    yield
    inst.get_settings.cache_clear()


def test_an_unusable_store_stops_before_any_change(homes, ollama_settings, capsys):
    claude, codex = homes
    cli = FakeCli()
    probe = FakeServices(store="~/.mnemo/mnemo.db is not a readable Mnemo store")
    assert inst.main(args(claude, codex, "--yes", checks=True), runner=cli, probe=probe) == 2
    out = capsys.readouterr().out
    assert "FIX  ~/.mnemo/mnemo.db is not a readable" in out and "run mnemo-install again" in out
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
    assert "ok   store at" in capsys.readouterr().out


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
    assert set(probe.compared) == {("local", "BAAI/bge-small-en-v1.5")}
    env = tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]["mnemo"]["env"]
    assert env["MNEMO_BACKEND"] == "local" and env["MNEMO_EMBED_DIM"] == "384"
    assert env["MNEMO_EMBED_MODEL"] == "BAAI/bge-small-en-v1.5" and env["MNEMO_MODEL_DIR"]


def test_memories_without_vectors_from_the_model_are_embedded_after_confirmation(
    homes, ollama_settings, capsys
):
    claude, codex = homes
    probe = FakeServices(missing=12)
    assert (
        inst.main(args(claude, codex, "--dry-run", checks=True), runner=FakeCli(), probe=probe) == 0
    )
    assert "Embed 12 stored memories with ollama nomic-embed-text" in capsys.readouterr().out
    assert probe.reindexed == []
    assert inst.main(args(claude, codex, "--yes", checks=True), runner=FakeCli(), probe=probe) == 0
    assert probe.reindexed == [("ollama", "nomic-embed-text")]
    assert "Embedded 12 memories" in capsys.readouterr().out


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
    assert data.pop("hooks").keys() == {"SessionStart"}  # the session brief
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
    real_which = inst.shutil.which
    monkeypatch.setattr(
        inst.shutil,
        "which",
        lambda name, mode=os.F_OK | os.X_OK, path=None: (
            real_which(name, mode=mode, path=path)
            if path is not None or os.path.dirname(name)
            else (None if name == "opencode" else f"/bin/{name}")
        ),
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
        inst.server_command(),
        inst.server_env(inst.Settings(backend="ollama"), "opencode", "ollama"),
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


def test_the_ollama_model_and_host_follow_the_agents():
    settings = inst.Settings(
        _env_file=None,
        backend="ollama",
        embed_model="mxbai-embed-large",
        embed_dim=1024,
        ollama_host="http://gpu-box:11434",
    )
    env = inst.server_env(settings, "codex", "ollama")
    assert env["MNEMO_EMBED_MODEL"] == "mxbai-embed-large"
    assert env["MNEMO_OLLAMA_HOST"] == "http://gpu-box:11434"
    assert env["MNEMO_EMBED_DIM"] == "1024" and "MNEMO_MODEL_DIR" not in env


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_install_warns_without_chmodding_an_open_store_directory(
    homes, tmp_path, monkeypatch, capsys
):
    import stat

    claude, codex = homes
    open_dir = tmp_path / "open-store"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    monkeypatch.setenv("MNEMO_DB", str(open_dir / "mnemo.db"))
    inst.get_settings.cache_clear()
    try:
        assert inst.main(args(claude, codex, "--dry-run"), runner=FakeCli()) == 0
        out = capsys.readouterr().out
        assert "0755" in out and "chmod 700" in out
        assert stat.S_IMODE(open_dir.stat().st_mode) == 0o755

        open_dir.chmod(0o700)
        assert inst.main(args(claude, codex, "--dry-run"), runner=FakeCli()) == 0
        assert "chmod 700" not in capsys.readouterr().out
    finally:
        inst.get_settings.cache_clear()


# ---- Windows compatibility (simulated) --------------------------------------


def test_server_command_resolves_scripts_dir_on_windows(tmp_path, monkeypatch):
    # 1. Windows venv / uv tool layout: python.exe and mnemo-mcp-direct.exe share Scripts\.
    venv_scripts = tmp_path / "uv" / "tools" / "mnemohive" / "Scripts"
    venv_scripts.mkdir(parents=True)
    server_exe = venv_scripts / "mnemo-mcp-direct.exe"
    server_exe.write_bytes(b"")
    server_exe.chmod(0o755)
    # Monkeypatch sys.platform (not os.name, which breaks PosixPath construction on macOS).
    monkeypatch.setattr(inst.sys, "platform", "win32")
    monkeypatch.setattr(inst.os, "pathsep", ";")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    monkeypatch.setattr(inst.sys, "executable", str(venv_scripts / "python.exe"))
    monkeypatch.setattr(
        inst.sysconfig,
        "get_path",
        lambda name, *a, **kw: str(venv_scripts) if name == "scripts" else None,
    )
    assert inst.server_command() == str(server_exe)

    # 2. Windows system Python layout: python.exe in root, console scripts in Scripts\.
    py_root = tmp_path / "Python312"
    py_scripts = py_root / "Scripts"
    py_scripts.mkdir(parents=True)
    sys_server_exe = py_scripts / "mnemo-mcp-direct.exe"
    sys_server_exe.write_bytes(b"")
    sys_server_exe.chmod(0o755)
    monkeypatch.setattr(inst.sys, "executable", str(py_root / "python.exe"))
    monkeypatch.setattr(
        inst.sysconfig,
        "get_path",
        lambda name, *a, **kw: str(py_scripts) if name == "scripts" else None,
    )
    assert inst.server_command() == str(sys_server_exe)

    # 3. Windows PATHEXT discovery also finds .cmd shims and ignores CWD decoy binaries.
    cmd_scripts = tmp_path / "shim" / "Scripts"
    cmd_scripts.mkdir(parents=True)
    shim_cmd = cmd_scripts / "mnemo-mcp-direct.CMD"
    shim_cmd.write_bytes(b"")
    shim_cmd.chmod(0o755)
    decoy_cwd = tmp_path / "untrusted-repo"
    decoy_cwd.mkdir()
    decoy = decoy_cwd / "mnemo-mcp-direct.exe"
    decoy.write_bytes(b"")
    decoy.chmod(0o755)
    monkeypatch.chdir(decoy_cwd)
    monkeypatch.setattr(inst.sys, "executable", str(cmd_scripts / "python.exe"))
    monkeypatch.setattr(
        inst.sysconfig,
        "get_path",
        lambda name, *a, **kw: str(cmd_scripts) if name == "scripts" else None,
    )
    assert inst.server_command() == str(shim_cmd)

    # 4. Windows fallback when script is not yet on disk uses Scripts\mnemo-mcp-direct.exe.
    empty_scripts = tmp_path / "empty" / "Scripts"
    monkeypatch.setattr(inst.sys, "executable", str(tmp_path / "empty" / "python.exe"))
    monkeypatch.setattr(
        inst.sysconfig,
        "get_path",
        lambda name, *a, **kw: str(empty_scripts) if name == "scripts" else None,
    )
    assert inst.server_command() == str(empty_scripts / "mnemo-mcp-direct.exe")

    # 5. POSIX fallback preserves Path(sys.executable).parent / "mnemo-mcp-direct".
    monkeypatch.setattr(inst.sys, "platform", "darwin")
    monkeypatch.setattr(inst.os, "pathsep", ":")
    posix_bin = tmp_path / "posix-venv" / "bin"
    monkeypatch.setattr(inst.sys, "executable", str(posix_bin / "python"))
    monkeypatch.setattr(
        inst.sysconfig,
        "get_path",
        lambda name, *a, **kw: str(posix_bin) if name == "scripts" else None,
    )
    assert inst.server_command() == str(posix_bin / "mnemo-mcp-direct")


def test_windows_paths_escape_and_round_trip_in_agent_configs(tmp_path, monkeypatch):
    # Deliberately include \n, \t, \b, \u escape traps, spaces, accented chars and emoji.
    win_cmd = r"C:\Users\nick\temp\backup\uv\tools\mnemohive\Scripts\mnemo-mcp-direct.exe"
    win_env = {
        "MNEMO_DB": r"C:\Users\nick\AppData\Local\new\mnemo.db",
        "MNEMO_MODEL_DIR": r"C:\Users\José\My Models\🚀\bge",
        "MNEMO_ACTOR": "codex",
    }

    # Codex TOML escapes backslashes and avoids illegal UTF-16 surrogates.
    updated_toml = inst.codex_config(EXISTING_TOML, win_cmd, win_env)
    assert r"C:\\Users\\nick\\temp\\backup" in updated_toml
    parsed_toml = tomllib.loads(updated_toml)["mcp_servers"]["mnemo"]
    assert parsed_toml["command"] == win_cmd
    assert parsed_toml["env"] == win_env
    assert inst.codex_config(updated_toml, win_cmd, win_env) == updated_toml
    assert inst.remove_block(updated_toml, inst.TOML_BEGIN, inst.TOML_END) == EXISTING_TOML

    # OpenCode JSON escapes backslashes and recognizes .exe / .EXE on reinstall & uninstall.
    opencode_file = tmp_path / "opencode.json"
    entry = inst.opencode_entry(win_cmd, win_env)
    written_json = inst.opencode_config("{}", opencode_file, entry)
    assert r"C:\\Users\\nick\\temp\\backup" in written_json
    assert json.loads(written_json)["mcp"]["mnemo"] == entry
    assert inst.opencode_config(written_json, opencode_file, entry) == written_json
    # Uppercase .EXE from Windows PATHEXT is still recognized as ours.
    upper = written_json.replace("mnemo-mcp-direct.exe", "mnemo-mcp-direct.EXE")
    assert json.loads(inst.opencode_config(upper, opencode_file, None)) == {}
    foreign = json.dumps({"mcp": {"mnemo": {"type": "local", "command": [r"C:\bin\other.exe"]}}})
    with pytest.raises(inst.InstallError, match="did not write"):
        inst.opencode_config(foreign, opencode_file, entry)

    # Claude Code `add-json` passes valid escaped JSON with the Windows command and paths.
    monkeypatch.setattr(inst.shutil, "which", lambda name, *a, **kw: f"C:\\bin\\{name}.exe")
    settings = inst.Settings(
        _env_file=None,
        backend="local",
        db=Path(r"C:\Users\nick\.mnemo\mnemo.db"),
        model_dir=Path(r"C:\Users\nick\.mnemo\models"),
    )
    commands, _, _ = inst.plan(
        agents=["claude"],
        uninstall=False,
        claude_dir=tmp_path / "claude",
        codex_home=tmp_path / "codex",
        settings=settings,
        backend="local",
        command=win_cmd,
        runner=FakeCli(),
    )
    add_cmd = commands[-1]
    assert add_cmd.argv[:4] == ["claude", "mcp", "add-json", "mnemo"]
    payload = json.loads(add_cmd.argv[4])
    assert payload["command"] == win_cmd
    assert payload["env"]["MNEMO_DB"] == str(settings.db)
    assert payload["env"]["MNEMO_MODEL_DIR"] == str(settings.model_dir)


def test_default_runner_resolves_cmd_shims_on_windows(monkeypatch):
    seen: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        seen.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(inst.os, "name", "nt")
    monkeypatch.setattr(inst.sys, "platform", "win32")
    monkeypatch.setattr(
        inst.shutil,
        "which",
        lambda name: r"C:\Users\Alice\AppData\Roaming\npm\claude.cmd" if name == "claude" else None,
    )
    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst._run_command(["claude", "mcp", "get", "mnemo"])
    assert seen == [
        (
            [r"C:\Users\Alice\AppData\Roaming\npm\claude.cmd", "mcp", "get", "mnemo"],
            {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"},
        )
    ]


def test_windows_default_config_locations_and_checkout_detection(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(inst.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(inst.shutil, "which", lambda _name: None)
    assert inst.default_codex_home() == tmp_path / ".codex"
    assert inst.default_opencode_dir() == tmp_path / ".config" / "opencode"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-xdg"))
    assert inst.default_opencode_dir() == tmp_path / "custom-xdg" / "opencode"

    # Case-insensitive site-packages check works on Windows (e.g. Lib\Site-Packages).
    monkeypatch.setattr(
        inst, "__file__", str(tmp_path / "Lib" / "Site-Packages" / "mnemo" / "install.py")
    )
    assert not inst.running_from_checkout()


# --- Cursor and Claude Desktop ---------------------------------------------------------

OTHER_SERVER = {"command": "docker", "args": ["mcp", "gateway", "run"]}


def cursor_args(claude: Path, codex: Path, cursor: Path, *extra: str) -> list[str]:
    return args(claude, codex, "--cursor-dir", str(cursor), *extra)


def test_cursor_gets_a_server_told_the_open_project_and_an_always_applied_rule(homes, tmp_path):
    claude, codex = homes
    cursor = tmp_path / "cursor"
    cursor.mkdir()
    config = cursor / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"docker": OTHER_SERVER}}, indent=4) + "\n")
    # Detected (the folder exists), so a plain install includes Cursor.
    assert inst.main(cursor_args(claude, codex, cursor, "--yes"), runner=FakeCli()) == 0
    written = config.read_text()
    servers = json.loads(written)["mcpServers"]
    assert servers["docker"] == OTHER_SERVER
    assert servers["mnemo"]["type"] == "stdio"
    assert servers["mnemo"]["command"] == inst.server_command()
    assert servers["mnemo"]["env"]["MNEMO_ACTOR"] == "cursor"
    assert servers["mnemo"]["env"]["MNEMO_PROJECT_DIR"] == "${workspaceFolder}"
    assert '\n    "mcpServers"' in written  # the file's own indent is kept
    rule = (cursor / "rules" / "mnemo.mdc").read_text()
    assert rule.startswith("---\ndescription:") and "alwaysApply: true" in rule.split("---")[1]
    assert inst.policy_text().strip() in rule

    before = {path: path.read_text() for path in (config, cursor / "rules" / "mnemo.mdc")}
    assert inst.main(cursor_args(claude, codex, cursor, "--yes"), runner=FakeCli()) == 0
    assert {path: path.read_text() for path in before} == before

    gone = cursor_args(claude, codex, cursor, "--uninstall", "--yes")
    assert inst.main(gone, runner=FakeCli()) == 0
    assert json.loads(config.read_text()) == {"mcpServers": {"docker": OTHER_SERVER}}
    assert not (cursor / "rules" / "mnemo.mdc").exists()


def test_cursor_is_left_alone_when_it_is_not_installed(homes, tmp_path):
    claude, codex = homes
    cursor = tmp_path / "no-cursor"
    assert inst.main(cursor_args(claude, codex, cursor, "--yes"), runner=FakeCli()) == 0
    assert not cursor.exists()


@pytest.mark.parametrize(
    "text",
    [
        '{\n  // mine\n  "mcpServers": {}\n}\n',
        json.dumps({"mcpServers": {"mnemo": {"command": "something-else"}}}),
    ],
    ids=["comments", "unmanaged-mnemo"],
)
def test_a_cursor_config_it_cannot_rewrite_skips_cursor_unless_asked_for(
    homes, tmp_path, capsys, text
):
    claude, codex = homes
    cursor = tmp_path / "cursor"
    cursor.mkdir()
    (cursor / "mcp.json").write_text(text)
    assert inst.main(cursor_args(claude, codex, cursor, "--yes"), runner=FakeCli()) == 0
    assert "Skipped Cursor:" in capsys.readouterr().out
    assert (cursor / "mcp.json").read_text() == text and not (cursor / "rules").exists()
    assert (claude / "CLAUDE.md").exists()  # the other agents were still installed

    asked = cursor_args(claude, codex, cursor, "--agent", "cursor", "--yes")
    assert inst.main(asked, runner=FakeCli()) == 2
    assert "mnemo-install:" in capsys.readouterr().err


def test_the_cursor_rule_keeps_what_someone_else_added(homes, tmp_path):
    claude, codex = homes
    cursor = tmp_path / "cursor"
    (cursor / "rules").mkdir(parents=True)
    assert inst.main(cursor_args(claude, codex, cursor, "--yes"), runner=FakeCli()) == 0
    rule = cursor / "rules" / "mnemo.mdc"
    rule.write_text(rule.read_text() + "\nAlso: prefer short answers.\n")
    gone = cursor_args(claude, codex, cursor, "--uninstall", "--yes")
    assert inst.main(gone, runner=FakeCli()) == 0
    assert rule.read_text().endswith("Also: prefer short answers.\n")


DESKTOP = {"preferences": {"quickEntryShortcut": "off"}, "coworkUserFilesPath": "/x"}


def test_claude_desktop_only_when_named_and_global(homes, tmp_path, capsys):
    claude, codex = homes
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    config.parent.mkdir()
    config.write_text(json.dumps(DESKTOP))
    base = args(claude, codex, "--claude-desktop-config", str(config))
    assert inst.main(base + ["--yes"], runner=FakeCli()) == 0
    assert json.loads(config.read_text()) == DESKTOP  # not named: untouched

    assert inst.main(base + ["--agent", "claude-desktop", "--yes"], runner=FakeCli()) == 0
    data = json.loads(config.read_text())
    assert {k: v for k, v in data.items() if k != "mcpServers"} == DESKTOP
    server = data["mcpServers"]["mnemo"]
    assert server["command"] == inst.server_command() and server["args"] == []
    assert server["env"]["MNEMO_ACTOR"] == "claude-desktop"
    assert "MNEMO_PROJECT_DIR" not in server["env"]
    out = capsys.readouterr().out
    assert "global memories only" in out and "Quit it fully" in out

    gone = base + ["--agent", "claude-desktop", "--uninstall", "--yes"]
    assert inst.main(gone, runner=FakeCli()) == 0
    assert json.loads(config.read_text()) == DESKTOP


def test_claude_desktop_config_location_per_system(tmp_path, monkeypatch):
    name = "claude_desktop_config.json"
    monkeypatch.setattr(inst.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(inst.sys, "platform", "darwin")
    assert inst.default_claude_desktop_config() == (
        tmp_path / "Library" / "Application Support" / "Claude" / name
    )
    monkeypatch.setattr(inst.sys, "platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert inst.default_claude_desktop_config() == tmp_path / ".config" / "Claude" / name

    monkeypatch.setattr(inst.sys, "platform", "win32")
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert inst.default_claude_desktop_config() == roaming / "Claude" / name
    (local / inst.MSIX_CLAUDE).mkdir(parents=True)  # only the Microsoft Store copy exists
    assert inst.default_claude_desktop_config() == local / inst.MSIX_CLAUDE / name
    (roaming / "Claude").mkdir(parents=True)  # both: the documented one wins
    assert inst.default_claude_desktop_config() == roaming / "Claude" / name


def test_the_preview_hides_credentials_in_cursor_servers(homes, tmp_path, capsys):
    claude, codex = homes
    cursor = tmp_path / "cursor"
    cursor.mkdir()
    secret = "q8Lm2Vx7Rt4Kp9Zw" * 2
    servers = {
        "github": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": secret}},
        "api": {"command": "tool", "args": [f"--api-key={secret}"]},
        "db": {"command": "pg", "args": [f"postgres://app:{secret}@db.example.com/app"]},
    }
    (cursor / "mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2))
    assert inst.main(cursor_args(claude, codex, cursor, "--dry-run"), runner=FakeCli()) == 0
    out = capsys.readouterr().out
    assert '"mnemo": {' in out
    assert secret not in out and secret[:12] not in out
    assert "postgres://app:<hidden>@db.example.com/app" in out


@pytest.mark.parametrize(
    ("value", "found"),
    [(None, "cwd"), ("", None), ("  ", None), ("${workspaceFolder}", None), ("missing", None)],
)
def test_project_root_never_falls_back_to_the_working_directory(tmp_path, value, found):
    from mnemo.project import project_root

    cwd = tmp_path / "cwd"
    if value == "missing":
        value = str(tmp_path / "nowhere")
    assert project_root(value, cwd) == (cwd if found == "cwd" else None)
    (tmp_path / "repo").mkdir()
    assert project_root(str(tmp_path / "repo"), cwd) == tmp_path / "repo"


def test_the_preview_keeps_a_last_line_without_a_newline_on_its_own_line(tmp_path):
    change = inst.FileChange(tmp_path / "mcp.json", '{"a": 1}', '{\n  "a": 2\n}\n')
    assert change.diff().splitlines()[3:] == [
        '-{"a": 1}',
        "\\ No newline at end of file",
        "+{",
        '+  "a": 2',
        "+}",
    ]


def test_the_preview_hides_credentials_on_one_line_json_and_inline_toml():
    secret = "q8Lm2Vx7Rt4Kp9Zw" * 2
    minified = json.dumps(
        {
            "mcpServers": {
                "gh": {"command": "x", "env": {"GITHUB_TOKEN": secret}},
                "a": {"args": [f"--api-key={secret}", "--port=8080"]},
            }
        },
        separators=(",", ":"),
    )
    shown = inst.hide_secrets(minified)
    assert secret not in shown
    assert '"GITHUB_TOKEN":"<hidden>"' in shown and '"--api-key=<hidden>"' in shown
    assert '"command":"x"' in shown and '"--port=8080"' in shown
    inline = f'mcp_servers.gh = {{ env = {{ GITHUB_TOKEN = "{secret}" }} }}'
    assert inst.hide_secrets(inline) == 'mcp_servers.gh = { env = { GITHUB_TOKEN = "<hidden>" } }'


def test_a_plain_uninstall_also_removes_claude_desktop_once_it_was_set_up(homes, tmp_path):
    claude, codex = homes
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    config.parent.mkdir()
    config.write_text(json.dumps(DESKTOP))
    base = args(claude, codex, "--claude-desktop-config", str(config))
    assert inst.main(base + ["--agent", "claude-desktop", "--yes"], runner=FakeCli()) == 0
    assert "mnemo" in json.loads(config.read_text())["mcpServers"]
    assert inst.main(base + ["--uninstall", "--yes"], runner=FakeCli()) == 0
    assert json.loads(config.read_text()) == DESKTOP


# --- Antigravity -----------------------------------------------------------------------


def antigravity_homes(tmp_path: Path) -> tuple[Path, Path]:
    """The standalone CLI's ~/.gemini and a T3 Code account folder, each with the empty
    mcp_config.json Antigravity creates."""
    home = Path(os.environ["HOME"])
    gemini = home / ".gemini"
    (gemini / "antigravity-cli").mkdir(parents=True)
    t3 = home / ".t3" / "userdata" / "providers" / "antigravity" / "ac0a3dfd"
    for folder in (gemini, t3):
        (folder / "config").mkdir(parents=True)
        (folder / "config" / "mcp_config.json").write_text("")
    return gemini, t3


def test_antigravity_gets_a_server_in_every_gemini_folder_it_reads(homes, tmp_path, capsys):
    claude, codex = homes
    gemini, t3 = antigravity_homes(tmp_path)
    assert inst.default_antigravity_dirs() == [gemini, t3]
    # Detected, so a plain install includes it.
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    out = capsys.readouterr().out
    assert f"Antigravity config: {gemini}" in out and f"Antigravity config: {t3}" in out
    assert "run `agy` from inside the project" in out
    for folder in (gemini, t3):
        config = folder / "config" / "mcp_config.json"
        server = json.loads(config.read_text())["mcpServers"]["mnemo"]
        assert server["command"] == inst.server_command() and server["args"] == []
        assert server["env"]["MNEMO_ACTOR"] == "antigravity"
        assert "MNEMO_PROJECT_DIR" not in server["env"]  # it starts the server in the project
    assert not (gemini / "GEMINI.md").exists()  # it reads the server's own instructions

    before = {f: (f / "config" / "mcp_config.json").read_text() for f in (gemini, t3)}
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    assert {f: (f / "config" / "mcp_config.json").read_text() for f in before} == before

    assert inst.main(args(claude, codex, "--uninstall", "--yes"), runner=FakeCli()) == 0
    for folder in (gemini, t3):
        assert json.loads((folder / "config" / "mcp_config.json").read_text()) == {}


def test_antigravity_keeps_other_servers_and_takes_a_folder_by_flag(homes, tmp_path):
    claude, codex = homes
    folder = tmp_path / "gem"
    (folder / "config").mkdir(parents=True)
    config = folder / "config" / "mcp_config.json"
    config.write_text(json.dumps({"mcpServers": {"docker": OTHER_SERVER}}, indent=2) + "\n")
    chosen = args(claude, codex, "--agent", "antigravity", "--antigravity-dir", str(folder))
    assert inst.main(chosen + ["--yes"], runner=FakeCli()) == 0
    servers = json.loads(config.read_text())["mcpServers"]
    assert servers["docker"] == OTHER_SERVER and servers["mnemo"]["env"]["MNEMO_ACTOR"] == (
        "antigravity"
    )
    assert inst.main(chosen + ["--uninstall", "--yes"], runner=FakeCli()) == 0
    assert json.loads(config.read_text()) == {"mcpServers": {"docker": OTHER_SERVER}}


def test_antigravity_is_left_alone_when_it_is_not_installed(homes, tmp_path, capsys):
    claude, codex = homes
    assert inst.default_antigravity_dirs() == []
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    assert "Antigravity" not in capsys.readouterr().out
    assert not (Path(os.environ["HOME"]) / ".gemini").exists()
    asked = args(claude, codex, "--agent", "antigravity", "--yes")
    assert inst.main(asked, runner=FakeCli()) == 2
    assert "Antigravity not found" in capsys.readouterr().err


def test_an_antigravity_config_it_cannot_rewrite_skips_that_folder_only(homes, tmp_path, capsys):
    claude, codex = homes
    gemini, t3 = antigravity_homes(tmp_path)
    unmanaged = json.dumps({"mcpServers": {"mnemo": {"command": "something-else"}}})
    (gemini / "config" / "mcp_config.json").write_text(unmanaged)
    assert inst.main(args(claude, codex, "--yes"), runner=FakeCli()) == 0
    assert f"Skipped Antigravity in {gemini}:" in capsys.readouterr().out
    assert (gemini / "config" / "mcp_config.json").read_text() == unmanaged
    assert "mnemo" in json.loads((t3 / "config" / "mcp_config.json").read_text())["mcpServers"]
    asked = args(claude, codex, "--agent", "antigravity", "--yes")
    assert inst.main(asked, runner=FakeCli()) == 2
