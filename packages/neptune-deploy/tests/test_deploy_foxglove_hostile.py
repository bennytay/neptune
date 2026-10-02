"""The Foxglove connector against hostile and failing APIs (ADR 0007 §8): every response, link and
id is untrusted, and one bad one costs that one and nothing else."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deploy_foxglove_fake import API_KEY, SIGNATURE, STREAM, FakeFoxglove
from neptune.identity import canonical_json
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory
from neptune.model.knowledge import Unknown
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.foxglove import (
    FoxgloveConfigError,
    FoxgloveSource,
    StreamEntry,
    foxglove_source,
)
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
    declared_of,
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
            damaged = change(recording)  # in place, or a replacement object
            fake.recordings[index] = damaged if isinstance(damaged, dict) else recording


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
    with connect(fake, tmp_path, project="-") as source:  # no server-side project filter
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
    assert not [r for r in fake.requests if "passwd" in r.path or (bad_id and bad_id in r.path[8:])]
    assert all(
        r.path in ("/v1/recordings", "/v1/devices", "/v1/data/topics", "/v1/data/stream")
        or r.path.startswith("/blob/")
        for r in fake.requests
    )


def test_non_object_entries_are_skipped_with_stable_names(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.recordings.extend([None, 7, "rec_x", ["rec_y"], {}, {"id": 5}])  # type: ignore[list-item]
    runs = []
    for page_size in (1, 4, 100):
        with connect(fake, tmp_path, project="-", page_size=page_size) as source:
            assert listed(source) == GOOD
            runs.append(
                ([(s.raw_key, s.reason) for s in source.index().skipped], source.findings())
            )
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
    fake.raw["/v1/recordings"] = json.dumps([*fake.recordings, {"id": "rec_\ud800"}]).encode(
        "ascii"
    )  # json.dumps escapes the lone surrogate: valid JSON, invalid Unicode
    with connect(fake, tmp_path) as source:
        assert listed(source) == GOOD
        reasons = {s.reason for s in source.index().skipped}
    assert "recording_id_invalid" in reasons
    for finding in source.findings():
        canonical_json.dumps(finding.content_json())


# --- Pagination --------------------------------------------------------------------------------


def test_a_server_that_ignores_the_offset_is_a_pagination_loop(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.ignore_offset = True  # every page is the first page again
    with connect(fake, tmp_path, page_size=2) as source:
        index = source.index()
    assert not index.complete
    assert codes(source) == ["deploy_foxglove.pagination_loop"]
    assert (
        len([r for r in fake.api_requests() if r.path == "/v1/recordings"]) == 2
    )  # stopped at once


def test_a_device_list_that_ignores_the_offset_stops_at_the_loop(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path, page_size=2) as source:
        recordings = source.index().recordings  # read first: only the device list will loop
        fake.ignore_offset = True
        fake.requests.clear()
        declared = source.declared(recordings[0].location)
    assert len([r for r in fake.api_requests() if r.path == "/v1/devices"]) == 2
    assert "deploy_foxglove.pagination_loop" in codes(source)
    assert declared.identifiers  # what the recording itself states is still declared


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
    assert set(codes(source)) == {
        "deploy_foxglove.import_incomplete",
        "deploy_foxglove.rate_limited",
    }
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


@pytest.mark.parametrize(
    "body",
    [b"{}", b'{"link": 5}', b'{"link": null}', b"[]", b'{"link": "x", "link": "y"}', b"nope"],
)
def test_a_stream_response_without_a_usable_link_is_response_invalid(
    tmp_path: Path, body: bytes
) -> None:
    fake = FakeFoxglove()
    fake.raw["/v1/data/stream"] = body
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    assert {w.reason for w in walked if isinstance(w, SkippedObject)} == {
        "response_invalid",
        "import_incomplete",
    }
    assert fake.link_requests() == []


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
    for knob, reason in (("no_length", "stream_unavailable"), ("no_total", "size_unknown")):
        fake = FakeFoxglove()
        fake.ignore_range = knob == "no_length"  # a chunked 200: no ranges, no Content-Length
        setattr(fake, knob, True)
        with connect(fake, tmp_path) as source:
            walked = list(source.walk())
        assert not [w for w in walked if isinstance(w, StreamEntry)], knob
        skipped = {w.reason for w in walked if isinstance(w, SkippedObject)}
        assert skipped == {reason, "import_incomplete"}, knob
        if knob == "no_length":
            finding = next(f for f in source.findings() if f.code.endswith(reason))
            assert finding.details == {"status": 200, "cause": "range_invalid"}


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
    for secret in (API_KEY, SIGNATURE, "127.0.0.1", "localhost", "blob", "Bearer", "://"):
        assert secret not in everything, secret


def test_a_workspace_switched_to_local_only_refuses_the_next_request(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with fake.serve() as endpoint:
        workspace = online(tmp_path)
        source = foxglove_source(
            "foxglove://-",
            network=workspace,
            options={"endpoint": endpoint, "store": "fixture"},
            credentials={"foxglove_api_key": API_KEY},
        )
        entry = first_entry(source)
        before = len(fake.requests)
        workspace.allow_network(False)  # every request asks again
        with pytest.raises(LocalOnlyError), source.open(entry.location) as stream:
            stream.read(10)
        with pytest.raises(LocalOnlyError):
            source.declared(source.index().recordings[0].location)
        assert len(fake.requests) == before  # the refusal sent nothing
        source.close()


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


# --- Malformed numbers and headers -------------------------------------------------------------

OVERFLOW = "9" * 5000  # past int()'s 4,300-digit limit


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Range": "bytes ²-9/10"},  # a superscript digit: str.isdigit says yes, int says no
        {"Content-Range": "bytes 0-³/10"},
        {"Content-Range": "bytes 0-9/¹"},
        {"Content-Range": "bytes -1-9/10"},
        {"Content-Range": f"bytes 0-9/{OVERFLOW}"},
        {"Content-Range": f"bytes {OVERFLOW}-9/10"},
        {"Content-Range": ""},
        {"Content-Range": "items 0-9/10"},
    ],
)
def test_a_malformed_content_range_fails_that_read_only(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        entry = first_entry(source)
        fake.headers = headers
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read(10)
        assert raised.value.code == "read_failed"
        fake.headers = {}
        with source.open(entry.location) as stream:
            assert stream.read(10) == STREAM[:10]  # the next read is fine


@pytest.mark.parametrize("length", ["²", "-1", OVERFLOW, "1e3", ""])
def test_a_malformed_content_length_on_an_ignored_range_is_not_a_size(
    tmp_path: Path, length: str
) -> None:
    fake = FakeFoxglove()
    fake.ignore_range = True
    fake.headers = {"Content-Length": length}
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    assert not [w for w in walked if isinstance(w, StreamEntry)]


@pytest.mark.parametrize(
    "size", ["9223372036854775808", "9" * 4400, "-100000000000000000000", "1.5", "5e3"]
)
def test_a_recording_size_beyond_the_apis_bigint_is_not_a_recording(
    tmp_path: Path, size: str
) -> None:
    fake = FakeFoxglove()
    body = json.dumps(fake.recordings).replace('"size": 5042', f'"size": {size}', 1)
    fake.raw["/v1/recordings"] = body.encode()
    with connect(fake, tmp_path) as source:
        index = source.index()
    if size == "9" * 4400:  # past int()'s limit: the page is not parseable at all
        assert not index.complete and index.recordings == ()
        assert codes(source) == ["deploy_foxglove.response_invalid"]
    else:  # that entry alone is not a recording
        assert len(index.recordings) == 4
        assert "deploy_foxglove.record_invalid" in codes(source)


def test_a_non_ascii_digit_in_an_option_time_is_refused(tmp_path: Path) -> None:
    with FakeFoxglove().serve() as endpoint, pytest.raises(FoxgloveConfigError):
        foxglove_source(
            "foxglove://-",
            network=online(tmp_path),
            options={"endpoint": endpoint, "store": "fixture", "start": "٢٠٢٦-09-30T00:00:00Z"},
            credentials={"foxglove_api_key": API_KEY},
        )


# --- Secrets in URLs ---------------------------------------------------------------------------


def texts(error: BaseException) -> list[str]:
    """The message of ``error`` and of everything it was raised from."""
    found = []
    current: BaseException | None = error
    while current is not None:
        found.append(str(current))
        current = current.__cause__ or current.__context__
    return found


@pytest.mark.parametrize(
    ("url", "endpoint"),
    [
        ("foxglove://-", "https://key:SUPERSECRET@api.example/v1"),
        ("foxglove://-", "https://api.example/v1?token=SUPERSECRET"),
        ("foxglove://-", "https://api.example/v1#SUPERSECRET"),
        ("foxglove://-", "http://api.example:9000/v1/SUPERSECRET"),  # plain http off loopback
        ("foxglove://user:SUPERSECRET@prj_a", "https://api.example/v1"),
        ("foxglove://prj_a/SUPERSECRET", "https://api.example/v1"),
        ("foxglove://prj_a?key=SUPERSECRET", "https://api.example/v1"),
    ],
)
def test_no_secret_in_a_url_reaches_an_error(tmp_path: Path, url: str, endpoint: str) -> None:
    with pytest.raises(FoxgloveConfigError) as raised:
        foxglove_source(
            url,
            network=online(tmp_path),
            options={"endpoint": endpoint, "store": "fixture"},
            credentials={"foxglove_api_key": API_KEY},
        )
    assert [text for text in texts(raised.value) if "SUPERSECRET" in text] == []


@pytest.mark.parametrize(
    "link",
    [
        "https://key:SUPERSECRET@api.example/blob/x?sig=SUPERSECRET",
        "http://127.0.0.1:1/blob/x?sig=SUPERSECRET#SUPERSECRET",
        "https://evil.example/blob/x?sig=SUPERSECRET",
    ],
)
def test_no_secret_in_a_refused_link_reaches_an_error_or_a_finding(
    tmp_path: Path, link: str
) -> None:
    fake = FakeFoxglove()
    fake.link_override = link
    with connect(fake, tmp_path) as source:
        entry = source.resolve(source.index().recordings[0])
        with pytest.raises(ObjectReadError) as raised:
            source.open(source.index().recordings[0].location)
    assert entry == "link_refused"
    everything = texts(raised.value) + [json.dumps(f.content_json()) for f in source.findings()]
    assert [text for text in everything if "SUPERSECRET" in text] == []


# --- Bounded documents, bytes and time ---------------------------------------------------------


def test_the_listing_byte_budget_stops_the_listing_the_same_for_every_page_size(
    tmp_path: Path,
) -> None:
    results = []
    for page_size in (1, 3, 100):
        fake = FakeFoxglove()
        with connect(fake, tmp_path, max_listing_bytes=1024, page_size=page_size) as source:
            index = source.index()
            results.append((listed(source), index.complete, source.findings()))
    assert results[0] == results[1] == results[2]
    kept, complete, findings = results[0]
    assert 0 < len(kept) < len(GOOD) and not complete
    (finding,) = [f for f in findings if f.code == "deploy_foxglove.listing_limit"]
    assert finding.details["max_listing_bytes"] == 1024


def test_one_huge_recording_document_is_not_used(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    broken(
        fake,
        ARM,
        lambda r: r.update(metadata=[{"name": "big", "metadata": {"k": "v" * (2 * 1024 * 1024)}}]),
    )
    with connect(fake, tmp_path) as source:
        assert listed(source) == sorted([AMR, LEGGED, MARINE, UNASSIGNED])
        assert (ARM.encode(), "record_invalid") in [
            (s.raw_key, s.reason) for s in source.index().skipped
        ]


@pytest.mark.slow
def test_ten_thousand_huge_unusable_ids_cost_a_bounded_few_bytes_each(tmp_path: Path) -> None:
    import tracemalloc

    fake = FakeFoxglove()
    fake.raw["/v1/recordings"] = json.dumps(
        [{"id": f"{i:05d}-" + "x" * 3_000, "size": 1} for i in range(10_000)]
    ).encode()
    with connect(fake, tmp_path, page_size=2000) as source:
        tracemalloc.start()
        index = source.index()
        held, _ = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    assert index.recordings == () and len(index.skipped) == 10_000
    assert all(len(s.raw_key) <= 256 and s.length >= 3000 for s in index.skipped)
    assert held < 16 * 2**20, f"{held / 2**20:.0f} MiB held"  # not the 30 MB of ids


def test_a_device_list_beyond_the_byte_budget_is_not_all_covered(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.devices = fake.devices + [
        {"id": f"dev_pad_{i}", "name": f"pad-{i}", "properties": {"k": "v" * 400}}
        for i in range(40)
    ]
    with connect(fake, tmp_path, max_listing_bytes=4096) as source:
        declared = [source.declared(r.location) for r in source.index().recordings]
    assert len(declared) == 5
    assert "deploy_foxglove.listing_limit" in codes(source)


def test_a_recording_with_a_huge_topic_list_is_unknown_not_unbounded(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    fake.topics[ARM] = [
        {
            "topic": f"/t{i}",
            "schemaName": "s",
            "schemaEncoding": "e",
            "encoding": "c",
            "version": "1",
        }
        for i in range(10_001)
    ]
    with connect(fake, tmp_path, page_size=2000) as source:
        declared = declared_of(source, ARM)
        assert declared.topics == () and isinstance(declared.topics_coverage, Unknown)
        assert "deploy_foxglove.listing_limit" in codes(source)


def test_a_trickling_body_is_cut_off_at_the_request_deadline(tmp_path: Path) -> None:
    import time

    fake = FakeFoxglove()
    fake.streams[ARM] = b"x" * 100
    with connect(fake, tmp_path, timeout=1.0) as source:
        entry = next(e for e in source.walk() if isinstance(e, StreamEntry) and e.key == ARM)
        fake.drip = 0.2  # each byte well inside the socket timeout; the whole body takes 20 s
        started = time.monotonic()
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read()
        elapsed = time.monotonic() - started
    assert raised.value.code == "read_failed" and elapsed < 5
    (finding,) = [f for f in source.findings() if f.code.endswith("read_failed")]
    assert finding.details["cause"] == "deadline_exceeded"


def test_declared_values_are_bounded_text(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    broken(fake, ARM, lambda r: r.update(key="k" * 5000))
    with connect(fake, tmp_path) as source:
        assert ARM not in listed(source)
