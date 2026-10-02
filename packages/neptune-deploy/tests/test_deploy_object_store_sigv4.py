"""SigV4 against AWS's published examples and against botocore on hostile paths (ADR 0006 §6)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from neptune_deploy.sources.object_store.sigv4 import (
    AwsCredentials,
    canonical_query,
    quote,
    sign,
)

DOCS_KEYS = AwsCredentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
SUITE_KEYS = "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"


def _signature(headers: dict[str, str]) -> str:
    return headers["authorization"].rpartition("Signature=")[2]


def test_the_s3_documentation_examples() -> None:
    """The two worked examples of "Signature Calculations for the Authorization Header" (S3)."""
    when = datetime(2013, 5, 24, tzinfo=UTC)
    host = "examplebucket.s3.amazonaws.com"
    got = sign(
        method="GET",
        host=host,
        path="/test.txt",
        query=[],
        headers={"Range": "bytes=0-9"},
        credentials=DOCS_KEYS,
        region="us-east-1",
        when=when,
    )
    assert got["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    listing = sign(
        method="GET",
        host=host,
        path="/",
        query=[("prefix", "J"), ("max-keys", "2")],
        headers={},
        credentials=DOCS_KEYS,
        region="us-east-1",
        when=when,
    )
    assert _signature(listing) == "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"


# Produced by botocore 1.43.107's S3SigV4Auth for host 127.0.0.1:9000 in us-east-1 with the
# SigV4 test suite's keys (``uv run --no-project --with botocore``), and matched by ``sign``.
BOTOCORE = [
    (
        "/bucket/a/../b",
        [],
        {"Range": "bytes=0-99"},
        None,
        "8dae042acc4a9cb109bfdce9e3d5bddcf669f9a728ebf3a34bb92d77c735d518",
    ),
    (
        "/bucket/a//b",
        [("versionId", "3/L4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY")],
        {},
        None,
        "528e24cb876215f5d6210b220b267ae02c52aea7e067f92b29b8b5695c73e179",
    ),
    (
        "/bucket/caf%C3%A9/e%CC%81%20x%2By",
        [],
        {"If-Match": '"abc"'},
        "SESSIONTOKEN",
        "f4e896742b0df27cff5cf17df7cca4c9de8267f63c8d305c75fa7ed3f14550aa",
    ),
    (
        "/bucket",
        [("versions", ""), ("prefix", "a b/é"), ("encoding-type", "url"), ("key-marker", "k+1")],
        {},
        None,
        "f27d14138ebeba5026e5b465b2f7749eb1f3dcc38dcd790e08c95e5ee9f9bc8f",
    ),
]


@pytest.mark.parametrize(("path", "query", "headers", "token", "expected"), BOTOCORE)
def test_hostile_paths_sign_as_botocore_signs_them(
    path: str,
    query: list[tuple[str, str]],
    headers: dict[str, str],
    token: str | None,
    expected: str,
) -> None:
    got = sign(
        method="GET",
        host="127.0.0.1:9000",
        path=path,
        query=query,
        headers=headers,
        credentials=AwsCredentials(*SUITE_KEYS, token),
        region="us-east-1",
        when=datetime(2026, 10, 2, 12, 59, 57, tzinfo=UTC),
    )
    assert _signature(got) == expected
    assert ("x-amz-security-token" in got) == (token is not None)


def test_signing_is_a_function_of_its_inputs() -> None:
    args = {
        "method": "GET",
        "host": "h",
        "path": "/b/k",
        "query": [("b", "2"), ("a", "1")],
        "headers": {"Range": "bytes=0-1"},
        "credentials": AwsCredentials(*SUITE_KEYS),
        "region": "us-east-1",
    }
    utc = datetime(2026, 1, 1, 12, tzinfo=UTC)
    shifted = utc.astimezone(timezone(timedelta(hours=9)))
    assert sign(**args, when=utc) == sign(**args, when=shifted)  # type: ignore[arg-type]
    reordered = {**args, "query": [("a", "1"), ("b", "2")]}
    assert sign(**args, when=utc) == sign(**reordered, when=utc)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone"):
        sign(**args, when=datetime(2026, 1, 1))  # type: ignore[arg-type]


def test_quoting_keeps_unreserved_characters_and_escapes_the_rest() -> None:
    assert quote("a b+c/d~e.f_g-h") == "a%20b%2Bc%2Fd~e.f_g-h"
    assert quote("a/b", safe="/") == "a/b"
    assert quote("\udcff") == "%FF"  # a marker that was not UTF-8 goes back as its byte
    assert canonical_query([("versions", ""), ("a", "x y")]) == "a=x%20y&versions="


@pytest.mark.parametrize("bad", ["", "a\nb", "\x00"])
def test_credentials_must_be_printable(bad: str) -> None:
    with pytest.raises(ValueError):
        AwsCredentials(bad, "secret")
    with pytest.raises(ValueError):
        AwsCredentials("key", bad)
    assert "secret" not in repr(AwsCredentials("key", "secret"))
