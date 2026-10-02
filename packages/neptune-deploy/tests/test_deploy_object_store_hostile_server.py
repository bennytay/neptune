"""A hostile store: malformed headers, secrets in URLs, huge keys and tokens, trickling bodies.

Each attack fails one object or stops the listing with a finding; none escapes as a bare exception
or reaches an output (ADR 0006 §4, §6, §8).
"""

import time
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_object_store_fake import Entry, FakeStore
from neptune.identity import canonical_json
from neptune.store.workspace import Workspace
from neptune_deploy.sources.object_store import (
    ObjectReadError,
    ObjectStoreConfigError,
    ObjectStoreSource,
    azure_source,
    s3_source,
)

KEYS = {"s3_access_key_id": "AKID", "s3_secret_access_key": "s3cr3t"}


def _workspace(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


@contextmanager
def connect(
    fake: FakeStore, tmp_path: Path, prefix: str = "", **options: Any
) -> Iterator[ObjectStoreSource]:
    with fake.serve() as endpoint:
        yield s3_source(
            f"s3://{fake.bucket}/{prefix}",
            network=_workspace(tmp_path),
            options={"endpoint": endpoint, "store": "site-a", **options},
            credentials=KEYS,
        )


# --- Malformed range and length headers ----------------------------------------------------------

OVERFLOW = "9" * 5000  # past int()'s 4,300-digit limit


@pytest.mark.parametrize(
    ("headers", "ignore_range"),
    [
        ({"Content-Range": "bytes ²-9/10"}, False),  # a superscript digit
        ({"Content-Range": "bytes 0-³/10"}, False),
        ({"Content-Range": "bytes 0-9/¹"}, False),
        ({"Content-Range": "bytes -1-9/10"}, False),
        ({"Content-Range": "bytes 0-9/-10"}, False),
        ({"Content-Range": f"bytes 0-{OVERFLOW}/10"}, False),
        ({"Content-Range": f"bytes 0-9/{OVERFLOW}"}, False),
        ({"Content-Range": "bytes 0-9/11"}, False),  # inconsistent with the listed size
        ({"Content-Range": "pages 0-9/10"}, False),
        ({"Content-Length": "²"}, True),
        ({"Content-Length": "-5"}, True),
        ({"Content-Length": OVERFLOW}, True),
        ({"Content-Length": "11"}, True),  # inconsistent with the listed size
    ],
)
def test_a_malformed_range_or_length_fails_that_object_only(
    tmp_path: Path, headers: dict[str, str], ignore_range: bool
) -> None:
    fake = FakeStore()
    fake.put("arm/a.bin", b"0123456789")
    fake.put("arm/b.bin", b"abcdefghij")
    with connect(fake, tmp_path) as source:
        bad, good = source.listing().entries
        fake.headers, fake.ignore_range = headers, ignore_range
        with pytest.raises(ObjectReadError) as raised, source.open(bad.location) as stream:
            stream.read(4)
        assert raised.value.code in ("read_failed", "object_changed")
        fake.headers, fake.ignore_range = {}, False
        with source.open(good.location) as stream:
            assert stream.read() == b"abcdefghij"  # the next object is unaffected
    (finding,) = source.findings()
    assert finding.subject == bad.location


# --- Secrets in URLs -----------------------------------------------------------------------------


def _texts(exc: BaseException) -> list[str]:
    texts = []
    current: BaseException | None = exc
    while current is not None:
        texts += [str(current), repr(current)]
        current = current.__cause__ or current.__context__
    return texts


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("s3://fleet-logs/", "https://AKID:SUPERSECRET@minio.example"),
        ("s3://fleet-logs/", "https://minio.example/?X-Amz-Signature=SUPERSECRET"),
        ("s3://fleet-logs/", "https://minio.example/#SUPERSECRET"),
        ("s3://fleet-logs/", "http://minio.example:9000/SUPERSECRET"),  # shown redacted
        ("s3://user:SUPERSECRET@fleet-logs/", "https://minio.example"),
    ],
)
def test_no_secret_in_a_url_reaches_an_error(tmp_path: Path, url: str, endpoint: str) -> None:
    with pytest.raises(ObjectStoreConfigError) as raised:
        s3_source(
            url,
            network=_workspace(tmp_path),
            options={"endpoint": endpoint, "store": "site-a"},
            credentials=KEYS,
        )
    assert [text for text in _texts(raised.value) if "SUPERSECRET" in text] == []
    if endpoint.startswith("http://"):  # plain http off the loopback: named as scheme://host:port
        assert str(raised.value).endswith(": http://minio.example:9000")


