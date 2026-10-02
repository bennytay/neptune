"""Hardening of the transport both connectors share (MVL-153 follow-ups, applied under ADR 0007):
a header or path http.client cannot encode, an absurd timeout, and a body shorter than its declared
length. Object-store reads and Foxglove reads go through the same code, so both are exercised."""

import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_foxglove_fake import API_KEY, FakeFoxglove
from neptune.store.workspace import Workspace
from neptune_deploy.sources.foxglove import FoxgloveConfigError, foxglove_source
from neptune_deploy.sources.foxglove.client import FoxgloveTransport
from neptune_deploy.sources.object_store import (
    ObjectStoreConfigError,
    azure_source,
    gcs_source,
    s3_source,
)
from neptune_deploy.sources.object_store.transport import (
    MAX_TIMEOUT,
    Endpoint,
    ShortRead,
    Transport,
    TransportError,
)
from neptune_deploy.sources.rerun import rerun_source
from neptune_deploy.sources.roboto import roboto_source

BODY = b"x" * 100


def _online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


def _drain(conn: socket.socket) -> None:
    """Read one whole request (headers, then a body of its Content-Length), so closing after the
    answer never resets the client mid-send."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            return
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    for line in head.lower().split(b"\r\n"):
        if line.startswith(b"content-length:"):
            remaining = int(line.split(b":")[1]) - len(rest)
            while remaining > 0:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                remaining -= len(chunk)


@contextmanager
def serve_raw(response: bytes) -> Iterator[Endpoint]:
    """A server that answers every request with ``response`` verbatim, then closes."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def run() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    _drain(conn)
                    conn.sendall(response)
                except OSError:
                    pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield Endpoint.parse(f"http://127.0.0.1:{listener.getsockname()[1]}")
    finally:
        listener.close()


def _short(declared: int, sent: bytes) -> bytes:
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (
        declared,
        sent,
    )


# --- (a) a header or path http.client cannot encode ---------------------------------------------


@pytest.mark.parametrize("kind", ["header", "path"])
def test_a_value_http_client_cannot_encode_is_a_transport_error_not_a_crash(
    tmp_path: Path, kind: str
) -> None:
    with serve_raw(_short(len(BODY), BODY)) as endpoint:
        transport = Transport(endpoint, _online(tmp_path), "test")
        path = "/ok" if kind == "header" else "/café€"
        headers = {"If-Match": "€-etag"} if kind == "header" else {}
        with pytest.raises(TransportError):
            transport.get(path, headers=headers)
        # the dropped connection is not left half-written: the next request is clean
        assert transport.get("/ok").body(1000) == BODY
        transport.drop()


def test_foxglove_posts_with_a_value_that_cannot_be_encoded_fail_as_a_transport_error(
    tmp_path: Path,
) -> None:
    with serve_raw(_short(len(BODY), BODY)) as endpoint:
        transport = FoxgloveTransport(endpoint, _online(tmp_path), "test")
        with pytest.raises(TransportError):
            transport.post_stream_request({"recordingId": "r"}, {"Authorization": "€"})
        transport.drop()


# --- (b) timeout is bounded above ---------------------------------------------------------------


def _refusals(tmp_path: Path, timeout: float) -> dict[str, Callable[[], object]]:
    """Every connector, built with ``timeout`` where the operator sets it."""
    network = _online(tmp_path)
    s3_keys = {"s3_access_key_id": "AKID", "s3_secret_access_key": "s3cr3t"}
    export = Path(__file__).parent / "fixtures" / "connectors" / "rerun" / "catalog_export.json"
    return {
        "s3": lambda: s3_source(
            "s3://bucket/cell/", network=network, options={"timeout": timeout}, credentials=s3_keys
        ),
        "gcs": lambda: gcs_source(
            "gs://bucket/cell/",
            network=network,
            options={"timeout": timeout},
            credentials={"gcs_access_token": "ya29.x"},
        ),
        "azure": lambda: azure_source(
            "az://account/container/cell/",
            network=network,
            options={"timeout": timeout},
            credentials={"azure_sas_token": "sig=x&sp=rl"},
        ),
        "roboto": lambda: roboto_source(
            "roboto://org_a/ds_a/",
            network=network,
            options={"timeout": timeout},
            credentials={"roboto_api_token": "k3y"},
        ),
        "rerun": lambda: rerun_source(
            str(export),
            network=network,
            options={"storage": {"s3": {"timeout": timeout}}},
            credentials=s3_keys,
        ),
        "foxglove": lambda: foxglove_source(
            "foxglove://prj_a",
            network=network,
            options={"timeout": timeout},
            credentials={"foxglove_api_key": API_KEY},
        ),
    }


