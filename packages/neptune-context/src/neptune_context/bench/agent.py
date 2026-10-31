"""One question through one arm: the tool-use loop, with usage counted and the transcript kept."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from neptune_context.bench.arms import Arm

FALLBACK_BETA = "server-side-fallback-2026-07-01"
USAGE = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass
class Transcript:
    answer: str
    messages: list[dict[str, Any]]
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    seconds: float = 0.0
    stop: str = ""
    model: str = ""


def client(key: str | None = None) -> Any:
    import anthropic

    return (
        anthropic.Anthropic(api_key=key, max_retries=6)
        if key
        else anthropic.Anthropic(max_retries=6)
    )


def _dump(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    dumped: dict[str, Any] = block.model_dump(exclude_none=True)
    return dumped


def _call(api: Any, **kwargs: Any) -> Any:
    with api.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **kwargs) as stream:
        return stream.get_final_message()


def ask(
    api: Any,
    arm: Arm,
    question: str,
    *,
    model: str,
    effort: str,
    max_tool_calls: int = 30,
    max_tokens: int = 32_000,
) -> Transcript:
    """Ask until Claude answers. Past ``max_tool_calls`` its tools answer "budget used up" and
    the next turn may not call tools."""
    started = time.monotonic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": arm.prompt(question)}]
    tools, system = arm.tools(), arm.system()
    usage = dict.fromkeys(USAGE, 0)
    calls, served, out_of_budget = 0, model, False
    while True:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort},
            "cache_control": {"type": "ephemeral"},
        }
        if tools:
            kwargs["tools"] = tools
            if out_of_budget:
                kwargs["tool_choice"] = {"type": "none"}
        message = _call(api, **kwargs)
        served = str(message.model)
        for key in USAGE:
            usage[key] += int(getattr(message.usage, key, 0) or 0)
        content = [_dump(b) for b in message.content]
        messages.append({"role": "assistant", "content": content})
        if message.stop_reason == "pause_turn":
            continue
        uses = [b for b in content if b.get("type") == "tool_use"]
        if message.stop_reason != "tool_use" or not uses:
            break
        results = []
        for use in uses:
            calls += 1
            if calls > max_tool_calls:
                out_of_budget = True
                body: Any = "Tool budget used up: answer now from what you have."
                error = True
            else:
                try:
                    body, error = arm.execute(use["name"], dict(use.get("input") or {})), False
                except Exception as exc:
                    body, error = f"{type(exc).__name__}: {exc}", True
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": use["id"],
                    "content": body,
                    "is_error": error,
                }
            )
        messages.append({"role": "user", "content": results})
    answer = "\n".join(b["text"] for b in messages[-1]["content"] if b.get("type") == "text")
    return Transcript(
        answer=answer,
        messages=messages,
        usage=usage,
        tool_calls=calls,
        seconds=round(time.monotonic() - started, 1),
        stop=str(message.stop_reason),
        model=served,
    )


def strip_images(value: Any) -> Any:
    """The transcript without base64 image data, for the results file."""
    if isinstance(value, list):
        return [strip_images(v) for v in value]
    if isinstance(value, dict):
        if value.get("type") == "image":
            return {"type": "image", "omitted": True}
        if value.get("type") in ("thinking", "redacted_thinking"):
            return {k: v for k, v in value.items() if k not in ("signature", "data")}
        return {k: strip_images(v) for k, v in value.items()}
    return value
