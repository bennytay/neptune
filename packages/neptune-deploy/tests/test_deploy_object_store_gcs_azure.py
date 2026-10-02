"""GCS and Azure Blob through the same source as S3: identity, revisions, reads (ADR 0006 §2)."""

import random
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_object_store_fake import FakeStore
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune.store.workspace import Workspace
from neptune_deploy.sources.object_store import (
    ObjectEntry,
    ObjectReadError,
    ObjectStoreConfigError,
    ObjectStoreSource,
    azure_source,
    gcs_source,
)

SAS = "sv=2021-08-06&ss=b&srt=co&sp=rl&se=2030-01-01T00:00:00Z&sig=c2lnbmF0dXJl%2Bx%3D"
PROVIDERS = ("gcs", "azure")


def _store(provider: str) -> FakeStore:
    if provider == "gcs":
        return FakeStore(provider="gcs", bucket="fleet-logs")
    return FakeStore(provider="azure", bucket="fleet-logs", sas_required=True)


@contextmanager
def connect(
    fake: FakeStore,
    tmp_path: Path,
    prefix: str = "",
    *,
    ledger: SourceLedger | None = None,
    **options: Any,
) -> Iterator[ObjectStoreSource]:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    with fake.serve() as endpoint:
        if fake.provider == "gcs":
            yield gcs_source(
                f"gs://{fake.bucket}/{prefix}",
                network=workspace,
                ledger=ledger,
                options={"endpoint": endpoint, **options},
                credentials={"gcs_access_token": "ya29.token-never-printed"},
            )
        else:
            yield azure_source(
                f"az://{fake.account}/{fake.bucket}/{prefix}",
                network=workspace,
                ledger=ledger,
                options={"endpoint": endpoint, **options},
                credentials={"azure_sas_token": SAS},
            )


def _connector(fake: FakeStore) -> str:
    return "deploy_gcs" if fake.provider == "gcs" else "deploy_azure_blob"


def _scope(fake: FakeStore) -> str:
    return "fleet-logs/" if fake.provider == "gcs" else f"{fake.account}/fleet-logs/"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_objects_are_keyed_by_generation_or_version(tmp_path: Path, provider: str) -> None:
    fake = _store(provider)
    version = fake.put("amr-07/route.geojson", b"{}")
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
    token = (
        f"generation:{version.generation}" if provider == "gcs" else f"version:{version.version_id}"
    )
    assert entry.location == ExternalObjectRef(
        _connector(fake), _scope(fake) + "amr-07/route.geojson", token
    )


