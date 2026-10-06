"""Python SDK over query and packets for callers that are not an agent or the CLI (ADR 0004).

- ``Client`` / ``AsyncClient``: ``query``, ``why``, ``diff``, ``hydrate``; same types both ways.
- ``Engine`` / ``AsyncEngine``: the seam an in-process engine implements; ``HttpEngine`` speaks the
  wire to a remote one; ``StubEngine`` answers from recorded packets so consumers build before C2.
- ``SdkError`` / ``ErrorCode``: every failure, structured; ``RetryPolicy``: idempotent reads only.
- ``Planner``: the natural-language planner bound to a resolver, a caller's defaults and a model
  (``Client(..., planner=...)``: ``plan``, ``choose``, ``ask``); ``entity_index`` builds the Demo v1
  resolver from a Memory graph document.
"""

from neptune_context.sdk.client import (
    NO_RETRY,
    AsyncClient,
    Client,
    RetryPolicy,
    diff_query,
    why_query,
)
from neptune_context.sdk.engine import AsyncEngine, Engine, to_async
from neptune_context.sdk.errors import ErrorCode, SdkError
from neptune_context.sdk.http import HttpEngine
from neptune_context.sdk.planning import Asked, NoModel, Planner, entity_index
from neptune_context.sdk.stub import StubEngine

__all__ = [
    "NO_RETRY",
    "Asked",
    "AsyncClient",
    "AsyncEngine",
    "Client",
    "Engine",
    "ErrorCode",
    "HttpEngine",
    "NoModel",
    "Planner",
    "RetryPolicy",
    "SdkError",
    "StubEngine",
    "diff_query",
    "entity_index",
    "to_async",
    "why_query",
]
