"""Config loads with the shipped defaults."""

from __future__ import annotations

from mnemo.config import Settings, get_settings


def _clean() -> Settings:
    # Ignore any developer-local .env so we assert the shipped defaults.
    return Settings(_env_file=None)


def test_search_settings_have_spec_defaults() -> None:
    s = _clean()
    assert s.search_floor == 0.5
    assert (s.search_w_rel, s.search_w_rec, s.search_w_imp) == (0.5, 0.2, 0.3)


def test_backend_defaults_to_local_ollama() -> None:
    s = _clean()
    assert s.backend == "ollama"
    assert s.embed_model == "nomic-embed-text"
    assert s.embed_dim == 768


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_the_store_defaults_to_one_file_in_the_home_directory(monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.delenv("MNEMO_DB", raising=False)
    assert Settings(_env_file=None).db == Path.home() / ".mnemo" / "mnemo.db"
    assert Settings(_env_file=None, db="~/elsewhere.db").db == Path.home() / "elsewhere.db"


def test_settings_from_the_postgres_releases_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("MNEMO_DSN", "postgresql://u:p@db:6000/x")
    monkeypatch.setenv("MNEMO_DB_PORT", "5433")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "dsn") and not hasattr(settings, "db_port")
