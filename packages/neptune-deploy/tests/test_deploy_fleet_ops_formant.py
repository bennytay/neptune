"""The Formant connector: stated records, Intervention records, read-only and local-only (ADR 0010).

A real HTTP server (``deploy_formant_fake``) serves recorded-shape fixtures of an AMR fleet, a
manipulator cell and a legged inspection robot. Nothing here reaches a network.
"""

import json
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

import pytest

from deploy_formant_fake import FakeFormant, fixture, serve
from neptune.identity.hashing import content_id
from neptune.model.ids import ExternalObjectRef
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.lifecycle import Intervention
from neptune.model.provenance import JsonPointer
from neptune.store.workspace import LocalOnlyError
from neptune_deploy.sources.fleet_ops import (
    DocumentReadError,
    FleetOpsConfigError,
    FormantSource,
    formant_source,
)
from neptune_deploy.sources.stated_records import parse_json

CREDENTIALS = {"formant_access_token": "test-token-123"}


class Online:
    def __init__(self) -> None:
        self.purposes: list[str] = []

    def require_network(self, purpose: str) -> None:
        self.purposes.append(purpose)


class Offline:
    def require_network(self, purpose: str) -> None:
        raise LocalOnlyError(purpose)


def source(endpoint: str, network: Any = None, **options: Any) -> FormantSource:
    return formant_source(
        "formant://org-acme",
        network=network or Online(),
        options={"endpoint": endpoint, "instance": "@acme-test", **options},
        credentials=CREDENTIALS,
    )


def codes(src: FormantSource) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for finding in src.findings():
        out.setdefault(finding.code.split(".")[-1], []).append(finding)
    return out


def _ir(record: Any) -> str:
    """An intervention's Formant id: its first identifier's value."""
    return str(record.identifiers.value[0].value.value)


def test_the_five_parts_are_documents_with_stated_tables() -> None:
    store = FakeFormant.standard()
    with serve(store) as endpoint:
        src = source(endpoint)
        entries = list(src.walk())
    names = [entry.part for entry in entries]
    assert names == ["annotations", "devices", "events", "interventions", "recordings"]
    catalog = src.catalog()
    tables = {table.name.value: table for table in catalog.of("structured_table")}
    assert set(tables) == {f"deploy_formant {name}" for name in names}
    for record in catalog.records:
        assert record.provenance.assertion_kind is AssertionKind.STATED
    # Every row and cell cites the document the source serves, at an exact JSON pointer.
    served = {entry.location.object_id: src.open(entry.location).read() for entry in entries}
    by_content = {content_id(data): data for data in served.values()}
    for row in catalog.of("structured_record"):
        assert row.provenance.evidence.source in by_content
        document: Any = parse_json(by_content[row.provenance.evidence.source])
        pointer = row.provenance.evidence.locator[-1]
        assert isinstance(pointer, JsonPointer)
        assert pointer.pointer.startswith("/items/")
        index = int(pointer.pointer.split("/")[2])
        assert isinstance(document, Mapping)
        assert document["items"][index]


def test_a_cell_is_the_value_as_stated_and_a_blank_is_unknown() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        src = source(endpoint)
        catalog = src.catalog()
    tables = {t.name.value: t for t in catalog.of("structured_table")}
    events = tables["deploy_formant events"]
    header = events.header.value
    rows = [r for r in catalog.of("structured_record") if r.table == events.id]
    severity = {r.cells[header.index("id")].value: r.cells[header.index("severity")] for r in rows}
    assert severity["ev-0001"].value == "critical"
    assert isinstance(severity["ev-0002"], Unknown)  # the API said null: a blank is not a fact
    tags = {r.cells[header.index("id")].value: r.cells[header.index("tags")] for r in rows}
    assert tags["ev-0001"].value == '{"zone":"aisle-14"}'
    assert tags["ev-0002"].value == "{}"  # an empty object, as stated
    annotations = tables["deploy_formant annotations"]
    arows = [r for r in catalog.of("structured_record") if r.table == annotations.id]
    message = annotations.header.value.index("message")
    assert any(isinstance(r.cells[message], Unknown) for r in arows)  # "" is Unknown


