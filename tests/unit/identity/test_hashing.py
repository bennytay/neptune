import hashlib
import io

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity.hashing import DEFAULT_CHUNK_SIZE, content_id, digest_stream

EMPTY_SHA256 = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class ShortReads(io.RawIOBase):
    """Returns at most ``step`` bytes per read, like a pipe or network stream."""

    def __init__(self, data: bytes, step: int) -> None:
        self._data = data
        self._pos = 0
        self._step = step

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        limit = self._step if size < 0 else min(size, self._step)
        block = self._data[self._pos : self._pos + limit]
        self._pos += len(block)
        return block


def reference_chunks(data: bytes, chunk_size: int) -> tuple[str, ...]:
    return tuple(
        "sha256:" + hashlib.sha256(data[i : i + chunk_size]).hexdigest()
        for i in range(0, len(data), chunk_size)
    )


def test_empty_source_has_no_chunks() -> None:
    artifact = digest_stream(io.BytesIO(b""))
    assert artifact.content_id == EMPTY_SHA256 == content_id(b"")
    assert artifact.size == 0
    assert artifact.chunks == ()
    assert artifact.chunk_size == DEFAULT_CHUNK_SIZE


@pytest.mark.parametrize(("size", "expected_chunks"), [(1, 1), (15, 1), (16, 1), (17, 2), (32, 2)])
def test_chunk_boundaries(size: int, expected_chunks: int) -> None:
    data = bytes(range(size))
    artifact = digest_stream(io.BytesIO(data), chunk_size=16)
    assert len(artifact.chunks) == expected_chunks
    assert artifact.chunks == reference_chunks(data, 16)
    assert artifact.content_id == content_id(data)


def test_single_chunk_hash_equals_content_id() -> None:
    artifact = digest_stream(io.BytesIO(b"hello"))
    assert artifact.chunks == (artifact.content_id,)


def test_default_chunk_size_boundary() -> None:
    data = b"\x5a" * (DEFAULT_CHUNK_SIZE + 1)
    artifact = digest_stream(io.BytesIO(data))
    assert artifact.chunks == reference_chunks(data, DEFAULT_CHUNK_SIZE)


def test_reads_from_current_position() -> None:
    stream = io.BytesIO(b"headerbody")
    stream.seek(6)
    assert digest_stream(stream).content_id == content_id(b"body")


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_rejects_non_positive_chunk_size(chunk_size: int) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        digest_stream(io.BytesIO(b"x"), chunk_size=chunk_size)


@given(st.binary(max_size=300), st.integers(1, 64), st.integers(1, 70))
def test_short_reads_never_move_chunk_boundaries(data: bytes, chunk_size: int, step: int) -> None:
    artifact = digest_stream(ShortReads(data, step), chunk_size=chunk_size)  # type: ignore[arg-type]
    assert artifact.content_id == content_id(data)
    assert artifact.size == len(data)
    assert artifact.chunks == reference_chunks(data, chunk_size)


@given(st.binary(max_size=300), st.integers(1, 64), st.integers(1, 64))
def test_chunk_size_never_changes_content_id(data: bytes, a: int, b: int) -> None:
    assert (
        digest_stream(io.BytesIO(data), chunk_size=a).content_id
        == digest_stream(io.BytesIO(data), chunk_size=b).content_id
    )
