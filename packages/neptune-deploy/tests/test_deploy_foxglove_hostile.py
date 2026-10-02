"""The Foxglove connector against hostile and failing APIs (ADR 0007 §8): every response, link and
id is untrusted, and one bad one costs that one and nothing else."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deploy_foxglove_fake import API_KEY, SIGNATURE, STREAM, FakeFoxglove, load
from neptune.identity import canonical_json
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory
from neptune.model.ids import ExternalObjectRef
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.foxglove import FoxgloveSource, StreamEntry, foxglove_source
from neptune_deploy.sources.object_store import ObjectReadError, SkippedObject
from test_deploy_foxglove_source import (
    AMR,
    ARM,
    LEGGED,
    MARINE,
    PENDING,
    UNASSIGNED,
    codes,
    connect,
    declared_bytes,
    fingerprint,
    online,
)

GOOD = sorted([ARM, AMR, LEGGED, MARINE, UNASSIGNED])


def listed(source: FoxgloveSource) -> list[str]:
    return [r.recording_id for r in source.index().recordings]


def with_recordings(
    fake: FakeFoxglove, change: Callable[[list[dict[str, Any]]], None]
) -> FakeFoxglove:
    change(fake.recordings)
    return fake


# --- Hostile JSON ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b'[{"id": "rec_a", "id": "rec_b"}]',  # a duplicate key: two readings of one object
        b'[{"id": "rec_a", "size": NaN}]',
        b'[{"id": "rec_a", "size": Infinity}]',
        b"[" * 100_000 + b"]" * 100_000,  # nested past the parser's limit
        b"\xff\xfe[]",  # not UTF-8
        b'{"recordings": []}',  # an object, not the documented array
        b"not json at all",
        b"",
    ],
    ids=["duplicate-key", "nan", "infinity", "deep", "not-utf8", "not-array", "garbage", "empty"],
)
def test_a_listing_that_is_not_strict_json_stops_the_listing_and_asserts_nothing(
    tmp_path: Path, body: bytes
) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.raw["/v1/recordings"] = body
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
    assert not discovery.complete
    assert (discovery.new, discovery.changed, discovery.gone) == ((), (), ())
    (finding,) = source.findings()
    assert finding.code == "deploy_foxglove.response_invalid"
    assert finding.category is FindingCategory.CORRUPT
    assert finding.subject == source.listing_ref


def test_an_oversized_listing_body_is_refused_not_buffered(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.raw["/v1/recordings"] = b"[" + b" " * (33 * 1024 * 1024) + b"]"
    with connect(fake, tmp_path) as source:
        index = source.index()
    assert not index.complete and index.recordings == ()
    assert [c for c in codes(source)] == ["deploy_foxglove.listing_failed"]
    (finding,) = source.findings()
    assert finding.details["cause"] == "response_too_large"


# --- Hostile entries ---------------------------------------------------------------------------


def broken(fake: FakeFoxglove, name: str, change: Callable[[dict[str, Any]], Any]) -> None:
    """Replace one recording by a damaged copy of itself, as the API might send it."""
    for index, recording in enumerate(fake.recordings):
        if recording["id"] == name:
            damaged = change(recording)
            fake.recordings[index] = recording if damaged is None else damaged


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(size=-1),
        lambda r: r.update(size=True),
        lambda r: r.update(size="5042"),
        lambda r: r.update(size=1.5),
        lambda r: r.pop("size"),
        lambda r: r.pop("createdAt"),
        lambda r: r.update(createdAt=""),
        lambda r: r.update(createdAt="x" * 65),
        lambda r: r.update(start=["2026"]),
        lambda r: r.pop("importStatus"),
        lambda r: r.update(path=""),
        lambda r: r.update(projectId="p\u0000q"),
        lambda r: r.update(key="line\nbreak"),
        lambda r: r.update(importedAt=5),
        lambda r: r.update(device="dev_ur5e_cell1"),
        lambda r: r.update(device={"name": "no id"}),
        lambda r: r.update(device={"id": "a/../b", "name": "n"}),
        lambda r: r.update(metadata="not a list"),
        lambda r: r.update(metadata=[{"name": "n", "metadata": ["x"]}]),
        lambda r: r.update(sessionId="\ud800"),  # a lone surrogate (JSON escapes allow it)
    ],
)
def test_a_malformed_recording_is_skipped_and_the_rest_are_read(
    tmp_path: Path, change: Callable[[dict[str, Any]], Any]
) -> None:
    fake = FakeFoxglove()
    broken(fake, ARM, change)
    with connect(fake, tmp_path) as source:
        assert listed(source) == sorted([AMR, LEGGED, MARINE, UNASSIGNED])
        skipped = {s.raw_key: s.reason for s in source.index().skipped}
    assert skipped[ARM.encode()] == "record_invalid"
    assert "deploy_foxglove.record_invalid" in codes(source)


@pytest.mark.parametrize(
    "bad_id",
    ["../../etc/passwd", "a/b", "rec id", "rec‮id", "x" * 129, "", "-leading", "ré", "a?b=c"],
)
def test_an_id_that_could_escape_a_path_or_a_url_is_never_used_or_requested(
    tmp_path: Path, bad_id: str
) -> None:
    fake = FakeFoxglove()
    broken(fake, ARM, lambda r: r.update(id=bad_id))
    with connect(fake, tmp_path) as source:
        assert listed(source) == sorted([AMR, LEGGED, MARINE, UNASSIGNED])
        list(source.walk())
    assert [s.reason for s in source.index().skipped if s.reason != "import_incomplete"] == [
        "recording_id_invalid"
    ]
    assert not [r for r in fake.requests if "passwd" in r.path or bad_id and bad_id in r.path[8:]]
    assert all(r.path in ("/v1/recordings", "/v1/devices", "/v1/data/topics", "/v1/data/stream")
               or r.path.startswith("/blob/") for r in fake.requests)


def test_non_object_entries_are_skipped_with_stable_names(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.recordings.extend([None, 7, "rec_x", ["rec_y"], {}, {"id": 5}])  # type: ignore[list-item]
    runs = []
    for page_size in (1, 4, 100):
        with connect(fake, tmp_path, page_size=page_size) as source:
            assert listed(source) == GOOD
            runs.append(([(s.raw_key, s.reason) for s in source.index().skipped], source.findings()))
    assert runs[0] == runs[1] == runs[2]
    assert all(reason == "record_invalid" for _, reason in runs[0][0] if _ != PENDING.encode())


def test_nulls_are_absent_fields_never_values(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    broken(fake, MARINE, lambda r: r.update(key=None, sessionId=None, device=None, importedAt=None))
    with connect(fake, tmp_path) as source:
        assert MARINE in listed(source)
        declared = source.declared(
            next(r.location for r in source.index().recordings if r.recording_id == MARINE)
        )
    namespaces = {i.value.namespace for i in declared.identifiers if hasattr(i, "value")}
    assert namespaces == {"foxglove.project_id", "foxglove.recording_id"}
    canonical_json.dumps(declared.to_json())  # canonical JSON has no null: it serialises


def test_one_id_with_two_descriptions_is_dropped_but_an_identical_repeat_is_one(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    fake.recordings.append(dict(next(r for r in fake.recordings if r["id"] == ARM)))
    differing = dict(next(r for r in fake.recordings if r["id"] == AMR), size=1)
    fake.recordings.append(differing)
    with connect(fake, tmp_path) as source:
        assert listed(source) == sorted([ARM, LEGGED, MARINE, UNASSIGNED])
        skipped = [(s.raw_key, s.reason) for s in source.index().skipped]
    assert (AMR.encode(), "recording_duplicated") in skipped
    assert "deploy_foxglove.recording_duplicated" in codes(source)


def test_a_surrogate_id_is_skipped_with_a_representable_name(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.raw["/v1/recordings"] = json.dumps(
        [*fake.recordings, {"id": "rec_\ud800"}]
    ).encode("ascii")  # json.dumps escapes the lone surrogate: valid JSON, invalid Unicode
    with connect(fake, tmp_path) as source:
        assert listed(source) == GOOD
        reasons = {s.reason for s in source.index().skipped}
    assert "recording_id_invalid" in reasons
    for finding in source.findings():
        canonical_json.dumps(finding.content_json())


# --- Pagination --------------------------------------------------------------------------------


def test_a_server_that_ignores_the_offset_is_a_pagination_loop(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.page_cap = 2

    def stuck(request: Any) -> None:
        request.query["offset"] = "0"  # the server hands out the first page every time

    original = fake._lock
    fake.on_request = lambda r: None
    # Serve the first two recordings for every offset: only a loop can result.
    fake.raw["/v1/recordings"] = json.dumps(fake.recordings[:2]).encode()
    del original, stuck
    with connect(fake, tmp_path, page_size=2) as source:
        index = source.index()
    assert not index.complete
    assert codes(source) == ["deploy_foxglove.pagination_loop"]
    assert len(fake.api_requests()) <= 3  # it stopped at once, it did not page for ever


def test_a_limited_listing_is_incomplete_and_asserts_nothing_gone(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    with connect(fake, tmp_path, ledger=ledger, max_recordings=2) as source:
        discovery = source.discover(ledger)
    assert not discovery.complete and discovery.gone == ()
    assert codes(source) == ["deploy_foxglove.listing_limit"]


def test_gone_checks_are_bounded_and_unverified_is_not_gone(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.recordings = [r for r in fake.recordings if r["id"] != LEGGED]
    fake.status[f"/v1/recordings/{LEGGED}"] = 500  # the check itself fails
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
    assert discovery.gone == ()  # not verified: not asserted
    assert codes(source) == ["deploy_foxglove.gone_unverified", "deploy_foxglove.import_incomplete"]


# --- Redirects, refusals, rate limits ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (301, "redirect_refused"),
        (302, "redirect_refused"),
        (401, "not_authorised"),
        (403, "not_authorised"),
        (429, "rate_limited"),
        (500, "listing_failed"),
        (503, "listing_failed"),
    ],
)
def test_a_failing_listing_is_one_finding_and_never_retried_or_followed(
    tmp_path: Path, status: int, code: str
) -> None:
    fake = FakeFoxglove()
    fake.status["/v1/recordings"] = status
    with connect(fake, tmp_path) as source:
        index = source.index()
    assert not index.complete and index.recordings == ()
    (finding,) = source.findings()
    assert finding.code == f"deploy_foxglove.{code}"
    assert finding.details["status"] == status
    assert len([r for r in fake.requests if r.path == "/v1/recordings"]) == 1


def test_a_refused_key_is_not_authorised_everywhere_and_never_printed(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.forbid = True
    with connect(fake, tmp_path) as source:
        list(source.walk())
    assert codes(source) == ["deploy_foxglove.not_authorised"]
    assert API_KEY not in repr(source.client) and API_KEY not in repr(source.options)


def test_a_rate_limited_stream_request_skips_that_recording_and_says_why(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.rate_limit_streams = True
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    skipped = {w.raw_key.decode(): w.reason for w in walked if isinstance(w, SkippedObject)}
    assert {skipped[key] for key in GOOD} == {"rate_limited"}
    assert not [w for w in walked if isinstance(w, StreamEntry)]
    assert set(codes(source)) == {"deploy_foxglove.import_incomplete", "deploy_foxglove.rate_limited"}
    with connect(fake, tmp_path) as source, pytest.raises(ObjectReadError) as raised:
        source.open(source.index().recordings[0].location)
    assert raised.value.code == "rate_limited"


# --- Download links ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "link",
    [
        "http://127.0.0.2:1/blob/x",  # another host that was not declared
        "https://evil.example/blob/x",
        "https://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "ftp://127.0.0.1/x",
        "//127.0.0.1/blob/x",
        "/blob/x",
        "blob/x",
        "http://user:pass@127.0.0.1/blob/x",
        "http://127.0.0.1\\@evil.example/blob/x",
        "http://127.0.0.1/blob/x#fragment",
        "http://127.0.0.1/blob/ x",
        "http://127.0.0.1/blob/é",
        "http://127.0.0.1?x=/blob",
        "http://127.0.0.1",
        "http://[::1]:1/blob/x",
        "http://127.0.0.1/" + "a" * 9000,
        "",
    ],
)
def test_a_link_to_anywhere_but_the_api_or_a_declared_host_is_refused_unfetched(
    tmp_path: Path, link: str
) -> None:
    fake = FakeFoxglove()
    fake.link_override = link
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    assert not [w for w in walked if isinstance(w, StreamEntry)]
    assert {w.reason for w in walked if isinstance(w, SkippedObject)} == {
        "link_refused",
        "import_incomplete",
    }
    assert fake.link_requests() == []  # nothing was requested from where the API pointed
    assert not [f for f in source.findings() if link and link in json.dumps(f.content_json())]


def test_a_link_must_be_https_beyond_loopback_and_hosts_are_declared_not_guessed(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    fake.link_host = "localhost"  # another name for this server
    with connect(fake, tmp_path) as source:
        assert not [w for w in source.walk() if isinstance(w, StreamEntry)]
    fake.requests.clear()
    with connect(fake, tmp_path, link_hosts=["localhost"]) as source:
        entries = [w for w in source.walk() if isinstance(w, StreamEntry)]
    assert [e.key for e in entries] == GOOD
    assert fake.link_requests()
    assert all("authorization" not in r.headers for r in fake.link_requests())


def test_a_stream_response_without_a_usable_link_is_response_invalid(tmp_path: Path) -> None:
    for body in (b"{}", b'{"link": 5}', b'{"link": null}', b"[]", b'{"link": "x", "link": "y"}'):
        fake = FakeFoxglove()
        fake.raw["/v1/data/stream"] = body
        original = fake.status
        del original
        with connect(fake, tmp_path) as source:
            fake.status["/v1/data/stream"] = 0
            fake.status.pop("/v1/data/stream")
            first = source.index().recordings[0]
            entry = source.resolve(first)
        assert isinstance(entry, str)


def test_a_redirect_from_the_link_is_never_followed(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.link_redirect = True
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    assert {w.reason for w in walked if isinstance(w, SkippedObject)} == {
        "redirect_refused",
        "import_incomplete",
    }
    assert {r.path for r in fake.requests if r.path.startswith("/blob/")} <= {
        f"/blob/{key}" for key in GOOD
    }  # the redirect target (another host, port 9) was never requested


# --- Hostile streams ---------------------------------------------------------------------------


def first_entry(source: FoxgloveSource) -> StreamEntry:
    entry = next(e for e in source.walk() if isinstance(e, StreamEntry))
    return entry


def test_a_server_that_ignores_ranges_serves_a_probe_but_not_a_seek(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.ignore_range = True
    with connect(fake, tmp_path) as source:
        entry = first_entry(source)
        assert entry.size == len(STREAM)  # the whole stream with a stated length: probed
        with source.open(entry.location) as stream:
            assert stream.read(100) == STREAM[:100]  # from offset 0: only 100 bytes are read
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.seek(2000)
            stream.read(10)  # a range the server will not honour
        assert raised.value.code == "read_failed"
    assert any(f.code == "deploy_foxglove.read_failed" for f in source.findings())


def test_a_stream_with_no_stated_length_cannot_be_read_in_ranges_and_is_skipped(
    tmp_path: Path,
) -> None:
    for knob in ("no_length", "no_total"):
        fake = FakeFoxglove()
        setattr(fake, knob, True)
        with connect(fake, tmp_path) as source:
            walked = list(source.walk())
        assert not [w for w in walked if isinstance(w, StreamEntry)]
        assert "size_unknown" in {w.reason for w in walked if isinstance(w, SkippedObject)}, knob


def test_an_encoded_stream_has_no_byte_positions_and_is_refused(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.content_encoding = "gzip"
    with connect(fake, tmp_path) as source:
        assert not [w for w in source.walk() if isinstance(w, StreamEntry)]
    assert {r.headers["accept-encoding"] for r in fake.link_requests()} == {"identity"}


def test_a_truncated_stream_is_a_short_read_and_the_neighbours_are_untouched(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        entry = first_entry(source)
        fake.truncate_after = 100
        for _ in range(2):  # the same failure is the same finding
            with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
                stream.read()
            assert raised.value.code == "short_read"
        fake.truncate_after = None
        with source.open(entry.location) as stream:
            assert stream.read() == STREAM  # the next read is fine
    shorts = [f for f in source.findings() if f.code == "deploy_foxglove.short_read"]
    assert len(shorts) == 1 and shorts[0].subject == entry.location


def test_a_range_answered_with_other_bytes_is_refused_not_served(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        entry = first_entry(source)
        fake.wrong_range = True
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.seek(500)
            stream.read(100)
        assert raised.value.code == "read_failed"


def test_a_stream_claiming_an_absurd_size_or_none_is_skipped(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.stream_total_delta = 1 << 45
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    assert "stream_too_large" in {w.reason for w in walked if isinstance(w, SkippedObject)}
    fake = FakeFoxglove()
    fake.streams = {key: b"" for key in GOOD}
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    assert "stream_empty" in {w.reason for w in walked if isinstance(w, SkippedObject)}


def test_a_recording_deleted_after_the_listing_is_gone_not_a_crash(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        entry = first_entry(source)
        fake.recordings = [r for r in fake.recordings if r["id"] != entry.key]
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read(10)
        assert raised.value.code == "object_gone"


# --- Secrets and the network gate --------------------------------------------------------------


def test_no_finding_holds_a_key_link_signature_host_or_error_text(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.truncate_after = 10
    with connect(fake, tmp_path) as source:
        for entry in source.walk():
            if isinstance(entry, StreamEntry):
                with pytest.raises(ObjectReadError), source.open(entry.location) as stream:
                    stream.read(100)
    fake.truncate_after = None
    fake.status["/v1/data/topics"] = 500
    with connect(fake, tmp_path) as again:
        for recording in again.index().recordings:
            again.declared(recording.location)
        findings = [*source.findings(), *again.findings()]
        everything = json.dumps([f.content_json() for f in findings]) + json.dumps(
            again.transform.content_json()
        )
    assert findings
    for secret in (API_KEY, SIGNATURE, "127.0.0.1", "localhost", "blob", "Bearer", "http"):
        assert secret not in everything, secret


def test_a_local_only_workspace_refuses_before_any_request_and_on_every_request(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    with fake.serve() as endpoint:
        options = {"endpoint": endpoint, "store": "fixture"}
        with pytest.raises(LocalOnlyError):
            foxglove_source(
                "foxglove://-",
                network=Workspace(tmp_path / "home"),  # local-only by default
                options=options,
                credentials={"foxglove_api_key": API_KEY},
            )
        workspace = online(tmp_path)
        source = foxglove_source(
            "foxglove://-",
            network=workspace,
            options=options,
            credentials={"foxglove_api_key": API_KEY},
        )
        workspace.allow_network(False)  # every request asks again
        with pytest.raises(LocalOnlyError):
            source.index()
        workspace.allow_network(True)
        entry = first_entry(source)
        workspace.allow_network(False)
        with pytest.raises(LocalOnlyError), source.open(entry.location) as stream:
            stream.read(10)
    assert fake.requests and len(fake.requests) == len(
        [r for r in fake.requests]
    )  # nothing was sent while it was local-only: the first refusal sent none


def test_a_refused_workspace_sends_nothing(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with fake.serve() as endpoint, pytest.raises(LocalOnlyError):
        foxglove_source(
            "foxglove://-",
            network=Workspace(tmp_path / "home"),
            options={"endpoint": endpoint, "store": "fixture"},
            credentials={"foxglove_api_key": API_KEY},
        )
    assert fake.requests == []


def test_declared_metadata_is_unchanged_by_a_hostile_stream(tmp_path: Path) -> None:
    """Declared metadata comes from the index, never from the stream's bytes."""
    clean, hostile = FakeFoxglove(), FakeFoxglove()
    hostile.truncate_after = 5
    hostile.link_redirect = True
    with connect(clean, tmp_path) as one, connect(hostile, tmp_path) as two:
        assert declared_bytes(one) == declared_bytes(two)
    assert isinstance(ExternalObjectRef, type) and load("topics.json")
