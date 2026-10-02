"""The S3 connector against an in-process S3 server: identity, discovery, reads (ADR 0006)."""

import io
import random
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_object_store_fake import Entry, FakeStore
from neptune.identity import canonical_json
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory
from neptune.model.ids import ExternalObjectRef
from neptune.model.source import SourceArtifact
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.object_store import (
    ObjectEntry,
    ObjectReadError,
    ObjectStoreSource,
    s3_source,
)
from neptune_deploy.sources.object_store.clients import S3Client

CREDENTIALS = {"s3_access_key_id": "AKIDEXAMPLE", "s3_secret_access_key": "s3cr3t-never-printed"}


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


@contextmanager
def connect(
    fake: FakeStore,
    tmp_path: Path,
    prefix: str = "",
    *,
    ledger: SourceLedger | None = None,
    **options: Any,
) -> Iterator[ObjectStoreSource]:
    with fake.serve() as endpoint:
        yield s3_source(
            f"s3://{fake.bucket}/{prefix}",
            network=online(tmp_path),
            ledger=ledger,
            options={"endpoint": endpoint, **options},
            credentials=CREDENTIALS,
        )


def fingerprint(
    source: ObjectStoreSource, ledger: SourceLedger, entries: Iterator[Any] | tuple[Any, ...]
) -> dict[str, SourceArtifact]:
    """What the compiler's scan does with a walk: digest each object, observe it in the ledger."""
    artifacts = {}
    for entry in entries:
        if isinstance(entry, ObjectEntry):
            with source.open(entry.location) as stream:
                artifact = digest_stream(stream, chunk_size=1024 * 1024)
            ledger.observe(entry.location, artifact)
            artifacts[entry.key] = artifact
    return artifacts


def codes(source: ObjectStoreSource) -> list[str]:
    return sorted(finding.code for finding in source.findings())


# --- Identity and revisions --------------------------------------------------------------------


