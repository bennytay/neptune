"""Reading a local source in place (ADR 0026): exact bytes, checked chunk by chunk, never copied."""

import io
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.discovery.reader import LocalReader, SourceChangedError
from neptune.discovery.source import LocalSource, SourceAccessError
from neptune.identity.hashing import digest_stream
from neptune.model.source import LocalPath

DATA = bytes(range(256)) * 3  # 768 bytes: 48 chunks of 16


def reader(tmp_path: Path, data: bytes = DATA, chunk_size: int = 16, **kwargs: int) -> LocalReader:
    (tmp_path / "log.bin").write_bytes(data)
    artifact = digest_stream(io.BytesIO(data), chunk_size=chunk_size)
    return LocalReader(LocalSource(tmp_path), LocalPath("log.bin"), artifact, **kwargs)


@settings(max_examples=50)
@given(st.integers(0, len(DATA)), st.integers(0, 100))
def test_reads_are_exact_across_chunk_boundaries(offset: int, length: int) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as directory, reader(Path(directory)) as source:
        assert source.read(offset, length) == DATA[offset : offset + length]
        assert (source.size, source.content_id) == (
            len(DATA),
            digest_stream(io.BytesIO(DATA)).content_id,
        )


def test_a_changed_chunk_is_refused_not_read(tmp_path: Path) -> None:
    source = reader(tmp_path)
    assert source.read(0, 4) == DATA[:4]
    with (tmp_path / "log.bin").open("r+b") as stream:
        stream.seek(100)
        stream.write(b"\xff")
    assert source.read(0, 4) == DATA[:4]  # chunk 0 was checked and is kept; it never changed
    with pytest.raises(SourceChangedError, match="chunk 6 changed"):
        source.read(96, 8)
    source.close()


def test_a_source_of_another_size_is_refused_at_open(tmp_path: Path) -> None:
    (tmp_path / "log.bin").write_bytes(DATA)
    artifact = digest_stream(io.BytesIO(DATA[:-1]), chunk_size=16)
    with pytest.raises(SourceChangedError, match="holds 768 bytes, not 767"):
        LocalReader(LocalSource(tmp_path), LocalPath("log.bin"), artifact)


def test_only_a_few_checked_chunks_are_kept(tmp_path: Path) -> None:
    source = reader(tmp_path, cache=2)
    source.read(0, len(DATA))
    assert len(source._cache) == 2  # bounded memory, whatever the source's size
    source.close()


def test_an_empty_source_reads_as_empty(tmp_path: Path) -> None:
    with reader(tmp_path, b"") as source:
        assert source.read(0, 10) == b""
        with pytest.raises(ValueError):
            source.read(1, 1)


def test_the_walk_policy_applies_symlinks_are_not_followed(tmp_path: Path) -> None:
    (tmp_path / "real.bin").write_bytes(DATA)
    (tmp_path / "link.bin").symlink_to("real.bin")
    artifact = digest_stream(io.BytesIO(DATA))
    with pytest.raises(SourceAccessError):
        LocalReader(LocalSource(tmp_path), LocalPath("link.bin"), artifact)


def test_bad_arguments_are_refused(tmp_path: Path) -> None:
    with reader(tmp_path) as source:
        for offset, length in ((-1, 1), (0, -1), (len(DATA) + 1, 0)):
            with pytest.raises(ValueError):
                source.read(offset, length)
    with pytest.raises(ValueError):
        reader(tmp_path, cache=0)