def test_userinfo_in_an_endpoint_is_refused_with_a_fixed_message(tmp_path: Path) -> None:
    with pytest.raises(ObjectStoreConfigError, match="declare credentials instead"):
        s3_source(
            "s3://fleet-logs/",
            network=_workspace(tmp_path),
            options={"endpoint": "https://a:b@minio.example", "store": "site-a"},
            credentials=KEYS,
        )


def test_no_credential_reaches_an_error_a_finding_or_the_transform(tmp_path: Path) -> None:
    fake = FakeStore(provider="azure", sas_required=True)
    fake.put("cell/a.csv", b"a")
    sas = "sv=2021-08-06&sp=rl&sig=SAS-SUPERSECRET"
    with fake.serve() as endpoint:
        source = azure_source(
            f"az://{fake.account}/{fake.bucket}/",
            network=_workspace(tmp_path),
            options={"endpoint": endpoint, "store": "site-a"},
            credentials={"azure_sas_token": sas},
        )
        (entry,) = source.listing().entries
        fake.truncate_after = 0
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
    outputs = [canonical_json.dumps(f.to_json()).decode() for f in source.findings()]
    outputs.append(canonical_json.dumps(source.transform.to_json()).decode())
    outputs += _texts(raised.value)
    assert len(source.findings()) == 1
    assert not [text for text in outputs if "SUPERSECRET" in text]


# --- Bounded keys, bytes and tokens --------------------------------------------------------------


@pytest.mark.slow
def test_twenty_thousand_30_kb_keys_cost_a_bounded_few_bytes_each(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.bulk({f"cell/{i:05d}".encode(): b"" for i in range(20_000)})
    junk = fake.put("elsewhere", b"")

    def huge(number: int, entries: list[Entry]) -> list[Entry]:  # outside the prefix, 30 KB each
        return [Entry(b"zz/" + e.key + b"x" * 30_000, junk, True) for e in entries]

    fake.rewrite = huge
    with connect(fake, tmp_path, "cell/", page_size=250) as source:
        tracemalloc.start()
        listing = source.listing()
        held, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert listing.complete and listing.entries == ()
    assert len(listing.skipped) == 20_000
    assert all(len(s.raw_key) <= 256 and s.length == 30_013 for s in listing.skipped)
    (finding,) = source.findings()
    assert finding.code == "deploy_s3.key_outside_prefix" and finding.details["count"] == 20_000
    assert held < 32 * 2**20, f"{held / 2**20:.0f} MiB held"  # not the 600 MB of keys
    assert peak < 256 * 2**20, f"{peak / 2**20:.0f} MiB peak"


def test_the_listing_byte_budget_stops_the_listing(tmp_path: Path) -> None:
    fake = FakeStore()
    for index in range(50):
        fake.put(f"fleet/{index:02d}-" + "k" * 200, b"x")
    with connect(fake, tmp_path, max_listing_bytes=4096, page_size=5) as source:
        listing = source.listing()
    assert not listing.complete and 0 < len(listing.entries) < 50
    (finding,) = source.findings()
    assert finding.code == "deploy_s3.listing_limit"
    assert finding.details["max_listing_bytes"] == 4096
    last = listing.entries[-1].key.encode()
    assert finding.details["covered_through_hex"] == last[:256].hex()  # NotCovered after it


def test_a_continuation_token_over_4_kib_stops_the_listing(tmp_path: Path) -> None:
    fake = FakeStore(versioned=False)
    for index in range(4):
        fake.put(f"amr/{index}.bag", b"x")
    fake.cursor_pad = 5000
    with connect(fake, tmp_path, versions=False, page_size=2) as source:
        listing = source.listing()
    assert not listing.complete and listing.entries == ()
    (finding,) = source.findings()
    assert finding.code == "deploy_s3.response_invalid"
    fake.cursor_pad = 4000  # under the cap: read through
    with connect(fake, tmp_path, versions=False, page_size=2) as source:
        assert len(source.listing().entries) == 4


# --- A trickling server --------------------------------------------------------------------------


def test_a_trickling_body_is_cut_off_at_the_request_deadline(tmp_path: Path) -> None:
    fake = FakeStore()
    fake.put("marine/ctd.csv", b"x" * 100)
    with connect(fake, tmp_path, timeout=1.0) as source:
        (entry,) = source.listing().entries
        fake.drip = 0.2  # each byte well inside the socket timeout; the whole body takes 20 s
        started = time.monotonic()
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
        elapsed = time.monotonic() - started
    assert raised.value.code == "read_failed" and elapsed < 5
    (finding,) = source.findings()
    assert finding.details["cause"] == "deadline_exceeded"
