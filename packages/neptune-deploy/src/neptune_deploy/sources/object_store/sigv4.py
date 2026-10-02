"""AWS Signature Version 4 for S3-compatible requests, with the standard library only (ADR 0006 §6).

``sign`` returns the headers to add to one request. It signs every header the caller passes
(lower-cased, values trimmed) plus ``host``, ``x-amz-date``, ``x-amz-content-sha256`` and, with a
session token, ``x-amz-security-token``. The path is signed exactly as it is sent: S3 does not
normalise paths, so neither does this (a key holding ``//`` or ``..`` is signed and sent verbatim).
The signing time is the one wall-clock reading in the connector; it reaches the request only,
never any output.
"""

import hashlib
import hmac
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

ALGORITHM: Final = "AWS4-HMAC-SHA256"
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
UNSIGNED_PAYLOAD: Final = "UNSIGNED-PAYLOAD"


@dataclass(frozen=True)
class AwsCredentials:
    """An access key pair, and a session token if the keys are temporary. Never printed."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("access_key_id", "secret_access_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or not value.isprintable():
                raise ValueError(f"{name} must be non-empty printable text")
        if self.session_token is not None and (
            not isinstance(self.session_token, str) or not self.session_token.isprintable()
        ):
            raise ValueError("session_token must be printable text")


def quote(value: str, *, safe: str = "") -> str:
    """RFC 3986 percent-encoding of UTF-8, unreserved characters (``A-Za-z0-9-._~``) kept.

    Text decoded with ``surrogateescape`` (a store's marker that is not UTF-8) is sent back as the
    bytes it came from.
    """
    return urllib.parse.quote(value.encode("utf-8", "surrogateescape"), safe="-_.~" + safe)


def canonical_query(query: Sequence[tuple[str, str]]) -> str:
    """Parameters encoded, then sorted by name and value (a bare ``versions`` is ``versions=``)."""
    pairs = sorted((quote(name), quote(value)) for name, value in query)
    return "&".join(f"{name}={value}" for name, value in pairs)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, day: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), day)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def sign(
    *,
    method: str,
    host: str,
    path: str,
    query: Sequence[tuple[str, str]],
    headers: Mapping[str, str],
    credentials: AwsCredentials,
    region: str,
    when: datetime,
    service: str = "s3",
    payload_sha256: str = EMPTY_SHA256,
) -> dict[str, str]:
    """The headers that sign this request: ``authorization`` and the ``x-amz-*`` it covers.

    ``path`` is the request path exactly as sent (already percent-encoded); ``host`` the ``Host``
    header's value. ``when`` must be timezone-aware.
    """
    if when.tzinfo is None:
        raise ValueError("the signing time must be timezone-aware")
    stamp = when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    added = {"x-amz-date": stamp, "x-amz-content-sha256": payload_sha256}
    if credentials.session_token is not None:
        added["x-amz-security-token"] = credentials.session_token
    signed = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    signed.update(added)
    signed["host"] = host
    names = sorted(signed)
    canonical = "\n".join(
        (
            method,
            path,
            canonical_query(query),
            "".join(f"{name}:{signed[name]}\n" for name in names),
            ";".join(names),
            payload_sha256,
        )
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        (ALGORITHM, stamp, scope, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
    )
    key = signing_key(credentials.secret_access_key, day, region, service)
    signature = hmac.new(key, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    added["authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return added
