"""What ``plan`` returns (ADR 0005 §1-§2): a typed query that is shown, never an answer.

A ``PlannedQuery`` is inference and says so: ``assertion_kind`` is always ``inferred`` and
``lineage`` names the model, the prompt template, the schema and the exact request and response
that produced it. ``findings`` carry every default or assumption the planner stated and every
reason the query is not yet safe to run; ``status`` summarises them. Only ``READY`` is executable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from neptune.identity import canonical_json
from neptune_context.query.codec import query_id, to_json

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject
    from neptune_context.query.findings import Refused
    from neptune_context.query.model import Query
    from neptune_context.query.plan.resolver import Mention

PLAN_FORMAT_VERSION: Final = 1


class PlanStatus(StrEnum):
    READY = "ready"  # a valid query, nothing blocking: safe to execute
    NEEDS_CHOICE = "needs_choice"  # a name is ambiguous: pick a candidate (``choose``)
    NEEDS_INPUT = "needs_input"  # something the question did not state: a clock, frame, unit, id
    INVALID = "invalid"  # the model's output is not a valid query; no query is offered
    FAILED = "failed"  # no usable model output: unavailable, refused, cut off, or bad input


class Severity(StrEnum):
    INFO = "info"  # a stated default or override: shown, never blocks
    BLOCKING = "blocking"  # the query must not run until the user resolves it


class PlanFindingCode(StrEnum):
    # Stated defaults and overrides (info).
    AS_OF_DEFAULT = "as_of_default"
    AS_OF_OVERRIDDEN = "as_of_overridden"
    INCLUDE_INFERRED_DEFAULT = "include_inferred_default"
    BUDGET_DEFAULT = "budget_default"
    CLOCK_DEFAULTED_TO_PRIMARY = "clock_defaulted_to_primary"
    CLOCK_DEFAULTED_TO_CALLER = "clock_defaulted_to_caller"
    ENTITY_CHOSEN = "entity_chosen"
    # Needs the user (blocking).
    AMBIGUOUS_ENTITY = "ambiguous_entity"
    UNKNOWN_ENTITY = "unknown_entity"
    ENTITY_KIND_MISMATCH = "entity_kind_mismatch"
    CLOCK_NOT_STATED = "clock_not_stated"
    CLOCK_NOT_DECLARED = "clock_not_declared"
    TIME_PHRASE_UNRESOLVED = "time_phrase_unresolved"
    FRAME_NOT_DECLARED = "frame_not_declared"
    UNIT_NOT_STATED = "unit_not_stated"
    BRIDGE_NOT_DECLARED = "bridge_not_declared"
    CLAIM_NOT_QUOTED = "claim_not_quoted"
    DRAFT_WITHDRAWN = "draft_withdrawn"
    # No usable output (blocking).
    BAD_INPUT = "bad_input"
    MODEL_UNAVAILABLE = "model_unavailable"
    MODEL_REFUSED = "model_refused"
    MODEL_TRUNCATED = "model_truncated"
    MODEL_OUTPUT_INVALID = "model_output_invalid"


@dataclass(frozen=True)
class PlanFinding:
    """One stated default, override or blocker. ``at`` is a JSON pointer into the query's
    canonical JSON (``/`` when it concerns the plan as a whole); ``details`` are stable strings
    (candidate ids, the clock used) a console can show beside the message."""

    code: PlanFindingCode
    severity: Severity
    at: str
    message: str
    details: tuple[str, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "at": self.at,
            "code": str(self.code),
            "details": list(self.details),
            "message": self.message,
            "severity": str(self.severity),
        }


@dataclass(frozen=True)
class ModelLineage:
    """Where a plan came from. Equal lineage and equal recorded response mean equal plans.

    ``template_sha256`` covers the system prompt (with its vocabularies) and the output schema
    the model was bound to; ``request_sha256`` the whole request; ``response_sha256`` the text
    the model returned. The last two are ``None`` when no model was asked (bad input).
    """

    model_id: str
    client_id: str
    template_sha256: str
    schema_id: str
    schema_version: int
    request_sha256: str | None = None
    response_sha256: str | None = None

    def to_json(self) -> JsonObject:
        out: JsonObject = {
            "client_id": self.client_id,
            "model_id": self.model_id,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "template_sha256": self.template_sha256,
        }
        if self.request_sha256 is not None:
            out["request_sha256"] = self.request_sha256
        if self.response_sha256 is not None:
            out["response_sha256"] = self.response_sha256
        return out


def response_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


@dataclass(frozen=True)
class PlannedQuery:
    """The result of planning one question.

    ``query`` is the typed query to show and let the user edit (``None`` for ``INVALID`` and
    ``FAILED``); it is executable only when ``status`` is ``READY``. ``mentions`` are the names
    found in the question and all their candidates, ``refusal`` is the query validator's own
    findings when the model's output was refused. Nothing here answers the question.
    """

    status: PlanStatus
    question: str
    query: Query | None
    lineage: ModelLineage
    mentions: tuple[Mention, ...] = ()
    findings: tuple[PlanFinding, ...] = ()
    refusal: Refused | None = None
    assertion_kind: Literal["inferred"] = "inferred"

    @property
    def executable(self) -> bool:
        return self.status is PlanStatus.READY and self.query is not None

    @property
    def query_id(self) -> str | None:
        return None if self.query is None else query_id(self.query)

    @property
    def blocking(self) -> tuple[PlanFinding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.BLOCKING)

    def to_json(self) -> JsonObject:
        out: JsonObject = {
            "assertion_kind": self.assertion_kind,
            "findings": [f.to_json() for f in self.findings],
            "format_version": PLAN_FORMAT_VERSION,
            "lineage": self.lineage.to_json(),
            "mentions": [m.to_json() for m in self.mentions],
            "question": self.question,
            "status": str(self.status),
        }
        if self.query is not None:
            out["query"] = to_json(self.query)
            out["query_id"] = query_id(self.query)
        if self.refusal is not None:
            out["refusal"] = self.refusal.to_json()
        return out

    def canonical_bytes(self) -> bytes:
        """Byte-identical for equal plans on every machine."""
        return canonical_json.dumps(self.to_json())
