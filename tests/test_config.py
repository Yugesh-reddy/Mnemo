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
