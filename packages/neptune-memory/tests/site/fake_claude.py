"""A stand-in for the Anthropic client in the improvement tests: no network, no key, no cost."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class FakeClaude:
    """``client.beta.messages.stream(...)`` answering with ``answers[i](dossier text)`` in turn."""

    def __init__(self, *answers: Callable[[str], dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=self)

    def stream(self, **request: Any) -> Any:
        self.calls.append(request)
        body = json.dumps(self.answers.pop(0)(request["messages"][0]["content"]))
        message = SimpleNamespace(
            stop_reason="end_turn",
            model=request["model"],
            content=[SimpleNamespace(type="text", text=body)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=0),
        )
        return _Stream(message)


class _Stream:
    def __init__(self, message: Any) -> None:
        self.message = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self.message


def fact_id(text: str, needle: str) -> str:
    """The id of the dossier line that holds ``needle``."""
    line = next(line for line in text.splitlines() if needle in line and line.startswith("fact:"))
    return line.split(" | ", 1)[0]


class FakeChatClaude:
    """``client.beta.messages.create(...)`` for the chat: each turn in ``script`` is a list of tool
    calls ``[(name, input), ...]`` or a final text; ``requests`` keeps what was sent."""

    def __init__(self, *script: list[tuple[str, dict[str, Any]]] | str) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=self)

    def create(self, **request: Any) -> Any:
        self.requests.append({**request, "messages": list(request["messages"])})
        turn = self.script.pop(0)
        if isinstance(turn, str):
            return SimpleNamespace(
                stop_reason="end_turn", content=[SimpleNamespace(type="text", text=turn)]
            )
        uses = [
            SimpleNamespace(type="tool_use", id=f"use-{len(self.requests)}-{i}", name=n, input=a)
            for i, (n, a) in enumerate(turn)
        ]
        return SimpleNamespace(stop_reason="tool_use", content=uses)
