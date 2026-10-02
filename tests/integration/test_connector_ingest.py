"""``neptune ingest <scheme>://…`` through a plugin connector, end to end (ADR 0067, MVL-45).

The connector is ``tests/fixtures/sources/fake_object_store.py``, installed as a real plugin
distribution: an object store in a directory, read in process, with no socket. The jobs are real
and sandboxed (the default), so adapters read the job's spool by descriptor. What is checked:

- the URI reaches the connector that declares its scheme, and only with the network allowed;
- the package is deterministic, and names every object by connector, key and revision token;
- a re-run fetches and hashes nothing unchanged and calls no adapter;
- a re-upload of the same bytes is hashed once under its new token, then recognised;
- a changed object is a new revision; a removed one an absence, never a deleted history, and
  only from a complete listing;
- an object that cannot be read is a finding, and the job commits.
"""

import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.cli import exit_codes, run
from neptune.discovery.policy import UNREADABLE
from neptune.model.ids import ExternalObjectRef
from neptune.model.source import SourceAbsence, SourceRevision
from neptune.sdk import JobEvent, Neptune, RemoteSource
from neptune.store.workspace import Workspace

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("forget_plugins")]

FAKE: Final = (
    Path(__file__).parents[1] / "fixtures" / "sources" / "fake_object_store.py"
).read_text(encoding="utf-8")
MODULE: Final = "neptune_test_fake_store"
URI: Final = "fake://fleet-logs/arm-cell/"
CSV: Final = b"t_ns,joint_1_rad\n0,0.10\n1000000,0.12\n2000000,0.15\n"
NOTE: Final = b"# Cell 3\n\nSecond shift; the gripper was recalibrated before episode 7.\n"
LOG: Final = b"episode 7 started\nepisode 7 ended\n"


@pytest.fixture
def fake(plugin_dists: ModuleType, plugin_site: Path) -> ModuleType:
    """The fake connector, installed as a ``neptune.sources`` plugin; its module."""
    plugin_dists.install(
        plugin_site,
        "neptune-test-fake-store",
        "1.0.0",
        sources={"fake_store": ":make"},
        module=FAKE,
    )
    Neptune(plugin_site / "probe-ws", adapters=[])  # imports it, as every client does
    import neptune_test_fake_store as module  # type: ignore[import-not-found]

    module.READS.clear()
    return module  # type: ignore[no-any-return]


@pytest.fixture
def store(fake: ModuleType, tmp_path: Path) -> Path:
    """A bucket prefix of an arm cell's run: joint angles, an operator note, an event log."""
    store = tmp_path / "store"
    store.mkdir()
    fake.put(store, "arm-cell/episode-7/joints.csv", CSV, "etag-joints-1")
    fake.put(store, "arm-cell/episode-7/notes.md", NOTE, "etag-notes-1")
    fake.put(store, "arm-cell/episode-7/events.log", LOG, "etag-log-1")
    fake.put(store, "other-cell/ignored.txt", b"not under the prefix\n", "etag-x")
    return store


def cli(*argv: str) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    out, err = io.StringIO(), io.StringIO()
    code = run([*argv, "--json"], stdout=out, stderr=err)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    events = [line["event"] for line in lines if line["type"] == "event"]
    return code, lines[-1] if lines else {}, events


def ingest(
    store: Path, workspace: Path, out: Path, *extra: str
) -> tuple[dict[str, Any], list[Any]]:
    options = json.dumps({"store": str(store)})
    code, result, events = cli(
        "ingest", URI, "--out", str(out), "-w", str(workspace), "--source-options", options, *extra
    )
    assert code == exit_codes.OK, result
    assert result["state"] == "committed"
    return result, events


