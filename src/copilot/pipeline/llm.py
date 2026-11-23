"""LLM calls for the graph nodes: LangChain `ChatAnthropic` + structured output, nothing custom.

    chat_model(...)          → ChatAnthropic (timeout, no SDK retries: LangGraph's
                               RetryPolicy on the node retries, so a retry is visible in the graph)
    structured(chat, Schema) → (parsed Pydantic object, usage). The API is given the JSON schema
                               (structured outputs), then the result is validated by Pydantic.
                               Any failure raises LLMOutputError: never treated as "safe".

Usage (tokens, cost) goes to OpenTelemetry metrics; LangSmith traces the call itself (prompts
and outputs contain MASKED text only).
"""

from dataclasses import dataclass

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr

from copilot import telemetry as tel
from copilot.pipeline.schemas import Usage

# USD per million tokens (input, output). Update with the model; used for the cost metric only.
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}


class LLMOutputError(Exception):
    """The model's answer failed validation (schema, refusal, cut off, business rules)."""


@dataclass
class LLMResult:
    parsed: BaseModel
    usage: Usage
    model: str


def chat_model(model: str, api_key: SecretStr, timeout_s: float, max_tokens: int = 1024):
    return ChatAnthropic(
        model=model,
        api_key=api_key,
        # no `temperature`: deprecated for current Claude models (the API returns 400); the
        # structured output (JSON schema) is what constrains the answer.
        timeout=timeout_s,
        max_retries=0,  # retries belong to the graph (RetryPolicy), not hidden in the SDK
        max_tokens=max_tokens,
    )


def structured(chat, schema: type[BaseModel], system: str, user: str, name: str) -> LLMResult:
    runnable = chat.with_structured_output(schema, method="json_schema", include_raw=True)
    out = runnable.invoke(
        [SystemMessage(system), HumanMessage(user)], config={"run_name": name, "tags": [name]}
    )
    raw = out.get("raw")
    if out.get("parsing_error") is not None or out.get("parsed") is None:
        stop = getattr(raw, "response_metadata", {}).get("stop_reason") if raw else None
        raise LLMOutputError(
            f"{name}: invalid output (stop_reason={stop}): {out.get('parsing_error')}"
        )
    model = getattr(chat, "model", "unknown")
    usage = _usage(raw, model)
    return LLMResult(out["parsed"], usage, model)


def _usage(raw, model: str) -> Usage:
    um = getattr(raw, "usage_metadata", None) or {}
    tin, tout = int(um.get("input_tokens", 0)), int(um.get("output_tokens", 0))
    cache = int((um.get("input_token_details") or {}).get("cache_read", 0) or 0)
    pin, pout = PRICES.get(model, (0.0, 0.0))
    cost = round((tin * pin + tout * pout) / 1e6, 8)
    tel.LLM_TOKENS.add(tin, {"model": model, "kind": "input"})
    tel.LLM_TOKENS.add(tout, {"model": model, "kind": "output"})
    if cache:
        tel.LLM_TOKENS.add(cache, {"model": model, "kind": "cache_read"})
    tel.LLM_COST.add(cost, {"model": model})
    return Usage(calls=1, input_tokens=tin, output_tokens=tout, cache_read_tokens=cache,
                 cost_usd=cost)  # fmt: skip
