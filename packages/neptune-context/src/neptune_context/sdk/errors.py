"""Structured SDK errors (ADR 0004 §5): one exception type, a stable code, never a bare string.

Every failure a caller can see is an ``SdkError`` with an ``ErrorCode``. A refused query carries the
query reader's findings; a transport or engine failure says whether retrying can help. Messages are
deterministic, bounded, and never contain a credential.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject
    from neptune_context.query.findings import QueryFinding

MAX_MESSAGE_CHARS: Final = 500


class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"  # the caller's call is malformed; fix it, do not retry
    QUERY_REFUSED = "query_refused"  # the query reader found findings; they are attached
    UNAUTHENTICATED = "unauthenticated"  # no or bad credential
    FORBIDDEN = "forbidden"  # a credential that may not do this
    NOT_FOUND = "not_found"  # the engine has no answer for this request
    UNAVAILABLE = "unavailable"  # the engine could not be reached or failed transiently; retryable
    TIMEOUT = "timeout"  # the engine did not answer in time; retryable
    INVALID_RESPONSE = "invalid_response"  # an answer that is not a packet for this query
    ENGINE_ERROR = "engine_error"  # the engine failed in a way that retrying will not fix


RETRYABLE: Final = frozenset({ErrorCode.UNAVAILABLE, ErrorCode.TIMEOUT})


def bounded(message: str) -> str:
    """``message`` cut to ``MAX_MESSAGE_CHARS``: an engine's reply cannot flood a log or a model."""
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return message[: MAX_MESSAGE_CHARS - 1] + "…"


class SdkError(Exception):
    """A call failed: ``code`` says why, ``retryable`` whether trying again can help."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        findings: tuple[QueryFinding, ...] = (),
    ) -> None:
        self.code = code
        self.message = bounded(message)
        self.findings = findings
        super().__init__(f"{code}: {self.message}")

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def to_json(self) -> JsonObject:
        return {
            "code": str(self.code),
            "findings": [f.to_json() for f in self.findings],
            "message": self.message,
            "retryable": self.retryable,
        }