def kinds(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [event["details"] for event in events if event["kind"] == kind]


def package_bytes(package: Path) -> dict[str, bytes]:
    """Every file of a package but ``volatile/`` (its envelope: clock, host, job; ADR 0022)."""
    return {
        str(path.relative_to(package)): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.relative_to(package).parts[0] != "volatile"
    }


def revisions(package: Path) -> list[dict[str, Any]]:
    lines = (package / "records" / "source_revision.jsonl").read_bytes().splitlines()
    return [json.loads(line) for line in lines]


def test_a_uri_is_read_by_the_connector_that_declares_its_scheme(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    result, events = ingest(store, tmp_path / "ws", tmp_path / "pkg", "--allow-network")
    assert result["sources"] == 3 and fake.BUILT == [URI]
    hashed = kinds(events, "source_hashed")
    keys = sorted(details["location"]["object_id"] for details in hashed)
    assert keys == [
        "fleet-logs/arm-cell/episode-7/events.log",
        "fleet-logs/arm-cell/episode-7/joints.csv",
        "fleet-logs/arm-cell/episode-7/notes.md",
    ]
    listed = revisions(tmp_path / "pkg")
    assert {r["location"]["connector_id"] for r in listed} == {"fake_store"}
    assert {r["location"]["revision_token"] for r in listed} == {
        "etag-joints-1",
        "etag-notes-1",
        "etag-log-1",
    }
    receipt = json.loads((tmp_path / "pkg" / "receipt.json").read_bytes())
    connector = [t for t in receipt["transforms"] if t["adapter_id"] == "fake_store"]
    assert len(connector) == 1  # the connector that listed the sources is in the lineage
    envelope = json.loads((tmp_path / "pkg" / "volatile" / "receipt-envelope.json").read_bytes())
    assert envelope["root"] == URI


def test_the_same_bucket_gives_byte_identical_packages(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    Workspace(tmp_path / "one").allow_network(True)
    Workspace(tmp_path / "two").allow_network(True)
    first, _ = ingest(store, tmp_path / "one", tmp_path / "pkg-one")
    second, _ = ingest(store, tmp_path / "two", tmp_path / "pkg-two")
    assert first["package"] == second["package"]
    assert package_bytes(tmp_path / "pkg-one") == package_bytes(tmp_path / "pkg-two")


def test_a_rerun_fetches_hashes_and_parses_nothing_unchanged(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    Workspace(tmp_path / "ws").allow_network(True)
    first, _ = ingest(store, tmp_path / "ws", tmp_path / "pkg-1")
    fake.READS.clear()
    again, events = ingest(store, tmp_path / "ws", tmp_path / "pkg-2")
    assert kinds(events, "source_hashed") == []
    assert len(kinds(events, "source_recognised")) == 3
    report = json.loads((tmp_path / "pkg-2" / "volatile" / "cache-report.json").read_bytes())
    assert report["calls"] == {"ingest": 0, "plan": 0, "probe": 0}
    assert fake.READS == []  # not a byte fetched
    assert package_bytes(tmp_path / "pkg-1") == package_bytes(tmp_path / "pkg-2")
    assert first["package"] == again["package"]


def test_a_reupload_of_the_same_bytes_is_hashed_once_then_recognised(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    Workspace(tmp_path / "ws").allow_network(True)
    ingest(store, tmp_path / "ws", tmp_path / "pkg-1")
    fake.retoken(store, "arm-cell/episode-7/joints.csv", "etag-joints-2")
    _, events = ingest(store, tmp_path / "ws", tmp_path / "pkg-2")
    (hashed,) = kinds(events, "source_hashed")  # a new token: the ledger cannot know the bytes
    assert hashed["location"]["revision_token"] == "etag-joints-2"
    assert not hashed["new_revision"] and not hashed["new_artifact"]
    fake.READS.clear()
    _, events = ingest(store, tmp_path / "ws", tmp_path / "pkg-3")
    assert kinds(events, "source_hashed") == [] and fake.READS == []
    recognised = {e["location"]["revision_token"] for e in kinds(events, "source_recognised")}
    assert "etag-joints-2" in recognised
    # The package names the token it was read under; content identity is the bytes alone.
    tokens = {r["location"]["revision_token"] for r in revisions(tmp_path / "pkg-3")}
    assert "etag-joints-2" in tokens and "etag-joints-1" not in tokens
    one = {r["content_id"] for r in revisions(tmp_path / "pkg-1")}
    assert one == {r["content_id"] for r in revisions(tmp_path / "pkg-3")}


def test_a_changed_object_is_a_new_revision_and_a_removed_one_an_absence(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path / "ws")
    workspace.allow_network(True)
    ingest(store, tmp_path / "ws", tmp_path / "pkg-1")
    fake.put(store, "arm-cell/episode-7/events.log", LOG + b"episode 8 started\n", "etag-log-2")
    fake.remove(store, "arm-cell/episode-7/notes.md")
    result, events = ingest(store, tmp_path / "ws", tmp_path / "pkg-2")
    (hashed,) = kinds(events, "source_hashed")
    assert hashed["new_revision"] and hashed["location"]["revision_token"] == "etag-log-2"
    (absent,) = kinds(events, "source_absent")
    assert absent["location"]["object_id"] == "fleet-logs/arm-cell/episode-7/notes.md"
    assert result["sources"] == 2
    ledger = workspace.load_ledger(URI)
    notes = ExternalObjectRef(
        "fake_store", "fleet-logs/arm-cell/episode-7/notes.md", "etag-notes-1"
    )
    assert isinstance(ledger.head(notes), SourceAbsence)  # history kept, the object gone
    assert any(r.location == notes for r in ledger.revisions())
    log = ExternalObjectRef("fake_store", "fleet-logs/arm-cell/episode-7/events.log", "etag-log-2")
    head = ledger.head(log)
    assert isinstance(head, SourceRevision) and len(head.supersedes) == 1


def test_an_incomplete_listing_asserts_nothing_gone(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path / "ws")
    workspace.allow_network(True)
    ingest(store, tmp_path / "ws", tmp_path / "pkg-1")
    fake.set_incomplete(store, True)
    _, events = ingest(store, tmp_path / "ws", tmp_path / "pkg-2")
    assert kinds(events, "source_absent") == []
    assert not workspace.load_ledger(URI).absences()


def test_an_object_that_cannot_be_read_is_a_finding_and_the_job_commits(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    Workspace(tmp_path / "ws").allow_network(True)
    fake.put(store, "arm-cell/episode-7/broken.bin", b"\x00" * 100, "etag-broken", fail=True)
    result, events = ingest(store, tmp_path / "ws", tmp_path / "pkg")
    assert result["sources"] == 3
    assert kinds(events, "entry_skipped")[0]["location"]["object_id"].endswith("broken.bin")
    receipt = json.loads((tmp_path / "pkg" / "receipt.json").read_bytes())
    codes = {f["code"] for f in receipt["findings"]}
    assert {UNREADABLE, "fake_store.read_failed"} <= codes


def test_a_local_only_workspace_refuses_the_connector(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    options = json.dumps({"store": str(store)})
    argv = ("ingest", URI, "--out", str(tmp_path / "pkg"), "-w", str(tmp_path / "ws"))
    code, result, _ = cli(*argv, "--source-options", options)
    assert (
        code == exit_codes.for_code("network_refused")
        and result["error"]["code"] == "network_refused"
    )
    assert fake.BUILT == []  # refused before the connector was built


def test_a_connector_can_be_named_and_an_unknown_one_is_a_configuration_error(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    Workspace(tmp_path / "ws").allow_network(True)
    options = json.dumps({"store": str(store)})
    base = ("-w", str(tmp_path / "ws"), "--source-options", options, "--dry-run")
    code, result, _ = cli("ingest", URI, "--connector", "fake_store", *base)
    assert code == exit_codes.OK and result["state"] == "planned"
    code, result, _ = cli("ingest", URI, "--connector", "no_such", *base)
    assert code == exit_codes.for_code("invalid_configuration")
    code, result, _ = cli("ingest", "nope://bucket/x", *base)
    assert code == exit_codes.for_code("invalid_configuration")
    assert "no installed connector reads nope://" in result["error"]["message"]
    code, result, _ = cli(
        "ingest", URI, "--source-options", "[1]", "-w", str(tmp_path / "ws"), "-n"
    )
    assert code == exit_codes.for_code("invalid_configuration")


def test_the_sdk_takes_a_remote_source_and_reports_progress(
    fake: ModuleType, store: Path, tmp_path: Path
) -> None:
    workspace = Workspace(tmp_path / "ws")
    workspace.allow_network(True)
    seen: list[JobEvent] = []
    client = Neptune(workspace)
    remote = RemoteSource(URI, options={"store": str(store)})
    planned = client.dry_run(remote, on_event=seen.append)
    assert planned.planned and planned.explanation is not None
    rendered = planned.explanation.render()
    assert "fake_store:fleet-logs/arm-cell/episode-7/joints.csv@etag-joints-1" in rendered
    result = client.ingest(remote, tmp_path / "pkg", resume=True)  # the dry run left its ledger
    assert result.committed
