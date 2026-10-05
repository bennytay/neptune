"""The SDK over HTTP (ADR 0004 §3): a real loopback server on the wire, plus hostile answers."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

import pytest
from neptune_ledger.api import dumps as ledger_dumps

from neptune.identity.canonical_json import dumps
from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import EvidenceItem
from neptune_context.query import Budget, Query, Why, loads
from neptune_context.sdk import (
    AsyncClient,
    Client,
    ErrorCode,
    HttpEngine,
    RetryPolicy,
    SdkError,
    StubEngine,
    wire,
)
from sdk_testing_context import (
    STEMS,
    error_body,
    golden_packet,
    golden_query,
    golden_stub,
    parse_hydrate_request,
    status_for,
    unresolvable,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

Reply = tuple[int, bytes] | tuple[int, bytes, dict[str, str]]


class _Seen:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []


@contextmanager
def serve(respond: Callable[[str, bytes, _Seen], Reply]) -> Iterator[tuple[str, _Seen]]:
    seen = _Seen()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            reply = respond(self.path, body, seen)
            status, data = reply[0], reply[1]
            self.send_response(status)
            for name, value in (reply[2] if len(reply) == 3 else {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def stub_server(stub: StubEngine) -> Callable[[str, bytes, _Seen], Reply]:
    def respond(path: str, body: bytes, seen: _Seen) -> Reply:
        try:
            if path == wire.QUERY_PATH:
                query = loads(body)
                assert isinstance(query, Query)
                return 200, canonical_bytes(stub.query(query))
            if path == wire.HYDRATE_PATH:
                ref, as_of = parse_hydrate_request(body)
                return 200, ledger_dumps(stub.hydrate(ref, as_of=as_of))
        except SdkError as error:
            return status_for(error), error_body(error)
        return 404, b""

    return respond


@pytest.mark.parametrize("stem", STEMS)
def test_every_golden_packet_round_trips_over_http(stem: str) -> None:
    with serve(stub_server(golden_stub())) as (url, seen):
        packet = Client(url).query(golden_query(stem))
        assert canonical_bytes(packet) == canonical_bytes(golden_packet(stem))
        assert seen.requests[0]["path"] == wire.QUERY_PATH
        assert seen.requests[0]["body"] == dumps(  # the query travels as its canonical JSON
            json.loads(seen.requests[0]["body"])
        )


def test_the_async_client_round_trips_over_http_too() -> None:
    with serve(stub_server(golden_stub())) as (url, _):
        packet = asyncio.run(AsyncClient(url).query(golden_query("q06")))
    assert packet == golden_packet("q06")


def test_hydrate_round_trips_the_ledgers_resolution_over_http() -> None:
    item = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    resolution = unresolvable(item.evidence)
    stub = StubEngine({}, {item.evidence: resolution})
    with serve(stub_server(stub)) as (url, seen):
        assert Client(url).hydrate(item, as_of=5) == resolution
        assert Client(url).hydrate(item) == resolution
        assert json.loads(seen.requests[0]["body"])["as_of"] == 5
        assert "as_of" not in json.loads(seen.requests[1]["body"])


def test_the_bearer_token_and_wire_version_are_sent_and_only_when_given() -> None:
    with serve(stub_server(golden_stub())) as (url, seen):
        Client(url, token="tok-123").query(golden_query("q01"))
        Client(url).query(golden_query("q01"))
    with_token, without = (r["headers"] for r in seen.requests)
    assert with_token["Authorization"] == "Bearer tok-123"
    assert "Authorization" not in without
    assert with_token["X-Neptune-Wire"] == "1"


def test_status_codes_map_to_structured_errors() -> None:
    cases = {
        401: ErrorCode.UNAUTHENTICATED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        408: ErrorCode.TIMEOUT,
        429: ErrorCode.UNAVAILABLE,
        500: ErrorCode.UNAVAILABLE,
        503: ErrorCode.UNAVAILABLE,
        504: ErrorCode.TIMEOUT,
        418: ErrorCode.ENGINE_ERROR,
    }
    for status, code in cases.items():
        with (
            serve(lambda *_, s=status: (s, b"not json")) as (url, _),
            pytest.raises(SdkError) as raised,
        ):
            Client(url, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
        assert raised.value.code is code, status


def test_an_unmapped_status_takes_the_code_its_body_names() -> None:
    # C1 gate review: a server's deterministic defect is not retried as an outage.
    for status, named in (
        (502, ErrorCode.INVALID_RESPONSE),
        (500, ErrorCode.ENGINE_ERROR),
        (410, ErrorCode.NOT_FOUND),
    ):
        reply = error_body(SdkError(named, "defect"))
        with (
            serve(lambda *_, s=status, r=reply: (s, r)) as (url, seen),
            pytest.raises(SdkError) as raised,
        ):
            Client(url).query(golden_query("q01"))
        assert raised.value.code is named, status
        assert len(seen.requests) == 1, "a non-retryable code is asked once"
    mapped = error_body(SdkError(ErrorCode.ENGINE_ERROR, "x"))
    with serve(lambda *_: (404, mapped)) as (url, _), pytest.raises(SdkError) as raised:
        Client(url, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
    assert raised.value.code is ErrorCode.NOT_FOUND  # a mapped status decides


def test_a_refused_query_answer_carries_its_findings_back() -> None:
    reply = error_body(SdkError(ErrorCode.QUERY_REFUSED, "no", findings=_findings()))
    with serve(lambda *_: (422, reply)) as (url, _), pytest.raises(SdkError) as raised:
        Client(url).query(golden_query("q01"))
    assert raised.value.code is ErrorCode.QUERY_REFUSED
    assert [str(f.code) for f in raised.value.findings] == ["empty_query"]
    assert not raised.value.retryable


def _findings() -> tuple[Any, ...]:
    from neptune_context.query import FindingCode, QueryFinding

    return (QueryFinding(FindingCode.EMPTY_QUERY, "/", "selects nothing"),)


def test_transient_http_failures_are_retried_then_succeed() -> None:
    answers = [(503, b""), (429, b""), (200, canonical_bytes(golden_packet("q01")))]
    with serve(lambda *_: answers.pop(0)) as (url, seen):
        pauses: list[float] = []
        client = Client(url, retry=RetryPolicy(attempts=3, backoff_s=(0.25,)), sleep=pauses.append)
        assert client.query(golden_query("q01")) == golden_packet("q01")
    assert len(seen.requests) == 3
    assert pauses == [0.25, 0.25]


def test_a_401_is_not_retried() -> None:
    with serve(lambda *_: (401, b"{}")) as (url, seen), pytest.raises(SdkError):
        Client(url, retry=RetryPolicy(attempts=3), sleep=lambda _: None).query(golden_query("q01"))
    assert len(seen.requests) == 1


def test_redirects_are_never_followed() -> None:
    with (
        serve(stub_server(golden_stub())) as (target, target_seen),
        serve(lambda *_: (307, b"", {"Location": target + wire.QUERY_PATH})) as (url, _),
        pytest.raises(SdkError) as raised,
    ):
        Client(url, token="tok", retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
    assert raised.value.code is ErrorCode.ENGINE_ERROR
    assert target_seen.requests == []  # the token never went to the redirect target


def test_hostile_answers_are_invalid_responses_not_crashes() -> None:
    good = canonical_bytes(golden_packet("q01"))
    answers = [
        b"",
        b"null",
        b"[]",
        b"{not json",
        b"\xff\xfe",
        good[:-20],  # truncated
        good.replace(b"packet_version", b"packet_vers1on"),
        good + good,  # trailing garbage
        b'{"a":1,"a":2}',
        b"NaN",
    ]
    for answer in answers:
        with serve(lambda *_, a=answer: (200, a)) as (url, _), pytest.raises(SdkError) as raised:
            Client(url).query(golden_query("q01"))
        assert raised.value.code is ErrorCode.INVALID_RESPONSE, answer[:20]
    item = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    with serve(lambda *_: (200, b"{}")) as (url, _), pytest.raises(SdkError) as raised:
        Client(url).hydrate(item)
    assert raised.value.code is ErrorCode.INVALID_RESPONSE


def test_an_answer_for_another_query_over_http_is_refused_by_verification() -> None:
    with (
        serve(lambda *_: (200, canonical_bytes(golden_packet("q02")))) as (url, _),
        pytest.raises(SdkError) as raised,
    ):
        Client(url).query(golden_query("q01"))
    assert raised.value.code is ErrorCode.INVALID_RESPONSE


def test_an_oversized_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wire, "MAX_RESPONSE_BYTES", 100)
    with (
        serve(lambda *_: (200, canonical_bytes(golden_packet("q01")))) as (url, _),
        pytest.raises(SdkError) as raised,
    ):
        Client(url).query(golden_query("q01"))
    assert raised.value.code is ErrorCode.INVALID_RESPONSE


def test_an_unreachable_engine_is_unavailable_and_a_slow_one_times_out() -> None:
    with serve(lambda *_: (200, b"")) as (url, _):
        pass  # the server is gone once the block ends
    with pytest.raises(SdkError) as gone:
        Client(url, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
    assert gone.value.code is ErrorCode.UNAVAILABLE

    release = threading.Event()

    def slow(*_: object) -> Reply:
        release.wait(5)
        return 200, b""

    with serve(slow) as (slow_url, _):
        with pytest.raises(SdkError) as late:
            Client(slow_url, timeout=0.2, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
        release.set()
    assert late.value.code is ErrorCode.TIMEOUT


def test_long_error_bodies_are_bounded_and_the_token_is_not_echoed() -> None:
    body = json.dumps({"error": {"code": "engine_error", "message": "x" * 9000}}).encode()
    with serve(lambda *_: (500, body)) as (url, _), pytest.raises(SdkError) as raised:
        Client(url, token="tok-abc", retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
    assert raised.value.code is ErrorCode.ENGINE_ERROR
    assert len(raised.value.message) == 500
    assert "tok-abc" not in str(raised.value)


def test_engine_urls_and_tokens_are_checked() -> None:
    bad_urls = [
        "",
        "example.org",
        "file:///etc/passwd",
        "http://",
        "https://user:pw@example.org",
        "https://example.org/?x=1",
        "https://example.org/#frag",
        "https://example.org:notaport",
    ]
    for url in bad_urls:
        with pytest.raises(SdkError) as raised:
            HttpEngine(url)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT, url
    for token in ("", " tok", "tok\n", "tök", "a b", "a\x00b"):
        with pytest.raises(SdkError):
            HttpEngine("https://example.org", token=token)
    for timeout in (0, -1, 3601):
        with pytest.raises(SdkError):
            HttpEngine("https://example.org", timeout=timeout)
    assert repr(HttpEngine("https://example.org/base/", token="tok")) == (
        "HttpEngine('https://example.org/base', token=set)"
    )


def test_a_base_path_is_kept() -> None:
    paths: list[str] = []

    def record(path: str, body: bytes, _: _Seen) -> Reply:
        paths.append(path)
        return 200, canonical_bytes(golden_packet("q01"))

    with serve(record) as (url, _):
        Client(url + "/neptune/").query(golden_query("q01"))
    assert paths == ["/neptune" + wire.QUERY_PATH]


def test_hydrate_request_parsing_is_strict() -> None:
    item = next(i for i in golden_packet("q04").items if isinstance(i, EvidenceItem))
    good = wire.hydrate_request(item.evidence, 7)
    assert parse_hydrate_request(good) == (item.evidence, 7)
    assert parse_hydrate_request(wire.hydrate_request(item.evidence, None))[1] is None
    bad = [
        b"",
        b"[]",
        b'{"as_of":1}',
        b'{"evidence":{}}',
        good.replace(b'"as_of":7', b'"as_of":true'),
        good.replace(b'"as_of":7', b'"as_of":-1'),
        good.replace(b'"as_of":7', b'"as_of":7,"extra":1'),
        b"\xff",
    ]
    for body in bad:
        with pytest.raises(SdkError) as raised:
            parse_hydrate_request(body)
        assert raised.value.code is ErrorCode.INVALID_ARGUMENT, body


def test_a_query_unknown_to_the_server_comes_back_as_not_found() -> None:
    unknown = Query(
        include_inferred=False, budget=Budget(items=2), explain=(Why("claim:sha256:" + "0" * 64),)
    )
    with serve(stub_server(golden_stub())) as (url, _), pytest.raises(SdkError) as raised:
        Client(url).query(unknown)
    assert raised.value.code is ErrorCode.NOT_FOUND


def _raw_server(script: Callable[[Any], None]) -> Iterator[str]:
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run() -> None:
        connection, _ = listener.accept()
        with connection:
            connection.recv(65536)
            script(connection)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()
        thread.join(5)


def test_a_malformed_status_line_is_unavailable_and_retried() -> None:
    for script in (
        lambda c: c.sendall(b"garbage\r\n\r\n"),
        lambda c: c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\nshort"),  # truncated
        lambda c: None,  # closes without answering
    ):
        for url in _raw_server(script):
            with pytest.raises(SdkError) as raised:
                Client(url, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
            assert raised.value.code is ErrorCode.UNAVAILABLE
            assert raised.value.retryable


def test_a_connection_reset_while_reading_an_error_body_keeps_the_status() -> None:
    def script(c: Any) -> None:
        c.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 500\r\n\r\npartial")

    for url in _raw_server(script):
        with pytest.raises(SdkError) as raised:
            Client(url, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
        assert raised.value.code is ErrorCode.UNAVAILABLE


def test_a_server_that_drips_bytes_hits_the_total_deadline() -> None:
    import time

    def script(c: Any) -> None:
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
        try:
            for _ in range(200):
                c.sendall(b"x" * 100)
                time.sleep(0.05)
        except OSError:
            pass

    for url in _raw_server(script):
        started = time.monotonic()
        with pytest.raises(SdkError) as raised:
            Client(url, timeout=0.5, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
        assert raised.value.code is ErrorCode.TIMEOUT
        assert time.monotonic() - started < 3


def test_environment_proxies_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve(lambda *_: (200, b"")) as (proxy, proxy_seen):
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        with serve(stub_server(golden_stub())) as (url, seen):
            Client(url, token="tok").query(golden_query("q01"))
    assert proxy_seen.requests == []
    assert seen.requests[0]["headers"]["Authorization"] == "Bearer tok"
