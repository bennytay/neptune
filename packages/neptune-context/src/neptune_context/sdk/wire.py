"""The wire form the SDK speaks to a remote engine (ADR 0004 §3), as pure functions.

Two POST routes, JSON both ways, UTF-8, no redirects:

- ``POST /v1/query``: body is the query's canonical JSON (ADR 0002); a 200 answer is the packet's
  canonical JSON (ADR 0003).
- ``POST /v1/hydrate``: body is ``{"evidence": <evidence ref>, "as_of": <int>}`` (``as_of``
  omitted for head); a 200 answer is the Ledger's ``Resolution`` in the catalog API's JSON form.

Any other status carries ``{"error": {"code": ..., "message": ..., "findings": [...]}}``. A client
trusts the HTTP status first (401, 403, 404, 408/429/5xx), then the body's code when it names one
of ours. An engine behind this wire is a server (Platform X2) and not part of this package; the
test support module has a reference server's half (status mapping, request parsing).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

from neptune.identity.canonical_json import dumps
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


def error_from_status(status: int, body: bytes) -> SdkError:
    """An ``SdkError`` for a non-200 answer: the status decides the code unless it is unspecific.

    A status the table does not map (a plain 5xx, an unusual 4xx) takes the code its body names, so
    a server's deterministic ``invalid_response`` or ``engine_error`` is not retried as an outage;
    without one, a 5xx is ``unavailable`` (retryable) and anything else ``engine_error``. A body
    that is not our error shape contributes no message beyond the status.
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
        if status not in _BY_STATUS:
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
