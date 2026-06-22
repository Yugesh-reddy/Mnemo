"""Azure transport must preserve model choices and the frozen pilot's bounds."""

import json
from copy import deepcopy

import httpx
import pytest

PROJECT = "https://mnemo.services.ai.azure.com/api/projects/Mnemo"


def completion(message, *, finish_reason="stop"):
    return {
        "model": "gpt-5.6-luna",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        PROJECT,
        "https://mnemo.services.ai.azure.com",
        "https://mnemo.services.ai.azure.com/openai/v1/",
    ],
)
async def test_azure_uses_resource_inference_endpoint_and_keeps_credentials_private(endpoint):
    from examples.azure_host import AzureChatHost

    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200, json=completion({"role": "assistant", "content": "Done."}))

    host = AzureChatHost(endpoint, "Mnemo", "test-secret", transport=httpx.MockTransport(handle))
    try:
        metadata = await host.metadata()
        response = await host([{"role": "user", "content": "Remember PostgreSQL"}], [])
        assert str(sent[0].url) == "https://mnemo.services.ai.azure.com/openai/v1/chat/completions"
        assert sent[0].headers["api-key"] == "test-secret"
        body = json.loads(sent[0].content)
        assert body["model"] == "Mnemo"
        assert body["max_completion_tokens"] == 512
        assert body["reasoning_effort"] == "none"
        assert "seed" not in body and "temperature" not in body
        assert response["provider_response"]["model"] == "gpt-5.6-luna"
        assert "test-secret" not in json.dumps([metadata, response])
    finally:
        await host.close()


async def test_azure_round_trips_multiple_tool_ids_and_exact_arguments():
    from examples.azure_host import AzureChatHost

    calls = [
        {
            "id": "call_a",
            "type": "function",
            "function": {"name": "memory_get", "arguments": '{"fact_id":"a"}'},
        },
        {
            "id": "call_b",
            "type": "function",
            "function": {"name": "memory_get", "arguments": '{"fact_id":"b"}'},
        },
    ]
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        message = (
            {"role": "assistant", "content": None, "tool_calls": calls}
            if len(requests) == 1
            else {"role": "assistant", "content": "Done."}
        )
        return httpx.Response(200, json=completion(message))

    host = AzureChatHost(PROJECT, "Mnemo", "secret", transport=httpx.MockTransport(handle))
    messages = [{"role": "user", "content": "Inspect both"}]
    original = deepcopy(messages)
    try:
        first = await host(messages, [])
        assert messages == original
        assert first["message"]["tool_calls"][0]["function"]["arguments"] == {"fact_id": "a"}
        messages.append(first["message"])
        messages.extend(
            [
                {
                    "role": "tool",
                    "tool_name": "memory_get",
                    "tool_call_id": "call_a",
                    "content": '{"code":"NOT_FOUND"}',
                },
                {
                    "role": "tool",
                    "tool_name": "memory_get",
                    "tool_call_id": "call_b",
                    "content": '{"value":"Rust"}',
                },
            ]
        )
        await host(messages, [])
        wire = requests[1]["messages"]
        assert [message["tool_call_id"] for message in wire[-2:]] == ["call_a", "call_b"]
        assert all("tool_name" not in message for message in wire)
        assert json.loads(wire[1]["tool_calls"][0]["function"]["arguments"]) == {"fact_id": "a"}
        assert first["provider_response"]["choices"][0]["message"]["tool_calls"] == calls
    finally:
        await host.close()


async def test_azure_failure_is_not_retried_and_budget_is_reserved_before_http():
    from examples.azure_host import AzureChatHost

    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(429, json={"error": {"code": "rate_limit"}})

    host = AzureChatHost(PROJECT, "Mnemo", "secret", transport=httpx.MockTransport(handle))
    try:
        metadata = await host.metadata()
        for _ in range(48):
            with pytest.raises(httpx.HTTPStatusError):
                await host([{"role": "user", "content": "hello"}], [])
        with pytest.raises(ValueError, match="request budget"):
            await host([{"role": "user", "content": "hello"}], [])
        assert len(sent) == 48
        assert metadata["usage"]["requests"] == 48
        assert metadata["exchanges"][0]["status_code"] == 429
    finally:
        await host.close()


