"""The Jira connector against an in-process Jira: identity, revisions, feed, attachments."""

from pathlib import Path

from deploy_records_fake import FakeServer, JiraBackend, Reply
from deploy_records_support import codes, fingerprint, jira, snapshot_json
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef


def test_issues_and_attachments_are_separate_sources_with_revision_tokens(tmp_path: Path) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        listing = source.listing()
        assert listing.complete and listing.mode == "snapshot"
        ids = [entry.id for entry in listing.entries]
        assert ids == [
            "issue/10001",
            "issue/10001/attachment/20001",
            "issue/10001/attachment/20002",
            "issue/10001/attachment/20003",
            "issue/10002",
            "issue/10003",
            "issue/10004",
        ]
        first = listing.entries[0].location
        assert first == ExternalObjectRef(
            "deploy_jira",
            "@site-a/OPS/issue/10001",
            "updated:2026-08-03T11:40:00.000+0200",
        )
        assert source.findings() == ()


def test_an_issue_snapshot_is_the_shape_the_jira_preset_reads(tmp_path: Path) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        [record] = snapshot_json(source, "issue/10001")
        assert record["key"] == "OPS-1"
        assert record["fields"]["issuetype"] == {"name": "Incident"}
        assert set(record) == {"key", "fields"}
        assert snapshot_json(source, "issue/10003")[0]["fields"]["description"] is None


def test_a_ticket_with_three_attachments_keeps_each_as_a_source_with_its_parent(
    tmp_path: Path,
) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        children = [e for e in source.listing().entries if "/attachment/" in e.id]
        assert len(children) == 3
        parent = next(e for e in source.listing().entries if e.id == "issue/10001")
        assert all(c.parent == parent.location for c in children)
        relations = source.relations()
        assert [(r.child.object_id.rsplit("/", 1)[1], r.kind) for r in relations] == [
            ("20001", "attachment_of"),
            ("20002", "attachment_of"),
            ("20003", "attachment_of"),
        ]
        assert all(r.parent == parent.location for r in relations)
        with source.open(children[0].location) as stream:
            assert stream.read() == b"%PDF-1.4 incident report AMR-14 dock3"
        assert children[0].size == len(b"%PDF-1.4 incident report AMR-14 dock3")


def test_attachment_names_are_hints_and_never_paths(tmp_path: Path) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        names = {
            e.id.rsplit("/", 1)[1]: e.name
            for e in source.listing().entries
            if "/attachment/" in e.id
        }
        assert names["20003"] == "lidar.log"  # "../../etc/lidar.log" keeps its last component only


def test_attachments_are_fetched_by_id_from_the_declared_site_never_from_the_record_url(
    tmp_path: Path,
) -> None:
    backend = JiraBackend()
    server = FakeServer(backend)
    with jira(server, tmp_path) as source:
        for entry in source.listing().entries:
            with source.open(entry.location) as stream:
                stream.read()
        fetched = server.requests("/attachment/content/")
        assert {r.path for r in fetched} == {
            f"/rest/api/2/attachment/content/{n}" for n in ("20001", "20002", "20003")
        }
        assert all(r.query == {"redirect": "false"} for r in fetched)
        assert server.other_methods == []


def test_an_issue_updated_in_place_is_a_new_revision_and_the_old_one_is_kept(
    tmp_path: Path,
) -> None:
    backend = JiraBackend()
    ledger = SourceLedger()
    with jira(FakeServer(backend), tmp_path) as source:
        first = fingerprint(source, ledger, source.walk())
    backend.edit("OPS-3", "2026-08-13T09:00:00.000+0200", status={"name": "Resolved"})
    with jira(FakeServer(backend), tmp_path, ledger=ledger) as source:
        discovery = source.discover(ledger)
        assert [e.id for e in discovery.changed] == ["issue/10003"]
        assert discovery.new == () and discovery.gone == ()
        assert len(discovery.unchanged) == 6
        second = fingerprint(source, ledger, source.walk())
    assert list(second) == ["issue/10003"]
    assert second["issue/10003"].content_id != first["issue/10003"].content_id
    chain = [r for r in ledger.revisions() if r.location.key[2] == "@site-a/OPS/issue/10003"]
    assert len(chain) == 2  # old and new, linked
    assert chain[0].id in chain[1].supersedes or chain[1].id in chain[0].supersedes


