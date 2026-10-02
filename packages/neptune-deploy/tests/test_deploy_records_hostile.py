"""A hostile record system: rate limits, partial and malformed pages, loops, hostile ids, limits.

Each attack fails one item or stops the listing with a finding; none escapes as a bare exception,
reaches an output, sends a request other than GET, or asserts a deletion it cannot know
(ADR 0008 §6-8).
"""

import json
from pathlib import Path
from typing import Any

import pytest

from deploy_records_fake import (
    FakeServer,
    JiraBackend,
    Reply,
    Request,
    ServiceNowBackend,
    reply_json,
)
from deploy_records_support import SECRETS, codes, fingerprint, jira, servicenow
from neptune.identity.revisions import SourceLedger
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.sources.records import ObjectReadError, RecordSource, jira_source

SEARCH = "/rest/api/2/search/jql"


def search(request: Request) -> bool:
    return request.path == SEARCH


def issue(
    number: int, updated: str = "2026-09-01T10:00:00.000+0200", **fields: Any
) -> dict[str, Any]:
    body = {"summary": f"issue {number}", "updated": updated, "attachment": [], **fields}
    return {"id": str(number), "key": f"OPS-{number}", "fields": body}


def page(issues: list[Any], token: str | None = None) -> Reply:
    body: dict[str, Any] = {"issues": issues}
    if token is None:
        body["isLast"] = True
    else:
        body["nextPageToken"] = token
    return reply_json(body)


def serve_one(tmp_path: Path, reply: Reply, **options: Any) -> RecordSource:
    server = FakeServer(JiraBackend())
    server.inject(search, reply, times=99)
    with jira(server, tmp_path, **options) as source:
        source.listing()
        return source


# --- Rate limits and failures ----------------------------------------------------------------


def test_a_rate_limit_is_a_finding_with_the_systems_retry_after_and_nothing_is_retried(
    tmp_path: Path,
) -> None:
    server = FakeServer(JiraBackend())
    server.inject(search, Reply(429, b"{}", {"Retry-After": "30"}), times=99)
    with jira(server, tmp_path) as source:
        listing = source.listing()
        assert not listing.complete and listing.entries == () and listing.cursor is None
        [finding] = source.findings()
        assert finding.code == "deploy_jira.rate_limited"
        assert finding.details == {"page": 0, "status": 429, "retry_after": 30}
        assert len(server.requests(SEARCH)) == 1  # no retry, no sleep


def test_a_retry_after_that_is_a_date_or_nonsense_is_not_used(tmp_path: Path) -> None:
    for value in ("Wed, 21 Oct 2026 07:28:00 GMT", "²", "-1", "9" * 5000):
        source = serve_one(tmp_path, Reply(429, b"{}", {"Retry-After": value}))
        [finding] = source.findings()
        assert finding.details == {"page": 0, "status": 429}


def test_a_rate_limit_part_way_through_leaves_a_cursor_to_resume_from(tmp_path: Path) -> None:
    backend = JiraBackend()
    server = FakeServer(backend)
    first = server.log  # noqa: F841
    seen = {"n": 0}

    def third(request: Request) -> bool:
        if request.path == SEARCH:
            seen["n"] += 1
            return seen["n"] == 3
        return False

    server.inject(third, Reply(429, b"{}", {"Retry-After": "5"}))
    with jira(server, tmp_path, page_size=1) as source:
        listing = source.listing()
        assert not listing.complete
        ids = [e.id for e in listing.entries]
        assert ids[0] == "issue/10001" and ids[-1] == "issue/10002" and len(ids) == 5
        assert listing.cursor == "deploy_jira/1:2026-08-10T08:30:00.000+0200"
    with jira(FakeServer(backend), tmp_path, since=listing.cursor) as resumed:
        assert [e.id for e in resumed.listing().entries if "/attachment/" not in e.id] == [
            "issue/10002",
            "issue/10003",
            "issue/10004",
        ]  # the cursor's own second is read again, and the ledger discards it


