"""A connector's source: classification against the ledger, the spool, verified reads (ADR 0067).

The connector is the in-process fake (``tests/fixtures/sources/fake_object_store.py``); nothing
here opens a socket.
"""

import io
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.discovery.external import (
    ExternalReader,
    ExternalRoot,
    ExternalSource,
    ExternalSourceError,
    Spool,
    fingerprint_external,
    listed_entries,
)
from neptune.discovery.policy import SIZE_CHANGED, UNREADABLE
from neptune.discovery.reader import SourceChangedError
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune.model.source import SourceRevision

SMALL: Final = 16  # a chunk size that splits the test objects into several chunks


class _Network:
    def require_network(self, purpose: str) -> None:
        return None


@pytest.fixture
def fake(fake_store: ModuleType) -> ModuleType:
    return fake_store


@pytest.fixture
def store(fake: ModuleType, tmp_path: Path) -> Path:
    store = tmp_path / "store"
    store.mkdir()
    fake.put(store, "p/a.txt", b"alpha bytes, long enough for three chunks\n", "a1")
    fake.put(store, "p/b.txt", b"beta\n", "b1")
    return store


def source(fake: ModuleType, store: Path) -> Any:
    return fake.make("fake://bucket/p/", network=_Network(), options={"store": str(store)})


def scan(fake: ModuleType, store: Path, ledger: SourceLedger, spool: Spool) -> Any:
    built = source(fake, store)
    return fingerprint_external(built, ledger, built.discover(ledger), spool, chunk_size=SMALL)


def ref(key: str, token: str) -> ExternalObjectRef:
    return ExternalObjectRef("fake_store", f"bucket/{key}", token)


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    (tmp_path / "spool").mkdir()
    return Spool(tmp_path / "spool")


def test_the_fake_has_the_shape_the_compiler_reads(fake: ModuleType, store: Path) -> None:
    assert isinstance(source(fake, store), ExternalSource)
    root = ExternalRoot("fake://bucket/p/", "fake_store", source(fake, store))
    assert root.connector == "fake_store"
    with pytest.raises(ExternalSourceError):
        ExternalRoot("fake://bucket/p/", "other", source(fake, store))
    with pytest.raises(ExternalSourceError):
        ExternalRoot("fake://bucket/p/", "fake_store", object())  # type: ignore[arg-type]


