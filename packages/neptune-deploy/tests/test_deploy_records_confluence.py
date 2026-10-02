"""The Confluence connector against an in-process Confluence v2 API: pages as documents."""

from pathlib import Path

import pytest

from deploy_records_fake import ConfluenceBackend, FakeServer
from deploy_records_support import CONFLUENCE_CREDENTIALS, confluence, fingerprint, online
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import RecordConfigError, confluence_source


def test_current_pages_are_documents_keyed_by_page_id_and_version_number(tmp_path: Path) -> None:
    with confluence(FakeServer(ConfluenceBackend()), tmp_path) as source:
        listing = source.listing()
        assert [e.id for e in listing.entries] == ["page/9001", "page/9002", "page/9003"]
        assert listing.entries[0].location == ExternalObjectRef(
            "deploy_confluence", "@site-a/5001/page/9001", "version:4"
        )
        assert listing.entries[1].name == "Quadruped stair policy.xhtml"
        with source.open(listing.entries[1].location) as stream:
            assert stream.read().startswith(b"<p>Stairs are walked only in supervised mode.</p>")
        assert source.findings() == ()


def test_a_page_edited_in_place_is_a_new_revision_with_the_old_kept(tmp_path: Path) -> None:
    backend = ConfluenceBackend()
    ledger = SourceLedger()
    with confluence(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.edit(
        "9003", "<ul><li>Check thruster current</li><li>Confirm geofence and tide</li></ul>"
    )
    with confluence(FakeServer(backend), tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.id for e in discovery.changed] == ["page/9003"]
        assert [e.location.revision_token for e in discovery.changed] == ["version:10"]
        fingerprint(source, ledger, source.walk())
    chain = [r for r in ledger.revisions() if r.location.key[2].endswith("page/9003")]
    assert len(chain) == 2


def test_a_deleted_page_is_gone_from_a_complete_listing(tmp_path: Path) -> None:
    backend = ConfluenceBackend()
    ledger = SourceLedger()
    with confluence(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.pages = backend.pages[:2]
    with confluence(FakeServer(backend), tmp_path) as source:
        assert [g.location.key[2].rsplit("/", 1)[1] for g in source.discover(ledger).gone] == [
            "9003"
        ]


def test_the_next_request_is_built_from_the_cursor_never_from_the_link(tmp_path: Path) -> None:
    server = FakeServer(ConfluenceBackend())
    with confluence(server, tmp_path, page_size=1) as source:
        assert len(source.listing().entries) == 3
    assert len(server.requests("/wiki/api/v2/pages")) == 3
    assert [r.query.get("cursor") for r in server.log] == [None, "1", "2"]


def test_there_is_no_change_feed_so_a_cursor_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RecordConfigError, match="no change feed"):
        confluence_source(
            "confluence://example.atlassian.net/5001",
            network=online(tmp_path),
            options={"since": "deploy_confluence/1:x"},
            credentials=CONFLUENCE_CREDENTIALS,
        )