def test_a_server_error_stops_the_listing_and_asserts_nothing_gone(tmp_path: Path) -> None:
    source = serve_one(tmp_path, Reply(503, b"busy"))
    assert codes(source) == ["deploy_jira.listing_failed"]
    assert not source.listing().complete


def test_unauthorised_is_access_denied_and_the_secret_is_nowhere(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    with server.serve() as host:
        source = jira_source(
            f"jira://{host}/OPS",
            network=_online(tmp_path),
            options={"scheme": "http", "instance": "site-a"},
            credentials={"email": "ops@example.com", "api_token": "wrong-token"},
        )
        source.listing()
        [finding] = source.findings()
        assert finding.code == "deploy_jira.access_denied"
        assert "wrong-token" not in repr(finding) and "wrong-token" not in repr(source.transform)


def _online(tmp_path: Path) -> Workspace:
    workspace = Workspace(tmp_path / "home")
    workspace.allow_network(True)
    return workspace


def test_a_truncated_body_is_a_failed_listing_not_a_short_page(tmp_path: Path) -> None:
    body = json.dumps({"issues": [issue(1)], "isLast": True}).encode()
    source = serve_one(tmp_path, Reply(200, body[:20], declared_length=len(body)))
    assert codes(source) == ["deploy_jira.listing_failed"]
    assert source.findings()[0].details["cause"] == "short_read"


def test_a_page_that_says_more_is_coming_and_names_none_is_invalid(tmp_path: Path) -> None:
    source = serve_one(tmp_path, reply_json({"issues": [issue(1)], "isLast": False}))
    assert codes(source) == ["deploy_jira.response_invalid"]


def test_a_server_that_trickles_bytes_cannot_hold_the_listing_open(tmp_path: Path) -> None:
    body = json.dumps({"issues": [], "isLast": True}).encode()
    source = serve_one(tmp_path, Reply(200, body, trickle=0.4), timeout=0.6)
    assert codes(source) == ["deploy_jira.listing_failed"]
    assert source.findings()[0].details["cause"] == "deadline_exceeded"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_never_followed(tmp_path: Path, status: int) -> None:
    server = FakeServer(JiraBackend())
    server.inject(
        search, Reply(status, b"", {"Location": "http://127.0.0.1:1/elsewhere"}), times=99
    )
    with jira(server, tmp_path) as source:
        source.listing()
        assert codes(source) == ["deploy_jira.redirect_refused"]
        assert len(server.log) == 1


def test_a_redirect_on_an_attachment_read_fails_that_read_only(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    server.inject(
        lambda r: "/attachment/content/20001" in r.path,
        Reply(303, b"", {"Location": "http://x"}),
        99,
    )
    with jira(server, tmp_path) as source:
        bad = next(e for e in source.listing().entries if e.id.endswith("20001"))
        good = next(e for e in source.listing().entries if e.id.endswith("20002"))
        with pytest.raises(ObjectReadError):
            source.open(bad.location).read()
        assert source.open(good.location).read() == b"PNG-ish bytes of the dock 3 photo"
        assert "deploy_jira.redirect_refused" in codes(source)


# --- Malformed documents ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b'{"issues": [], "issues": [], "isLast": true}',  # a repeated key
        b'{"issues": [{"id": "1", "fields": {"updated": NaN}}], "isLast": true}',
        b'{"issues": [], "isLast": Infinity}',
        b"\xff\xfe{\x00}",  # not UTF-8
        b"[" * 5000 + b"]" * 5000,  # nesting
        b"",
        b"<html>login</html>",
        b'{"issues": "not an array", "isLast": true}',
        b'["not", "an", "object"]',
    ],
)
def test_a_malformed_document_stops_the_listing_with_response_invalid(
    tmp_path: Path, body: bytes
) -> None:
    source = serve_one(tmp_path, Reply(200, body))
    assert codes(source) == ["deploy_jira.response_invalid"]


