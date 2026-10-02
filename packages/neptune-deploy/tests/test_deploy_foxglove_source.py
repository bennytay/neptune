"""The Foxglove connector against recorded API responses: identity, discovery, declared metadata,
ranged reads (ADR 0007). No live calls: ``deploy_foxglove_fake`` serves the recorded fixtures."""

import copy
import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from deploy_foxglove_fake import API_KEY, STREAM, FakeFoxglove, load
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef, LogicalId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Known,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import AdapterLocator, JsonPointer
from neptune.model.source import SourceArtifact
from neptune.store.workspace import Workspace
from neptune_deploy.sources.foxglove import (
    DeclaredRecording,
    FoxgloveSource,
    StreamEntry,
    foxglove_source,
)
from neptune_deploy.sources.object_store import ObjectReadError, SkippedObject

SITE = "fixture"
ARM, AMR, LEGGED, MARINE, PENDING, UNASSIGNED = (
    "rec_arm_cell_0001",
    "rec_amr_fleet_0002",
    "rec_legged_0003",
    "rec_marine_0004",
    "rec_pending_0005",
    "rec_unassigned_0006",
)


def online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


@contextmanager
def connect(
    fake: FakeFoxglove,
    tmp_path: Path,
    *,
    ledger: SourceLedger | None = None,
    project: str = "prj_plant_a",
    **options: Any,
) -> Iterator[FoxgloveSource]:
    with fake.serve() as endpoint:
        source = foxglove_source(
            f"foxglove://{project}",
            network=online(tmp_path),
            ledger=ledger,
            options={"endpoint": endpoint, "store": SITE, **options},
            credentials={"foxglove_api_key": API_KEY},
        )
        try:
            yield source
        finally:
            source.close()


def object_key(recording_id: str) -> tuple[str, ...]:
    return ("external", "deploy_foxglove", f"{SITE}:recording/{recording_id}")


def location(recording_id: str, token: str) -> ExternalObjectRef:
    return ExternalObjectRef("deploy_foxglove", f"{SITE}:recording/{recording_id}", token)


def fingerprint(
    source: FoxgloveSource, ledger: SourceLedger, entries: Iterator[Any]
) -> dict[str, SourceArtifact]:
    """What the compiler's scan does with a walk: digest each stream, observe it in the ledger."""
    artifacts = {}
    for entry in entries:
        if isinstance(entry, StreamEntry):
            with source.open(entry.location) as stream:
                artifact = digest_stream(stream, chunk_size=1024)
            ledger.observe(entry.location, artifact)
            artifacts[entry.key] = artifact
    return artifacts


def codes(source: FoxgloveSource) -> list[str]:
    return sorted(finding.code for finding in source.findings())


def declared_bytes(source: FoxgloveSource) -> bytes:
    """Every recording's declared metadata, as canonical JSON bytes."""
    documents = [source.declared(r.location).to_json() for r in source.index().recordings]
    return canonical_json.dumps(documents)


# --- Identity ----------------------------------------------------------------------------------


