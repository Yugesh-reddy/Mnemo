"""M0 smoke tests: config loads and the spec §5 thresholds are correct.

CLAUDE.md requires the thresholds (0.90 / 0.92 / 0.5) to live in config and be
covered by tests — this is that coverage.
"""

from __future__ import annotations

from mnemo.config import Settings, get_settings


def _clean() -> Settings:
    # Ignore any developer-local .env so we assert the shipped defaults.
    return Settings(_env_file=None)


def test_thresholds_have_spec_defaults() -> None:
    s = _clean()
    assert s.update_sim == 0.90
    assert s.dedup_sim == 0.92
    assert s.confidence_floor == 0.5


def test_backend_defaults_to_local_ollama() -> None:
    s = _clean()
    assert s.backend == "ollama"
    assert s.embed_model == "nomic-embed-text"
    assert s.embed_dim == 768


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


def test_gate_knobs_have_spec_defaults() -> None:
    s = _clean()
    # Lasting defaults (spec §18, validated by v11).
    assert (s.w_imp, s.w_spec, s.w_nov) == (0.6, 0.0, 0.4)
    assert s.novelty_mode == "identity" and "just" not in s.transient_markers
    assert s.w_src == 0.4
    assert s.durable_cutoff == 0.70
    assert s.ephemeral_floor == 0.55
    assert "preferred_database" in s.predicate_vocab
    assert "timezone" in s.predicate_vocab


def test_database_port_sets_both_default_dsns(monkeypatch) -> None:
    for name in ("MNEMO_DSN", "MNEMO_TEST_DSN", "MNEMO_DB_PORT"):
        monkeypatch.delenv(name, raising=False)
    assert Settings(_env_file=None).dsn == "postgresql://mnemo:mnemo@localhost:5432/mnemo"
    moved = Settings(_env_file=None, db_port=5433)
    assert moved.dsn == "postgresql://mnemo:mnemo@localhost:5433/mnemo"
    assert moved.test_dsn == "postgresql://mnemo:mnemo@localhost:5433/mnemo_test"
    explicit = Settings(_env_file=None, db_port=5433, dsn="postgresql://u:p@db:6000/x")
    assert explicit.dsn == "postgresql://u:p@db:6000/x"
    assert explicit.test_dsn == "postgresql://mnemo:mnemo@localhost:5433/mnemo_test"


def test_compose_publishes_postgres_on_localhost_only() -> None:
    from pathlib import Path

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    assert '"127.0.0.1:${MNEMO_DB_PORT:-5432}:5432"' in compose
    assert '- "5432:5432"' not in compose
    # No fixed container name, so separate checkouts (Compose projects) can run side by side.
    assert "container_name" not in compose
