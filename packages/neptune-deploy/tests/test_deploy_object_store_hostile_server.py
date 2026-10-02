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

from deploy_object_store_fake import Entry, FakeStore, Version
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


def _limited(tmp_path: Path, keys: list[str], page_size: int, **options: Any) -> tuple[Any, ...]:
    fake = FakeStore()
    for key in keys:
        fake.put(key, b"x")
    with connect(fake, tmp_path, page_size=page_size, **options) as source:
        listing = source.listing()
    return listing, tuple(f.id for f in source.findings()), source.findings()


def test_the_byte_budget_stops_at_the_same_entry_whatever_the_page_size(tmp_path: Path) -> None:
    keys = [f"fleet/{index:02d}-" + "k" * 200 for index in range(50)]
    assert {len(key.encode()) for key in keys} == {209}
    reference = _limited(tmp_path, keys, 1000, max_listing_bytes=4096)
    listing, _, findings = reference
    assert not listing.complete and 0 < len(listing.entries) < 50
    assert [e.key for e in listing.entries] == keys[: len(listing.entries)]
    (finding,) = findings
    assert finding.code == "deploy_s3.listing_limit"
    assert finding.details["max_listing_bytes"] == 4096
    last = listing.entries[-1].key.encode()
    assert finding.details["covered_through_hex"] == last.hex()  # not covered after it
    for page_size in (1, 2, 5, 7, 50):
        assert _limited(tmp_path, keys, page_size, max_listing_bytes=4096)[:2] == reference[:2]


def test_the_object_limit_cuts_in_byte_order_past_a_shared_prefix(tmp_path: Path) -> None:
    shared = "fleet/" + "p" * 300  # longer than the 256 bytes an unused key keeps
    keys = [shared + "a" * 10, shared + "b" * 10, shared + "c"]
    reference = _limited(tmp_path, keys, 1000, max_objects=1)
    listing, _, (finding,) = reference
    assert [e.key for e in listing.entries] == [shared + "a" * 10]
    assert finding.details["covered_through_hex"] == (shared + "a" * 10).encode().hex()
    for page_size in (1, 2, 3):
        assert _limited(tmp_path, keys, page_size, max_objects=1)[:2] == reference[:2]


def test_unused_keys_count_against_the_object_limit_in_byte_order(tmp_path: Path) -> None:
    shared = "fleet/" + "p" * 300
    used = [f"fleet/{index:02d}.bag" for index in range(6)]
    huge = [shared + "y" * 1000, shared + "z" * 1000]  # too long: unused, sharing 256 bytes
    early = ["fleet/00a" + "y" * 1100, "fleet/01a" + "y" * 1100]  # too long, among the used keys
    keys = [*used, *huge, *early]
    reference = _limited(tmp_path, keys, 1000, max_objects=5)
    listing, _, findings = reference
    # Byte order: 00.bag, 00a…, 01.bag, 01a…, 02.bag; the fifth entry is the limit.
    assert [e.key for e in listing.entries] == used[:3]
    assert [s.length for s in listing.skipped] == [1109, 1109]
    assert sorted(f.code for f in findings) == ["deploy_s3.key_too_long", "deploy_s3.listing_limit"]
    limit = next(f for f in findings if f.code == "deploy_s3.listing_limit")
    assert limit.details["max_objects"] == 5
    assert limit.details["covered_through_hex"] == used[2].encode().hex()
    complete = _limited(tmp_path, keys, 1000)
    assert complete[0].complete and [s.reason for s in complete[0].skipped] == ["key_too_long"] * 4
    assert {f.details.get("count") for f in complete[2]} == {4}
    for page_size in (1, 2, 3, 4):
        assert _limited(tmp_path, keys, page_size, max_objects=5)[:2] == reference[:2]


