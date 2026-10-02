"""D2 gate: connector identity, read-only and local-only guarantees, for every connector (MVL-158).

Each guarantee of ``docs/reviews/d2-stress-test.md`` is one test here, run over every connector's
rig (``deploy_d2_rigs``). Networked connectors are served by their own fake behind one hostile
reverse proxy (``deploy_d2_proxy``), so the same attacks come from the same code for all of them,
and the proxy's log is every request each connector sent. ROS 2 diagnostics are a mapper over a
package, not a Source, and have their own section. Where a guarantee is already proven in depth by
a connector's own tests, the gate cites them, and ``test_every_cited_test_exists`` keeps the
citations honest.

1. External identity survives a re-sync.
2. A changed object yields a new revision, and the old one stays intact.
3. Nothing is written to the remote.
4. Local-only mode refuses the source.
5. Credentials never reach adapters.
6. A hostile remote produces findings, not exceptions or hangs.
7. Determinism.
"""

import ast
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from deploy_d2_proxy import ATTACKS, Seen
from deploy_d2_rigs import NETWORKED, RIGS, OpenRmfRig, Rig, clean_sync, is_read
from deploy_d2_support import (
    Wire,
    emitted,
    entries,
    exception_texts,
    fingerprint,
    ledger_json,
    location_of,
    no_sockets,
    spellings,
)
from neptune.identity.revisions import SourceLedger
from neptune.model.ids import ExternalObjectRef
from neptune.store.package import read_files, read_package
from neptune.store.workspace import LocalOnlyError, Workspace
from neptune_deploy.diagnostics import load_mapping, map_diagnostics_files
from neptune_deploy.sources.fleet_ops import FleetOpsConfigError, open_rmf_source

TESTS = Path(__file__).parent
HOSTILE_TIMEOUT = 0.5  # seconds: the connector's own declared timeout under attack
BOUND = 60.0  # seconds any one hostile run may take before the gate calls it a hang


@pytest.fixture(params=sorted(RIGS))
def rig(request: pytest.FixtureRequest, tmp_path: Path) -> Rig:
    built: Rig = RIGS[request.param](tmp_path)
    return built


@pytest.fixture(params=NETWORKED)
def remote(request: pytest.FixtureRequest, tmp_path: Path) -> Rig:
    built: Rig = RIGS[request.param](tmp_path)
    return built


def order(location: ExternalObjectRef) -> tuple[str, str]:
    return location.object_id, location.revision_token


def close(source: Any) -> None:
    if hasattr(source, "close"):
        source.close()


def bounded(call: Callable[[], Any], seconds: float = BOUND) -> Any:
    """``call()`` in a thread; a run still going after ``seconds`` is a hang and fails."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"still running after {seconds} s: a hang"
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


# --- 1. External identity survives a re-sync -----------------------------------------------------


def test_g1_external_identity_survives_a_resync(rig: Rig) -> None:
    """A second sync (a new server port, another page size, a new source) sees every object at
    the identity the first gave it, and against the first's ledger nothing is new or changed."""
    ledger = SourceLedger()
    with rig.running():
        first = rig.source()
        before = sorted((e.location for e in entries(first.walk())), key=order)
        fingerprint(first, ledger)
        close(first)
    assert before
    with rig.running():
        again = rig.source(ledger=ledger, **rig.repage)
        discovery = again.discover(ledger)
        after = sorted((e.location for e in entries(rig.source(**rig.repage).walk())), key=order)
        close(again)
    assert after == before
    assert not discovery.new and not discovery.changed
    assert not getattr(discovery, "gone", ())
    assert sorted((location_of(item) for item in discovery.unchanged), key=order) == before


# --- 2. A changed object is a new revision; the old one stays intact ------------------------------


def test_g2_a_changed_object_is_a_new_revision_and_the_old_stays_intact(rig: Rig) -> None:
    ledger = SourceLedger()
    with rig.running():
        first = rig.source()
        fingerprint(first, ledger)
        close(first)
    old = ledger.revisions()
    rig.change()
    with rig.running():
        later = rig.source(ledger=ledger)
        discovery = later.discover(ledger)
        changed = {location_of(item).object_id for item in discovery.changed}
        assert changed == rig.changed
        assert not discovery.new and not getattr(discovery, "gone", ())
        fingerprint(later, ledger)
        close(later)
    revisions = ledger.revisions()
    assert set(old) <= set(revisions)  # append-only: every earlier revision is kept as it was
    for object_id in rig.changed:
        chain = [r for r in revisions if r.location.key[2] == object_id]
        assert len(chain) == 2, object_id
        (previous,) = [r for r in chain if r in old]
        (current,) = [r for r in chain if r not in old]
        assert current.supersedes == (previous.id,)
        assert isinstance(previous.location, ExternalObjectRef)
        assert isinstance(current.location, ExternalObjectRef)
        assert previous.location.revision_token != current.location.revision_token
        assert previous.content_id != current.content_id
        assert ledger.artifact(previous.content_id) is not None  # the old bytes' identity stays
    unchanged = {r.location.key for r in old if r.location.key[2] not in rig.changed}
    for key in unchanged:
        assert len([r for r in revisions if r.location.key == key]) == 1


