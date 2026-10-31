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
