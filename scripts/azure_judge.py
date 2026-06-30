"""Experiment-only Azure OpenAI v1 judge for identity routing (v12).

Subclasses ``LLMVerifier`` so the prompt, the JSON verdict schema, local acceptance
and retry handling are unchanged; only the transport differs. It is used only for
routing's two checks (does the turn contradict a stored value, does a stored value
already state the candidate). The write gate keeps its configured verifier.

Every request is reserved against a hard budget before it is sent. HTTP 429/5xx
and transport failures get at most two infrastructure retries, counted against the
budget. A response from any model other than ``gpt-5.6-luna`` stops the judge.
Budget exhaustion raises ``RuntimeError``, which ``LLMVerifier.verify`` does not
swallow, so an exhausted run fails loudly instead of routing on fallback verdicts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from examples.azure_host import inference_url
from mnemo.quality import LLMVerifier

EXPECTED_MODEL = "gpt-5.6-luna"
MAX_REQUESTS = 150
MAX_TOTAL_TOKENS = 200_000
MAX_COMPLETION_TOKENS = 512
INFRA_RETRIES = 2


class JudgeBudgetExhausted(RuntimeError):
    """The frozen cap, or a model-identity failure, stops every further call."""


class AzureJudge(LLMVerifier):
    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_key: str,
        threshold: float,
        max_retries: int = 1,
        timeout: float = 45.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not deployment.strip() or not api_key.strip():
            raise ValueError("Azure deployment name and API key are required")
        self.backend, self.model = "azure", deployment
        self.threshold, self.max_retries = threshold, max_retries
        self.base_url = inference_url(endpoint)
        self.fatal_error: str | None = None
        self.usage = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "unmetered_requests": 0}
        self.exchanges: list[dict[str, Any]] = []
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={"api-key": api_key},
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def _reserve(self) -> None:
        if self.fatal_error:
            raise JudgeBudgetExhausted(self.fatal_error)
        spent = self.usage["input_tokens"] + self.usage["output_tokens"]
        if self.usage["requests"] >= MAX_REQUESTS or spent >= MAX_TOTAL_TOKENS:
            self.fatal_error = (
                f"v12 judge budget exhausted: {self.usage['requests']} requests, {spent} tokens"
            )
            raise JudgeBudgetExhausted(self.fatal_error)
        self.usage["requests"] += 1

    def _request(self, prompt: str) -> Mapping[str, Any]:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "reasoning_effort": "none",
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "temperature": 0,
        }
        for attempt in range(INFRA_RETRIES + 1):
            self._reserve()
            exchange: dict[str, Any] = {"request": body, "attempt": attempt}
            self.exchanges.append(exchange)
            try:
                response = self._client.post("chat/completions", json=body)
            except httpx.TransportError as exc:
                exchange["transport_error"] = repr(exc)
                if attempt == INFRA_RETRIES:
                    raise
                self._sleep(2.0 * 2**attempt)
                continue
            exchange["status_code"] = response.status_code
            try:
                exchange["response"] = response.json()
            except ValueError:
                exchange["response"] = {"non_json": response.text[:2000]}
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == INFRA_RETRIES:
                    response.raise_for_status()
                self._sleep(2.0 * 2**attempt)
                continue
            response.raise_for_status()
            raw = exchange["response"]
            model = raw.get("model", "")
            if model != EXPECTED_MODEL and not model.startswith(EXPECTED_MODEL + "-"):
                self.fatal_error = f"Azure returned model {model!r}; expected {EXPECTED_MODEL}"
                raise JudgeBudgetExhausted(self.fatal_error)
            tokens = raw.get("usage") or {}
            if "prompt_tokens" in tokens:
                self.usage["input_tokens"] += tokens["prompt_tokens"]
                self.usage["output_tokens"] += tokens.get("completion_tokens", 0)
            else:
                self.usage["unmetered_requests"] += 1
            result = json.loads(raw["choices"][0]["message"].get("content") or "")
            if not isinstance(result, Mapping):
                raise TypeError("verifier JSON must be an object")
            return result
        raise AssertionError("unreachable")