def test_a_deleted_issue_is_gone_from_a_complete_snapshot_only(tmp_path: Path) -> None:
    backend = JiraBackend()
    ledger = SourceLedger()
    with jira(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.delete("OPS-4")
    with jira(FakeServer(backend), tmp_path) as source:
        gone = source.discover(ledger).gone
        assert [g.location.key[2] for g in gone] == ["@site-a/OPS/issue/10004"]
    broken = FakeServer(backend)
    broken.inject(lambda r: True, Reply(500, b"{}"), times=99)
    with jira(broken, tmp_path) as source:
        assert source.discover(ledger).gone == ()  # a failed listing asserts nothing
        assert "deploy_jira.listing_failed" in codes(source)


def test_a_removed_attachment_is_gone_when_its_issue_is_listed_again(tmp_path: Path) -> None:
    backend = JiraBackend()
    ledger = SourceLedger()
    with jira(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    backend.issues[0]["fields"]["attachment"] = backend.issues[0]["fields"]["attachment"][:2]
    backend.edit("OPS-1", "2026-08-04T10:00:00.000+0200")
    with jira(FakeServer(backend), tmp_path) as source:
        first = source.listing().entries[0]
        cursor = source.cursor
    assert first.id == "issue/10001"
    assert cursor == "deploy_jira/1:2026-08-20T12:50:00.000+0200"
    with jira(
        FakeServer(backend), tmp_path, since="deploy_jira/1:2026-08-04T10:00:00.000+0200"
    ) as source:
        assert source.listing().mode == "incremental"
        gone = source.discover(ledger).gone
        assert [g.location.key[2] for g in gone] == ["@site-a/OPS/issue/10001/attachment/20003"]


def test_the_change_feed_resumes_from_the_cursor_and_lists_only_what_changed(
    tmp_path: Path,
) -> None:
    backend = JiraBackend()
    server = FakeServer(backend)
    with jira(server, tmp_path) as source:
        cursor = source.cursor
        assert cursor == "deploy_jira/1:2026-08-20T12:50:00.000+0200"
    backend.edit("OPS-2", "2026-08-21T07:00:00.000+0200", summary="Re-teach pick frame, cell 2")
    with jira(server, tmp_path, since=cursor) as source:
        listing = source.listing()
        assert listing.mode == "incremental" and listing.complete
        ids = {e.id for e in listing.entries}
        assert "issue/10002" in ids and "issue/10001" not in ids
        assert source.cursor == "deploy_jira/1:2026-08-21T07:00:00.000+0200"
        jql = server.requests("/search/jql")[-1].query["jql"]
        assert 'updated >= "2026-08-19 12:50"' in jql  # widened by a day: JQL's zone is the user's


def test_pages_of_any_size_give_the_same_listing(tmp_path: Path) -> None:
    results = []
    for size in (1, 2, 3, 100):
        with jira(FakeServer(JiraBackend()), tmp_path, page_size=size) as source:
            results.append(tuple(e.location for e in source.listing().entries))
    assert len(set(results)) == 1
    assert len(results[0]) == 7


def test_the_listing_is_deterministic_across_sources(tmp_path: Path) -> None:
    runs = []
    for _ in range(2):
        with jira(FakeServer(JiraBackend()), tmp_path) as source:
            data = [source.open(e.location).read() for e in source.listing().entries]
            runs.append((source.listing(), data, source.findings()))
    assert runs[0] == runs[1]


def test_attachments_can_be_switched_off(tmp_path: Path) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path, attachments=False) as source:
        assert not any("/attachment/" in e.id for e in source.listing().entries)
        assert "attachment" not in snapshot_json(source, "issue/10001")[0]["fields"]
    assert codes(source) == []