def test_integer_times_are_on_a_named_clock_whose_meaning_is_unknown() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        catalog = source(endpoint).catalog()
        declared = source(endpoint, clock={"epoch": "unix", "resolution": "1/1000"}).catalog()
    (domain,) = (d for d in catalog.of("timestamp_domain") if d.scope == ("events",))
    assert domain.field == "startTime"
    assert isinstance(domain.epoch, Unknown) and isinstance(domain.resolution, Unknown)
    assert isinstance(domain.role, Unknown) and isinstance(domain.timescale, Unknown)
    (known,) = (d for d in declared.of("timestamp_domain") if d.scope == ("events",))
    assert isinstance(known.epoch, Known) and known.epoch.value == "unix"
    assert domain.id != known.id  # the declaration is part of the transform, so of every id


def test_interventions_become_intervention_records_exactly_as_stated() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        src = source(endpoint)
        catalog = src.catalog()
    records: dict[str, Any] = {_ir(r): r for r in catalog.of("intervention")}
    assert set(records) == {"ir-0001", "ir-0002", "ir-0003"}  # the request with no id builds none
    first = records["ir-0001"]
    assert first.kind == Intervention.kind
    assert first.provenance.assertion_kind is AssertionKind.STATED
    assert first.mode.value == "teleop"
    assert first.authority.value == "remote operator"
    assert first.reason.value == "AMR-07 stuck at dock door"
    assert [c.value for c in first.commands.value] == ["clear_obstacle", "resume"]
    assert first.outcome.value == "resolved"
    assert [m.value.value for m in first.machines.value] == ["dev-amr-07"]
    # Not in the API's answer: not covered, never a guess. Nothing is linked to anything else.
    assert isinstance(first.site, NotCovered) and isinstance(first.configuration, NotCovered)
    assert first.related == Known(())
    # Times are on a clock the text names: an offset says an instant, nothing else is added.
    assert isinstance(first.start, Known) and isinstance(first.end, Known)
    assert first.end.value.ticks - first.start.value.ticks == (8 * 60 + 30) * 1000
    # Each field is its own clock record, as each is a field of its own: the two are never merged
    # into one clock here, so nothing assumes that `time` and `endTime` tick together.
    assert first.start.value.domain_id != first.end.value.domain_id
    # Every value cites its own key in the document.
    for name, key in (("mode", "interventionType"), ("reason", "message"), ("start", "time")):
        state = getattr(first, name)
        pointer = state.provenance.evidence.locator[-1]
        assert pointer.pointer.endswith("/" + key)
    clocks = {d.id: d for d in catalog.of("timestamp_domain") if d.scope == ("interventions",)}
    assert first.start.value.domain_id in clocks
    # A second with a different resolution (whole seconds) has its own clock, never converted.
    second = records["ir-0002"]
    assert isinstance(second.start, Known)
    assert second.start.value.domain_id != first.start.value.domain_id
    assert clocks[second.start.value.domain_id].resolution.value == Fraction(1)
    assert clocks[first.start.value.domain_id].resolution.value == Fraction(1, 1000)
    assert second.commands == Known(())  # an empty list: the request states none


def test_a_time_no_declared_format_reads_is_unknown_with_a_finding() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        src = source(endpoint)
        catalog = src.catalog()
    third = next(r for r in catalog.of("intervention") if _ir(r) == "ir-0003")
    assert isinstance(third.start, Unknown)
    assert isinstance(third.end, NotCovered | Unknown)
    found = codes(src)
    assert [f.details["field"] for f in found["value_unreadable"]] == ["time"]
    assert found["record_skipped"][0].details["count"] == 1
    # The text is still in the table, as stated.
    tables = {t.name.value: t for t in catalog.of("structured_table")}
    table = tables["deploy_formant interventions"]
    column = table.header.value.index("time")
    rows = [r for r in catalog.of("structured_record") if r.table == table.id]
    assert "the second shift" in {
        r.cells[column].value for r in rows if isinstance(r.cells[column], Known)
    }


