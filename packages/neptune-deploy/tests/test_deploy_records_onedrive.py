"""The OneDrive and SharePoint connector against an in-process Graph drive: the delta feed, the
revision tokens, and the one redirect it follows (ADR 0008)."""

from pathlib import Path

import pytest

from deploy_records_fake import FakeServer, Reply, reply_json
from deploy_records_fake_graph import GraphBackend
from deploy_records_support import codes, fingerprint, onedrive
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune_deploy.sources.records import ObjectReadError

SCOPE = "@site-a/b!siteA_lib01/"


def test_files_are_sources_keyed_by_item_id_and_content_tag(tmp_path: Path) -> None:
    with onedrive(FakeServer(GraphBackend()), tmp_path) as source:
        listing = source.listing()
        assert listing.complete and listing.mode == "snapshot"
        assert [e.id for e in listing.entries] == [
            "item/01REQUAL000000001",
            "item/01RISKXLS00000001",
            "item/01SOPDOCK00000001",
        ]  # folders and the shortcut are not documents
        assert listing.skipped == ()
        assert codes(source) == []
        assert listing.entries[0].location == ExternalObjectRef(
            "deploy_onedrive",
            SCOPE + "item/01REQUAL000000001",
            'ctag:"c:{6D0B1E4F-3A5B-4C41-9F70-0F3E2D1A9B01},3"',
        )
        assert listing.entries[2].name == "SOP dock charging.pdf"
        with source.open(listing.entries[0].location) as stream:
            assert stream.read() == b"%PDF-1.4 requalification report AMR fleet 3"


