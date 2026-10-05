"""The wire form the SDK speaks to a remote engine (ADR 0004 §3), as pure functions.

Two POST routes, JSON both ways, UTF-8, no redirects:

- ``POST /v1/query``: body is the query's canonical JSON (ADR 0002); a 200 answer is the packet's
  canonical JSON (ADR 0003).
- ``POST /v1/hydrate``: body is ``{"evidence": <evidence ref>, "as_of": <int>}`` (``as_of``
  omitted for head); a 200 answer is the Ledger's ``Resolution`` in the catalog API's JSON form.

Any other status carries ``{"error": {"code": ..., "message": ..., "findings": [...]}}``. A client
trusts the HTTP status first (401, 403, 404, 408/429/5xx), then the body's code when it names one
of ours. An engine behind this wire is a server (Platform X2) and not part of this package; these
functions are what both sides share.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from neptune.identity.canonical_json import dumps
from neptune.model.provenance import evidence_ref_from_json
from neptune_context.query.findings import FindingCode, QueryFinding
from neptune_context.sdk.errors import ErrorCode, SdkError, bounded

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue
    from neptune.model.provenance import EvidenceRef

WIRE_VERSION: Final = 1
QUERY_PATH: Final = "/v1/query"
HYDRATE_PATH: Final = "/v1/hydrate"
MAX_RESPONSE_BYTES: Final = 64 * 1024 * 1024 + 1  # a packet is at most 64 MiB (ADR 0003 §8)
MAX_REQUEST_BYTES: Final = 64 * 1024

_BY_STATUS: Final = {
    400: ErrorCode.INVALID_ARGUMENT,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    408: ErrorCode.TIMEOUT,
    504: ErrorCode.TIMEOUT,
    422: ErrorCode.QUERY_REFUSED,
    429: ErrorCode.UNAVAILABLE,
}
_BY_CODE: Final = {
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.QUERY_REFUSED: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.INVALID_RESPONSE: 502,
    ErrorCode.ENGINE_ERROR: 500,
}


def status_for(error: SdkError) -> int:
    """The HTTP status a server answers with for ``error``."""
    return _BY_CODE[error.code]


def error_body(error: SdkError) -> bytes:
    """The body of a non-200 answer."""
    return dumps({"error": error.to_json()})


def error_from_status(status: int, body: bytes) -> SdkError:
    """An ``SdkError`` for a non-200 answer: the status decides the code unless it is unspecific.

    A 5xx is ``unavailable`` (retryable) unless the body names ``engine_error``; a body that is not
    our error shape contributes no message beyond the status.
    """
    code = _BY_STATUS.get(status) or (
        ErrorCode.UNAVAILABLE if status >= 500 else ErrorCode.ENGINE_ERROR
    )
    message = f"engine answered HTTP {status}"
    findings: tuple[QueryFinding, ...] = ()
    try:
        error = json.loads(body.decode("utf-8"))["error"]
        named = ErrorCode(error["code"])
        if isinstance(error["message"], str):
            message = error["message"]
        if status >= 500 and named is ErrorCode.ENGINE_ERROR:
            code = named
        if code is ErrorCode.QUERY_REFUSED:
            findings = tuple(
                QueryFinding(FindingCode(f["code"]), str(f["at"]), str(f["message"]))
                for f in error["findings"]
            )
    except (ValueError, KeyError, TypeError, RecursionError):
        pass
    return SdkError(code, bounded(message), findings=findings)


def hydrate_request(evidence: EvidenceRef, as_of: int | None) -> bytes:
    document: dict[str, JsonValue] = {"evidence": evidence.to_json()}
    if as_of is not None:
        document["as_of"] = as_of
    return dumps(document)


def parse_hydrate_request(body: bytes) -> tuple[EvidenceRef, int | None]:
    """Read a hydrate request strictly; raises ``SdkError(INVALID_ARGUMENT)`` on any defect."""
    try:
        document: Any = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict) or not {"evidence"} <= set(document) <= {
            "as_of",
            "evidence",
        }:
            raise ValueError("a hydrate request has evidence and, optionally, as_of")
        as_of = document.get("as_of")
        if as_of is not None and (
            isinstance(as_of, bool) or not isinstance(as_of, int) or not 0 <= as_of < 2**63
        ):
            raise ValueError("as_of is a Ledger transaction or null")
        return evidence_ref_from_json(document["evidence"]), as_of
    except (ValueError, TypeError, RecursionError) as error:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, f"bad hydrate request: {error}") from error