@pytest.mark.parametrize("timeout", [1e10, MAX_TIMEOUT + 1, 10**30, float("inf"), float("nan")])
@pytest.mark.parametrize("connector", ["s3", "gcs", "azure", "roboto", "rerun", "foxglove"])
def test_an_absurd_timeout_is_a_configuration_error_for_every_connector(
    tmp_path: Path, connector: str, timeout: float
) -> None:
    with pytest.raises((ObjectStoreConfigError, FoxgloveConfigError)):
        _refusals(tmp_path, timeout)[connector]()


@pytest.mark.parametrize("connector", ["s3", "gcs", "azure", "roboto", "rerun", "foxglove"])
def test_the_refusal_is_the_timeout_and_nothing_else_in_the_fixture(
    tmp_path: Path, connector: str
) -> None:
    """The same construction with a legal timeout works: the refusals above are the timeout."""
    source = _refusals(tmp_path, MAX_TIMEOUT)[connector]()
    close = getattr(source, "close", None)
    if close is not None:
        close()


def test_the_largest_allowed_timeout_is_accepted_by_foxglove(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    options: dict[str, Any] = {"timeout": MAX_TIMEOUT, "store": "fixture"}
    with fake.serve() as endpoint:
        source = foxglove_source(
            "foxglove://prj_a",
            network=_online(tmp_path),
            options={**options, "endpoint": endpoint},
            credentials={"foxglove_api_key": API_KEY},
        )
        source.close()


# --- (c) a body shorter than its declared Content-Length ----------------------------------------


def test_a_body_that_ends_before_its_content_length_is_a_short_read(tmp_path: Path) -> None:
    with serve_raw(_short(len(BODY) + 50, BODY)) as endpoint:
        transport = Transport(endpoint, _online(tmp_path), "test")
        with pytest.raises(ShortRead):
            transport.get("/x").body(1000)


def test_foxglove_link_responses_are_checked_against_their_content_length_too(
    tmp_path: Path,
) -> None:
    with serve_raw(_short(len(BODY) + 50, BODY)) as endpoint:
        transport = FoxgloveTransport(endpoint, _online(tmp_path), "test")
        with pytest.raises(ShortRead):
            transport.post_stream_request({"recordingId": "r"}, {}).body(1000)


@pytest.mark.parametrize("declared", ["abc", "-5", "١٢", "9" * 40, ""])
def test_a_content_length_that_is_not_a_valid_number_is_not_trusted_as_a_length(
    tmp_path: Path, declared: str
) -> None:
    response = f"HTTP/1.1 200 OK\r\nContent-Length: {declared}\r\n\r\n".encode() + BODY
    with serve_raw(response) as endpoint:
        transport = Transport(endpoint, _online(tmp_path), "test")
        try:
            body = transport.get("/x").body(1000)
        except TransportError:
            return  # http.client may refuse the header itself: also a structured error
        assert body == BODY  # no declared length to hold it to: it ends where the server did


def test_a_complete_body_is_returned_whole(tmp_path: Path) -> None:
    with serve_raw(_short(len(BODY), BODY)) as endpoint:
        transport = Transport(endpoint, _online(tmp_path), "test")
        assert transport.get("/x").body(1000) == BODY
