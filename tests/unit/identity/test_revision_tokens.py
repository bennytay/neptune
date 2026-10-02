"""The ledger keeps every revision token seen over an external revision's bytes (ADR 0067)."""

import io
from pathlib import Path

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef, RecordId
from neptune.model.source import LocalPath
from neptune.store.workspace import TOKENS_FILE, Workspace, WorkspaceError

A = digest_stream(io.BytesIO(b"bytes A"))
B = digest_stream(io.BytesIO(b"bytes B"))
URI = "s3://fleet-logs/arm-cell/"


def at(token: str, key: str = "fleet-logs/arm-cell/episode-7.mcap") -> ExternalObjectRef:
    return ExternalObjectRef("deploy_s3", key, token)


def test_a_new_token_over_the_same_bytes_is_remembered_not_a_revision() -> None:
    ledger = SourceLedger()
    first = ledger.observe(at("etag-1"), A)
    assert first.new_revision and first.new_token
    again = ledger.observe(at("etag-2"), A)
    assert not again.new_revision and again.new_token
    assert again.revision == first.revision
    assert ledger.tokens() == ((first.revision.id, ("etag-2",)),)
    seen_again = ledger.observe(at("etag-2"), A)
    assert not seen_again.new_revision and not seen_again.new_token


def test_every_token_seen_for_the_head_is_recognised() -> None:
    ledger = SourceLedger()
    head = ledger.observe(at("etag-1"), A).revision
    ledger.observe(at("etag-2"), A)
    assert ledger.recognise(at("etag-1")) == head
    assert ledger.recognise(at("etag-2")) == head
    assert ledger.recognise(at("etag-3")) is None
    assert ledger.recognise(at("etag-1", "fleet-logs/other.mcap")) is None


def test_a_token_of_an_older_revision_is_not_recognised() -> None:
    ledger = SourceLedger()
    ledger.observe(at("v1"), A)
    changed = ledger.observe(at("v2"), B)
    assert changed.new_revision
    assert ledger.recognise(at("v1")) is None  # the object has held other bytes since
    assert ledger.recognise(at("v2")) == changed.revision


def test_an_absent_object_is_not_recognised() -> None:
    ledger = SourceLedger()
    ledger.observe(at("v1"), A)
    ledger.mark_absent(at("v1"))
    assert ledger.recognise(at("v1")) is None


def test_tokens_never_change_content_or_revision_identity() -> None:
    one, two = SourceLedger(), SourceLedger()
    one.observe(at("etag-1"), A)
    two.observe(at("etag-1"), A)
    two.observe(at("etag-2"), A)
    assert one.artifacts() == two.artifacts() and one.revisions() == two.revisions()


def test_a_local_location_has_no_token() -> None:
    ledger = SourceLedger()
    observed = ledger.observe(LocalPath("run.mcap"), A)
    assert not observed.new_token and ledger.tokens() == ()
    with pytest.raises(TypeError):
        ledger.recognise(LocalPath("run.mcap"))  # type: ignore[arg-type]


def test_a_ledger_rebuilt_with_its_tokens_recognises_them() -> None:
    ledger = SourceLedger()
    head = ledger.observe(at("etag-1"), A).revision
    ledger.observe(at("etag-3"), A)
    ledger.observe(at("etag-2"), A)
    again = SourceLedger(
        ledger.artifacts(), ledger.revisions(), ledger.absences(), dict(ledger.tokens())
    )
    assert again.tokens() == ((head.id, ("etag-2", "etag-3")),)
    assert again.recognise(at("etag-3")) == head


@pytest.mark.parametrize(
    "tokens",
    [
        {RecordId("rec:sha256:" + "0" * 64): ["etag-2"]},  # no such revision
        "own",  # its own token listed as an extra
        "local",  # a local revision has no tokens
        "text",  # one string, not a list
        "number",
        "empty",
    ],
)
def test_tokens_that_do_not_fit_the_ledger_are_refused(tokens: object) -> None:
    ledger = SourceLedger()
    external = ledger.observe(at("etag-1"), A).revision
    local = ledger.observe(LocalPath("run.mcap"), A).revision
    named = {
        "own": {external.id: ["etag-1"]},
        "local": {local.id: ["etag-2"]},
        "text": {external.id: "etag-2"},
        "number": {external.id: [7]},
        "empty": {external.id: [""]},
    }
    given = named[tokens] if isinstance(tokens, str) else tokens
    with pytest.raises(ValueError):
        SourceLedger(ledger.artifacts(), ledger.revisions(), (), given)  # type: ignore[arg-type]


def test_the_workspace_keeps_tokens_with_the_ledger(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws")
    ledger = SourceLedger()
    head = ledger.observe(at("etag-1"), A).revision
    ledger.observe(at("etag-2"), A)
    workspace.save_ledger(URI, ledger)
    assert workspace.has_ledger(URI)
    loaded = workspace.load_ledger(URI)
    assert loaded.recognise(at("etag-2")) == head
    assert loaded.tokens() == ledger.tokens()
    directory = next((tmp_path / "ws" / "ledgers").iterdir())
    assert (directory / "root").read_bytes() == URI.encode()
    line = (directory / TOKENS_FILE).read_bytes()
    assert canonical_json.loads(line.rstrip(b"\n")) == {"revision": head.id, "tokens": ["etag-2"]}


def test_a_uri_is_its_own_ledger_exactly_as_given(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws")
    ledger = SourceLedger()
    ledger.observe(at("etag-1"), A)
    workspace.save_ledger(URI, ledger)
    assert not workspace.has_ledger(URI.rstrip("/"))  # another prefix: only the connector knows
    assert not workspace.has_ledger(tmp_path)


def test_text_that_is_not_a_uri_is_refused_as_a_root(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws")
    with pytest.raises(WorkspaceError):
        workspace.load_ledger("relative/folder")


def test_a_ledger_saved_before_tokens_existed_loads(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "ws")
    ledger = SourceLedger()
    ledger.observe(at("etag-1"), A)
    workspace.save_ledger(URI, ledger)
    directory = next((tmp_path / "ws" / "ledgers").iterdir())
    (directory / TOKENS_FILE).unlink()
    assert workspace.load_ledger(URI).revisions() == ledger.revisions()


@pytest.mark.parametrize(
    "line",
    [
        b'{"revision":"rec:sha256:' + b"0" * 64 + b'","tokens":["x"]}\n',  # unknown revision
        b"[]\n",
        b'{"revision":"x","tokens":["a"]}\n',
        b'{"revision":"REV","tokens":["b","a"]}\n',  # unsorted
        b'{"revision":"REV","tokens":[]}\n',
        b'{"extra":1,"revision":"REV","tokens":["a"]}\n',
    ],
)
def test_a_damaged_tokens_file_is_refused(tmp_path: Path, line: bytes) -> None:
    workspace = Workspace(tmp_path / "ws")
    ledger = SourceLedger()
    head = ledger.observe(at("etag-1"), A).revision
    workspace.save_ledger(URI, ledger)
    directory = next((tmp_path / "ws" / "ledgers").iterdir())
    (directory / TOKENS_FILE).write_bytes(line.replace(b"REV", head.id.encode()))
    with pytest.raises(ValueError):
        workspace.load_ledger(URI)