def test_a_declared_time_format_is_what_reads_the_text() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        src = source(endpoint, time_formats=["%Y-%m-%dT%H:%M:%SZ%z"])
        catalog = src.catalog()
    # The declared format needs an offset after the Z: none of the fixture's times fit it.
    assert all(isinstance(r.start, Unknown) for r in catalog.of("intervention"))
    assert codes(src)["value_unreadable"]


def test_recordings_are_referenced_never_fetched() -> None:
    store = FakeFormant.standard()
    with serve(store) as endpoint:
        src = source(endpoint)
        catalog = src.catalog()
    assert any(r.provenance for r in catalog.of("structured_table"))
    found = codes(src)
    assert found["recording_not_fetched"][0].details["records"] == 2
    assert {r.path.split("/")[3] for r in store.requests} == {
        "devices",
        "events",
        "annotations",
        "intervention-requests",
        "files",
    }  # file records only: no download request exists to send


def test_the_source_only_ever_sends_the_five_queries_with_the_token_to_one_host() -> None:
    store = FakeFormant.standard()
    with serve(store) as endpoint:
        source(endpoint).catalog()
    assert {r.method for r in store.requests} == {"POST"}
    assert all(r.path.endswith("/query") for r in store.requests)
    assert all(r.headers["Authorization"] == "Bearer test-token-123" for r in store.requests)
    assert all(
        r.body is not None and r.body["organizationId"] == "org-acme" for r in store.requests
    )


def test_pages_are_followed_and_the_filter_is_the_declared_one() -> None:
    store = FakeFormant.standard()
    with serve(store) as endpoint:
        source(
            endpoint, page_size=1, device_ids=["dev-amr-07"], **{"from": "2026-03-01T00:00:00Z"}
        ).catalog()
    queries = [r for r in store.requests if "intervention-requests" in r.path]
    bodies: list[dict[str, Any]] = [q.body or {} for q in queries]
    assert [b.get("continuationToken") for b in bodies] == [None, "page-2"]
    assert bodies[0]["limit"] == 1
    assert bodies[0]["deviceIds"] == ["dev-amr-07"]
    assert bodies[0]["from"] == "2026-03-01T00:00:00Z"
    devices = next(r for r in store.requests if "devices" in r.path).body or {}
    assert devices["ids"] == ["dev-amr-07"] and "from" not in devices


def test_identity_is_instance_organisation_part_and_a_content_token() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        entries = list(source(endpoint).walk())
    first = entries[0].location
    assert isinstance(first, ExternalObjectRef)
    assert first.connector_id == "deploy_formant"
    assert first.object_id == "@acme-test/org-acme/annotations"
    assert first.revision_token.startswith("records:")


def test_the_same_data_is_the_same_bytes_whatever_the_paging() -> None:
    results = []
    for size in (1, 2, 100):
        store = FakeFormant.standard()
        with serve(store) as endpoint:
            src = source(endpoint, page_size=size)
            results.append(
                (
                    [(e.location, src.open(e.location).read()) for e in src.walk()],
                    [r.to_json() for r in src.catalog().records],
                    [f.id for f in src.findings()],
                )
            )
    # Page size is not part of what was read, so no object, record, finding or id depends on it.
    assert results[0] == results[1] == results[2]


def test_two_runs_are_byte_identical() -> None:
    runs = []
    for _ in range(2):
        with serve(FakeFormant.standard()) as endpoint:
            src = source(endpoint)
            runs.append([json.dumps(r.to_json(), sort_keys=True) for r in src.catalog().records])
    assert runs[0] == runs[1]


def test_every_record_round_trips_the_compilers_strict_readers() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        records = source(endpoint).catalog().records
    for record in records:
        reader = RECORD_KINDS[record.kind][1]
        assert reader(record.to_json()) == record