def test_the_download_follows_the_redirect_once_and_sends_no_credential(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        source.open(entry.location).close()
        content = server.requests("/items/")
        assert [r.headers["authorization"] for r in content] == [
            "Bearer onedrive-token-never-printed"
        ]
        assert len(backend.downloads) == 1
        hop = backend.downloads[0]
        assert "authorization" not in hop.headers and hop.query["tempauth"] == "a+b=="


def test_a_file_edited_in_place_is_a_new_revision_and_the_old_one_is_kept(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with onedrive(server, tmp_path) as source:
        first = fingerprint(source, ledger, source.walk())
    backend.edit("01SOPDOCK00000001", b"%PDF-1.4 SOP dock charging v8")
    with onedrive(server, tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.id for e in discovery.changed] == ["item/01SOPDOCK00000001"]
        assert discovery.changed[0].location.revision_token.endswith(',8"')
        second = fingerprint(source, ledger, source.walk())
    assert list(second) == ["item/01SOPDOCK00000001"]
    assert second["item/01SOPDOCK00000001"].content_id != first["item/01SOPDOCK00000001"].content_id
    key = SCOPE + "item/01SOPDOCK00000001"
    chain = sorted(
        (r for r in ledger.revisions() if r.location.key[2] == key),
        key=lambda r: len(r.supersedes),
    )
    assert len(chain) == 2 and chain[1].supersedes == (chain[0].id,)


def test_a_rename_moves_the_etag_not_the_content_tag_so_it_is_no_new_revision(
    tmp_path: Path,
) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with onedrive(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    backend.rename("01SOPDOCK00000001", "SOP dock charging (final).pdf")
    with onedrive(server, tmp_path, ledger=ledger, since=cursor) as source:
        discovery = source.discover(ledger)
        assert discovery.changed == () and discovery.new == ()
        assert [e.id for e, _ in discovery.unchanged] == ["item/01SOPDOCK00000001"]


def test_the_delta_token_of_a_snapshot_resumes_with_only_what_changed(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    with onedrive(server, tmp_path) as source:
        source.listing()
        cursor = source.cursor
        assert cursor == "deploy_onedrive/1:t0"
    backend.edit("01REQUAL000000001", b"%PDF-1.4 requalification report AMR fleet 3 rev B")
    backend.add("01CALIB000000001", "calibration.json", b'{"k": 1}')
    backend.delete("01RISKXLS00000001")
    with onedrive(server, tmp_path, since=cursor) as source:
        listing = source.listing()
        assert listing.mode == "incremental" and listing.complete
        assert [e.id for e in listing.entries] == [
            "item/01CALIB000000001",
            "item/01REQUAL000000001",
        ]
        assert listing.removed == ("item/01RISKXLS00000001",)
        assert source.cursor == "deploy_onedrive/1:t3"


def test_a_deleted_file_is_gone_in_the_ledger(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    ledger = SourceLedger()
    with onedrive(server, tmp_path) as source:
        fingerprint(source, ledger, source.walk())
        cursor = source.cursor
    backend.delete("01RISKXLS00000001")
    with onedrive(server, tmp_path, since=cursor, ledger=ledger) as source:
        gone = source.discover(ledger).gone
        assert [g.location.key[2] for g in gone] == [SCOPE + "item/01RISKXLS00000001"]


@pytest.mark.parametrize("param", ["$skiptoken", "token"])
@pytest.mark.parametrize("size", [1, 2, 200])
def test_pages_of_any_size_and_either_link_style_give_the_same_listing(
    tmp_path: Path, size: int, param: str
) -> None:
    backend = GraphBackend()
    backend.link_param = param
    with onedrive(FakeServer(backend), tmp_path, page_size=size) as source:
        got = [(e.id, e.location.revision_token) for e in source.listing().entries]
        assert source.cursor == "deploy_onedrive/1:t0"
    with onedrive(FakeServer(GraphBackend()), tmp_path) as source:
        assert got == [(e.id, e.location.revision_token) for e in source.listing().entries]


def test_a_next_page_is_built_from_the_token_never_from_the_link(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    with onedrive(server, tmp_path, page_size=2) as source:
        source.listing()
    deltas = server.requests("/root/delta")
    assert len(deltas) >= 3 and {r.path for r in server.log if "/dl/" not in r.path} <= {
        r.path for r in deltas
    }  # no request went to a path a link named, only the delta path with a token


def test_an_item_stated_twice_by_the_feed_is_the_later_statement(tmp_path: Path) -> None:
    backend = GraphBackend()
    backend.stale["01REQUAL000000001"] = {
        **backend._meta(backend.items["01REQUAL000000001"]),
        "cTag": '"c:{6D0B1E4F-3A5B-4C41-9F70-0F3E2D1A9B01},2"',
        "size": 3,
        "file": {"mimeType": "application/pdf", "hashes": {}},
    }
    with onedrive(FakeServer(backend), tmp_path, page_size=1) as source:
        entries = {e.id: e for e in source.listing().entries}
        assert entries["item/01REQUAL000000001"].location.revision_token.endswith(',3"')
        assert "deploy_onedrive.record_duplicated" not in codes(source)


def test_a_file_with_no_content_tag_falls_back_to_the_etag(tmp_path: Path) -> None:
    backend = GraphBackend()
    backend.items["01SOPDOCK00000001"]["ctag"] = None
    with onedrive(FakeServer(backend), tmp_path) as source:
        tokens = {e.id: e.location.revision_token for e in source.listing().entries}
    assert tokens["item/01SOPDOCK00000001"].startswith('etag:"{6D0B')
    assert tokens["item/01REQUAL000000001"].startswith("ctag:")


def test_hashes_the_file_states_are_checked_and_a_mismatch_is_object_changed(
    tmp_path: Path,
) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        backend.items["01REQUAL000000001"]["content"] = (
            b"%PDF-1.4 requalification report AMR fleeX 3"
        )
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location)
        assert str(raised.value).endswith("object_changed")
        assert "deploy_onedrive.object_changed" in codes(source)


def test_a_file_that_states_no_cryptographic_hash_is_checked_for_size_only(tmp_path: Path) -> None:
    backend = GraphBackend()
    backend.hide_hashes = True
    with onedrive(FakeServer(backend), tmp_path) as source:
        entry = source.listing().entries[0]
        assert source.open(entry.location).read().startswith(b"%PDF")


def test_the_hosts_a_download_may_redirect_to_default_to_microsofts(tmp_path: Path) -> None:
    from neptune_deploy.sources.records.systems.onedrive import DEFAULT_DOWNLOAD_HOSTS

    assert "sharepoint.com" in DEFAULT_DOWNLOAD_HOSTS and all(
        "." in host for host in DEFAULT_DOWNLOAD_HOSTS
    )


def test_a_list_page_that_names_no_continuation_is_invalid(tmp_path: Path) -> None:
    server = FakeServer(GraphBackend())
    server.inject(lambda r: "/root/delta" in r.path, reply_json({"value": []}))
    with onedrive(server, tmp_path) as source:
        assert source.listing().complete is False
        assert codes(source) == ["deploy_onedrive.response_invalid"]


def test_a_page_with_a_next_link_and_a_delta_link_is_invalid(tmp_path: Path) -> None:
    server = FakeServer(GraphBackend())
    both = {
        "value": [],
        "@odata.nextLink": "http://x/v1.0/drives/d/root/delta?$skiptoken=s1",
        "@odata.deltaLink": "http://x/v1.0/drives/d/root/delta?token=t1",
    }
    server.inject(lambda r: "/root/delta" in r.path, reply_json(both))
    with onedrive(server, tmp_path) as source:
        assert source.listing().complete is False
        assert codes(source) == ["deploy_onedrive.response_invalid"]


def test_unusable_items_are_findings_and_the_rest_are_still_listed(tmp_path: Path) -> None:
    backend = GraphBackend()
    good = backend._meta(backend.items["01SOPDOCK00000001"])
    page = {
        "value": [
            {**good, "id": "../etc/passwd"},
            {**good, "id": "01NOSIZE0000000001", "size": "many"},
            {**good, "id": "01NOTAG00000000001", "cTag": None, "eTag": None},
            {**good, "id": "01BADHASH00000001", "file": {"hashes": {"sha1Hash": "zz"}}},
            good,
        ],
        "@odata.deltaLink": "http://x/v1.0/drives/d/root/delta?token=t9",
    }
    server = FakeServer(backend)
    server.inject(lambda r: "/root/delta" in r.path, reply_json(page))
    with onedrive(server, tmp_path) as source:
        listing = source.listing()
        assert [e.id for e in listing.entries] == ["item/01SOPDOCK00000001"]
        assert codes(source) == [
            "deploy_onedrive.id_invalid",
            "deploy_onedrive.record_invalid",
            "deploy_onedrive.size_invalid",
        ]


# --- The redirect is the system's statement: checked, never trusted --------------------------


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/dl/x?tempauth=1",  # not an allowed host
        "https://127.0.0.1.evil.example/dl/x?tempauth=1",  # a suffix, not a prefix
        "http://169.254.169.254/latest/meta-data/",  # plain http to a non-loopback host
        "https://user:pass@127.0.0.1/dl/x",  # user information
        "http://127.0.0.1:1/dl/x#frag",  # a fragment
        "ftp://127.0.0.1/dl/x",
        "/relative/path",
        "http://127.0.0.1:9/dl/a b",  # a space
    ],
)
def test_a_redirect_to_anywhere_else_is_refused_and_nothing_is_sent_there(
    tmp_path: Path, target: str
) -> None:
    backend = GraphBackend()
    backend.redirect_to = target
    server = FakeServer(backend)
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location)
        assert str(raised.value).endswith("redirect_refused")
    assert backend.downloads == []
    assert "deploy_onedrive.redirect_refused" in codes(source)


def test_the_followed_download_may_not_redirect_again(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    server.inject(
        lambda r: r.path.startswith("/dl/"), Reply(302, b"", {"Location": "http://127.0.0.1:1/x"})
    )
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location)
        assert str(raised.value).endswith("redirect_refused")


def test_a_redirect_that_is_not_a_download_redirect_is_never_followed(tmp_path: Path) -> None:
    backend = GraphBackend()
    server = FakeServer(backend)
    server.inject(
        lambda r: r.path.endswith("/content"),
        Reply(301, b"", {"Location": "http://127.0.0.1:1/x"}),
    )
    with onedrive(server, tmp_path) as source:
        entry = source.listing().entries[0]
        with pytest.raises(ObjectReadError):
            source.open(entry.location)
    assert backend.downloads == []


def test_a_download_of_another_length_is_object_changed(tmp_path: Path) -> None:
    backend = GraphBackend()
    with onedrive(FakeServer(backend), tmp_path) as source:
        entry = source.listing().entries[0]
        backend.items["01REQUAL000000001"]["content"] += b"!"
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location)
        assert str(raised.value).endswith("object_changed")


def test_a_drive_that_is_not_a_graph_drive_id_is_refused(tmp_path: Path) -> None:
    from deploy_records_support import ONEDRIVE_CREDENTIALS, online
    from neptune_deploy.sources.records import RecordConfigError, onedrive_source

    for url in ("onedrive://b!ok/extra", "onedrive://bad%20id", "onedrive://", "onedrive://a.b"):
        with pytest.raises(RecordConfigError):
            onedrive_source(url, network=online(tmp_path), credentials=ONEDRIVE_CREDENTIALS)


@pytest.mark.parametrize("hosts", [[], ["com"], ["Bad Host"], "sharepoint.com", [1]])
def test_download_hosts_are_domains_with_a_dot(tmp_path: Path, hosts: object) -> None:
    from deploy_records_support import ONEDRIVE_CREDENTIALS, online
    from neptune_deploy.sources.records import RecordConfigError, onedrive_source

    with pytest.raises(RecordConfigError):
        onedrive_source(
            "onedrive://b!ok",
            network=online(tmp_path),
            credentials=ONEDRIVE_CREDENTIALS,
            options={"download_hosts": hosts},  # type: ignore[dict-item]
        )


def test_the_listing_is_deterministic_across_sources(tmp_path: Path) -> None:
    results = []
    for size in (1, 3, 3):
        with onedrive(FakeServer(GraphBackend()), tmp_path, page_size=size) as source:
            results.append(
                (source.listing().entries, source.findings(), source.cursor, source.transform)
            )
    assert results[0][:3] == results[1][:3]  # whatever the page size
    assert results[1] == results[2]  # and the producer record is the same for the same config
