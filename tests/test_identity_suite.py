"""The v9 identity-suite harness scores exact current sets (no models involved)."""

import json

from mnemo.config import Settings
from mnemo.quality import Verdict
from scripts.identity_suite import run_suite


class SubstringVerifier:
    def verify(self, candidate, source_text):
        ok = str(candidate.object) in source_text
        label = "entailment" if ok else "neutral"
        return Verdict(accepted=ok, label=label, probability=1.0, reason="s", backend="t")


async def test_suite_scores_false_replacements_and_extras(
    _disposable_test_db, fake_embedder, tmp_path
):
    turns = [
        {"text": "I play the guitar.", "predicate": "plays_instrument", "value": "guitar"},
        {"text": "I also play the piano.", "predicate": "plays_instrument", "value": "piano"},
    ]
    cases = tmp_path / "cases.json"
    cases.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "c",
                        "kind": "members",
                        "turns": turns,
                        "expected_current": ["guitar", "piano"],
                    }
                ]
            }
        )
    )
    reports = {}
    for routing in ("off", "contradiction"):
        settings = Settings(_env_file=None, identity_routing=routing)
        reports[routing] = await run_suite(
            cases,
            settings=settings,
            embedder=fake_embedder,
            verifier=SubstringVerifier(),
            dsn=_disposable_test_db,
        )
    off, on = (reports[k]["results"][0] for k in ("off", "contradiction"))
    assert (off["passed"], off["missing"], off["current"]) == (False, ["guitar"], ["piano"])
    assert (on["passed"], on["current"]) == (True, ["guitar", "piano"])
    assert reports["off"]["false_replacements"] == 1
    assert reports["contradiction"]["passed"] == 1
    assert on["decisions"][-1]["identity"]["route"] == "new_member"
