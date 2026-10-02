"""The Roboto connector against hostile configuration, responses and signed URLs (ADR 0009 §6)."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_roboto_fake import DATASET, ORG, TOKEN, FakeRoboto, content_for
from neptune.model.knowledge import Known, Unknown
from neptune.store.workspace import Workspace
from neptune_deploy.sources.object_store import ObjectReadError
from neptune_deploy.sources.object_store.config import ObjectStoreConfigError
from neptune_deploy.sources.roboto import (
    RobotoApi,
    RobotoOptions,
    RobotoSource,
    roboto_source,
)
from neptune_deploy.sources.roboto.config import api_token, parse_url

CREDENTIALS = {"roboto_api_token": TOKEN}
CAL = "amr07/calibration.yaml"


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


@contextmanager
def connect(fake: FakeRoboto, tmp_path: Path, **options: Any) -> Iterator[RobotoSource]:
    with fake.serve() as endpoint:
        yield roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=online(tmp_path),
            options={"endpoint": endpoint, **options},
            credentials=CREDENTIALS,
        )


def codes(source: RobotoSource) -> list[str]:
    return sorted(finding.code for finding in source.findings())


def read_first(source: RobotoSource, key: str = CAL) -> bytes:
    entry = next(e for e in source.listing().entries if e.key == key)
    return source.open(entry.location).read(8)


def page(items: list[Any], token: str | None = None) -> bytes:
    return json.dumps({"data": {"items": items, "next_token": token}}).encode()


# --- Configuration -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "s3://og_a/ds_b/",
        "roboto://",
        "roboto://og_a",
        "roboto://og_a//x",
        "roboto://og_a/ds_b:c/",
        "roboto://og a/ds_b/",
        "roboto://user:pw@og_a/ds_b/",
        "roboto://og_a/ds_b/" + "p" * 5000,
        "roboto://og_a/ds_b/\ud800",
        "roboto://" + "o" * 65 + "/ds_b/",
        5,
    ],
)
def test_a_url_that_is_not_one_org_one_dataset_and_a_prefix_is_refused(url: Any) -> None:
    with pytest.raises(ObjectStoreConfigError):
        parse_url(url)


def test_a_prefix_is_taken_verbatim() -> None:
    where = parse_url("roboto://og_a/ds_b/a b/%41/../é")
    assert (where.org, where.dataset, where.prefix) == ("og_a", "ds_b", "a b/%41/../é")
    assert where.object_id("k") == "og_a/ds_b/k"


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"nope": 1}, "unknown options"),
        ({"endpoint": 5}, "endpoint is a URL"),
        ({"endpoint": "http://example.com"}, "http"),
        ({"endpoint": "ftp://api.example"}, "ftp|http|scheme"),
        ({"content_hosts": "a.example"}, "content_hosts"),
        ({"content_hosts": ["a b"]}, "content_hosts"),
        ({"content_hosts": ["https://a"]}, "content_hosts"),
        ({"api_version": "latest"}, "api_version"),
        ({"events": "yes"}, "events is true or false"),
        ({"comments": 1}, "comments is true or false"),
        ({"max_records": 0}, "max_records"),
        ({"max_records": True}, "max_records"),
        ({"page_size": 1001}, "page_size"),
        ({"max_listing_bytes": 10}, "max_listing_bytes"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": "5"}, "timeout"),
        ({"event_clock": "unix"}, "event_clock"),
        ({"event_clock": {"epoch": "unix", "zone": "utc"}}, "event_clock"),
        ({"event_clock": {"resolution": "-1"}}, "event_clock"),
    ],
)
def test_options_are_closed_and_checked(options: dict[str, Any], match: str) -> None:
    with pytest.raises(ObjectStoreConfigError, match=match):
        RobotoOptions.parse(options)


def test_the_boundaries_of_the_counts_are_accepted() -> None:
    parsed = RobotoOptions.parse({"max_records": 1, "page_size": 1000, "max_listing_bytes": 1024})
    assert (parsed.max_records, parsed.page_size, parsed.max_listing_bytes) == (1, 1000, 1024)
    assert RobotoOptions.parse(
        {"content_hosts": ["B.example:8443", "b.example:8443"]}
    ).content_hosts == ("b.example:8443",)


def test_the_token_is_declared_or_named_and_never_ambient() -> None:
    assert api_token({"roboto_api_token": "abc"}, {}) == "abc"
    assert api_token(None, {"NEPTUNE_ROBOTO_API_TOKEN": "env-token"}) == "env-token"
    ambient = {"ROBOTO_API_KEY": "x", "ROBOTO_API_TOKEN": "y", "HOME": "/root"}
    with pytest.raises(ObjectStoreConfigError, match="no Roboto API token"):
        api_token(None, ambient)
    with pytest.raises(ObjectStoreConfigError, match="no Roboto API token"):
        api_token({}, {"NEPTUNE_ROBOTO_API_TOKEN": "env-token"})  # declared, so env is not mixed in
    for bad in ("", "a b", "tab\there", "nul\x00", "é", "line\nbreak"):
        with pytest.raises(ObjectStoreConfigError):
            api_token({"roboto_api_token": bad}, {})
    with pytest.raises(ObjectStoreConfigError, match="unknown credentials"):
        api_token({"roboto_api_token": "a", "s3_access_key_id": "b"}, {})


def test_the_token_is_never_printed(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        read_first(source)
        source.catalog()
        everything = repr(source) + repr(source.api) + repr(source.transform)
        everything += "".join(repr(f) for f in source.findings())
        everything += json.dumps(source.transform.config, default=str)
        assert isinstance(source.api, RobotoApi)
    assert TOKEN not in everything and "127.0.0.1" not in everything


# --- The API's answers ----------------------------------------------------------------------------


def test_a_redirect_is_a_finding_and_is_never_followed(tmp_path: Path) -> None:
    fake = FakeRoboto()
    fake.redirect = 302
    with connect(fake, tmp_path) as source:
        assert source.listing().entries == ()
        assert not source.listing().complete
        catalog = source.catalog()
    assert "deploy_roboto.redirect_refused" in codes(source)
    assert all(r.headers.get("host", "").startswith("127.0.0.1") for r in fake.requests)
    assert catalog.rows == ()  # nothing was invented


def test_a_wrong_token_or_organisation_is_a_failed_listing_not_a_crash(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with fake.serve() as endpoint:
        source = roboto_source(
            f"roboto://og_other/{DATASET}/",
            network=online(tmp_path),
            options={"endpoint": endpoint},
            credentials=CREDENTIALS,
        )
        assert source.listing().entries == ()
    (finding,) = [f for f in source.findings() if f.code == "deploy_roboto.listing_failed"]
    assert finding.details["status"] == 403


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b"[]",
        b'{"nodata": 1}',
        b'{"data": []}',
        b'{"data": {"items": "x"}}',
        b'{"data": {"items": [], "next_token": 5}}',
        b'{"data": {"items": [], "next_token": "' + b"t" * 5000 + b'"}}',
        b'{"data": {"items": [], "items": []}}',  # a duplicate key: two readers would disagree
        b'{"data": {"items": [], "n": NaN}}',
        b'{"data": ' + b"[" * 500 + b"]" * 500 + b"}",
    ],
)
def test_a_malformed_files_page_stops_the_listing_with_one_finding(
    tmp_path: Path, body: bytes
) -> None:
    fake = FakeRoboto()
    fake.raw_files = lambda token: body
    with connect(fake, tmp_path) as source:
        listing = source.listing()
        catalog = source.catalog()
    assert listing.entries == () and not listing.complete
    assert codes(source) == ["deploy_roboto.response_invalid"]
    assert catalog.documents  # the catalog is still built, from what the API did say


def test_a_page_over_the_byte_bound_is_refused_not_buffered(tmp_path: Path) -> None:
    fake = FakeRoboto()
    big = page([{"relative_path": "x" * 1000, "pad": "y" * 9_000_000}])
    fake.raw_files = lambda token: big
    with connect(fake, tmp_path) as source:
        assert source.listing().entries == ()
    assert [c for c in codes(source) if c.startswith("deploy_roboto.")]


def test_a_files_page_looping_back_stops_the_listing(tmp_path: Path) -> None:
    fake = FakeRoboto()
    one = json.loads(page([fake.record(CAL)], "again"))
    fake.raw_files = lambda token: json.dumps(one).encode()
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert [e.key for e in listing.entries] == [CAL] and not listing.complete
    assert codes(source) == ["deploy_roboto.pagination_loop"]


def _record(fake: FakeRoboto, **changes: Any) -> dict[str, Any]:
    return {**fake.record(CAL), **changes}


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"version": None}, "revision_invalid"),
        ({"version": -1}, "revision_invalid"),
        ({"version": True}, "revision_invalid"),
        ({"version": 1.5}, "revision_invalid"),
        ({"version": 10**25}, "revision_invalid"),
        ({"file_id": "a/b"}, "revision_invalid"),
        ({"file_id": "fl:1"}, "revision_invalid"),
        ({"file_id": 7}, "revision_invalid"),
        ({"status": "archived"}, "revision_invalid"),
        ({"status": ["available"]}, "revision_invalid"),
        ({"relative_path": None}, "revision_invalid"),
        ({"size": -3}, "size_invalid"),
        ({"size": "120"}, "size_invalid"),
        ({"size": 10**25}, "size_invalid"),
        ({"relative_path": "amr07/bad\ud800.yaml"}, "key_not_utf8"),
    ],
)
def test_a_file_record_without_a_usable_revision_or_size_is_skipped_with_its_reason(
    tmp_path: Path, changes: dict[str, Any], code: str
) -> None:
    fake = FakeRoboto()
    bad = _record(fake, **changes)
    good = fake.record("amr07/run_0914_am.mcap")
    fake.raw_files = lambda token: page([bad, good, "not a record"])
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert [e.key for e in listing.entries] == ["amr07/run_0914_am.mcap"]
    assert {s.reason for s in listing.skipped} >= {code}
    assert f"deploy_roboto.{code}" in codes(source)


def test_a_file_is_listed_at_the_boundary_sizes(tmp_path: Path) -> None:
    fake = FakeRoboto()
    zero = _record(fake, relative_path="z/zero", file_id="fl_zero", size=0)
    huge = _record(fake, relative_path="z/huge", file_id="fl_huge", size=2**62)
    fake.raw_files = lambda token: page([zero, huge])
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert {e.key: e.size for e in listing.entries} == {"z/zero": 0, "z/huge": 2**62}


def test_the_same_path_listed_twice_with_different_versions_is_used_by_neither(
    tmp_path: Path,
) -> None:
    fake = FakeRoboto()
    one, two = _record(fake, version=3), _record(fake, version=4)
    fake.raw_files = lambda token: page([one, two])
    with connect(fake, tmp_path) as source:
        listing = source.listing()
    assert listing.entries == ()
    assert codes(source) == ["deploy_roboto.key_duplicated"]


# --- Annotations -----------------------------------------------------------------------------------


def test_a_malformed_events_page_is_one_finding_and_the_rest_stands(tmp_path: Path) -> None:
    fake = FakeRoboto()
    fake.raw_events = lambda token: b'{"data": {"items": [{"event_id": "e", "event_id": "f"}]}}'
    with connect(fake, tmp_path) as source:
        catalog = source.catalog()
    (finding,) = [f for f in source.findings() if f.code == "deploy_roboto.catalog_invalid"]
    assert finding.details["part"] == "events"
    assert {d.ref.object_id.rpartition(":")[2] for d in catalog.documents} == {
        "dataset",
        "files",
        "comments",
    }


def test_events_that_loop_stop_with_a_finding_and_keep_what_was_read(tmp_path: Path) -> None:
    fake = FakeRoboto()
    looping = json.loads(page(list(fake.event_pages[None]["data"]["items"]), "again"))
    fake.raw_events = lambda token: json.dumps(looping).encode()
    with connect(fake, tmp_path) as source:
        catalog = source.catalog()
    assert [f.details["cause"] for f in source.findings()] == ["pagination_loop"]
    assert catalog.rows


def test_the_dataset_record_failing_is_a_finding_and_files_and_events_stand(
    tmp_path: Path,
) -> None:
    fake = FakeRoboto()
    fake.dataset = ["not an object"]
    with connect(fake, tmp_path) as source:
        catalog = source.catalog()
    (finding,) = [f for f in source.findings() if f.details.get("part") == "dataset"]
    assert finding.code == "deploy_roboto.catalog_invalid"
    assert {d.ref.object_id.rpartition(":")[2] for d in catalog.documents} == {
        "files",
        "events",
        "comments",
    }


def test_odd_event_values_are_kept_as_stated_or_unknown_and_never_interpreted(
    tmp_path: Path,
) -> None:
    fake = FakeRoboto()
    events = [
        {"event_id": "e1", "name": "stop", "start_time": "soon", "end_time": 1.5, "tags": None},
        {"event_id": "e2", "name": "", "start_time": True, "metadata": {"b": [1, {"a": None}]}},
        {"event_id": "e3", "name": "bad \ud800 text", "start_time": 5, "end_time": -5},
        {"event_id": "e4", "name": "huge", "start_time": 2**70},
    ]
    fake.raw_events = lambda token: page(events)
    with connect(fake, tmp_path, event_clock={"epoch": "unix"}) as source:
        catalog = source.catalog()
    (table,) = [t for t in catalog.tables if getattr(t.name, "value", None) == "roboto events"]
    assert isinstance(table.header, Known)
    rows = {
        cells["event_id"].value: cells
        for cells in (
            dict(zip(table.header.value, r.cells, strict=True))
            for r in catalog.rows
            if r.table == table.id
        )
    }
    assert rows["e1"]["start_time"].value == "soon"  # text stays text: no time was made of it
    assert isinstance(rows["e1"]["@clock:start_time"], Unknown)  # a string is on no clock
    assert rows["e1"]["end_time"].value == 1.5
    assert isinstance(rows["e1"]["tags"], Unknown)  # null is not an empty list
    assert isinstance(rows["e2"]["name"], Unknown)  # "" is not a name
    assert rows["e2"]["start_time"].value is True
    assert json.loads(rows["e2"]["metadata"].value) == {"b": [1, {"a": None}]}
    assert isinstance(rows["e3"]["name"], Unknown)
    assert rows["e3"]["end_time"].value == -5  # a negative time is stated as given
    assert rows["e4"]["start_time"].value == 2**70
    (finding,) = source.findings()
    assert finding.code == "deploy_roboto.value_unrepresentable"
    assert finding.subject.object_id == f"{ORG}/{DATASET}:events"  # the document, as its id says


def test_events_in_another_page_order_give_the_same_documents(tmp_path: Path) -> None:
    one, two = FakeRoboto(), FakeRoboto()
    two.event_pages = {
        None: one.event_pages["tok_events_2"]
        | {"data": {**one.event_pages["tok_events_2"]["data"], "next_token": "tok_events_2"}},
        "tok_events_2": one.event_pages[None]
        | {"data": {**one.event_pages[None]["data"], "next_token": None}},
    }
    with connect(one, tmp_path / "a") as first, connect(two, tmp_path / "b") as second:
        assert [d.data for d in first.catalog().documents] == [
            d.data for d in second.catalog().documents
        ]
        assert first.catalog().rows == second.catalog().rows


def test_an_event_time_range_on_a_clock_is_two_records_citing_the_same_event(
    tmp_path: Path,
) -> None:
    clock = {"epoch": "unix", "timescale": "posix", "resolution": "1/1000000000", "role": "sample"}
    with connect(FakeRoboto(), tmp_path, event_clock=clock) as source:
        catalog = source.catalog()
        transform = source.transform
    starts = [d for d in catalog.domains if d.field == "start_time"]
    ends = [d for d in catalog.domains if d.field == "end_time"]
    assert len(starts) == len(ends) == 1 and starts[0].id != ends[0].id
    assert str(starts[0].role.value) == "sample"  # type: ignore[union-attr]
    assert transform.config["event_clock"] == {  # type: ignore[index]
        "epoch": "unix",
        "resolution": "1/1000000000",
        "role": "sample",
        "timescale": "posix",
    }


# --- Signed URLs ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        lambda base, path: "http://evil.example" + path,
        lambda base, path: "https://127.0.0.1:1" + path,
        lambda base, path: base.replace("http://", "http://user:pw@") + path,
        lambda base, path: base + "/content/../../v1/x",
        lambda base, path: base + "/content/%2e%2e/x",
        lambda base, path: base + "/content/..\\x",
        lambda base, path: base + path + "#frag",
        lambda base, path: base + path + "?a+b=c",
        lambda base, path: base + path + "?=x",
        lambda base, path: base + path + "?k=%ff",
        lambda base, path: base + path + "/" + "a" * 9000,
        lambda base, path: base + path + "?k=a b",
        lambda base, path: "file:///etc/passwd",
        lambda base, path: "//evil.example/x",
        lambda base, path: "javascript:alert(1)",
    ],
)
def test_a_signed_url_that_is_not_a_plain_read_of_an_allowed_host_is_never_requested(
    tmp_path: Path, url: Any
) -> None:
    fake = FakeRoboto()
    fake.signed_url = url
    with connect(fake, tmp_path) as source:
        with pytest.raises(ObjectReadError) as raised:
            read_first(source)
    assert raised.value.code == "read_failed"
    assert fake.content_requests() == []
    assert "deploy_roboto.read_failed" in codes(source)
    shown = "".join(repr(f.details) + f.message for f in source.findings())
    assert "evil" not in shown and TOKEN not in shown


def test_a_content_host_the_operator_named_is_read_without_the_token(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with fake.serve() as endpoint:
        port = endpoint.rpartition(":")[2]
        fake.signed_url = lambda base, path: f"http://localhost:{port}{path}"
        source = roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=online(tmp_path),
            options={"endpoint": endpoint, "content_hosts": [f"localhost:{port}"]},
            credentials=CREDENTIALS,
        )
        assert read_first(source) == content_for(CAL, 120)[:8]
    content = fake.content_requests()
    assert content and all("authorization" not in r.headers for r in content)
    assert all(r.headers["host"].startswith("localhost:") for r in content)
    assert source.findings() == ()


def test_the_same_host_under_another_name_is_refused_unless_named(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with fake.serve() as endpoint:
        port = endpoint.rpartition(":")[2]
        fake.signed_url = lambda base, path: f"http://localhost:{port}{path}"
        source = roboto_source(
            f"roboto://{ORG}/{DATASET}/",
            network=online(tmp_path),
            options={"endpoint": endpoint},
            credentials=CREDENTIALS,
        )
        with pytest.raises(ObjectReadError):
            read_first(source)
    assert fake.content_requests() == []


@pytest.mark.parametrize(
    "knob",
    [
        {"content_status": 500},
        {"content_status": 404},
        {"content_status": 302},
        {"content_range_shift": 1},
    ],
)
def test_a_content_host_that_fails_or_lies_is_a_read_finding(
    tmp_path: Path, knob: dict[str, Any]
) -> None:
    fake = FakeRoboto()
    for name, value in knob.items():
        setattr(fake, name, value)
    with connect(fake, tmp_path) as source:
        with pytest.raises(ObjectReadError):
            read_first(source)
    assert [c for c in codes(source) if c.startswith("deploy_roboto.")]


def test_a_content_host_that_ignores_ranges_is_read_from_the_start_only(tmp_path: Path) -> None:
    fake = FakeRoboto()
    fake.ignore_range = True
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key == CAL)
        assert source.fetch(entry, 0, 8) == content_for(CAL, 120)[:8]  # a 200 from byte 0 is sound
        with pytest.raises(ObjectReadError) as raised:
            source.fetch(entry, 60, 8)  # the same answer to a later range is not
    assert raised.value.code == "read_failed"


def test_a_file_whose_record_moved_on_is_a_changed_read_and_asks_for_no_url(
    tmp_path: Path,
) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key == CAL)
        fake.reupload(CAL, b"z" * 120)
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location).read(8)
    assert raised.value.code == "object_changed"
    assert not [r for r in fake.requests if r.path.endswith("/signed-url")]
    assert fake.content_requests() == []


def test_a_range_outside_the_listed_size_is_refused_before_any_request(tmp_path: Path) -> None:
    fake = FakeRoboto()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.key == CAL)
        before = len(fake.requests)
        for start, length in ((-1, 4), (0, 0), (118, 4), (121, 1)):
            with pytest.raises(ValueError, match="outside"):
                source.fetch(entry, start, length)
        assert len(fake.requests) == before
        assert source.fetch(entry, 116, 4) == content_for(CAL, 120)[116:120]  # the last bytes