def test_the_index_is_every_complete_recording_by_id_with_external_identity(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        index = source.index()
    assert index.complete
    assert [r.recording_id for r in index.recordings] == sorted(
        [ARM, AMR, LEGGED, MARINE, UNASSIGNED]
    )
    arm = next(r for r in index.recordings if r.recording_id == ARM)
    assert arm.location == location(
        ARM, "import:2026-09-30T06:15:02.123456789Z;created:2026-09-30T06:10:00Z;size:5042"
    )
    # A recording that is not imported has no data to stream: skipped, counted, never asserted gone.
    assert [(s.raw_key, s.reason) for s in index.skipped] == [
        (PENDING.encode(), "import_incomplete")
    ]
    (finding,) = source.findings()
    assert finding.code == "deploy_foxglove.import_incomplete"
    assert finding.details["statuses"] == {"pending": 1}


def test_only_get_requests_and_one_post_to_the_stream_endpoint_are_ever_sent(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        list(source.walk())
        for recording in source.index().recordings:
            source.declared(recording.location)
    posts = [r for r in fake.requests if r.method != "GET"]
    assert posts and {(r.method, r.path) for r in posts} == {("POST", "/v1/data/stream")}
    assert {r.method for r in fake.requests} == {"GET", "POST"}


def test_walk_measures_each_stream_and_hints_mcap(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        walked = list(source.walk())
    entries = [e for e in walked if isinstance(e, StreamEntry)]
    assert [e.key for e in entries] == sorted([ARM, AMR, LEGGED, MARINE, UNASSIGNED])
    # The stream's size is measured; it is not the recording's stored ``size`` (that is a fact).
    assert {e.size for e in entries} == {len(STREAM)}
    assert {e.name for e in entries} == {
        f"{key}.mcap" for key in (ARM, AMR, LEGGED, MARINE, UNASSIGNED)
    }
    assert [(w.raw_key, w.reason) for w in walked if isinstance(w, SkippedObject)] == [
        (PENDING.encode(), "import_incomplete")
    ]


def test_the_ids_are_the_apis_and_the_project_is_not_part_of_identity(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as one:
        in_project = [r.location for r in one.index().recordings]
    assert {r.query.get("projectId") for r in fake.api_requests()} == {"prj_plant_a"}
    fake.requests.clear()
    with connect(fake, tmp_path, project="-") as every:
        assert [r.location for r in every.index().recordings] == in_project
    assert all("projectId" not in r.query for r in fake.api_requests())
    assert {r.headers["user-agent"] for r in fake.requests} == {"neptune-deploy-foxglove/0.1.0"}


def test_a_declared_store_scopes_ids_and_the_public_endpoint_does_not(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        assert source.index().recordings[0].location.object_id.startswith(f"{SITE}:recording/")
    with fake.serve() as endpoint, pytest.raises(Exception, match="store"):
        foxglove_source(
            "foxglove://-",
            network=online(tmp_path),
            options={"endpoint": endpoint},
            credentials={"foxglove_api_key": API_KEY},
        )


# --- Revisions and discovery ---------------------------------------------------------------------


def test_a_recording_imported_again_is_a_new_revision_of_the_same_recording(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        before = {r.recording_id: r.location for r in source.index().recordings}
        fingerprint(source, ledger, source.walk())
    fake.recordings = load("recordings_after_reimport.json")  # the arm cell's import ran again
    fake.requests.clear()
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [r.recording_id for r in discovery.changed] == [ARM]
        (changed,) = discovery.changed
        assert changed.location.key == before[ARM].key  # one recording...
        assert changed.location.revision_token != before[ARM].revision_token  # ...a new revision
        assert discovery.new == () and discovery.gone == ()
        assert [r.recording_id for r, _ in discovery.unchanged] == sorted(
            [AMR, LEGGED, MARINE, UNASSIGNED]
        )
        fingerprint(source, ledger, source.walk())
    # Only the changed recording's stream was requested: unchanged ones are never fetched.
    streams = [r for r in fake.requests if r.method == "POST"]
    assert {r.body for r in streams} == {streams[0].body} and ARM.encode() in streams[0].body
    # Identical bytes under the new import are no new revision (root ADR 0009): the token only
    # observes. The ledger holds one revision per recording.
    assert len(ledger.revisions()) == 5


def test_a_recording_uploaded_again_under_the_same_key_is_a_new_recording_never_a_merge(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        old = source.declared(
            next(r.location for r in source.index().recordings if r.recording_id == AMR)
        )
    fake.recordings = load("recordings_after_reupload.json")  # deleted, uploaded again: a new id
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        (new,) = discovery.new
        assert new.recording_id == "rec_amr_fleet_0102"
        # The old id is gone, but only because the API says so (404), not because a page lacks it.
        assert [r.location.key for r in discovery.gone] == [object_key(AMR)]
        again = source.declared(new.location)
    assert any(r.method == "GET" and r.path == f"/v1/recordings/{AMR}" for r in fake.requests)
    # Both declare the same key; neither is the other. Consolidation is Memory's decision.
    key = LogicalId("foxglove.recording_key", "amr07-2026-09-30-b")
    for declared in (old, again):
        assert key in {i.value for i in declared.identifiers if isinstance(i, Known)}
    assert old.location != again.location
    assert identifiers(old).keys() & identifiers(again).keys() >= {
        ("foxglove.recording_key", "amr07-2026-09-30-b"),
        ("foxglove.device_id", "dev_amr_07"),
    }
    assert recording_ids(old) == {AMR} and recording_ids(again) == {"rec_amr_fleet_0102"}


def test_a_missing_recording_is_not_gone_while_the_api_still_has_it(tmp_path: Path) -> None:
    """Offset paging over a live index can skip an entry; a blank is never an absence."""
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.skip_one = True  # later pages shift: the oldest recording is never listed
    with connect(fake, tmp_path, ledger=ledger, page_size=2) as source:
        discovery = source.discover(ledger)
    assert discovery.gone == ()
    # A listing narrowed by a filter lists fewer recordings; the rest are not gone either.
    with connect(fake, tmp_path, ledger=ledger, device_id="dev_amr_07") as source:
        assert [r.recording_id for r in source.index().recordings] == [AMR]
        assert source.discover(ledger).gone == ()


def test_an_incomplete_listing_asserts_nothing_gone(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.recordings = [r for r in fake.recordings if r["id"] != LEGGED]
    fake.status = {"/v1/recordings": 500}
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
    assert not discovery.complete and discovery.gone == () and discovery.new == ()
    assert codes(source) == ["deploy_foxglove.listing_failed"]


def test_gone_is_asserted_when_the_api_says_not_found(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.recordings = [r for r in fake.recordings if r["id"] != LEGGED]
    with connect(fake, tmp_path, ledger=ledger) as source:
        gone = source.discover(ledger).gone
    assert [r.location.key for r in gone] == [object_key(LEGGED)]


def test_walk_with_a_ledger_yields_only_what_changed_and_costs_no_other_stream(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    fake.requests.clear()
    with connect(fake, tmp_path, ledger=ledger) as source:
        walked = list(source.walk())
    assert [w.reason for w in walked if not isinstance(w, StreamEntry)] == ["import_incomplete"]
    assert not [w for w in walked if isinstance(w, StreamEntry)]
    assert [r for r in fake.requests if r.method == "POST" or r.path.startswith("/blob/")] == []


# --- Declared metadata ---------------------------------------------------------------------------


def declared_of(source: FoxgloveSource, recording_id: str) -> DeclaredRecording:
    recording = next(r for r in source.index().recordings if r.recording_id == recording_id)
    return source.declared(recording.location)


def recording_ids(declared: DeclaredRecording) -> set[str]:
    return {v for (ns, v) in identifiers(declared) if ns == "foxglove.recording_id"}


def identifiers(declared: DeclaredRecording) -> dict[tuple[str, str], Any]:
    found = {}
    for knowledge in declared.identifiers:
        if isinstance(knowledge, Known):
            value = knowledge.value
        else:
            assert isinstance(knowledge, Ambiguous)
            value = knowledge.candidates[0].value
        found[(value.namespace, value.value)] = knowledge
    return found


def test_declared_ids_are_stated_knowledge_citing_the_response_object(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        declared = declared_of(source, ARM)
        transform = source.transform
    found = identifiers(declared)
    assert set(found) == {
        ("foxglove.device_id", "dev_ur5e_cell1"),
        ("foxglove.device_name", "ur5e-cell-1"),
        ("foxglove.project_id", "prj_plant_a"),
        ("foxglove.recording_id", ARM),
        ("foxglove.recording_key", "cell1-2026-09-30-a"),
        ("foxglove.session_id", "rs_shift_0930"),
    }
    assert list(found) == sorted(found)  # sorted by (namespace, value)
    device = found[("foxglove.device_id", "dev_ur5e_cell1")]
    assert isinstance(device, Known)
    grounding = device.provenance
    assert grounding.assertion_kind is AssertionKind.STATED  # type: ignore[union-attr]
    evidence = grounding.evidence  # type: ignore[union-attr]
    assert evidence.source == declared.location  # the recording revision the API described
    locator, pointer = evidence.locator
    assert isinstance(locator, AdapterLocator) and locator.kind == "deploy_foxglove:response"
    fields = dict(locator.fields)
    assert fields["call"] == "GET /recordings" and fields["recording"] == ARM
    object_hash = fields["object_sha256"]
    assert isinstance(object_hash, str) and object_hash.startswith("sha256:")
    assert pointer == JsonPointer("/device/id")
    assert grounding.transform == transform.id  # type: ignore[union-attr]
    # The hash is of the response object: the same object, the same hash, whatever the page size.
    stripped = next(r for r in fake.recordings if r["id"] == ARM)
    assert object_hash == content_id(canonical_json.dumps(stripped))


def test_facts_are_verbatim_and_an_absent_one_is_unknown_never_a_default(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    for recording in fake.recordings:
        if recording["id"] == MARINE:
            del recording["importedAt"]
            recording["importedAt"] = None  # the API may send null for an absent field
    with connect(fake, tmp_path) as source:
        declared = declared_of(source, MARINE)
        arm = declared_of(source, ARM)
    facts = dict(declared.facts)
    assert isinstance(facts["imported_at"], Unknown)
    assert isinstance(facts["start"], Known) and facts["start"].value == "2026-09-28T08:00:00Z"
    assert facts["size"].value == 5042  # type: ignore[union-attr]
    # Times stay as the API states them: no UTC conversion, no rounding of nanoseconds.
    assert dict(arm.facts)["imported_at"].value == "2026-09-30T06:15:02.123456789Z"  # type: ignore[union-attr]
    assert "device" not in {name for name, _ in declared.facts}


def test_a_recording_with_no_device_declares_no_device_identifier(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        declared = declared_of(source, UNASSIGNED)
    namespaces = {namespace for namespace, _ in identifiers(declared)}
    assert not namespaces & {"foxglove.device_id", "foxglove.device_name"}
    assert declared.device_properties == ()


def test_device_properties_are_values_and_identifiers_only_where_the_operator_declares(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        plain = declared_of(source, AMR)
    assert [k.value.name for k in plain.device_properties] == ["asset_tag", "embodiment"]  # type: ignore[union-attr]
    assert not [n for n, _ in identifiers(plain) if n.startswith("foxglove.device_property.")]
    with connect(fake, tmp_path, identifier_properties=["asset_tag"]) as source:
        tagged = declared_of(source, AMR)
        other = declared_of(source, LEGGED)  # no asset_tag: nothing declared, nothing invented
    assert ("foxglove.device_property.asset_tag", "AMR-0007") in identifiers(tagged)
    assert not [n for n, _ in identifiers(other) if n.startswith("foxglove.device_property.")]
    # Two devices that share a property are two devices: every declaration is per recording.
    assert ("foxglove.device_id", "dev_amr_07") in identifiers(tagged)


def test_a_device_rename_is_ambiguous_until_the_index_agrees_and_never_a_new_revision(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        before = declared_of(source, ARM)
    fake.devices = load("devices_after_rename.json")  # renamed; its id is unchanged
    with connect(fake, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        after = declared_of(source, ARM)
        # The bytes did not change, so nothing is re-read: the ledger sees no new revision.
        assert (discovery.new, discovery.changed, discovery.gone) == ((), (), ())
        assert after.location == before.location
        assert codes(source) == [
            "deploy_foxglove.device_name_differs",
            "deploy_foxglove.import_incomplete",
        ]
    names = [k for k in after.identifiers if not isinstance(k, Known) and isinstance(k, Ambiguous)]
    (ambiguous,) = names
    assert [c.value.value for c in ambiguous.candidates] == ["ur5e-cell-1", "ur5e-cell-1-retrofit"]
    assert [c.provenance.evidence.locator[0].fields[0][1] for c in ambiguous.candidates] == [  # type: ignore[union-attr]
        "GET /recordings",
        "GET /devices",
    ]
    # The id is the stable thing, and it is unchanged: declared, not merged.
    assert (
        identifiers(after)[("foxglove.device_id", "dev_ur5e_cell1")]
        == identifiers(before)[("foxglove.device_id", "dev_ur5e_cell1")]
    )
    # Once the index states the new name too, it is one name again.
    fake.recordings = copy.deepcopy(fake.recordings)
    for recording in fake.recordings:
        if recording["id"] == ARM:
            recording["device"]["name"] = "ur5e-cell-1-retrofit"
    with connect(fake, tmp_path) as source:
        settled = declared_of(source, ARM)
        assert "deploy_foxglove.device_name_differs" not in codes(source)
    name = identifiers(settled)[("foxglove.device_name", "ur5e-cell-1-retrofit")]
    assert isinstance(name, Known)


def test_declared_topics_are_stated_with_their_count_and_unread_topics_say_why(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        declared = declared_of(source, LEGGED)
    assert [k.value.topic for k in declared.topics] == ["/battery", "/diagnostics", "/imu"]  # type: ignore[union-attr]
    schemaless = declared.topics[1]
    assert isinstance(schemaless, Known) and schemaless.value.schema_name == ""
    assert isinstance(declared.topics_coverage, Known) and declared.topics_coverage.value == 3
    topic_calls = [r for r in fake.api_requests() if r.path == "/v1/data/topics"]
    assert topic_calls and all(
        set(r.query) <= {"recordingId", "limit", "offset"} for r in topic_calls
    )
    with connect(fake, tmp_path, topics=False) as source:
        off = declared_of(source, LEGGED)
    assert off.topics == () and isinstance(off.topics_coverage, NotCovered)
    fake.status["/v1/data/topics"] = 500
    with connect(fake, tmp_path) as source:
        failed = declared_of(source, LEGGED)
        assert "deploy_foxglove.topics_failed" in codes(source)
    assert failed.topics == () and isinstance(failed.topics_coverage, Unknown)


def test_mcap_metadata_records_are_declared_verbatim(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        declared = declared_of(source, ARM)
    (record,) = declared.metadata
    assert isinstance(record, Known)
    assert record.value.name == "cell" and record.value.value == {"line": "B", "station": "4"}


# --- Determinism -------------------------------------------------------------------------------


def test_everything_is_independent_of_page_size_and_how_the_server_pages(tmp_path: Path) -> None:
    results = []
    for page_size, cap in ((1, None), (2, None), (1000, None), (2000, 2), (3, 1)):
        fake = FakeFoxglove()
        fake.page_cap = cap
        with connect(fake, tmp_path, page_size=page_size) as source:
            walked = [
                (type(w).__name__, getattr(w, "location", None), getattr(w, "size", None))
                for w in source.walk()
            ]
            results.append(
                (walked, declared_bytes(source), source.findings(), source.index().complete)
            )
    assert all(result == results[0] for result in results)
    assert results[0][3]


def test_the_same_run_twice_is_byte_identical(tmp_path: Path) -> None:
    runs = []
    for _ in range(2):
        fake = FakeFoxglove()
        with connect(fake, tmp_path) as source:
            list(source.walk())
            runs.append(
                (declared_bytes(source), [f.id for f in source.findings()], source.transform.id)
            )
    assert runs[0] == runs[1]


def test_a_limited_index_keeps_the_same_recordings_for_every_page_size(tmp_path: Path) -> None:
    kept = []
    for page_size in (1, 2, 5):
        fake = FakeFoxglove()
        with connect(fake, tmp_path, page_size=page_size, max_recordings=3) as source:
            index = source.index()
            kept.append(([r.recording_id for r in index.recordings], index.complete, codes(source)))
    assert kept[0] == kept[1] == kept[2]
    assert kept[0][1] is False and "deploy_foxglove.listing_limit" in kept[0][2]


# --- Ranged reads ------------------------------------------------------------------------------


def test_open_reads_the_stream_in_ranges_never_the_whole_recording_for_a_probe(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    big = STREAM * 40  # 200 KiB
    fake.streams[ARM] = big
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.walk() if isinstance(e, StreamEntry) and e.key == ARM)
        assert entry.size == len(big)
        fake.requests.clear()
        with source.open(entry.location) as stream:
            head = stream.read(1024)  # what a probe does
        assert head == big[:1024]
        blobs = fake.link_requests()
        assert len(blobs) == 1 and blobs[0].headers["range"] == "bytes=0-65535"  # one small window
        fake.requests.clear()
        with source.open(entry.location) as stream:
            stream.seek(150_000)
            assert stream.read(100) == big[150_000:150_100]
            stream.seek(-10, io.SEEK_END)
            assert stream.read() == big[-10:]
        assert all("range" in r.headers for r in fake.link_requests())
        with source.open(entry.location) as stream:
            assert stream.read() == big  # hashing reads it all, in windows of at most 8 MiB


def test_each_read_asks_for_a_fresh_link_for_the_whole_recording_as_mcap(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.walk() if isinstance(e, StreamEntry) and e.key == ARM)
        fake.requests.clear()
        with source.open(entry.location) as stream:
            stream.read(10)
    (post,) = [r for r in fake.requests if r.method == "POST"]
    assert canonical_json.loads(post.body) == {
        "compressionFormat": "lz4",
        "includeAttachments": True,
        "outputFormat": "mcap",
        "recordingId": ARM,
    }  # no time window, no topic filter, no other export option


def test_the_api_key_reaches_the_api_and_never_the_download_link(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        for entry in source.walk():
            if isinstance(entry, StreamEntry):
                with source.open(entry.location) as stream:
                    stream.read(100)
    assert fake.link_requests()
    assert all("authorization" not in r.headers for r in fake.link_requests())
    assert all(r.headers["authorization"] == f"Bearer {API_KEY}" for r in fake.api_requests())
    # The signature in the link is sent as the API wrote it (percent-escapes and all).
    assert {r.query["sig"] for r in fake.link_requests()} == {"a+b/c=="}


def test_the_reader_checks_every_chunk_against_the_fingerprint(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        artifacts = fingerprint(source, ledger, source.walk())
        entry = next(e for e in source.walk() if isinstance(e, StreamEntry) and e.key == ARM)
        artifact = artifacts[ARM]
        assert artifact.content_id == content_id(STREAM)
        reader = source.reader(entry.location, artifact)
        assert reader.content_id == artifact.content_id and reader.size == len(STREAM)
        assert reader.read(1000, 3000) == STREAM[1000:4000]
        assert reader.read(len(STREAM) - 5, 50) == STREAM[-5:]
        tampered = bytearray(STREAM)
        tampered[2000] ^= 0xFF
        fake.streams[ARM] = bytes(tampered)  # the stream changes after it was fingerprinted
        fresh = source.reader(entry.location, artifact)
        with pytest.raises(ObjectReadError) as raised:
            fresh.read(1024, 1024)
        assert raised.value.code == "object_changed"
        assert fresh.read(0, 1024) == STREAM[:1024]  # the chunks before it still verify
    assert "deploy_foxglove.object_changed" in codes(source)


def test_an_adapter_reading_in_small_steps_gets_the_whole_mcap_from_ranged_reads(
    tmp_path: Path,
) -> None:
    """What an adapter does through ``reader()``: many small ranged reads, each chunk checked.
    (Members may not import a format adapter, so the MCAP adapter itself is not run here.)"""
    fake = FakeFoxglove()
    ledger = SourceLedger()
    with connect(fake, tmp_path) as source:
        artifacts = fingerprint(source, ledger, source.walk())
        entry = next(e for e in source.walk() if isinstance(e, StreamEntry) and e.key == ARM)
        reader = source.reader(entry.location, artifacts[ARM])
        fake.requests.clear()
        pieces = [reader.read(offset, 777) for offset in range(0, reader.size, 777)]
    data = b"".join(pieces)
    assert data == STREAM and data[:8] == data[-8:] == b"\x89MCAP0\r\n"
    chunks = -(-len(STREAM) // 1024)  # the artifact was hashed in 1 KiB chunks
    assert len([r for r in fake.requests if r.method == "POST"]) == chunks
    assert len(fake.link_requests()) == chunks  # one ranged GET per chunk, each from a fresh link


def test_a_stream_that_changed_size_is_object_changed_never_a_mix(tmp_path: Path) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        entry = next(e for e in source.walk() if isinstance(e, StreamEntry) and e.key == ARM)
        fake.streams[ARM] = STREAM + b"re-imported with more data"
        with pytest.raises(ObjectReadError) as raised, source.open(entry.location) as stream:
            stream.read(100)
        assert raised.value.code == "object_changed"
    (finding,) = [f for f in source.findings() if f.code.endswith("object_changed")]
    assert finding.subject == entry.location


def test_open_refuses_another_connectors_location_and_an_unlisted_revision(
    tmp_path: Path,
) -> None:
    fake = FakeFoxglove()
    with connect(fake, tmp_path) as source:
        with pytest.raises(TypeError):
            source.open(ExternalObjectRef("deploy_s3", "bucket/key", "etag:x"))
        with pytest.raises(ObjectReadError) as raised:
            source.open(location(ARM, "import:old;created:old;size:1"))
        assert raised.value.code == "not_listed"
        with pytest.raises(ObjectReadError):
            source.declared(location("rec_unknown", "import:-;created:x;size:1"))
    assert source.findings() == source.findings()  # a caller's mistake is not the store's finding
    assert not [f for f in source.findings() if "not_listed" in f.code]
