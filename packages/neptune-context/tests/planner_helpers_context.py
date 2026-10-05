"""Shared helpers for the planner tests: the golden world, a scripted model, a plan shortcut."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING

from neptune_context.eval import planner_golden as pg
from neptune_context.query import Query, to_json
from neptune_context.query.model import HEAD, AsOf
from neptune_context.query.plan import (
    DeclaredIdentifierIndex,
    Defaults,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    PlannedQuery,
    plan,
)
from planner_golden_context import GOLDEN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune_context.query.plan.client import Stop


@cache
def world() -> tuple[DeclaredIdentifierIndex, Mapping[str, Defaults]]:
    return pg.load_world(GOLDEN)


@dataclass
class ScriptedModel:
    """A model that returns a fixed text (or raises), and counts how often it was asked."""

    text: str | Query | None
    stop: Stop = "end"
    fail: str | None = None
    model: str = "claude-sonnet-5-5"
    requests: list[ModelRequest] = field(default_factory=list)
    client_id: str = "scripted"

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.fail is not None:
            raise ModelUnavailable(self.fail)
        text = json.dumps(to_json(self.text)) if isinstance(self.text, Query) else self.text
        return ModelResponse(text, self.stop, self.model)


def planned(
    question: str,
    model: ScriptedModel,
    *,
    profile: str = "agent",
    as_of: AsOf = HEAD,
) -> PlannedQuery:
    index, profiles = world()
    return plan(question, as_of, profiles[profile], resolver=index, client=model)
