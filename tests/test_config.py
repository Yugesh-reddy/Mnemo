"""Config loads with the shipped defaults."""

from __future__ import annotations

from mnemo.config import Settings, get_settings


def _clean() -> Settings:
    # Ignore any developer-local .env so we assert the shipped defaults.
    return Settings(_env_file=None)


def test_search_settings_have_spec_defaults() -> None:
    s = _clean()
    assert s.search_floor == 0.60  # the default local model's floor
    assert (s.search_w_rel, s.search_w_rec, s.search_w_imp) == (0.5, 0.2, 0.3)
    assert Settings(_env_file=None, backend="ollama").search_floor == 0.5


def test_backend_defaults_to_the_model_inside_mnemo(monkeypatch) -> None:
    for name in ("MNEMO_BACKEND", "MNEMO_EMBED_MODEL", "MNEMO_EMBED_DIM"):
        monkeypatch.delenv(name, raising=False)
    s = _clean()
    assert (s.backend, s.embed_model, s.embed_dim) == ("local", "BAAI/bge-small-en-v1.5", 384)
    ollama = Settings(_env_file=None, backend="ollama")
    assert (ollama.embed_model, ollama.embed_dim) == ("nomic-embed-text", 768)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_the_store_defaults_to_one_file_in_the_home_directory(monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.delenv("MNEMO_DB", raising=False)
    assert Settings(_env_file=None).db == Path.home() / ".mnemo" / "mnemo.db"
    assert Settings(_env_file=None, db="~/elsewhere.db").db == Path.home() / "elsewhere.db"


def test_settings_default_paths_follow_home_dynamically(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.delenv("MNEMO_DB", raising=False)
    monkeypatch.delenv("MNEMO_MODEL_DIR", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    settings = Settings(_env_file=None)
    assert settings.db == tmp_path / ".mnemo" / "mnemo.db"
    assert settings.model_dir == tmp_path / ".mnemo" / "models"


def test_settings_from_the_postgres_releases_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("MNEMO_DSN", "postgresql://u:p@db:6000/x")
    monkeypatch.setenv("MNEMO_DB_PORT", "5433")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "dsn") and not hasattr(settings, "db_port")