def test_a_response_that_is_not_json_or_is_compressed_is_refused(tmp_path: Path) -> None:
    body = json.dumps({"issues": [], "isLast": True}).encode()
    for headers in (
        {"Content-Type": "text/html"},
        {"Content-Type": "application/json", "Content-Encoding": "gzip"},
    ):
        source = serve_one(tmp_path, Reply(200, body, headers))
        assert codes(source) == ["deploy_jira.response_invalid"]


def test_a_page_body_over_the_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from neptune_deploy.sources.records import http

    monkeypatch.setattr(http, "MAX_PAGE_BYTES", 64)
    body = json.dumps({"issues": [issue(1)], "isLast": True}).encode()
    source = serve_one(tmp_path, Reply(200, body))
    assert codes(source) == ["deploy_jira.response_invalid"]


def test_numbers_keep_the_text_the_system_wrote(tmp_path: Path) -> None:
    fields = (
        b'{"updated": "2026-09-01T10:00:00.000+0200", "attachment": [],'
        b' "estimate": 1.50, "big": 1e2, "n": 12345678901234567890123}'
    )
    body = b'{"issues": [{"id": "7", "key": "OPS-7", "fields": ' + fields + b'}], "isLast": true}'
    source = serve_one(tmp_path, Reply(200, body))
    entry = source.listing().entries[0]
    data = source.open(entry.location).read()
    assert (
        b'"estimate":1.50' in data and b'"big":1e2' in data and b"12345678901234567890123" in data
    )


def test_a_string_with_a_lone_surrogate_makes_that_record_unrepresentable_only(
    tmp_path: Path,
) -> None:
    good = json.dumps(issue(2)).encode()
    bad = (
        b'{"id": "1", "key": "OPS-1", "fields": {"updated": "2026-09-01T10:00:00.000+0200",'
        b' "summary": "x\\ud800y", "attachment": []}}'
    )
    body = b'{"issues": [' + bad + b"," + good + b'], "isLast": true}'
    source = serve_one(tmp_path, Reply(200, body))
    assert [e.id for e in source.listing().entries] == ["issue/2"]
    assert codes(source) == ["deploy_jira.record_unrepresentable"]


def test_unicode_digits_and_odd_numbers_are_never_read_as_numbers(tmp_path: Path) -> None:
    attachments = [
        {
            "id": "20001",
            "filename": "a.pdf",
            "size": "²",
            "created": "2026-09-01T10:00:00.000+0200",
        },
        {
            "id": "20002",
            "filename": "b.pdf",
            "size": "9" * 5000,
            "created": "2026-09-01T10:00:00.000+0200",
        },
        {"id": "20003", "filename": "c.pdf", "size": -5, "created": "2026-09-01T10:00:00.000+0200"},
        {"id": "²", "filename": "d.pdf", "size": 1, "created": "2026-09-01T10:00:00.000+0200"},
        {
            "id": "20005",
            "filename": "e.pdf",
            "size": 1.5,
            "created": "2026-09-01T10:00:00.000+0200",
        },
    ]
    body = json.dumps({"issues": [issue(1, attachment=attachments)], "isLast": True}).encode()
    source = serve_one(tmp_path, Reply(200, body))
    assert [e.id for e in source.listing().entries] == ["issue/1"]
    assert set(codes(source)) == {"deploy_jira.size_invalid", "deploy_jira.id_invalid"}


# --- Hostile identities ----------------------------------------------------------------------


