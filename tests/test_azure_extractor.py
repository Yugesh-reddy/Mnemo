"""v8 experiment adapter: unchanged messages, frozen options, hard budget. No network."""

import json

import httpx
import pytest

from mnemo.extraction import _extraction_messages
from scripts import azure_extractor as az

ENDPOINT = "https://example-resource.openai.azure.com/openai/v1/"
SOURCE = "I use Postgres for my project."
GOOD = {
    "facts": [
        {
            "evidence": "I use Postgres for my project.",
            "subject": "user",
            "predicate": "uses_database",
            "object": "Postgres",
            "confidence": 0.95,
            "importance": 7,
        }
    ]
}


def reply(content, *, model="gpt-5.6-luna-2026-07-09", status=200, usage=(100, 20)):
    return httpx.Response(
        status,
        json={
            "model": model,
            "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]},
        },
    )


def extractor(responses, seen=None):
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(request)
        return queue.pop(0)

    return az.AzureExtractor(
        endpoint=ENDPOINT,
        deployment="gpt-5.6-luna",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_sends_unchanged_messages_with_frozen_options():
    seen = []
    facts = extractor([reply(GOOD)], seen).extract(SOURCE)
    assert [f.object for f in facts] == ["Postgres"]
    request = seen[0]
    assert str(request.url) == ENDPOINT + "chat/completions"
    assert request.headers["api-key"] == "test-key"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body == {
        "model": "gpt-5.6-luna",
        "messages": _extraction_messages(SOURCE, "user", None),
        "response_format": {"type": "json_object"},
        "reasoning_effort": "none",
        "max_completion_tokens": 2048,
        "temperature": 0,
    }


def test_validation_retries_reuse_the_production_feedback_messages():
    seen = []
    invented = {"facts": [{**GOOD["facts"][0], "evidence": "I love Postgres."}]}
    e = extractor([reply(invented), reply(GOOD)], seen)
    assert [f.object for f in e.extract(SOURCE)] == ["Postgres"]
    retry = json.loads(seen[1].content)["messages"]
    assert retry[:3] == _extraction_messages(SOURCE, "user", "x", json.dumps(invented))[:3]
    assert retry[3]["content"].startswith("Validator feedback (not source evidence): ")
    assert e.usage == {
        "requests": 2,
        "input_tokens": 200,
        "output_tokens": 40,
        "unmetered_requests": 0,
    }


def test_request_cap_stops_before_sending(monkeypatch):
    monkeypatch.setattr(az, "MAX_REQUESTS", 1)
    seen = []
    e = extractor([reply(GOOD)], seen)
    e.extract(SOURCE)
    with pytest.raises(az.BudgetExhausted, match="1 requests"):
        e.extract(SOURCE)
    assert len(seen) == 1


def test_token_cap_stops_before_sending(monkeypatch):
    monkeypatch.setattr(az, "MAX_TOTAL_TOKENS", 100)
    seen = []
    e = extractor([reply(GOOD, usage=(90, 10))], seen)
    e.extract(SOURCE)
    with pytest.raises(az.BudgetExhausted, match="100 tokens"):
        e.extract(SOURCE)
    assert len(seen) == 1


def test_rate_limits_retry_within_the_budget():
    e = extractor([httpx.Response(429, json={"error": "slow down"}), reply(GOOD)])
    assert [f.object for f in e.extract(SOURCE)] == ["Postgres"]
    assert e.usage["requests"] == 2
    assert [x["status_code"] for x in e.exchanges] == [429, 200]


def test_persistent_server_errors_surface_after_two_retries():
    e = extractor([httpx.Response(503, json={})] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        e.extract(SOURCE)
    assert e.usage["requests"] == 3


def test_unexpected_model_stops_all_further_calls():
    seen = []
    e = extractor([reply(GOOD, model="gpt-4o")], seen)
    with pytest.raises(az.BudgetExhausted, match="gpt-4o"):
        e.extract(SOURCE)
    with pytest.raises(az.BudgetExhausted):
        e.extract(SOURCE)
    assert len(seen) == 1


def test_smoke_drops_temperature_only_when_azure_rejects_it():
    rejected = httpx.Response(
        400, json={"error": {"message": "Unsupported parameter: 'temperature'"}}
    )
    seen = []
    e = extractor([rejected, reply({"facts": []})], seen)
    record = e.smoke()
    assert record["temperature_sent"] is False
    assert record["parsed_facts"] == []
    assert "temperature" in json.loads(seen[0].content)
    assert "temperature" not in json.loads(seen[1].content)


def test_smoke_aborts_on_other_client_errors():
    e = extractor([httpx.Response(401, json={"error": "bad key"})])
    with pytest.raises(httpx.HTTPStatusError):
        e.smoke()
    assert e.send_temperature is True
