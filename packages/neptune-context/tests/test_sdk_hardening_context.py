"""SDK hardening carried from the MVL-110 review (MVL-147): whole-answer deadline, stub reads."""

from __future__ import annotations

import os
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest

from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import MAX_PACKET_BYTES
from neptune_context.sdk import Client, ErrorCode, RetryPolicy, SdkError, StubEngine
from sdk_testing_context import golden_packet, golden_query

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def _server(script: Callable[[Any], None]) -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run() -> None:
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(5)
            try:
                connection.recv(65536)  # the request head (and usually the body)
                script(connection)
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        listener.close()
        thread.join(10)


def _drip(chunks: list[bytes], pause: float) -> Callable[[Any], None]:
    def script(connection: Any) -> None:
        for chunk in chunks:
            connection.sendall(chunk)
            time.sleep(pause)

    return script


def _timed(url: str, timeout: float = 0.5) -> tuple[SdkError, float]:
    started = time.monotonic()
    with pytest.raises(SdkError) as raised:
        Client(url, timeout=timeout, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
    return raised.value, time.monotonic() - started


def test_a_server_that_drips_its_headers_hits_the_whole_answer_deadline() -> None:
    # One header byte every 0.1 s never trips a per-wait timeout of 0.5 s; the deadline does.
    head = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 200 + b"\r\n\r\n"
    for url in _server(_drip([bytes([b]) for b in head], 0.1)):
        error, elapsed = _timed(url)
        assert error.code is ErrorCode.TIMEOUT and error.retryable
        assert elapsed < 3


def test_a_server_that_drips_its_status_line_hits_the_deadline_too() -> None:
    for url in _server(_drip([b"H", b"T", b"T", b"P"] * 50, 0.1)):
        error, elapsed = _timed(url)
        assert error.code is ErrorCode.TIMEOUT
        assert elapsed < 3


def test_a_server_that_never_answers_times_out_within_the_deadline() -> None:
    for url in _server(lambda c: time.sleep(3)):
        error, elapsed = _timed(url)
        assert error.code is ErrorCode.TIMEOUT
        assert elapsed < 2.5


def test_a_prompt_answer_is_unaffected_by_the_deadline() -> None:
    body = canonical_bytes(golden_packet("q01"))
    reply = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    for url in _server(lambda c: c.sendall(reply)):
        packet = Client(url, timeout=5, retry=RetryPolicy(attempts=1)).query(golden_query("q01"))
        assert packet == golden_packet("q01")


# --- StubEngine.from_directory ------------------------------------------------------------------


def _write_packet(path: Path) -> None:
    path.write_bytes(canonical_bytes(golden_packet("q01")))


def test_symlinks_and_non_regular_files_are_skipped_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_packet(outside / "q02.json")
    folder = tmp_path / "packets"
    folder.mkdir()
    _write_packet(folder / "q01.json")
    (folder / "linked.json").symlink_to(outside / "q02.json")
    (folder / "dir.json").mkdir()
    (folder / "notes.txt").write_text("not a packet", encoding="utf-8")
    if hasattr(os, "mkfifo"):
        os.mkfifo(folder / "pipe.json")  # reading it would block forever
    stub = StubEngine.from_directory(folder)
    assert stub.query_ids == (golden_packet("q01").query_id,)


def test_an_oversized_file_is_refused_before_it_is_read(tmp_path: Path) -> None:
    folder = tmp_path / "packets"
    folder.mkdir()
    big = folder / "big.json"
    with big.open("wb") as handle:
        handle.truncate(MAX_PACKET_BYTES + 1)  # sparse: no bytes written, size over the cap
    started = time.monotonic()
    with pytest.raises(SdkError) as raised:
        StubEngine.from_directory(folder)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert "over the packet maximum" in raised.value.message
    assert time.monotonic() - started < 1  # never read 64 MiB


def test_a_directory_with_no_packets_is_refused(tmp_path: Path) -> None:
    (tmp_path / "q01.json").mkdir()
    with pytest.raises(SdkError) as raised:
        StubEngine.from_directory(tmp_path)
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
