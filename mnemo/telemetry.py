"""Measured request/token counters; no inferred token counts or hardcoded prices."""

from __future__ import annotations

from typing import Any


def record_usage(component: Any, payload: dict[str, Any]) -> None:
    usage = getattr(component, "usage", None)
    if usage is None:
        usage = component.usage = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "unmetered_requests": 0,
        }
    usage["requests"] += 1
    tokens = payload.get("usage", {})
    incoming = tokens.get(
        "prompt_tokens", tokens.get("total_tokens", payload.get("prompt_eval_count"))
    )
    outgoing = tokens.get("completion_tokens", payload.get("eval_count", 0))
    if incoming is None:
        usage["unmetered_requests"] += 1
    else:
        usage["input_tokens"] += incoming
        usage["output_tokens"] += outgoing
