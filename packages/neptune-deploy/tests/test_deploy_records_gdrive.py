"""The Google Drive connector against an in-process Drive: revisions, change feed, checksums."""

import json
from pathlib import Path

import pytest

from deploy_records_fake import DriveBackend, FakeServer, Reply, Request, md5_of, reply_json
from deploy_records_support import codes, fingerprint, gdrive
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import ObjectReadError


def test_files_with_bytes_are_sources_keyed_by_file_id_and_version(tmp_path: Path) -> None:
    with gdrive(FakeServer(DriveBackend()), tmp_path) as source:
        listing = source.listing()
        assert listing.complete and listing.mode == "snapshot"
        assert [e.id for e in listing.entries] == [
            "file/file_requal_report",
            "file/file_risk_xlsx",
            "file/file_sop_dock",
        ]
        assert listing.entries[0].location == ExternalObjectRef(
            "deploy_gdrive", "@site-a/my-drive/file/file_requal_report", "version:3"
        )
        assert listing.entries[2].name == "SOP dock charging.pdf"
        with source.open(listing.entries[0].location) as stream:
            assert stream.read() == b"%PDF-1.4 requalification report arm cell 2"


def test_native_google_documents_are_a_finding_not_an_export_of_unknown_length(
    tmp_path: Path,
) -> None:
    with gdrive(FakeServer(DriveBackend()), tmp_path) as source:
        assert [s.raw_id for s in source.listing().skipped] == ["file/native_doc_1"]
        assert codes(source) == ["deploy_gdrive.type_unsupported"]


def test_a_document_revised_in_place_is_a_new_revision_and_the_old_one_is_kept(
    tmp_path: Path,
) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with gdrive(server, tmp_path) as source:
        first = fingerprint(source, ledger, source.walk())
    backend.edit("file_sop_dock", b"%PDF-1.4 SOP dock charging v8")
    with gdrive(server, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.id for e in discovery.changed] == ["file/file_sop_dock"]
        assert [e.location.revision_token for e in discovery.changed] == ["version:8"]
        second = fingerprint(source, ledger, source.walk())
    assert list(second) == ["file/file_sop_dock"]
    assert second["file/file_sop_dock"].content_id != first["file/file_sop_dock"].content_id
    key = "@site-a/my-drive/file/file_sop_dock"
    chain = sorted(
        (r for r in ledger.revisions() if r.location.key[2] == key),
        key=lambda r: len(r.supersedes),
    )
    assert len(chain) == 2 and chain[1].supersedes == (chain[0].id,)


def test_a_version_bump_over_identical_bytes_is_no_new_revision(tmp_path: Path) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with gdrive(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.touch("file_sop_dock")  # a share or a rename: version 8, same bytes
    with gdrive(server, tmp_path, ledger=ledger) as source:
        assert [e.id for e in source.discover(ledger).changed] == ["file/file_sop_dock"]
        before = len(ledger.revisions())
        fingerprint(source, ledger, source.walk())
    assert len(ledger.revisions()) == before  # observed, but the bytes are the same revision


def test_the_change_feed_resumes_from_the_start_token_taken_before_the_snapshot(
    tmp_path: Path,
) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    with gdrive(server, tmp_path) as source:
        source.listing()
        cursor = source.cursor
        assert cursor == "deploy_gdrive/1:0"
        order = [r.path for r in server.log]
        assert order.index("/drive/v3/changes/startPageToken") < order.index("/drive/v3/files")
    backend.edit("file_requal_report", b"%PDF-1.4 requalification report arm cell 2 rev B")
    backend.add("file_new_calibration", "calibration.json", b'{"k": 1}', "application/json")
    backend.trash("file_risk_xlsx")
    backend.remove("file_sop_dock")
    with gdrive(server, tmp_path, since=cursor) as source:
        listing = source.listing()
        assert listing.mode == "incremental" and listing.complete
        assert [e.id for e in listing.entries] == [
            "file/file_new_calibration",
            "file/file_requal_report",
        ]
        assert listing.removed == ("file/file_risk_xlsx", "file/file_sop_dock")
        assert source.cursor == "deploy_gdrive/1:4"


def test_removed_and_trashed_files_are_gone_in_the_ledger(tmp_path: Path) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with gdrive(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    backend.trash("file_risk_xlsx")
    backend.remove("file_sop_dock")
    with gdrive(server, tmp_path, since=cursor, ledger=ledger) as source:
        gone = source.discover(ledger).gone
        assert sorted(g.location.key[2].rsplit("/", 1)[1] for g in gone) == [
            "file_risk_xlsx",
            "file_sop_dock",
        ]


def test_change_pages_of_any_size_give_the_same_result(tmp_path: Path) -> None:
    results = set()
    for size in (1, 2, 1000):
        backend = DriveBackend()
        server = FakeServer(backend)
        with gdrive(server, tmp_path) as source:
            cursor = source.cursor
        for name in ("a_file", "b_file", "c_file"):
            backend.add(name, f"{name}.pdf", name.encode())
        with gdrive(server, tmp_path, since=cursor, page_size=size) as source:
            results.add((tuple(e.location for e in source.listing().entries), source.cursor))
    assert len(results) == 1


def test_a_download_is_checked_against_the_listed_checksum(tmp_path: Path) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    with gdrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        backend.files["file_requal_report"]["content"] = b"X" * entry.size  # same length, new bytes
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location).read()
        assert raised.value.code == "object_changed"
        assert "deploy_gdrive.object_changed" in codes(source)


def test_a_download_of_another_length_is_object_changed_never_accepted(tmp_path: Path) -> None:
    server = FakeServer(DriveBackend())
    with gdrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        server.inject(lambda r: r.query.get("alt") == "media", Reply(200, b"short"))
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location).read()
        assert raised.value.code == "object_changed"


def test_incomplete_search_is_partial_and_asserts_nothing_gone(tmp_path: Path) -> None:
    backend = DriveBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with gdrive(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    original = backend.handle

    def partial(request: Request) -> Reply | None:
        reply = original(request)
        if reply is not None and request.path == "/drive/v3/files":
            body = json.loads(reply.body)
            body["incompleteSearch"] = True
            body["files"] = body["files"][:1]
            return reply_json(body)
        return reply

    backend.handle = partial  # type: ignore[method-assign]
    with gdrive(server, tmp_path) as source:
        assert source.discover(ledger).gone == ()
        assert "deploy_gdrive.listing_partial" in codes(source)


def test_the_mime_type_option_limits_what_is_exported_without_findings(tmp_path: Path) -> None:
    with gdrive(FakeServer(DriveBackend()), tmp_path, mime_types=["application/pdf"]) as source:
        assert [e.id for e in source.listing().entries] == [
            "file/file_requal_report",
            "file/file_sop_dock",
        ]


def test_md5_of_helper_matches_the_listing(tmp_path: Path) -> None:
    assert md5_of(b"") == "d41d8cd98f00b204e9800998ecf8427e"
