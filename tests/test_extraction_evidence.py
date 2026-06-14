"""Evidence is checked against trusted input, never treated as its own authority."""

import json

import pytest

from mnemo.extraction import ExtractionWorker, OllamaExtractor, OpenAIExtractor
from mnemo.models import ExtractedFact
from mnemo.quality import Verdict


@pytest.mark.parametrize("extractor_class", [OllamaExtractor, OpenAIExtractor])
def test_provider_retries_fabricated_quote_and_preserves_valid_evidence(extractor_class):
    kwargs = {"api_key": "test"} if extractor_class is OpenAIExtractor else {}
    extractor = extractor_class(model="stub", max_retries=1, **kwargs)
    source = "I use Python and R to build predictive models."
    replies = iter(
        [
            {
                "facts": [
                    {
                        "subject": "user",
                        "predicate": "uses_language",
                        "object": "Python",
                        "evidence": "I use Python as an editor.",
                    }
                ]
            },
            {
                "facts": [
                    {
                        "subject": "user",
                        "predicate": "uses_language",
                        "object": "Python",
                        "evidence": source,
                    }
                ]
            },
        ]
    )
    extractor._chat = lambda *args: json.dumps(next(replies))
    try:
        facts = extractor.extract(source)
        assert len(facts) == 1
        assert facts[0].evidence == source
        assert facts[0].predicate == "uses_language"
    finally:
        extractor.close()


def test_provider_does_not_silently_accept_ungrounded_output():
    extractor = OllamaExtractor(model="stub", max_retries=0)
    extractor._chat = lambda *args: json.dumps(
        {"facts": [{"subject": "user", "predicate": "favorite_fruit", "object": "strawberries"}]}
    )
    try:
        with pytest.raises(ValueError, match="after retries"):
            extractor.extract("Strawberries are my favorite fruit.")
    finally:
        extractor.close()


class Accept:
    def __init__(self):
        self.sources = []

    def verify(self, candidate, source):
        self.sources.append(source)
        return Verdict(
            accepted=True, label="entailment", probability=1, reason="Test verifier", backend="stub"
        )


async def test_worker_rechecks_plugin_evidence_and_audits_each_span(store, db):
    source = "Résumé: I learned to make kimchi. I use Python."

    class Extractor:
        def extract(self, text, role="user"):
            return [
                ExtractedFact(
                    subject="user",
                    predicate="learned_to_make",
                    object="kimchi",
                    evidence="I learned to make kimchi.",
                    importance=8,
                ),
                ExtractedFact(
                    subject="user",
                    predicate="uses_language",
                    object="Python",
                    evidence="I use Python.",
                    importance=8,
                ),
                ExtractedFact(
                    subject="user",
                    predicate="allergy",
                    object="peanuts",
                    evidence="I have a peanut allergy.",
                    importance=8,
                ),
                # Existing third-party extractors need not supply the new optional field.
                ExtractedFact(
                    subject="user", predicate="programming_language", object="Python", importance=8
                ),
            ]

    verifier = Accept()
    await store.observe("quoted", source, "trusted-session")
    await ExtractionWorker(db, store.embedder, Extractor(), verifier).process_one()
    rows = await db.fetch(
        "SELECT candidate, source_span, outcome FROM quality_decision ORDER BY candidate_index"
    )
    assert len(verifier.sources) == 3
    assert set(verifier.sources) == {source}
    for index, quote in enumerate(["I learned to make kimchi.", "I use Python."]):
        span = json.loads(rows[index]["source_span"])
        evidence = span["evidence"]
        assert evidence["text"] == quote
        assert source[evidence["start"] : evidence["end"]] == quote
        assert evidence["offset_unit"] == "unicode_codepoint"
        assert span["turn_ids"] == ["quoted"]
        assert span["session_id"] == "trusted-session"
    assert rows[2]["outcome"] == "rejected"
    assert "evidence" not in json.loads(rows[2]["source_span"])
    assert "evidence" not in json.loads(rows[3]["source_span"])
    assert (
        await db.fetchval(
            "SELECT count(*) FROM memory_event e JOIN memory_fact f USING(fact_id) "
            "WHERE f.predicate='allergy'"
        )
        == 0
    )
    for row in await db.fetch("SELECT source_span FROM memory_event ORDER BY seq LIMIT 2"):
        span = json.loads(row["source_span"])
        assert span["evidence"]["text"] in source


@pytest.mark.parametrize("extractor_class", [OllamaExtractor, OpenAIExtractor])
def test_retry_explains_quote_failure_to_provider(extractor_class):
    from types import SimpleNamespace

    kwargs = {"api_key": "test"} if extractor_class is OpenAIExtractor else {}
    extractor = extractor_class(model="stub", max_retries=1, **kwargs)
    source = "I own a stand mixer."
    requests = []

    def respond(url, *, json):
        requests.append(json)
        evidence = "I own an editor." if len(requests) == 1 else source
        content = __import__("json").dumps(
            {
                "facts": [
                    {
                        "subject": "user",
                        "predicate": "owns_appliance",
                        "object": "stand mixer",
                        "evidence": evidence,
                    }
                ]
            }
        )
        payload = (
            {"message": {"content": content}}
            if extractor_class is OllamaExtractor
            else {"choices": [{"message": {"content": content}}]}
        )
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)

    extractor._client.post = respond
    try:
        assert extractor.extract(source)[0].object == "stand mixer"
        assert len(requests) == 2
        assert "evidence is absent from source" in requests[1]["messages"][-1]["content"]
        assert source in requests[1]["messages"][1]["content"]
        assert requests[1]["messages"][2]["role"] == "assistant"
        assert "I own an editor." in requests[1]["messages"][2]["content"]
    finally:
        extractor.close()


@pytest.mark.parametrize("value", [None, ""])
def test_provider_retries_an_assertion_without_a_value(value):
    source = "I made kimchi at a fermentation workshop."
    extractor = OllamaExtractor(model="stub", max_retries=1)
    replies = iter([value, "kimchi at a fermentation workshop"])
    extractor._chat = lambda *args: json.dumps(
        {
            "facts": [
                {
                    "subject": "user",
                    "predicate": "made",
                    "object": next(replies),
                    "evidence": source,
                }
            ]
        }
    )
    try:
        assert extractor.extract(source)[0].object == "kimchi at a fermentation workshop"
    finally:
        extractor.close()