def test_ids_that_are_not_what_the_system_documents_are_not_used(tmp_path: Path) -> None:
    issues = [
        issue(1),
        {**issue(2), "id": "../../etc/passwd"},
        {**issue(3), "id": "9" * 5000},
        {**issue(4), "id": "1/attachment/2"},
        {**issue(5), "id": None},
        {**issue(6), "fields": None},
        {**issue(7), "key": ""},
        "not an object",
    ]
    source = serve_one(tmp_path, page(issues))
    assert [e.id for e in source.listing().entries] == ["issue/1"]
    by_code = {f.code: f.details for f in source.findings()}
    assert set(by_code) == {"deploy_jira.id_invalid", "deploy_jira.record_invalid"}
    # Findings hold counts and capped hex, never the hostile text or 5,000 digits.
    hexes = [h for d in by_code.values() for h in d["ids_hex"]]  # type: ignore[union-attr]
    assert all(len(str(h)) <= 512 for h in hexes)


def test_a_huge_unusable_id_is_held_as_a_prefix_with_its_length_and_digest(tmp_path: Path) -> None:
    source = serve_one(tmp_path, page([{**issue(1), "id": "9" * 100_000}]))
    [skip] = source.listing().skipped
    assert len(skip.raw_id) == 256 and skip.length == len("issue/") + 100_000
    assert len(skip.sha256) == 64


def test_one_id_listed_twice_with_two_revisions_is_used_for_neither(tmp_path: Path) -> None:
    body = page([issue(1), issue(1, "2026-09-02T10:00:00.000+0200"), issue(2)])
    source = serve_one(tmp_path, body)
    assert [e.id for e in source.listing().entries] == ["issue/2"]
    assert codes(source) == ["deploy_jira.record_duplicated"]


def test_one_id_listed_twice_identically_is_one_record(tmp_path: Path) -> None:
    source = serve_one(tmp_path, page([issue(1), issue(1)]))
    assert [e.id for e in source.listing().entries] == ["issue/1"]
    assert source.findings() == ()


def test_an_unusable_listed_record_is_never_called_gone(tmp_path: Path) -> None:
    backend = JiraBackend()
    ledger = SourceLedger()
    with jira(FakeServer(backend), tmp_path) as source:
        fingerprint(source, ledger, source.walk())
    server = FakeServer(backend)
    broken = [json.loads(json.dumps(i)) for i in backend.issues]
    for i in broken:
        i["fields"] = (
            {k: v for k, v in i["fields"].items() if k != "updated"}
            if i["key"] == "OPS-4"
            else i["fields"]
        )
    server.inject(search, page([i for i in broken]), times=99)
    with jira(server, tmp_path) as source:
        discovery = source.discover(ledger)
        assert discovery.gone == ()  # seen, unusable: nothing is known of it


def test_attachment_names_are_never_paths(tmp_path: Path) -> None:
    names = [
        "../../x.pdf",
        "C:\\Windows\\evil.exe",
        "a\x00b.pdf",
        "\u202etxt.exe",
        "",
        "..",
        "x" * 5000 + ".pdf",
        "a/b/c",
    ]
    attachments = [
        {"id": str(20000 + i), "filename": n, "size": 1, "created": "2026-09-01T10:00:00.000+0200"}
        for i, n in enumerate(names)
    ]
    source = serve_one(tmp_path, page([issue(1, attachment=attachments)]))
    got = [e.name for e in source.listing().entries if "/attachment/" in e.id]
    assert len(got) == len(names)
    for name in got:
        assert name and "/" not in name and "\\" not in name and "\x00" not in name
        assert len(name.encode()) <= 255 and name not in ("..", ".")
    assert got[0] == "x.pdf" and got[-1] == "c"


# --- Loops and limits ------------------------------------------------------------------------


