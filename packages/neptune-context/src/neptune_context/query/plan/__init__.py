"""The natural-language query planner (ADR 0005): English in, a typed Query out, never an answer.

``plan(text, as_of, defaults, resolver=..., client=...)`` returns a ``PlannedQuery``: the query to
show and edit, the model lineage, the entity candidates found and every default or assumption as
a structured finding. The planner is inference (``assertion_kind="inferred"``); the answer still
comes from the deterministic engine, from a typed query only. SDK and MCP wiring is MVL-110's.
"""

from neptune_context.query.plan.client import (
    DEFAULT_MODEL,
    AnthropicClient,
    ModelClient,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    Recording,
    RecordingClient,
    RecordingMissing,
    ReplayClient,
    anthropic_arguments,
    dump_recordings,
    load_recordings,
)
from neptune_context.query.plan.planner import Defaults, choose, plan
from neptune_context.query.plan.prompt import (
    build_request,
    output_schema,
    system_prompt,
    template_sha256,
)
from neptune_context.query.plan.resolver import (
    DeclaredIdentifierIndex,
    Entity,
    EntityResolver,
    Mention,
)
from neptune_context.query.plan.result import (
    ModelLineage,
    PlanFinding,
    PlanFindingCode,
    PlannedQuery,
    PlanStatus,
    Severity,
)

__all__ = [
    "DEFAULT_MODEL",
    "AnthropicClient",
    "DeclaredIdentifierIndex",
    "Defaults",
    "Entity",
    "EntityResolver",
    "Mention",
    "ModelClient",
    "ModelLineage",
    "ModelRequest",
    "ModelResponse",
    "ModelUnavailable",
    "PlanFinding",
    "PlanFindingCode",
    "PlanStatus",
    "PlannedQuery",
    "Recording",
    "RecordingClient",
    "RecordingMissing",
    "ReplayClient",
    "Severity",
    "anthropic_arguments",
    "build_request",
    "choose",
    "dump_recordings",
    "load_recordings",
    "output_schema",
    "plan",
    "system_prompt",
    "template_sha256",
]