def test_a_versioned_bucket_lists_latest_versions_keyed_by_version_id(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("arm-cell/joint_states.mcap", b"first")
    second = fake.put("arm-cell/joint_states.mcap", b"second take")
    calib = fake.put("arm-cell/calib.yaml", b"k: 1\n")
    fake.put("arm-cell/removed.bag", b"gone soon")
    fake.delete("arm-cell/removed.bag")
    with connect(fake, tmp_path, "arm-cell/") as source:
        listing = source.listing()
    assert listing.complete
    assert [entry.key for entry in listing.entries] == [
        "arm-cell/calib.yaml",
        "arm-cell/joint_states.mcap",
    ]
    assert listing.entries[1].location == ExternalObjectRef(
        "deploy_s3", "fleet-logs/arm-cell/joint_states.mcap", f"version:{second.version_id}"
    )
    assert listing.entries[1].size == len(b"second take")
    assert listing.entries[0].location.revision_token == f"version:{calib.version_id}"
    assert source.findings() == ()


def test_an_unversioned_listing_is_keyed_by_etag(tmp_path: Path) -> None:
    fake = FakeStore(versioned=False)
    version = fake.put("amr-07/route.geojson", b"{}")
    with connect(fake, tmp_path, versions=False) as source:
        (entry,) = source.listing().entries
    assert entry.location.revision_token == f"etag:{version.etag}"
    assert {request.query.get("list-type") for request in fake.requests} == {"2"}


def test_a_changed_object_under_one_key_is_a_new_revision(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("quadruped/run.mcap", b"gait log v1")
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    (first,) = ledger.revisions()
    fake.put("quadruped/run.mcap", b"gait log v2, re-exported")
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [entry.key for entry in discovery.changed] == ["quadruped/run.mcap"]
        assert (discovery.new, discovery.unchanged, discovery.gone) == ((), (), ())
        fingerprint(source, ledger, source.walk())
    head = ledger.head(discovery.changed[0].location)
    assert head is not None and head.supersedes == (first.id,)
    assert len(ledger.revisions()) == 2 and len(ledger.artifacts()) == 2


def test_a_new_version_with_the_same_bytes_is_not_a_new_revision(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("arm/cal.yaml", b"same")
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.put("arm/cal.yaml", b"same")  # a new version id over identical bytes
    with connect(fake, tmp_path, ledger=ledger) as source:
        assert [entry.key for entry in source.discover(ledger).changed] == ["arm/cal.yaml"]
        fingerprint(source, ledger, source.walk())
    assert len(ledger.revisions()) == 1  # root ADR 0009: the token is observational


def test_discovery_probes_only_new_or_changed_objects(tmp_path: Path) -> None:
    fake = FakeStore()
    for name in ("a.mcap", "b.mcap", "c.mcap"):
        fake.put(f"fleet/{name}", name.encode() * 100)
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.put("fleet/b.mcap", b"changed")
    fake.put("fleet/d.mcap", b"new")
    fake.requests.clear()
    with connect(fake, tmp_path, ledger=ledger) as source:
        walked = [entry.key for entry in source.walk() if isinstance(entry, ObjectEntry)]
        assert walked == ["fleet/b.mcap", "fleet/d.mcap"]
        discovery = source.discover(ledger)
        assert [entry.key for entry, _ in discovery.unchanged] == ["fleet/a.mcap", "fleet/c.mcap"]
        fingerprint(source, ledger, source.walk())
    fetched = {request.path for request in fake.object_requests()}
    assert fetched == {"/fleet-logs/fleet/b.mcap", "/fleet-logs/fleet/d.mcap"}


def test_gone_objects_are_asserted_only_from_a_complete_listing(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("cell/a.csv", b"a")
    fake.put("cell/b.csv", b"b")
    ledger = SourceLedger()
    with connect(fake, tmp_path, page_size=1) as source:
        fingerprint(source, ledger, source.walk())
    fake.delete("cell/b.csv")
    fake.put("other/c.csv", b"outside the prefix: never gone from this source")
    with connect(fake, tmp_path, "cell/", page_size=1) as source:
        gone = source.discover(ledger).gone
    assert [revision.location.key for revision in gone] == [
        ("external", "deploy_s3", "fleet-logs/cell/b.csv")
    ]
    fake.put("cell/c.csv", b"c")
    fake.loop = True  # the listing cannot finish: nothing may be called gone
    with connect(fake, tmp_path, "cell/", page_size=1) as source:
        discovery = source.discover(ledger)
    assert not discovery.complete and discovery.gone == ()
    assert codes(source) == ["deploy_s3.pagination_loop"]


# --- Reads ---------------------------------------------------------------------------------------


def test_a_truncated_download_is_a_short_read_finding(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("amr/scan.bag", bytes(range(256)) * 64)
    fake.truncate_after = 100
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        for _ in range(2):  # the same failure is the same finding
            with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
                stream.read()
            assert raised.value.code == "short_read"
    (finding,) = source.findings()
    assert finding.code == "deploy_s3.short_read"
    assert finding.category is FindingCategory.FAILED
    assert finding.subject == entry.location
    assert finding.details == {"length": 16384, "offset": 0}


def test_reads_fetch_only_the_ranges_read(tmp_path: Path) -> None:
    fake = FakeStore()
    data = random.Random(7).randbytes(3 * 1024 * 1024 + 5)
    fake.put("humanoid/episode.mcap", data)
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        fake.requests.clear()
        with source.open(entry.location) as stream:
            assert stream.read(64) == data[:64]
            stream.seek(-5, io.SEEK_END)
            assert stream.read() == data[-5:]
        assert [r.headers["range"] for r in fake.object_requests()] == [
            "bytes=0-65535",  # a first read fetches one 64 KiB window, not the object
            f"bytes={len(data) - 5}-{len(data) - 1}",
        ]
        with source.open(entry.location) as stream:
            artifact = digest_stream(stream, chunk_size=1024 * 1024)
        fake.requests.clear()
        reader = source.reader(entry.location, artifact)
        assert reader.content_id == artifact.content_id and reader.size == len(data)
        assert reader.read(2 * 1024 * 1024 + 10, 100) == data[2 * 1024 * 1024 + 10 :][:100]
        assert reader.read(2 * 1024 * 1024, 20) == data[2 * 1024 * 1024 :][:20]  # cached
        assert reader.read(len(data), 10) == b""
    assert [r.headers["range"] for r in fake.object_requests()] == ["bytes=2097152-3145727"]
    assert all(r.query.get("versionId") for r in fake.object_requests())  # pinned


def test_bytes_that_no_longer_match_the_listing_are_object_changed(tmp_path: Path) -> None:
    fake = FakeStore(versioned=False)
    fake.put("arm/cal.yaml", b"0123456789")
    with connect(fake, tmp_path, versions=False) as source:
        (entry,) = source.listing().entries
        fake.put("arm/cal.yaml", b"9876543210")  # same size, other bytes: If-Match fails
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
    assert raised.value.code == "object_changed"
    assert codes(source) == ["deploy_s3.object_changed"]
    assert source.findings()[0].details == {"length": 10, "offset": 0, "status": 412}


def test_a_chunk_that_fails_its_hash_is_never_served(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("arm/cal.yaml", b"0123456789")
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        wrong = digest_stream(io.BytesIO(b"abcdefghij"), chunk_size=4)
        reader = source.reader(entry.location, wrong)
        with pytest.raises(ObjectReadError) as raised:
            reader.read(0, 2)
    assert raised.value.code == "object_changed"
    assert source.findings()[0].details == {"chunk": 0, "length": 4, "offset": 0}


def test_a_version_deleted_after_listing_is_object_gone(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("aerial/flight.ulg", b"ulog")
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        fake.history[b"aerial/flight.ulg"].clear()  # the version is permanently deleted
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
    assert raised.value.code == "object_gone"
    assert codes(source) == ["deploy_s3.object_gone"]


def test_a_store_that_ignores_ranges_is_read_from_zero_only(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("marine/sonar.bin", b"0123456789" * 20_000)
    fake.ignore_range = True
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        with source.open(entry.location) as stream:
            assert stream.read(10) == b"0123456789"  # a 200 from byte 0: only 64 KiB is read
            stream.seek(150_000)
            with pytest.raises(ObjectReadError) as raised:
                stream.read(5)
    assert raised.value.code == "read_failed"
    (finding,) = source.findings()
    assert finding.details == {"cause": "range_invalid", "length": 50_000, "offset": 150_000}


def test_opening_what_was_not_listed_is_refused(tmp_path: Path) -> None:
    fake = FakeStore()
    version = fake.put("amr/a.bag", b"a")
    with connect(fake, tmp_path) as source:
        stale = ExternalObjectRef("deploy_s3", "fleet-logs/amr/a.bag", "version:older")
        with pytest.raises(ObjectReadError) as raised:
            source.open(stale)
        assert raised.value.code == "not_listed"
        with pytest.raises(TypeError):
            source.open(ExternalObjectRef("deploy_gcs", "fleet-logs/amr/a.bag", version.version_id))
    assert source.findings() == ()


# --- The network boundary ----------------------------------------------------------------------


def test_a_local_only_workspace_refuses_the_source_before_any_request(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("amr/a.bag", b"a")
    with fake.serve() as endpoint:
        with pytest.raises(LocalOnlyError):
            s3_source(
                f"s3://{fake.bucket}/",
                network=Workspace(tmp_path / "home"),  # local-only by default
                options={"endpoint": endpoint},
                credentials=CREDENTIALS,
            )
        workspace = online(tmp_path)
        source = s3_source(
            f"s3://{fake.bucket}/",
            network=workspace,
            options={"endpoint": endpoint},
            credentials=CREDENTIALS,
        )
        workspace.allow_network(False)  # every request asks again
        with pytest.raises(LocalOnlyError):
            source.listing()
    assert fake.requests == []


def test_requests_are_signed_gets_and_secrets_reach_no_output(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("amr/a.bag", b"abc")
    fake.put("amr/b.bag", b"defg")
    fake.truncate_after = 1
    with connect(fake, tmp_path) as source:
        for entry in source.listing().entries:
            with pytest.raises(ObjectReadError), source.open(entry.location) as stream:
                stream.read()
    assert {request.method for request in fake.requests} == {"GET"}
    for request in fake.requests:
        authorization = request.headers["authorization"]
        assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
        assert "x-amz-content-sha256" in request.headers
    documents = [*(f.to_json() for f in source.findings()), source.transform.to_json()]
    texts = [canonical_json.dumps(d).decode("utf-8") for d in documents]
    assert len(documents) == 3  # two short reads and the transform
    for text in texts:  # outputs name neither the secret nor where the store is
        assert "s3cr3t" not in text and "127.0.0.1" not in text
    assert isinstance(source.client, S3Client)
    assert "s3cr3t" not in repr(source.client.credentials)


def test_anonymous_access_sends_no_credentials(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("public/map.pgm", b"P5")
    with fake.serve() as endpoint:
        source = s3_source(
            f"s3://{fake.bucket}/public/",
            network=online(tmp_path),
            options={"endpoint": endpoint, "anonymous": True},
            environ={},
        )
        assert [entry.key for entry in source.listing().entries] == ["public/map.pgm"]
    assert all("authorization" not in request.headers for request in fake.requests)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused_never_followed(tmp_path: Path, status: int) -> None:
    fake = FakeStore()
    fake.put("amr/a.bag", b"a")
    fake.redirect = status
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert listing.entries == () and not listing.complete
    (finding,) = source.findings()
    assert finding.code == "deploy_s3.redirect_refused"
    assert finding.details == {"page": 0, "status": status}
    assert len(fake.requests) == 1  # nothing went to the Location it named


def test_a_redirect_on_a_read_is_refused(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("amr/a.bag", b"a")
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        fake.redirect = 307
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
    assert raised.value.code == "redirect_refused"
    assert len(fake.object_requests()) == 1


# --- Hostile listings ----------------------------------------------------------------------------

NFC = unicodedata.normalize("NFC", "robots/café.mcap")
NFD = unicodedata.normalize("NFD", "robots/café.mcap")
HOSTILE_KEYS = {
    "robots/a/../b.mcap": b"dot-dot",
    "robots/b.mcap": b"the real b",
    "robots/a//b.mcap": b"double slash",
    "robots/./c.mcap": b"dot",
    "robots/%2e%2e/d.mcap": b"percent dots",
    "robots/space and+plus.mcap": b"space",
    "robots/new\nline.mcap": b"newline",
    "robots/tab\tand\x01ctl.mcap": b"control",
    NFC: b"composed",
    NFD: b"decomposed",
    "robots/" + "x" * 1017: b"exactly 1024 bytes",
}


def hostile_store() -> FakeStore:
    fake = FakeStore()
    for key, data in HOSTILE_KEYS.items():
        fake.put(key, data)
    fake.put(b"robots/" + b"y" * 1018, b"1025 bytes: longer than S3 allows")
    fake.put(b"robots/\xff\xfe-not-utf8", b"latin-1 bytes")
    return fake


def test_keys_are_kept_verbatim_and_never_normalised(tmp_path: Path) -> None:
    fake = hostile_store()
    with connect(fake, tmp_path, "robots/") as source:
        listing = source.listing()
        keys = [entry.key for entry in listing.entries]
        assert keys == sorted(HOSTILE_KEYS)
        assert NFC in keys and NFD in keys and NFC != NFD  # two objects, never merged
        for entry in listing.entries:
            with source.open(entry.location) as stream:
                assert stream.read() == HOSTILE_KEYS[entry.key]  # its own bytes, not a neighbour's
    findings = {finding.code: finding for finding in source.findings()}
    assert sorted(findings) == ["deploy_s3.key_not_utf8", "deploy_s3.key_too_long"]
    assert findings["deploy_s3.key_not_utf8"].details == {
        "count": 1,
        "keys_hex": [b"robots/\xff\xfe-not-utf8".hex()],
    }
    assert findings["deploy_s3.key_too_long"].details["count"] == 1
    assert [s.reason for s in listing.skipped] == ["key_not_utf8", "key_too_long"]


def test_keys_outside_the_prefix_and_ambiguous_keys_are_not_used(tmp_path: Path) -> None:
    fake = FakeStore()
    kept = fake.put("cell/a.csv", b"a")
    fake.put("cell/b.csv", b"b")
    other = fake.put("secrets/creds.txt", b"not asked for")

    def rewrite(number: int, entries: list[Entry]) -> list[Entry]:
        injected = [Entry(b"secrets/creds.txt", other, True)]
        repeat = [e for e in entries if e.key == b"cell/a.csv"]  # an identical repeat is harmless
        twin = [Entry(b"cell/b.csv", kept, True) for e in entries if e.key == b"cell/b.csv"]
        return entries + injected + repeat + twin

    fake.rewrite = rewrite
    with connect(fake, tmp_path, "cell/") as source:
        listing = source.listing()
    assert [entry.key for entry in listing.entries] == ["cell/a.csv"]
    assert codes(source) == ["deploy_s3.key_duplicated", "deploy_s3.key_outside_prefix"]


def test_a_listing_declaring_a_document_type_is_refused_unparsed(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("amr/a.bag", b"a")
    fake.doctype = True
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert listing.entries == () and not listing.complete
    assert codes(source) == ["deploy_s3.response_invalid"]


def test_the_object_limit_stops_the_listing_and_says_so(tmp_path: Path) -> None:
    fake = FakeStore()
    for index in range(10):
        fake.put(f"fleet/{index:02d}.bag", b"x")
    with connect(fake, tmp_path, max_objects=4, page_size=3) as source:
        listing = source.listing()
    assert [entry.key for entry in listing.entries] == [f"fleet/{i:02d}.bag" for i in range(4)]
    assert not listing.complete
    assert codes(source) == ["deploy_s3.listing_limit"]


# --- Determinism ---------------------------------------------------------------------------------


def _run(tmp_path: Path, seed: int | None, page_size: int) -> tuple[Any, ...]:
    fake = hostile_store()
    fake.shuffle = random.Random(seed) if seed is not None else None
    with connect(fake, tmp_path, "robots/", page_size=page_size) as source:
        listing = source.listing()
    return (
        listing,
        tuple(f.id for f in source.findings()),
        source.transform.id,
    )


def test_listing_and_findings_do_not_depend_on_pages_or_their_order(tmp_path: Path) -> None:
    reference = _run(tmp_path, None, 1000)
    for seed, page_size in ((1, 1), (2, 3), (3, 7), (4, 1000)):
        assert _run(tmp_path, seed, page_size) == reference
    assert reference[0].complete and len(reference[0].entries) == len(HOSTILE_KEYS)
