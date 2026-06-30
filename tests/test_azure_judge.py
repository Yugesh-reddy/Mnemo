"""v12 Azure judge: unchanged verifier prompt, frozen options, hard budget. No network."""

import json

import httpx
import pytest

from mnemo.models import ExtractedFact
from scripts import azure_judge as az

ENDPOINT = "https://example-resource.openai.azure.com/openai/v1/"
FACT = ExtractedFact(subject="user", predicate="works_at", object="Acme Corp")
TURN = "I left Acme Corp and now work at Globex."


def reply(label="contradiction", probability=0.99, *, model="gpt-5.6-luna-2026-07-09"):
    content = {"label": label, "probability": probability, "reason": "r", "evidence": TURN}
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 40},
        },
    )


def judge(responses, seen=None):
    queue = list(responses)

    def handler(request):
        if seen is not None:
            seen.append(request)
        return queue.pop(0)

    return az.AzureJudge(
        endpoint=ENDPOINT,
        deployment="gpt-5.6-luna",
        api_key="test-key",
        threshold=0.99,
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_uses_the_unchanged_verifier_prompt_with_frozen_options():
    seen = []
    verdict = judge([reply()], seen).verify(FACT, TURN)
    assert (verdict.label, verdict.probability, verdict.backend) == ("contradiction", 0.99, "azure")
    body = json.loads(seen[0].content)
    assert seen[0].headers["api-key"] == "test-key"
    assert {k: body[k] for k in ("reasoning_effort", "max_completion_tokens", "temperature")} == {
        "reasoning_effort": "none",
        "max_completion_tokens": 512,
        "temperature": 0,
    }
    prompt = body["messages"][0]["content"]
    assert prompt.startswith("Does SOURCE support the complete ASSERTION")
    assert '"object": "Acme Corp"' in prompt and TURN in prompt


def test_entailment_is_accepted_only_at_the_threshold():
    assert judge([reply("entailment", 0.99)]).verify(FACT, TURN).accepted
    assert not judge([reply("entailment", 0.95)]).verify(FACT, TURN).accepted


def test_budget_exhaustion_raises_instead_of_returning_a_verdict(monkeypatch):
    monkeypatch.setattr(az, "MAX_REQUESTS", 1)
    j = judge([reply()])
    j.verify(FACT, TURN)
    with pytest.raises(az.JudgeBudgetExhausted):
        j.verify(FACT, TURN)


def test_unexpected_model_stops_the_judge():
    j = judge([reply(model="gpt-4o")])
    with pytest.raises(az.JudgeBudgetExhausted, match="gpt-4o"):
        j.verify(FACT, TURN)
    with pytest.raises(az.JudgeBudgetExhausted):
        j.verify(FACT, TURN)


def test_rate_limits_retry_within_the_budget():
    j = judge([httpx.Response(429, json={}), reply()])
    assert j.verify(FACT, TURN).label == "contradiction"
    assert j.usage["requests"] == 2