# --- 3. Nothing is written to the remote --------------------------------------------------------


def test_g3_every_request_is_on_the_read_only_surface(remote: Rig) -> None:
    seen: list[Seen] = []
    wire = Wire()
    with wire.recording():
        ledger = SourceLedger()
        with remote.running() as proxy:
            source = remote.source()
            artifacts = fingerprint(source, ledger)
            for entry in entries(source.walk()):
                if entry.size and hasattr(source, "reader"):
                    source.reader(entry.location, artifacts[entry.location.object_id]).read(0, 1)
            emitted(source)
            close(source)
            seen += proxy.requests()
            ports = {proxy.port}
        remote.change()
        with remote.running() as proxy:
            later = remote.source(ledger=ledger)
            later.discover(ledger)
            fingerprint(later, ledger)
            close(later)
            seen += proxy.requests()
            ports.add(proxy.port)
    assert seen
    refused = [(r.method, r.path) for r in seen if not remote.allowed(r)]
    assert refused == [], f"{remote.connector} sent requests off its read-only surface"
    # The proxy saw everything: no request went anywhere else (a link host, an ambient endpoint).
    assert {(s.host, s.port) for s in wire.sent} <= {("127.0.0.1", port) for port in ports}
    assert len(wire.sent) == len(seen)


def test_g3_open_rmf_never_writes_its_directory_or_opens_a_socket(tmp_path: Path) -> None:
    rig = OpenRmfRig(tmp_path)

    def state() -> dict[str, tuple[bytes, int]]:
        return {
            str(p.relative_to(rig.root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(rig.root.rglob("*"))
        }

    before = state()
    with no_sockets():
        source = rig.source()
        fingerprint(source, SourceLedger())
        emitted(source)
    assert state() == before  # no byte, timestamp, journal or WAL file


# --- 4. Local-only mode refuses the source ------------------------------------------------------


def test_g4_a_local_only_workspace_refuses_the_source_before_any_request(remote: Rig) -> None:
    wire = Wire()
    with wire.recording(), remote.running() as proxy:
        local = Workspace(remote.home())  # local-only is the default
        with pytest.raises(LocalOnlyError):
            remote.source(network=local)
        assert proxy.requests() == []
    assert wire.sent == []


def test_g4_switching_to_local_only_refuses_the_next_request(remote: Rig) -> None:
    with remote.running() as proxy:
        workspace = remote.network()
        source = remote.source(network=workspace)
        workspace.allow_network(False)
        with pytest.raises(LocalOnlyError):
            list(source.walk())
        assert proxy.requests() == []
        workspace.allow_network(True)
        listed = entries(source.walk())
        workspace.allow_network(False)
        sent = len(proxy.requests())
        refused = 0
        for entry in listed:
            try:
                with source.open(entry.location) as stream:
                    stream.read()
            except LocalOnlyError:
                refused += 1
        assert len(proxy.requests()) == sent  # nothing reached the remote after the switch
        if remote.has_reads:
            assert refused, "a read that needs the network went ahead in local-only mode"
        close(source)


def test_g4_open_rmf_is_local_and_needs_no_network(tmp_path: Path) -> None:
    rig = OpenRmfRig(tmp_path)
    with no_sockets():
        local = rig.source(network=Workspace(tmp_path / "home"))  # local-only
        assert entries(local.walk())


# --- 5. Credentials never reach adapters --------------------------------------------------------


def test_g5_credentials_reach_only_the_api_and_never_any_output(remote: Rig) -> None:
    ledger = SourceLedger()
    texts: list[str] = []
    seen: list[Seen] = []
    endpoints: list[str] = []
    with remote.running() as proxy:
        endpoints += [proxy.authority, proxy.upstream.removeprefix("http://")]
        source = remote.source()
        fingerprint(source, ledger)
        texts.append(emitted(source))
        close(source)
        seen += proxy.requests()
    remote.change()
    with remote.running() as proxy:
        endpoints += [proxy.authority, proxy.upstream.removeprefix("http://")]
        later = remote.source(ledger=ledger)
        later.discover(ledger)
        fingerprint(later, ledger)
        texts.append(emitted(later))
        close(later)
        seen += proxy.requests()
    texts.append(ledger_json(ledger))
    leaks = remote.leak_spellings()
    for text in texts:
        assert not [s for s in leaks if s in text], f"{remote.connector} leaked a credential"
        for endpoint in endpoints:  # nor the endpoint: it says where bytes came from, not what
            assert endpoint not in text
    carrying = [r for r in seen if any(s in json.dumps(r.headers) + r.query for s in leaks)]
    assert remote.sent_secret is not None
    sent = spellings(remote.sent_secret, user=remote.basic_user)
    assert any(any(s in json.dumps(r.headers) + r.query for s in sent) for r in carrying), (
        "positive control: the credential was sent to the API"
    )
    assert all(remote.carries(r) for r in carrying)
    assert not [r for r in seen if any(s in r.path for s in leaks)]


def test_g5_open_rmf_takes_no_credentials(tmp_path: Path) -> None:
    rig = OpenRmfRig(tmp_path)
    with pytest.raises(FleetOpsConfigError) as refused:
        open_rmf_source(rig.root, credentials={"token": "rmf-secret-never-printed"})
    assert "rmf-secret-never-printed" not in exception_texts(refused.value)


# --- 6. A hostile remote produces findings, not exceptions or hangs -----------------------------


@pytest.mark.parametrize("attack", ATTACKS)
def test_g6_a_hostile_listing_is_findings_never_an_exception_or_a_hang(
    remote: Rig, attack: str
) -> None:
    """Every request the connector sends meets ``attack``: it stops with findings, in time."""
    with remote.running(lambda request: attack) as proxy:
        source = remote.source(**remote.timeout(HOSTILE_TIMEOUT))

        def run() -> tuple[list[Any], str]:
            walked = list(source.walk())
            return walked, emitted(source, walked=walked)

        started = time.monotonic()
        walked, text = bounded(run)
        elapsed = time.monotonic() - started
        close(source)
        assert proxy.requests()
    findings = source.findings()
    assert findings, f"{remote.connector} met {attack} and said nothing"
    assert all(
        f.code.startswith(f"{remote.connector}.") or f.code.startswith("deploy_s3.")
        for f in findings
    )
    assert entries(walked) == [] or remote.connector == "deploy_open_rmf"
    assert not [s for s in remote.leak_spellings() if s in text]
    assert elapsed < BOUND


@pytest.mark.parametrize("attack", ATTACKS)
def test_g6_a_hostile_read_fails_that_object_with_a_finding(remote: Rig, attack: str) -> None:
    """The listing is clean; then every byte read meets ``attack``. Each failed read raises the
    source's ``OSError`` (so the caller quarantines that object and nothing else) and is a finding;
    nothing else escapes."""
    if not remote.has_reads:
        pytest.skip(f"{remote.connector} reads no bytes of its own: content is in the listing")
    with remote.running() as proxy:
        source = remote.source(**remote.timeout(HOSTILE_TIMEOUT))
        listed = entries(source.walk())
        assert listed
        before = len(source.findings())
        proxy.attack = lambda request: attack if is_read(request) else None

        def run() -> list[BaseException]:
            failed: list[BaseException] = []
            for entry in listed:
                try:
                    with source.open(entry.location) as stream:
                        stream.read()
                except OSError as exc:
                    failed.append(exc)
            return failed

        failed = bounded(run)
        close(source)
    assert failed, f"{remote.connector}: no read met {attack}"
    for exc in failed:
        assert getattr(exc, "code", None), repr(exc)
        assert not [s for s in remote.leak_spellings() if s in exception_texts(exc)]
    assert len(source.findings()) > before


def test_g6_a_listing_over_its_declared_budget_stops_with_a_finding(remote: Rig) -> None:
    with remote.running():
        clean = list(remote.source().walk())
        source = remote.source(**remote.budget)
        walked = bounded(lambda: list(source.walk()))
        close(source)
    codes = {f.code.rpartition(".")[2] for f in source.findings()}
    assert remote.limit_code in codes, codes
    assert entries(walked) != entries(clean)  # fewer objects, or partial documents


# --- 7. Determinism -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(RIGS))
def test_g7_two_clean_syncs_emit_identical_bytes(name: str, tmp_path: Path) -> None:
    """Two syncs, each with its own fake, proxy and port, emit the same bytes."""
    assert clean_sync(name, tmp_path / "a") == clean_sync(name, tmp_path / "b")


@pytest.mark.parametrize("attack", ["truncate", "redirect_loop", "oversized"])
def test_g7_a_hostile_run_is_as_deterministic_as_a_clean_one(
    remote: Rig, attack: str, tmp_path: Path
) -> None:
    runs = []
    for attempt in range(2):
        rig = type(remote)(tmp_path / str(attempt))
        with rig.running(lambda request: attack):
            source = rig.source(**rig.timeout(HOSTILE_TIMEOUT))
            walked = bounded(lambda: list(source.walk()))  # noqa: B023
            runs.append(emitted(source, walked=walked))
            close(source)
    assert runs[0] == runs[1]


def test_g7_every_connector_is_identical_under_other_hash_seeds() -> None:
    outputs = []
    for seed in ("0", "4242"):
        result = subprocess.run(
            [sys.executable, str(TESTS / "deploy_d2_rigs.py")],
            cwd=TESTS,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(TESTS)},
            capture_output=True,
            text=True,
            timeout=600,
            check=True,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert sorted(line.split()[0] for line in outputs[0].splitlines()) == sorted(RIGS)


# --- ROS 2 diagnostics: a mapper over a package, not a Source -----------------------------------

DIAGNOSTICS = TESTS / "fixtures" / "fleet_ops" / "diagnostics"


def _tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def test_diagnostics_map_offline_deterministically_and_leave_the_base_untouched() -> None:
    before = _tree(DIAGNOSTICS)
    mapping = load_mapping(DIAGNOSTICS / "mappings" / "vendor_legged.json")
    with no_sockets():
        runs = [
            map_diagnostics_files(
                read_package(DIAGNOSTICS / "packages" / "legged_patrol"), [mapping]
            )
            for _ in range(2)
        ]
    assert runs[0] == runs[1]
    assert _tree(DIAGNOSTICS) == before
    package = read_files(runs[0])
    base = read_package(DIAGNOSTICS / "packages" / "legged_patrol")
    # Identity: every mapped value cites bytes the base package's own ledger holds, at the same
    # revision, and the ledger is carried unchanged: the mapper reads no source of its own.
    ledger = {r.content_id for r in base.records if r.kind == "source_revision"}
    carried = [
        r.to_json() for r in package.records if r.kind in ("source_revision", "source_artifact")
    ]
    assert carried == [
        r.to_json() for r in base.records if r.kind in ("source_revision", "source_artifact")
    ]
    cited = {
        r.provenance.evidence.source
        for r in package.records
        if r.kind in ("structured_record", "structured_table", "timestamp_domain")
    }
    assert cited and cited <= ledger


# --- Citations: the report's cells backed by a connector's own tests -----------------------------

CITED: dict[str, tuple[str, ...]] = {
    # Open-RMF's hostile cases (MVL-156): the gate cites them rather than repeat them.
    "test_deploy_fleet_ops_rmf_hostile.py": (
        "test_paths_that_leave_the_directory_are_refused",
        "test_symlinks_are_not_followed_to_a_file_or_through_a_directory",
        "test_a_directory_and_a_fifo_are_not_regular_files",
        "test_a_file_over_its_limit_is_refused_before_it_is_read",
        "test_json_lines_are_read_and_a_damaged_line_fails_that_file",
        "test_something_that_is_not_a_database_or_is_cut_short_is_a_finding",
        "test_a_database_swapped_for_another_file_while_it_is_read_is_discarded",
        "test_a_view_that_never_ends_is_refused_before_it_runs",
        "test_a_read_that_does_too_much_work_is_stopped_by_work_not_by_the_clock",
        "test_a_view_is_never_read_so_its_giant_cell_is_never_built",
        "test_a_virtual_table_and_a_table_with_a_trigger_are_not_read_either",
        "test_a_cell_over_the_limit_stops_the_read_and_keeps_the_rows_before_it",
        "test_many_rows_that_exceed_the_budget_stop_it_with_the_rows_already_read",
        "test_one_budget_covers_every_part_read_from_one_database",
        "test_the_database_cannot_be_written_through_the_uri",
    ),
}


def test_every_cited_test_exists() -> None:
    for module, names in CITED.items():
        tree = ast.parse((TESTS / module).read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        assert set(names) <= defined, sorted(set(names) - defined)