def _outside(fake: FakeStore, count: int) -> None:
    """``count`` objects under ``cell/`` that the store lists as short keys outside the prefix."""
    fake.bulk({f"cell/{i:06d}".encode(): b"" for i in range(count)})

    def moved(number: int, entries: list[Entry]) -> list[Entry]:
        return [Entry(b"o/" + e.key[5:], e.version, e.latest) for e in entries]

    fake.rewrite = moved


def test_many_short_unused_keys_hold_no_more_than_the_byte_budget(tmp_path: Path) -> None:
    budget = 2**20
    fake = FakeStore()
    _outside(fake, 40_000)  # charged by key bytes alone, these would hold about 10 MB
    with connect(fake, tmp_path, "cell/", max_objects=1) as warm:
        warm.listing()  # the fake builds and keeps its own entries, outside what is measured
    results = []
    for page_size in (1000, 97):
        with connect(fake, tmp_path, "cell/", page_size=page_size, max_listing_bytes=budget) as src:
            tracemalloc.start()
            before = tracemalloc.get_traced_memory()[0]
            listing = src.listing()
            grown = tracemalloc.get_traced_memory()[0] - before
            tracemalloc.stop()
        assert grown <= 2 * budget, f"{grown / 2**20:.1f} MiB held for a 1 MiB budget"
        assert not listing.complete and listing.entries == ()
        assert 0 < len(listing.skipped) < 40_000
        limit = next(f for f in src.findings() if f.code == "deploy_s3.listing_limit")
        assert limit.details["max_listing_bytes"] == budget
        results.append((listing, tuple(f.id for f in src.findings())))
    assert results[0] == results[1]  # the same cut at every page size
    skipped = results[0][0].skipped
    assert [s.raw_key for s in skipped] == [b"o/%06d" % i for i in range(len(skipped))]


ROBOT = "\U0001f916"  # one character, stored at 4 bytes per character with every other one


@pytest.mark.parametrize(
    ("key_tail", "version_id"),
    [
        ("k" * 1000, "v1"),  # 1 KB ASCII keys
        (ROBOT + "k" * 1000, "v1"),  # one emoji makes CPython store the whole key at 4 B a char
        ("", ROBOT + "v" * 1000),  # the same for a token
    ],
    ids=["ascii_keys", "emoji_keys", "emoji_tokens"],
)
def test_used_keys_hold_no_more_than_the_byte_budget(
    tmp_path: Path, key_tail: str, version_id: str
) -> None:
    budget = 2**20
    fake = FakeStore()
    for index in range(2_000):
        key = f"cell/{index:05d}{key_tail}".encode()
        fake.history[key] = [Version(b"", f"{version_id}{index:05d}", index + 1)]
    with connect(fake, tmp_path, "cell/", max_objects=1) as warm:
        warm.listing()  # the fake builds and keeps its own entries, outside what is measured
    results = []
    for page_size in (1000, 97):
        with connect(fake, tmp_path, "cell/", page_size=page_size, max_listing_bytes=budget) as src:
            tracemalloc.start()
            before = tracemalloc.get_traced_memory()[0]
            listing = src.listing()
            grown = tracemalloc.get_traced_memory()[0] - before
            tracemalloc.stop()
        assert grown <= 2 * budget, f"{grown / 2**20:.1f} MiB held for a 1 MiB budget"
        assert not listing.complete and 0 < len(listing.entries) < 2_000
        (limit,) = src.findings()
        assert limit.details["max_listing_bytes"] == budget
        results.append((listing, limit.id))
    assert results[0] == results[1]  # the same cut at every page size


def test_the_object_limit_counts_unused_keys_at_every_page_size(tmp_path: Path) -> None:
    fake = FakeStore()
    _outside(fake, 40)
    reference = None
    for page_size in (1000, 1, 3, 7):
        with connect(fake, tmp_path, "cell/", page_size=page_size, max_objects=25) as source:
            listing = source.listing()
        result = (listing, tuple(f.id for f in source.findings()))
        assert len(listing.skipped) == 25 and not listing.complete
        reference = reference or result
        assert result == reference


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