async def test_azure_rejects_oversized_input_before_network():
    from examples.azure_host import AzureChatHost

    def unexpected(request):
        pytest.fail("Oversized request reached the network")

    host = AzureChatHost(PROJECT, "Mnemo", "secret", transport=httpx.MockTransport(unexpected))
    try:
        with pytest.raises(ValueError, match="input budget"):
            await host([{"role": "user", "content": "a" * 32769}], [])
    finally:
        await host.close()


@pytest.mark.parametrize("model,finish_reason", [("gpt-4o", "stop"), ("gpt-5.6-luna", "length")])
async def test_azure_rejects_wrong_model_or_truncated_generation(model, finish_reason):
    from examples.azure_host import AzureChatHost

    raw = completion({"role": "assistant", "content": "Done."}, finish_reason=finish_reason)
    raw["model"] = model

    def handle(request):
        return httpx.Response(200, json=raw)

    host = AzureChatHost(PROJECT, "Mnemo", "secret", transport=httpx.MockTransport(handle))
    try:
        metadata = await host.metadata()
        with pytest.raises(ValueError):
            await host([{"role": "user", "content": "Remember PostgreSQL"}], [])
        assert metadata["exchanges"][0]["response"] == raw
    finally:
        await host.close()


async def test_azure_malformed_arguments_are_retained_and_rejected_without_a_write(store):
    from examples.agent_undo import PilotTools, run_turn
    from examples.azure_host import AzureChatHost
    from mnemo.direct import DirectMemory

    count = 0

    def handle(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(
                200,
                json=completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "bad_arguments",
                                "type": "function",
                                "function": {"name": "memory_create", "arguments": '{"subject":'},
                            }
                        ],
                    },
                    finish_reason="tool_calls",
                ),
            )
        body = json.loads(request.content)
        assert json.loads(body["messages"][-1]["content"])["code"] == "INVALID_INPUT"
        return httpx.Response(
            200, json=completion({"role": "assistant", "content": "Unable to write."})
        )

    host = AzureChatHost(PROJECT, "Mnemo", "secret", transport=httpx.MockTransport(handle))
    try:
        result = await run_turn(
            host, await PilotTools.build(DirectMemory(store)), "Remember PostgreSQL"
        )
        assert result["tool_calls"][0]["arguments"] == '{"subject":'
        assert result["tool_calls"][0]["result"]["code"] == "INVALID_INPUT"
        assert await store.conn.fetchval("SELECT count(*) FROM memory_event") == 0
    finally:
        await host.close()


def test_azure_settings_load_local_credentials_without_printing_them(tmp_path):
    from mnemo.config import Settings

    path = tmp_path / ".env"
    path.write_text(
        f"MNEMO_AZURE_ENDPOINT={PROJECT}\nMNEMO_AZURE_DEPLOYMENT=Mnemo\nAZURE_OPENAI_API_KEY=private-test-key\n"
    )
    settings = Settings(_env_file=path)
    assert settings.azure_endpoint == PROJECT
    assert settings.azure_deployment == "Mnemo"
    assert settings.azure_api_key.get_secret_value() == "private-test-key"
    assert "private-test-key" not in repr(settings)
    assert "private-test-key" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mnemo.services.ai.azure.com",
        "https://evil.example",
        PROJECT + "?key=secret",
        "https://user:secret@mnemo.services.ai.azure.com",
        PROJECT + "/unexpected",
    ],
)
def test_azure_rejects_invalid_endpoints_before_sending_credentials(endpoint):
    from examples.azure_host import AzureChatHost

    with pytest.raises(ValueError):
        AzureChatHost(endpoint, "Mnemo", "secret")


async def test_azure_pilot_preserves_call_ids_in_tool_results(store):
    from examples.agent_undo import PilotTools, run_turn
    from examples.azure_host import AzureChatHost
    from mnemo.direct import DirectMemory

    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "real_id",
                                "type": "function",
                                "function": {
                                    "name": "memory_search",
                                    "arguments": '{"query":"database"}',
                                },
                            }
                        ],
                    }
                ),
            )
        assert requests[-1]["messages"][-1]["tool_call_id"] == "real_id"
        return httpx.Response(
            200, json=completion({"role": "assistant", "content": "Which memory should I change?"})
        )

    host = AzureChatHost(PROJECT, "Mnemo", "secret", transport=httpx.MockTransport(handle))
    try:
        result = await run_turn(host, await PilotTools.build(DirectMemory(store)), "Undo that")
        assert result["status"] == "answered"
        assert len(result["tool_calls"]) == 1
        assert await store.conn.fetchval("SELECT count(*) FROM memory_event") == 0
    finally:
        await host.close()