def test_a_local_only_workspace_refuses_it_before_anything_is_built() -> None:
    with pytest.raises(LocalOnlyError):
        formant_source("formant://org-acme", network=Offline(), options={}, credentials=CREDENTIALS)


def test_a_workspace_switched_to_local_only_refuses_the_next_request() -> None:
    store = FakeFormant.standard()

    class Switch:
        calls = 0

        def require_network(self, purpose: str) -> None:
            self.calls += 1
            if self.calls > 1:
                raise LocalOnlyError(purpose)

    with serve(store) as endpoint, pytest.raises(LocalOnlyError):
        source(endpoint, network=Switch()).catalog()
    assert store.requests == []


def test_an_unusable_part_is_a_finding_and_the_rest_is_kept() -> None:
    store = FakeFormant.standard()
    store.status["events"] = 500
    store.status["annotations"] = 302
    store.raw["devices"] = b'{"items": [}'
    with serve(store) as endpoint:
        src = source(endpoint)
        names = [e.part for e in src.walk()]
    assert names == ["interventions", "recordings"]
    found = codes(src)
    assert {f.details["part"] for f in found["part_failed"]} == {"events", "annotations"}
    assert {f.details["cause"] for f in found["part_failed"]} == {"http_status", "redirect_refused"}
    assert found["part_invalid"][0].details["part"] == "devices"
    # No finding names the server, a URL, a token or error text.
    blob = json.dumps([f.to_json() for f in src.findings()])
    assert "127.0.0.1" not in blob and "test-token" not in blob


def test_a_continuation_loop_and_the_record_limit_stop_the_part_where_it_stands() -> None:
    store = FakeFormant.standard()
    store.loop.add("interventions")
    with serve(store) as endpoint:
        looped = source(endpoint)
        looped.catalog()
        limited = source(endpoint, max_records=1)
        limited.catalog()
    loop = codes(looped)["part_failed"][0]
    assert (loop.details["cause"], loop.details["part"]) == ("pagination_loop", "interventions")
    assert loop.details["records"] == 4  # what was read before the loop is kept
    limit = next(f for f in codes(limited)["part_limit"] if f.details["part"] == "interventions")
    assert limit.details["cause"] == "record_limit" and limit.details["records"] == 1


def test_a_trickling_server_cannot_hold_a_request_past_its_timeout() -> None:
    store = FakeFormant.standard()
    store.trickle.add("devices")
    with serve(store) as endpoint:
        src = source(
            endpoint,
            timeout=0.5,
            events=False,
            annotations=False,
            interventions=False,
            recordings=False,
        )
        assert list(src.walk()) == []
    assert codes(src)["part_failed"][0].details["cause"] == "deadline_exceeded"


def test_a_wrong_token_is_a_finding_without_the_token() -> None:
    store = FakeFormant.standard()
    store.token = "another-token"
    with serve(store) as endpoint:
        src = source(endpoint)
        assert list(src.walk()) == []
    found = codes(src)["part_failed"]
    assert {f.details["status"] for f in found} == {401}
    assert "test-token" not in json.dumps([f.to_json() for f in src.findings()])


def test_a_document_is_served_as_listed_and_another_revision_is_refused() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        src = source(endpoint)
        entry = next(iter(src.walk()))
        assert src.open(entry.location).read().startswith(b'{"items":[')
        stale = ExternalObjectRef(
            entry.location.connector_id, entry.location.object_id, "records:" + "0" * 64
        )
        with pytest.raises(DocumentReadError) as changed:
            src.open(stale)
        gone = ExternalObjectRef("deploy_formant", "@acme-test/org-acme/nothing", "records:x")
        with pytest.raises(DocumentReadError) as missing:
            src.open(gone)
    assert (changed.value.code, missing.value.code) == ("object_changed", "object_gone")