def test_an_unversioned_azure_container_is_keyed_by_etag(tmp_path: Path) -> None:
    fake = FakeStore(provider="azure", versioned=False)
    version = fake.put("cell/plc.csv", b"a,b\n")
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        assert entry.location.revision_token == f"etag:0x{version.etag[:16].upper()}"
        with source.open(entry.location) as stream:
            assert stream.read() == b"a,b\n"
        assert fake.object_requests()[0].headers["if-match"] == f'"0x{version.etag[:16].upper()}"'


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_changed_object_is_a_new_revision_and_only_it_is_probed(
    tmp_path: Path, provider: str
) -> None:
    fake = _store(provider)
    fake.put("arm/a.mcap", b"a" * 1000)
    fake.put("arm/b.mcap", b"b" * 1000)
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        for entry in source.walk():
            assert isinstance(entry, ObjectEntry)
            with source.open(entry.location) as stream:
                ledger.observe(entry.location, digest_stream(stream))
    first = {revision.location.key: revision for revision in ledger.revisions()}
    fake.put("arm/b.mcap", b"b changed")
    fake.requests.clear()
    with connect(fake, tmp_path, ledger=ledger) as source:
        (changed,) = source.walk()
        assert isinstance(changed, ObjectEntry) and changed.key == "arm/b.mcap"
        with source.open(changed.location) as stream:
            observation = ledger.observe(changed.location, digest_stream(stream))
    assert observation.new_revision
    assert observation.revision.supersedes == (first[changed.location.key].id,)
    assert len(fake.object_requests()) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_truncated_download_is_a_short_read_finding(tmp_path: Path, provider: str) -> None:
    fake = _store(provider)
    fake.put("marine/sonar.bin", b"x" * 5000)
    fake.truncate_after = 10
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
    assert raised.value.code == "short_read"
    assert [f.code for f in source.findings()] == [f"{_connector(fake)}.short_read"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_reads_are_ranged_and_pinned(tmp_path: Path, provider: str) -> None:
    fake = _store(provider)
    data = random.Random(3).randbytes(200_000)
    version = fake.put("humanoid/episode.mcap", data)
    with connect(fake, tmp_path) as source:
        (entry,) = source.listing().entries
        fake.requests.clear()
        with source.open(entry.location) as stream:
            stream.seek(150_000)
            assert stream.read(16) == data[150_000:150_016]
    (request,) = fake.object_requests()
    assert request.headers["range"] == "bytes=150000-199999"
    if provider == "gcs":
        assert request.query["generation"] == str(version.generation)
    else:
        assert request.query["versionid"] == version.version_id
        assert request.query["sp"] == "rl" and "sig" in request.query


@pytest.mark.parametrize("provider", PROVIDERS)
def test_keys_that_are_not_utf8_are_reported_and_others_kept_verbatim(
    tmp_path: Path, provider: str
) -> None:
    fake = _store(provider)
    keys = ["robots/a/../b", "robots/b", "robots/a//b", "robots/café", "robots/café"]
    for key in keys:
        fake.put(key, key.encode())
    fake.put(b"robots/\xff-latin1", b"?")
    with connect(fake, tmp_path, "robots/") as source:
        listing = source.listing()
        assert [entry.key for entry in listing.entries] == sorted(keys)
        for entry in listing.entries:
            with source.open(entry.location) as stream:
                assert stream.read() == entry.key.encode()
    (finding,) = source.findings()
    assert finding.code == f"{_connector(fake)}.key_not_utf8"
    # GCS names are JSON text: a byte that is not UTF-8 arrives as a lone surrogate, kept as such.
    raw = "robots/\udcff-latin1".encode("utf-8", "surrogatepass")
    expected = raw if provider == "gcs" else b"robots/\xff-latin1"
    assert finding.details == {"count": 1, "keys_hex": [expected.hex()]}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_redirects_are_refused(tmp_path: Path, provider: str) -> None:
    fake = _store(provider)
    fake.put("a", b"a")
    fake.redirect = 302
    with connect(fake, tmp_path) as source:
        assert not source.listing().complete
    assert [f.code for f in source.findings()] == [f"{_connector(fake)}.redirect_refused"]
    assert len(fake.requests) == 1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_pagination_loop_stops_the_listing(tmp_path: Path, provider: str) -> None:
    fake = _store(provider)
    for index in range(5):
        fake.put(f"fleet/{index}.bag", b"x")
    fake.loop = True
    with connect(fake, tmp_path, page_size=2) as source:
        listing = source.listing()
    assert not listing.complete and [e.key for e in listing.entries] == [
        "fleet/0.bag",
        "fleet/1.bag",
    ]
    assert [f.code for f in source.findings()] == [f"{_connector(fake)}.pagination_loop"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_listing_does_not_depend_on_pages_or_their_order(tmp_path: Path, provider: str) -> None:
    def run(seed: int | None, page_size: int) -> tuple[object, ...]:
        fake = _store(provider)
        for index in range(40):
            fake.put(f"fleet/amr-{index % 7}/{index:03d}.mcap", bytes([index]))
        fake.put(b"fleet/\xfe", b"bad key")
        fake.shuffle = random.Random(seed) if seed is not None else None
        with connect(fake, tmp_path, "fleet/", page_size=page_size) as source:
            return source.listing(), tuple(f.id for f in source.findings())

    reference = run(None, 1000)
    for seed, page_size in ((1, 1), (2, 5), (3, 1000)):
        assert run(seed, page_size) == reference


def test_a_writable_sas_token_is_refused_before_any_request(tmp_path: Path) -> None:
    fake = FakeStore(provider="azure")
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    with fake.serve() as endpoint:
        for permissions in ("rwl", "racwdl", "w", ""):
            with pytest.raises(ObjectStoreConfigError):
                azure_source(
                    f"az://{fake.account}/{fake.bucket}/",
                    network=workspace,
                    options={"endpoint": endpoint},
                    credentials={"azure_sas_token": SAS.replace("sp=rl", f"sp={permissions}")},
                )
    assert fake.requests == []
