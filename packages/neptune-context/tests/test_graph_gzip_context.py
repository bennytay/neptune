"""Gzip graph documents (MVL-147): Memory's pipeline-built snapshot is a ``.json.gz``.

``read_graph_document`` reads a name ending ``.gz`` as one gzip member and nothing else, and holds
the size cap against what the stream decompresses to, not only the file's size: the cap is
patched small here so a bomb is generated in the test (a few kilobytes that expand past it) and
nothing large is committed. Every refusal is a ``ValueError`` (the CLI's exit 2), never a crash.
"""

from __future__ import annotations

import gzip
import json
import os
import zlib
from typing import TYPE_CHECKING, Any

import pytest

import retrieve_fixtures_context as F
from neptune_context import engine, pins
from neptune_context.engine import read_graph_document
from neptune_context.mcp.__main__ import main

if TYPE_CHECKING:
    from pathlib import Path

CAP = 1024 * 1024


def document_bytes() -> bytes:
    return json.dumps(F.demo_document().to_json(), sort_keys=True).encode()


def gz(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_the_frozen_snapshot_is_a_small_graph_schema_2_0_0_gzip_the_codec_reads() -> None:
    raw = F.DEMO_SNAPSHOT.read_bytes()
    assert raw[:2] == b"\x1f\x8b" and F.DEMO_SNAPSHOT.name.endswith(".json.gz")
    assert len(raw) < 512 * 1024  # the repository's fixture limit
    document = read_graph_document(F.DEMO_SNAPSHOT)
    # Frozen at 2.0.0; a 2.x document of an older minor reads as written under the 2.2.0 pin.
    assert document.to_json()["graph_schema"] == "2.0.0"
    assert pins.GRAPH_SCHEMA_VERSION.split(".")[0] == "2"
    assert int(document.head) == 2 and document.resolution.claims


def test_a_gzip_and_a_plain_document_read_the_same(tmp_path: Path) -> None:
    data = document_bytes()
    plain = tmp_path / "graph.json"
    plain.write_bytes(data)
    zipped = gz(tmp_path / "graph.json.gz", gzip.compress(data, mtime=0))
    assert read_graph_document(zipped).to_json() == read_graph_document(plain).to_json()
    assert read_graph_document(zipped).to_json() == read_graph_document(zipped).to_json()


def test_the_cap_holds_to_the_byte_on_what_a_gzip_decompresses_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = document_bytes()
    zipped = gz(tmp_path / "graph.json.gz", gzip.compress(data, mtime=0))
    monkeypatch.setattr(engine, "MAX_GRAPH_BYTES", len(data))
    assert read_graph_document(zipped).head  # exactly the cap: read
    monkeypatch.setattr(engine, "MAX_GRAPH_BYTES", len(data) - 1)
    with pytest.raises(ValueError, match="decompresses to more than"):
        read_graph_document(zipped)


def test_a_gzip_bomb_is_refused_without_being_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "MAX_GRAPH_BYTES", CAP)
    # 200 MiB of one byte deflates to ~200 KB: under the cap as a file, 200 times it expanded.
    bomb = zlib.compressobj(wbits=zlib.MAX_WBITS | 16)
    packed = b"".join(bomb.compress(b"0" * (1 << 20)) for _ in range(200)) + bomb.flush()
    path = gz(tmp_path / "bomb.json.gz", packed)
    assert len(packed) < CAP
    expanded: list[int] = []
    real = zlib.decompressobj

    def watched(*args: Any, **kwargs: Any) -> object:
        decoder = real(*args, **kwargs)

        class Watch:
            eof = property(lambda self: decoder.eof)
            unconsumed_tail = property(lambda self: decoder.unconsumed_tail)
            unused_data = property(lambda self: decoder.unused_data)

            def decompress(self, data: bytes, max_length: int = 0) -> bytes:
                out = decoder.decompress(data, max_length)
                expanded.append(len(out))
                return out

        return Watch()

    monkeypatch.setattr(zlib, "decompressobj", watched)  # the one engine._read_gzip calls
    with pytest.raises(ValueError, match="decompresses to more than"):
        read_graph_document(path)
    assert sum(expanded) <= CAP + 1  # never more than the cap and one byte, however big the bomb


def test_a_compressed_file_larger_than_the_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "MAX_GRAPH_BYTES", 4096)
    noise = gzip.compress(os.urandom(64 * 1024), mtime=0)  # incompressible: the file itself is big
    with pytest.raises(ValueError, match="larger than 4096 bytes"):
        read_graph_document(gz(tmp_path / "noise.json.gz", noise))