def test_options_are_closed_credentials_are_never_ambient_and_urls_are_checked() -> None:
    for bad in (
        {"endpoint": "http://example.com", "instance": "@x"},
        {"endpoint": "http://127.0.0.1:1"},
    ):
        with pytest.raises(FleetOpsConfigError):
            formant_source(
                "formant://org-acme", network=Online(), options=bad, credentials=CREDENTIALS
            )
    for options in (
        {"unknown": 1},
        {"page_size": 0},
        {"time_formats": ["%Q"]},
        {"clock": {"x": 1}},
    ):
        with pytest.raises(FleetOpsConfigError):
            formant_source(
                "formant://org-acme",
                network=Online(),
                options={"instance": "@x", **options},
                credentials=CREDENTIALS,
            )
    for url in ("formant://", "formant://a/b", "https://api.formant.io", "formant://../x"):
        with pytest.raises(FleetOpsConfigError):
            formant_source(url, network=Online(), credentials=CREDENTIALS)
    with pytest.raises(FleetOpsConfigError):
        formant_source("formant://org-acme", network=Online(), environ={"FORMANT_API_KEY": "x"})
    with pytest.raises(FleetOpsConfigError):
        formant_source(
            "formant://org-acme", network=Online(), credentials={"formant_access_token": "a b"}
        )
    with pytest.raises(FleetOpsConfigError):
        formant_source(
            "formant://org-acme", network=Online(), credentials={"other": "x"}, environ={}
        )
    ok = formant_source(
        "formant://org-acme",
        network=Online(),
        environ={"NEPTUNE_FORMANT_ACCESS_TOKEN": "env-token"},
    )
    assert "env-token" not in repr(ok) and "env-token" not in repr(ok.api)


def test_the_transform_holds_what_decided_the_records_and_no_secret_or_endpoint() -> None:
    with serve(FakeFormant.standard()) as endpoint:
        src = source(endpoint, device_ids=["dev-amr-07"])
        config = json.dumps(src.transform.config)
        assert endpoint not in config and "test-token" not in config
    assert "dev-amr-07" in config and "org-acme" in config


def test_fixture_is_recorded_shape_with_a_spread_of_embodiments() -> None:
    roles = {d["tags"]["role"] for d in fixture("devices")["items"]}
    assert roles == {"amr", "manipulator", "legged"}


def test_a_page_that_breaks_keeps_what_was_read_before_it() -> None:
    store = FakeFormant.standard()
    store.bad_page["interventions"] = 2
    with serve(store) as endpoint:
        src = source(endpoint)
        catalog = src.catalog()
    assert {_ir(r) for r in catalog.of("intervention")} == {
        "ir-0001",
        "ir-0002",
    }
    (invalid,) = codes(src)["part_invalid"]
    assert (invalid.details["part"], invalid.details["records"]) == ("interventions", 2)
    assert "kept" in invalid.message  # the finding says what is and is not covered


def test_a_command_that_is_not_text_is_cited_where_it_is() -> None:
    store = FakeFormant.standard()
    store.pages["interventions"] = [{"items": [{"id": "ir-9", "commands": ["stop", 5, "go"]}]}]
    with serve(store) as endpoint:
        src = source(endpoint)
        catalog = src.catalog()
    (record,) = catalog.of("intervention")
    assert [c.value for c in record.commands.value] == ["stop", "go"]
    (finding,) = codes(src)["value_unreadable"]
    assert finding.subject.locator[-1] == JsonPointer("/items/0/commands/1")
    assert finding.details["field"] == "commands/1"
    document = next(d for d in catalog.documents if d.ref.object_id.endswith("/interventions"))
    assert finding.subject.source == document.content_id


def test_a_read_error_can_be_copied_and_raised_through_a_context_manager() -> None:
    import contextlib
    import copy
    import pickle

    location = ExternalObjectRef("deploy_formant", "@x/o/events", "records:y")
    error = DocumentReadError("object_gone", location)
    for clone in (copy.copy(error), pickle.loads(pickle.dumps(error))):
        assert isinstance(clone, DocumentReadError) and clone.code == "object_gone"

    @contextlib.contextmanager
    def guarded() -> Any:
        yield

    with pytest.raises(DocumentReadError), guarded():
        raise error
