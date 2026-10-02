"""The declared REST (CMMS) connector against an in-process API described by a profile."""

import copy
import csv
import io
from pathlib import Path
from typing import Any

import pytest

from deploy_records_fake import FakeServer, RestBackend
from deploy_records_support import REST_CREDENTIALS, cmms_profile, codes, fingerprint, online, rest
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import RecordConfigError, rest_source

WO1, WO2, WO3 = "record/501", "record/502", "record/503"
ATT = "record/501/attachment/71"


def test_work_orders_become_csv_sources_with_the_columns_a_mapping_reads(tmp_path: Path) -> None:
    with rest(FakeServer(RestBackend()), tmp_path) as source:
        listing = source.listing()
        assert [e.id for e in listing.entries] == [WO1, ATT, WO2, WO3]
        assert listing.entries[0].location == ExternalObjectRef(
            "deploy_rest", "@site-a/cmms-example/record/501", "updated:2026-06-20T14:05:00Z"
        )
        assert listing.entries[0].name == "WO-501.csv"
        with source.open(listing.entries[0].location) as stream:
            header, row = list(csv.reader(io.StringIO(stream.read().decode())))
        assert header == [
            "WO Number", "WO Type", "Asset ID", "Site", "Completed", "Problem", "Work Performed",
        ]  # fmt: skip
        assert row[:3] == ["WO-501", "PM", "ARM-02"]
        assert source.findings() == ()


def test_the_attachment_is_a_separate_source_with_a_declared_parent(tmp_path: Path) -> None:
    with rest(FakeServer(RestBackend()), tmp_path) as source:
        [relation] = source.relations()
        assert relation.child.object_id.endswith(ATT)
        assert relation.parent.object_id.endswith(WO1)
        entry = next(e for e in source.listing().entries if e.id == ATT)
        with source.open(entry.location) as stream:
            assert stream.read() == b"joint,torque\n3,41.2\n"


def test_the_api_key_goes_in_the_declared_header_and_the_query_is_declared(tmp_path: Path) -> None:
    server = FakeServer(RestBackend())
    with rest(server, tmp_path) as source:
        source.listing()
    first = server.log[0]
    assert first.headers["session-token"] == "cmms-session-token-never-printed"
    assert "authorization" not in first.headers
    assert first.query["status"] == "all" and first.query["limit"] == "1000"


def test_edit_between_syncs_and_incremental_resume_with_a_since_parameter(tmp_path: Path) -> None:
    backend = RestBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with rest(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    assert cursor == "deploy_rest/1:2026-06-22T10:05:00Z"
    backend.edit(502, "2026-06-25T08:00:00Z", problem="Left drive wheel noise and vibration")
    backend.delete(503)
    with rest(server, tmp_path, since=cursor, ledger=ledger) as source:
        listing = source.listing()
        assert listing.mode == "incremental"
        assert [e.id for e in listing.entries] == [WO2]
        discovery = source.discover(ledger)
        assert [e.id for e in discovery.changed] == [WO2]
        assert discovery.gone == ()  # an incremental listing asserts no absence: no deletion feed
        assert server.requests("/work-orders")[-1].query["updatedAfter"] == "2026-06-22T10:05:00Z"
        assert source.cursor == "deploy_rest/1:2026-06-25T08:00:00Z"


def test_a_snapshot_finds_the_deleted_work_order(tmp_path: Path) -> None:
    backend = RestBackend()
    ledger = SourceLedger()
    with rest(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.delete(503)
    with rest(FakeServer(backend), tmp_path) as source:
        assert [g.location.key[2].endswith(WO3) for g in source.discover(ledger).gone] == [True]


def test_cursor_pages_of_any_size_give_the_same_listing(tmp_path: Path) -> None:
    seen = set()
    for size in (1, 2, 3, 1000):
        with rest(FakeServer(RestBackend()), tmp_path, page_size=size) as source:
            seen.add(tuple(e.location for e in source.listing().entries))
    assert len(seen) == 1


def _profile(**changes: Any) -> dict[str, Any]:
    profile = copy.deepcopy(cmms_profile())
    profile.update(changes)
    return profile


@pytest.mark.parametrize("style", ["offset", "page"])
def test_offset_and_page_styles_stop_on_an_empty_page(tmp_path: Path, style: str) -> None:
    class Paged(RestBackend):
        def handle(self, request):  # type: ignore[no-untyped-def]
            if request.path != "/api/v1/work-orders":
                return super().handle(request)
            size = int(request.query["limit"])
            at = (
                int(request.query.get("offset", 0))
                if style == "offset"
                else (int(request.query["page"]) - 1) * size
            )
            from deploy_records_fake import reply_json

            rows = [dict(o, attachments=[]) for o in self.orders[at : at + size]]
            return reply_json({"data": rows})

    paging = (
        {"style": "offset", "limit_param": "limit", "offset_param": "offset"}
        if style == "offset"
        else {"style": "page", "limit_param": "limit", "page_param": "page"}
    )
    server = FakeServer(Paged())
    with server.serve() as host:
        source = rest_source(
            f"rest://{host}",
            network=online(tmp_path),
            options={
                "scheme": "http",
                "instance": "site-a",
                "page_size": 2,
                "profile": _profile(paging=paging),
            },
            credentials=REST_CREDENTIALS,
        )
        assert [e.id for e in source.listing().entries] == [WO1, WO2, WO3]
        assert len(server.requests("/work-orders")) == 3  # 2 + 1 + the empty page


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(extra=1),
        lambda p: p["records"].update(path="api/v1"),
        lambda p: p["records"].update(path="/a b"),
        lambda p: p["records"].update(id="id"),
        lambda p: p["records"]["revision"].update(kind="clock"),
        lambda p: p["paging"].update(style="stream"),
        lambda p: p.update(auth={"header": "Host"}),
        lambda p: p.update(auth={"header": "Bad Header"}),
        lambda p: p["snapshot"].update(format="xml"),
        lambda p: p["attachments"].update(path="/no/placeholders"),
        lambda p: p.update(schema="neptune-deploy.record-profile/2"),
        lambda p: p.update(query={"k": "line\nbreak"}),
    ],
)
def test_a_profile_that_is_not_closed_and_checked_is_refused_before_any_request(
    tmp_path: Path, mutate: Any
) -> None:
    profile = copy.deepcopy(cmms_profile())
    mutate(profile)
    with pytest.raises(RecordConfigError):
        rest_source(
            "rest://example.com",
            network=online(tmp_path),
            options={"profile": profile},
            credentials=REST_CREDENTIALS,
        )


def test_codes_are_quiet_on_a_clean_run(tmp_path: Path) -> None:
    with rest(FakeServer(RestBackend()), tmp_path) as source:
        source.listing()
        assert codes(source) == []