def test_a_next_page_token_seen_before_stops_the_listing(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    counter = {"n": 0}

    def looping(request: Request) -> bool:
        return request.path == SEARCH

    def make(request: Request) -> Reply:
        counter["n"] += 1
        return page([issue(counter["n"] + 100)], token="same")

    original = server._respond

    def respond(request: Request) -> Reply:
        if looping(request):
            server.log.append(request)
            return make(request)
        return original(request)

    server._respond = respond  # type: ignore[method-assign]
    with jira(server, tmp_path) as source:
        listing = source.listing()
        assert not listing.complete
        assert codes(source) == ["deploy_jira.pagination_loop"]
        assert len(server.requests(SEARCH)) == 2  # the second page names "same" again
        assert len(listing.entries) == 2


def test_a_server_that_ignores_the_offset_is_a_loop(tmp_path: Path) -> None:
    server = FakeServer(ServiceNowBackend())
    backend = server.backend
    original = backend.handle

    def ignoring(request: Request) -> Reply | None:
        request.query["sysparm_offset"] = "0"
        return original(request)

    backend.handle = ignoring  # type: ignore[method-assign]
    with servicenow(server, tmp_path, page_size=2) as source:
        assert not source.listing().complete
        assert "deploy_servicenow.pagination_loop" in codes(source)


def test_the_record_limit_stops_the_listing_and_says_so(tmp_path: Path) -> None:
    issues = [issue(n) for n in range(1, 30)]
    source = serve_one(tmp_path, page(issues), max_records=10)
    listing = source.listing()
    assert len(listing.entries) == 10 and not listing.complete
    assert codes(source) == ["deploy_jira.listing_limit"]
    assert source.findings()[0].details == {"max_records": 10}


def test_rejected_ids_count_toward_the_record_limit(tmp_path: Path) -> None:
    junk = [{**issue(n), "id": f"x{n}"} for n in range(1, 30)]
    source = serve_one(tmp_path, page(junk), max_records=10)
    assert len(source.listing().skipped) <= 10
    assert "deploy_jira.listing_limit" in codes(source)


def test_the_listing_byte_budget_stops_the_listing(tmp_path: Path) -> None:
    source = serve_one(tmp_path, page([issue(n) for n in range(1, 200)]), max_listing_bytes=2048)
    assert not source.listing().complete
    assert source.findings()[0].details == {"max_listing_bytes": 2048}


def test_the_snapshot_byte_budget_stops_the_listing(tmp_path: Path) -> None:
    source = serve_one(tmp_path, page([issue(n) for n in range(1, 200)]), max_snapshot_bytes=2000)
    assert not source.listing().complete
    assert source.findings()[0].details == {"max_snapshot_bytes": 2000}


def test_an_attachment_over_the_size_limit_is_not_used_and_is_never_fetched(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    with jira(server, tmp_path, max_attachment_bytes=34) as source:
        sizes = {e.id: e.size for e in source.listing().entries if "/attachment/" in e.id}
        assert sizes == {"issue/10001/attachment/20002": 33}
        assert "deploy_jira.record_too_large" in codes(source)
        assert server.requests("/attachment/content/") == []


def test_a_cursor_longer_than_the_cap_stops_the_listing(tmp_path: Path) -> None:
    source = serve_one(tmp_path, page([issue(1)], token="t" * 5000))
    assert not source.listing().complete
    assert codes(source) == ["deploy_jira.response_invalid"]


# --- Reads -----------------------------------------------------------------------------------


def test_an_attachment_that_changed_size_is_object_changed_and_one_that_is_gone_is_object_gone(
    tmp_path: Path,
) -> None:
    backend = JiraBackend()
    server = FakeServer(backend)
    with jira(server, tmp_path) as source:
        entries = {
            e.id.rsplit("/", 1)[1]: e for e in source.listing().entries if "/attachment/" in e.id
        }
        backend.bytes["20001"] += b" appended"
        del backend.bytes["20002"]
        with pytest.raises(ObjectReadError) as changed:
            source.open(entries["20001"].location).read()
        assert changed.value.code == "object_changed"
        with pytest.raises(ObjectReadError) as gone:
            source.open(entries["20002"].location).read()
        assert gone.value.code == "object_gone"
        assert (
            source.open(entries["20003"].location).read() == b"lidar timeout at 09:14:58 ack 09:15"
        )


def test_a_truncated_attachment_is_a_short_read(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    with jira(server, tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.id.endswith("20001"))
        server.inject(
            lambda r: "/attachment/content/" in r.path,
            Reply(200, b"%PDF", declared_length=entry.size),
        )
        with pytest.raises(ObjectReadError) as raised:
            source.open(entry.location).read()
        assert raised.value.code == "short_read"


def test_opening_what_was_not_listed_is_refused(tmp_path: Path) -> None:
    with jira(FakeServer(JiraBackend()), tmp_path) as source:
        entry = source.listing().entries[0]
        from dataclasses import replace

        stale = replace(entry.location, revision_token="updated:older")
        with pytest.raises(ObjectReadError) as raised:
            source.open(stale)
        assert raised.value.code == "not_listed"
        with pytest.raises(TypeError):
            from neptune.model.ids import ExternalObjectRef

            source.open(ExternalObjectRef("deploy_s3", "x", "y"))


def test_a_reader_serves_nothing_whose_bytes_changed(tmp_path: Path) -> None:
    from neptune.identity.hashing import digest_stream

    backend = JiraBackend()
    with jira(FakeServer(backend), tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.id.endswith("20003"))
        artifact = digest_stream(source.open(entry.location), chunk_size=1024)
        reader = source.reader(entry.location, artifact)
        assert reader.read(0, 7) == b"lidar t"
        assert reader.read(entry.size - 2, 99) == b"15"
    with jira(FakeServer(backend), tmp_path) as source:
        entry = next(e for e in source.listing().entries if e.id.endswith("20003"))
        backend.bytes["20003"] = b"X" * entry.size
        with pytest.raises(ObjectReadError):
            source.reader(entry.location, artifact).read(0, 1)


# --- Boundaries ------------------------------------------------------------------------------


def test_a_local_only_workspace_refuses_before_any_request(tmp_path: Path) -> None:
    server = FakeServer(JiraBackend())
    with server.serve() as host, pytest.raises(LocalOnlyError):
        jira_source(
            f"jira://{host}/OPS",
            network=Workspace(tmp_path / "home"),
            options={"scheme": "http", "instance": "site-a"},
            credentials={"email": "a@b.c", "api_token": "t"},
        )
    assert server.log == []


def test_a_workspace_switched_to_local_only_refuses_the_next_request(tmp_path: Path) -> None:
    workspace = _online(tmp_path)
    server = FakeServer(JiraBackend())
    with server.serve() as host:
        source = jira_source(
            f"jira://{host}/OPS",
            network=workspace,
            options={"scheme": "http", "instance": "site-a"},
            credentials={"email": "ops@example.com", "api_token": "jira-secret"},
        )
        workspace.allow_network(False)
        with pytest.raises(LocalOnlyError):
            source.listing()  # a policy refusal, never a finding
    assert server.log == []


def test_every_system_only_ever_sends_get_and_leaks_no_secret(tmp_path: Path) -> None:
    from deploy_records_fake import (
        ConfluenceBackend,
        DriveBackend,
        RestBackend,
    )
    from deploy_records_support import confluence, gdrive, rest

    cases = [
        (jira, JiraBackend()),
        (servicenow, ServiceNowBackend()),
        (gdrive, DriveBackend()),
        (confluence, ConfluenceBackend()),
        (rest, RestBackend()),
    ]
    for opener, backend in cases:
        server = FakeServer(backend)
        with opener(server, tmp_path) as source:
            for entry in source.listing().entries:
                source.open(entry.location).read()
            text = repr(source.findings()) + repr(source.transform) + repr(source.listing())
            assert not any(secret in text for secret in SECRETS)
            assert not any(secret in repr(source.system) for secret in SECRETS)
        assert server.other_methods == [], opener
        assert {r.method for r in server.log} == {"GET"}
