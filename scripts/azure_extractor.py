"""Experiment-only Azure OpenAI v1 extractor for the v8 stronger-model probe.

Only the transport differs from production extraction. Messages come from the
unchanged ``_extraction_messages``; parsing, exact-evidence validation and the
bounded validation retries are inherited from ``OpenAIExtractor.extract``. This
class is not wired into ``build_extractor`` and changes no production default.

Every request is reserved against a hard budget before it is sent. Rate-limit,
server and transport failures get at most two infrastructure retries, which
also count against the budget. A response from any model other than
``gpt-5.6-luna`` stops the extractor.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from examples.azure_host import inference_url
from mnemo.extraction import OllamaExtractor, OpenAIExtractor, _extraction_messages

EXPECTED_MODEL = "gpt-5.6-luna"
MAX_REQUESTS = 64
MAX_TOTAL_TOKENS = 150_000
MAX_COMPLETION_TOKENS = 2048
INFRA_RETRIES = 2
SMOKE_TURN = "I switched our team's CI from Jenkins to GitHub Actions last month."


class BudgetExhausted(RuntimeError):
    """The frozen request/token cap, or a model-identity failure, stops all calls."""


class AzureExtractor(OpenAIExtractor):
    def __init__(
        self,
        *,
        endpoint: str,
        deployment: str,
        api_key: str,
        max_retries: int = 2,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not deployment.strip() or not api_key.strip():
            raise ValueError("Azure deployment name and API key are required")
        self.model = deployment
        self.base_url = inference_url(endpoint)
        self.max_retries = max_retries
        self.send_temperature = True
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
            raise BudgetExhausted(self.fatal_error)
        spent = self.usage["input_tokens"] + self.usage["output_tokens"]
        if self.usage["requests"] >= MAX_REQUESTS or spent >= MAX_TOTAL_TOKENS:
            raise BudgetExhausted(
                f"v8 budget exhausted: {self.usage['requests']} requests, {spent} tokens"
            )
        self.usage["requests"] += 1  # Reserved before sending, like the pilot transport.

    def _post(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        for attempt in range(INFRA_RETRIES + 1):
            self._reserve()
            body: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "reasoning_effort": "none",
                "max_completion_tokens": MAX_COMPLETION_TOKENS,
            }
            if self.send_temperature:
                body["temperature"] = 0
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
                raise BudgetExhausted(self.fatal_error)
            tokens = raw.get("usage") or {}
            if "prompt_tokens" in tokens:
                self.usage["input_tokens"] += tokens["prompt_tokens"]
                self.usage["output_tokens"] += tokens.get("completion_tokens", 0)
            else:
                self.usage["unmetered_requests"] += 1
            return raw
        raise AssertionError("unreachable")

    def _chat(
        self,
        text: str,
        role: str,
        validation_error: str | None = None,
        previous_content: str | None = None,
    ) -> str:
        raw = self._post(_extraction_messages(text, role, validation_error, previous_content))
        return raw["choices"][0]["message"].get("content") or ""

    def smoke(self) -> dict[str, Any]:
        """One synthetic, non-development call that fixes the temperature rule.

        Frozen rule: if Azure rejects ``temperature`` with HTTP 400, omit it for
        the whole run and resend once. Any other failure aborts before dev turns.
        """
        messages = _extraction_messages(SMOKE_TURN, "user", None)
        try:
            raw = self._post(messages)
        except httpx.HTTPStatusError as exc:
            detail = str(self.exchanges[-1].get("response", "")).lower()
            if exc.response.status_code != 400 or "temperature" not in detail:
                raise
            self.send_temperature = False
            raw = self._post(messages)
        content = raw["choices"][0]["message"].get("content") or ""
        record: dict[str, Any] = {
            "turn": SMOKE_TURN,
            "temperature_sent": self.send_temperature,
            "model": raw.get("model"),
            "finish_reason": raw["choices"][0].get("finish_reason"),
        }
        try:  # Informational only: validation here never gates the run.
            facts = OllamaExtractor._parse(content, SMOKE_TURN)
            record["parsed_facts"] = [f.model_dump(mode="json") for f in facts]
        except ValueError as exc:
            record["parse_error"] = str(exc)
        return record
