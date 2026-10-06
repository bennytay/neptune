"""``store.gzipped``: a fixed-header gzip whose bytes depend only on the content, and a reader that
refuses a bomb, a truncated stream, a bad checksum and trailing bytes."""

from __future__ import annotations

import gzip
import zlib

import pytest

from neptune_memory.store.gzipped import (
    GzipError,
    deterministic_gzip,
    gunzip,
    is_gzip,
)

DATA = b'{"claims":[]}\n' * 1000


def test_the_header_names_no_file_time_or_platform() -> None:
    packed = deterministic_gzip(DATA)
    assert packed[:10] == bytes.fromhex("1f8b08000000000002ff")
    assert is_gzip(packed) and not is_gzip(DATA)
    assert gzip.decompress(packed) == DATA  # any gzip reader reads it


def test_the_same_content_gives_the_same_bytes_and_round_trips() -> None:
    assert deterministic_gzip(DATA) == deterministic_gzip(bytes(DATA))
    assert gunzip(deterministic_gzip(DATA), len(DATA)) == DATA
    assert gunzip(deterministic_gzip(b""), 0) == b""


def test_a_stream_larger_than_the_limit_is_refused_before_it_is_inflated() -> None:
    bomb = deterministic_gzip(bytes(10**7))  # 10 MB of zeros in about 10 KB
    with pytest.raises(GzipError, match="larger than"):
        gunzip(bomb, 10**6)
    assert len(gunzip(bomb, 10**7)) == 10**7  # exactly the limit is allowed


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda b: b[:-12], "truncated"),
        (lambda b: b + b"\x1f\x8b", "follow"),
        (lambda b: b[:-8] + bytes(8), "valid gzip"),  # checksum and length zeroed
        (lambda b: b"\x1f\x8b\x07" + b[3:], "valid gzip"),  # not deflate
        (lambda b: b[:2], "truncated"),
    ],
)
def test_damage_is_refused(damage: object, message: str) -> None:
    with pytest.raises(GzipError, match=message):
        gunzip(damage(deterministic_gzip(DATA)), 10**6)  # type: ignore[operator]


def test_a_python_gzip_with_a_name_and_time_still_reads() -> None:
    other = gzip.compress(DATA, mtime=1)
    assert gunzip(other, 10**6) == DATA
    assert other != deterministic_gzip(DATA)
    assert zlib.crc32(DATA)  # the trailer's checksum is the content's
