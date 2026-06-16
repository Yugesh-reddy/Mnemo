"""Frozen strict evaluation normalization, independent of storage identity policy.

Version 1 copies the behavior at bca6566. Changes require a new scoring version
and a comparison under both versions; semantic reviews live in a separate layer.
"""

from typing import Any

STRICT_SCORING_VERSION = "mnemo-strict-v1"
_ALIASES_V1 = {
    "favorite_db": "preferred_database",
    "favourite_db": "preferred_database",
    "favorite_database": "preferred_database",
    "favourite_database": "preferred_database",
    "preferred_db": "preferred_database",
    "favorite_language": "preferred_language",
    "favourite_language": "preferred_language",
    "database_system": "db_engine",
    "preferred_lang": "preferred_language",
}


def assertion_key(subject: str, predicate: str, value: Any) -> tuple[str, str]:
    subject = " ".join(subject.strip().lower().split())
    predicate = " ".join(predicate.strip().lower().split())
    predicate = _ALIASES_V1.get(predicate, predicate)
    value = str(value).strip().casefold()
    if value in {"postgres", "postgresql", "psql"}:
        value = "postgresql"
    return f"{subject}|{predicate}", value