def test_new_objects_are_fetched_once_into_the_spool_and_hashed(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    ledger = SourceLedger()
    result = scan(fake, store, ledger, spool)
    assert [item.fetched for item in result.listed] == [True, True]
    assert [item.location for item in result.listed] == [ref("p/a.txt", "a1"), ref("p/b.txt", "b1")]
    for item in result.listed:
        content = item.observation.revision.content_id
        assert spool.holds(content)
        with spool.open(content) as copy:  # type: ignore[union-attr]
            assert digest_stream(copy).content_id == content
    assert result.complete and result.absences == () and result.findings == ()


def test_recognised_objects_are_carried_forward_unfetched(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    ledger = SourceLedger()
    scan(fake, store, ledger, spool)
    fake.READS.clear()
    again = scan(fake, store, ledger, spool)
    assert [item.fetched for item in again.listed] == [False, False]
    assert fake.READS == []


def test_a_retokened_object_is_fetched_once_then_recognised_though_the_connector_says_changed(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    ledger = SourceLedger()
    scan(fake, store, ledger, spool)
    fake.retoken(store, "p/a.txt", "a2")
    second = scan(fake, store, ledger, spool)
    assert [item.fetched for item in second.listed] == [True, False]
    built = source(fake, store)
    (changed,) = built.discover(ledger).changed  # it compares the first token only
    assert changed.location == ref("p/a.txt", "a2")
    fake.READS.clear()
    third = fingerprint_external(built, ledger, built.discover(ledger), spool, chunk_size=SMALL)
    assert [item.fetched for item in third.listed] == [False, False] and fake.READS == []
    assert third.listed[0].location.revision_token == "a2"


def test_gone_objects_become_absences_only_from_a_complete_listing(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    ledger = SourceLedger()
    scan(fake, store, ledger, spool)
    fake.remove(store, "p/b.txt")
    fake.put(store, "p/c.txt", b"gamma\n", "c1")
    fake.set_incomplete(store, True)  # lists only the first of the two
    partial = scan(fake, store, ledger, spool)
    assert partial.absences == () and not partial.complete
    fake.set_incomplete(store, False)
    full = scan(fake, store, ledger, spool)
    (absence,) = full.absences
    assert absence.location == ref("p/b.txt", "b1")


def test_an_object_that_fails_to_read_is_unread_with_a_finding(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    fake.put(store, "p/bad.bin", b"\x00" * 40, "x1", fail=True)
    ledger = SourceLedger()
    result = scan(fake, store, ledger, spool)
    assert result.unread == (ref("p/bad.bin", "x1"),)
    (finding,) = result.findings
    assert finding.code == UNREADABLE and finding.subject == ref("p/bad.bin", "x1")
    assert finding.details == {"cause": "read_failed", "error": "ObjectReadError"}
    assert ledger.head(ref("p/bad.bin", "x1")) is None  # nothing asserted about it


def test_an_object_served_at_another_size_than_listed_is_unread(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    built = source(fake, store)
    served = {"p/a.txt": b"alpha", "p/b.txt": b"beta, grown since it was listed\n"}

    class Resized(type(built)):  # type: ignore[misc]
        def open(self, location: Any) -> Any:
            return io.BytesIO(served[location.object_id.removeprefix("bucket/")])

    resized = Resized(built.bucket, built.prefix, built.store)
    ledger = SourceLedger()
    result = fingerprint_external(resized, ledger, resized.discover(ledger), spool)
    assert result.unread == (ref("p/a.txt", "a1"), ref("p/b.txt", "b1"))
    assert [f.code for f in result.findings] == [SIZE_CHANGED, SIZE_CHANGED]
    assert [f.details for f in result.findings] == [
        {"size_fetched": 5, "size_listed": 42},
        {"size_fetched": 6, "size_listed": 5},  # one byte past the listing, never more
    ]
    assert ledger.revisions() == () and list(spool.directory.iterdir()) == []


@dataclass(frozen=True)
class _Discovery:
    new: tuple[Any, ...] = ()
    changed: tuple[Any, ...] = ()
    unchanged: tuple[Any, ...] = ()
    gone: tuple[Any, ...] = ()
    complete: bool = True


def test_a_connector_that_breaks_the_protocol_is_refused(
    fake: ModuleType, store: Path, spool: Spool
) -> None:
    built = source(fake, store)
    entry = built.discover(SourceLedger()).new[0]
    other = replace(entry, location=ExternalObjectRef("other", "bucket/p/a.txt", "a1"))
    for broken in (
        _Discovery(new=(entry, entry)),  # one object twice
        _Discovery(new=(other,)),  # another connector's object
        _Discovery(new=(replace(entry, size=-1),)),
        _Discovery(new=(replace(entry, size=True),)),
    ):
        with pytest.raises(ExternalSourceError):
            listed_entries(built, broken)
    ledger = SourceLedger()
    observed = ledger.observe(entry.location, digest_stream(io.BytesIO(b"x"))).revision
    stranger = SourceRevision(observed.id, ref("p/zz.txt", "z"), observed.content_id, ())
    for gone in (
        _Discovery(new=(entry,), gone=(observed,)),  # listed and gone at once
        _Discovery(gone=(stranger,)),  # not the ledger's head
    ):
        with pytest.raises(ExternalSourceError):
            fingerprint_external(built, ledger, gone, spool)


def test_a_reader_reads_ranges_lazily_and_verifies_every_chunk(
    fake: ModuleType, store: Path, spool: Spool, tmp_path: Path
) -> None:
    ledger = SourceLedger()
    built = source(fake, store)
    result = fingerprint_external(built, ledger, built.discover(ledger), spool, chunk_size=SMALL)
    artifact = ledger.artifact(result.listed[0].observation.revision.content_id)
    assert artifact is not None and len(artifact.chunks) == 3
    empty = Spool(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    fake.READS.clear()
    with ExternalReader(built, ref("p/a.txt", "a1"), artifact, empty) as reader:
        assert reader.read(17, 5) == b" enou"  # inside the second chunk
        assert fake.READS and min(start for _, start, _ in fake.READS) == 16  # not from 0
        assert reader.read(0, 5) == b"alpha"
        assert not empty.holds(artifact.content_id)  # ranged reads spool nothing
    # Bytes the store now serves differently fail their chunk's hash.
    (store / "objects" / "p" / "a.txt").write_bytes(b"ALPHA BYTES, long enough for three chunks\n")
    reader = ExternalReader(built, ref("p/a.txt", "a1"), artifact, empty)
    with reader, pytest.raises(SourceChangedError):
        reader.read(0, 5)


def test_a_reader_spools_the_object_for_a_sandboxed_call(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    ledger = SourceLedger()
    built = source(fake, store)
    (tmp_path / "first").mkdir()
    result = fingerprint_external(
        built, ledger, built.discover(ledger), Spool(tmp_path / "first"), chunk_size=SMALL
    )
    artifact = ledger.artifact(result.listed[0].observation.revision.content_id)
    assert artifact is not None
    (tmp_path / "later").mkdir()
    later = Spool(tmp_path / "later")  # a later job: nothing spooled yet
    with ExternalReader(built, ref("p/a.txt", "a1"), artifact, later) as reader:
        descriptor = reader.fileno()
        assert later.holds(artifact.content_id) and descriptor >= 0
        fake.READS.clear()
        assert reader.read(0, 5) == b"alpha" and fake.READS == []  # from the copy
    (store / "objects" / "p" / "a.txt").write_bytes(b"ALPHA BYTES, long enough for three chunks\n")
    (tmp_path / "third").mkdir()
    third = Spool(tmp_path / "third")
    reader = ExternalReader(built, ref("p/a.txt", "a1"), artifact, third)
    with reader, pytest.raises(SourceChangedError):
        reader.fileno()
    assert not third.holds(artifact.content_id)
    assert [p.name for p in (tmp_path / "third").iterdir()] == []  # nothing half-kept


def test_a_spool_reads_no_more_than_one_byte_past_the_listing(tmp_path: Path) -> None:
    spool = Spool(tmp_path)
    endless = io.BytesIO(b"x" * 1000)
    artifact = spool.fill(endless, size=10)
    assert artifact.size == 11 and not spool.holds(artifact.content_id)
    assert endless.tell() == 11