def test_a_truncated_gzip_is_refused(tmp_path: Path) -> None:
    whole = F.DEMO_SNAPSHOT.read_bytes()
    for cut in (len(whole) - 1, len(whole) - 8, len(whole) // 2, 10, 2):
        with pytest.raises(ValueError, match="truncated"):
            read_graph_document(gz(tmp_path / f"cut{cut}.json.gz", whole[:cut]))
    with pytest.raises(ValueError, match="truncated"):
        read_graph_document(gz(tmp_path / "empty.json.gz", b""))


def test_bytes_that_are_not_gzip_are_refused_under_a_gz_name(tmp_path: Path) -> None:
    for name, data in {
        "json": document_bytes(),  # a plain document is not gzip, whatever it is called
        "text": b"not gzip at all, not even close",
        "magic-only": b"\x1f\x8b\x08\x00garbage-after-the-magic",
        "zlib": zlib.compress(document_bytes()),
    }.items():
        with pytest.raises(ValueError, match="gzip"):
            read_graph_document(gz(tmp_path / f"{name}.json.gz", data))


def test_a_corrupt_gzip_is_refused(tmp_path: Path) -> None:
    whole = bytearray(F.DEMO_SNAPSHOT.read_bytes())
    whole[len(whole) // 2] ^= 0xFF  # a flipped byte in the deflate stream
    with pytest.raises(ValueError, match="gzip"):
        read_graph_document(gz(tmp_path / "flipped.json.gz", bytes(whole)))
    checksum = bytearray(F.DEMO_SNAPSHOT.read_bytes())
    checksum[-8] ^= 0xFF  # the CRC32 trailer
    with pytest.raises(ValueError, match="gzip"):
        read_graph_document(gz(tmp_path / "crc.json.gz", bytes(checksum)))


def test_trailing_bytes_and_a_second_member_are_refused(tmp_path: Path) -> None:
    whole = F.DEMO_SNAPSHOT.read_bytes()
    second = gzip.compress(b"{}", mtime=0)
    for name, data in {
        "garbage": whole + b"trailing garbage",
        "zeros": whole + b"\x00" * 16,
        "member": whole + second,
        "twice": whole + whole,
    }.items():
        with pytest.raises(ValueError, match="after its gzip member"):
            read_graph_document(gz(tmp_path / f"{name}.json.gz", data))


def test_a_gzip_of_something_that_is_not_a_graph_is_refused(tmp_path: Path) -> None:
    for name, payload in {
        "empty": b"",
        "array": b"[]",
        "nan": b'{"head": NaN}',
        "duplicate": b'{"kind": "memory.graph", "kind": "memory.graph"}',
        "deep": b"[" * 100_000 + b"]" * 100_000,
    }.items():
        with pytest.raises(ValueError):
            read_graph_document(gz(tmp_path / f"{name}.json.gz", gzip.compress(payload, mtime=0)))


def test_only_a_regular_file_is_read_under_a_gz_name(tmp_path: Path) -> None:
    fifo = tmp_path / "graph.json.gz"
    os.mkfifo(fifo)  # refused before it is opened: a FIFO would block a reader forever
    with pytest.raises(ValueError, match="not a regular file"):
        read_graph_document(fifo)
    folder = tmp_path / "folder.json.gz"
    folder.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        read_graph_document(folder)
    link = tmp_path / "link.json.gz"
    link.symlink_to(F.DEMO_SNAPSHOT)  # a link to a regular file is that file
    assert read_graph_document(link).head == read_graph_document(F.DEMO_SNAPSHOT).head
    dangling = tmp_path / "dangling.json.gz"
    dangling.symlink_to(tmp_path / "nowhere.json.gz")
    with pytest.raises(OSError):
        read_graph_document(dangling)


def test_the_cli_serves_a_gz_graph_and_refuses_a_bad_one_with_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    whole = F.DEMO_SNAPSHOT.read_bytes()
    bad = [
        gz(tmp_path / "truncated.json.gz", whole[:-20]),
        gz(tmp_path / "text.json.gz", b"plain text"),
        gz(tmp_path / "trailing.json.gz", whole + b"junk"),
        gz(tmp_path / "member.json.gz", whole + whole),
    ]
    for path in bad:
        assert main(["--memory", str(path)]) == 2
    err = capsys.readouterr().err
    assert err.count("neptune mcp:") == len(bad)
    assert "truncated" in err and "not a valid gzip" in err and "after its gzip member" in err
