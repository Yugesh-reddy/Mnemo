"""Bounded Azure OpenAI v1 transport for the direct-memory host pilot."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

import httpx

JSON = dict[str, Any]
MAX_REQUESTS = 48
MAX_REQUEST_BYTES = 32768
MAX_COMPLETION_TOKENS = 512


def inference_url(endpoint: str) -> str:
    """Resolve a Foundry project URL to its resource's OpenAI v1 endpoint."""
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(
            r"[a-zA-Z0-9-]+\.(?:services\.ai|openai)\.azure\.com", parsed.hostname or ""
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"(?:/openai/v1/?|/api/projects/[\w.-]+/?|/)?", parsed.path)
    ):
        raise ValueError("Use an HTTPS Azure resource, project, or OpenAI v1 endpoint")
    return f"https://{parsed.hostname}/openai/v1/"


class AzureChatHost:
    """Translate wire formats only; preserve the model's tool choices and arguments."""

    def __init__(
        self,
        endpoint: str,
        deployment: str,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = inference_url(endpoint)
        if not deployment.strip() or not api_key.strip():
            raise ValueError("Azure deployment name and AZURE_OPENAI_API_KEY are required")
        self.deployment = deployment
        self.fatal_error: str | None = None
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.exchanges: list[JSON] = []
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"api-key": api_key},
            timeout=45,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    async def metadata(self) -> JSON:
        # Deployment/model identity is confirmed by each actual provider response.
        # No extra inference calls are spent on a separate connectivity probe.
        return {
            "provider": "azure",
            "deployment": self.deployment,
            "expected_model": "gpt-5.6-luna",
            "base_url": self.base_url,
            "options": {"reasoning_effort": "none", "max_completion_tokens": MAX_COMPLETION_TOKENS},
            "limits": {"requests": MAX_REQUESTS, "request_bytes": MAX_REQUEST_BYTES},
            "usage": self.usage,
            "exchanges": self.exchanges,
        }

    @staticmethod
    def wire_messages(messages: list[JSON]) -> list[JSON]:
        wire = []
        for original in messages:
            message = {"role": original["role"], "content": original.get("content", "")}
            if original["role"] == "tool":
                message["tool_call_id"] = original["tool_call_id"]
            if original.get("tool_calls"):
                message["tool_calls"] = deepcopy(original["tool_calls"])
                for call in message["tool_calls"]:
                    arguments = call["function"]["arguments"]
                    if not isinstance(arguments, str):
                        call["function"]["arguments"] = json.dumps(arguments)
            wire.append(message)
        return wire

    async def __call__(self, messages: list[JSON], tools: list[JSON]) -> JSON:
        if self.fatal_error:
            raise ValueError(self.fatal_error)
        if self.usage["requests"] >= MAX_REQUESTS:
            raise ValueError("Azure pilot request budget exhausted")
        body = {
            "model": self.deployment,
            "messages": self.wire_messages(messages),
            "tools": tools,
            "stream": False,
            "reasoning_effort": "none",
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
        }
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("Azure pilot input budget exceeded; transcript was not truncated")
        self.usage["requests"] += 1  # Reserve before sending, including ambiguous network failures.
        exchange: JSON = {"request": body}
        self.exchanges.append(exchange)
        response = await self.client.post(
            "chat/completions", content=encoded, headers={"Content-Type": "application/json"}
        )
        exchange["status_code"] = response.status_code
        try:
            raw = response.json()
        except ValueError:
            exchange["response"] = {"error": "Non-JSON response"}
            response.raise_for_status()
            raise ValueError("Azure returned a non-JSON response") from None
        exchange["response"] = raw
        response.raise_for_status()
        usage = raw.get("usage", {})
        for name in ("prompt_tokens", "completion_tokens"):
            self.usage[name] += usage.get(name, 0)
        model = raw.get("model", "")
        if model != "gpt-5.6-luna" and not model.startswith("gpt-5.6-luna-"):
            self.fatal_error = (
                f"Azure deployment returned {model!r}; expected gpt-5.6-luna. "
                "No tool calls were executed from this response."
            )
            raise ValueError(self.fatal_error)
        choice = raw["choices"][0]
        if choice.get("finish_reason") not in {"stop", "tool_calls"}:
            raise ValueError(f"Azure generation did not finish: {choice.get('finish_reason')}")
        message = deepcopy(choice["message"])
        if message.get("content") is None:
            message["content"] = ""
        for call in message.get("tool_calls") or []:
            if not isinstance(call.get("id"), str) or not call["id"]:
                raise ValueError("Azure returned a tool call without its ID")
            arguments = call["function"]["arguments"]
            if isinstance(arguments, str):
                try:
                    call["function"]["arguments"] = json.loads(arguments)
                except ValueError:
                    pass  # The tool adapter rejects malformed arguments; never repair them.
        return {"message": message, "provider_response": raw}

    async def close(self) -> None:
        await self.client.aclose()
