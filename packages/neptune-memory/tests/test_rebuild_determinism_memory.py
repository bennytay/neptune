"""Memory's first guarantee (``docs/guarantees.md``, ADR 0016): the same Ledger snapshot and the
same consolidator set give the same graph, byte for byte.

Over the archetype Ledger (``memory_archetype_ledger``: six embodiments, three registration
transactions, every G2 consolidator emitting):

- two consolidations from scratch, with the consolidators registered in shuffled orders, give
  byte-identical graph documents, dumps and snapshot records, and equal snapshot ids; so do two
  ``memory rebuild`` processes under different hash seeds;
- incremental consolidation (each new registration consolidated onto the graph) holds the same
  ``MemorySnapshot`` and, as of the head, the same claims as a full rebuild; what the later builds
  no longer emit is withdrawn, never deleted.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from random import Random
from typing import TYPE_CHECKING, Any, Final

import pytest

from memory_archetype_ledger import ARCHETYPE, packages_at
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune_memory.cli import OK, REFUSED, USAGE, main
from neptune_memory.consolidate.base import ConsolidatorOutput
from neptune_memory.consolidate.snapshot import (
    GraphExtendError,
    PlanError,
    Registration,
    consolidate,
    default_registrations,
    extend,
    plan,
)
from neptune_memory.ledger import (
    ExportedPackage,
    LedgerExport,
    StubLedger,
    ledger_export_from_json,
)
from neptune_memory.schema.codec import graph_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.supersede import as_of, is_closure

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim, ClaimId

HEAD: Final = max(ARCHETYPE)
CONSOLIDATORS: Final = (
    "memory.calibration",
    "memory.configuration",
    "memory.coverage",
    "memory.episodes",
    "memory.events",
    "memory.identity",
    "memory.runs",
    "memory.time",
)


def ledger(snapshot: int = HEAD) -> StubLedger:
    return StubLedger({pid: (1, records) for pid, records in packages_at(snapshot).items()})


def export() -> LedgerExport:
    landed = sorted((pid, tx, recs) for tx, pkgs in ARCHETYPE.items() for pid, recs in pkgs.items())
    packages = tuple(ExportedPackage(pid, 1, tx, tuple(recs)) for pid, tx, recs in landed)
    return LedgerExport(HEAD, "stub", packages)


@pytest.fixture(scope="module")
def ledger_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ledger") / "ledger.json"
    path.write_bytes(canonical_json.dumps(export().to_json()))  # type: ignore[arg-type]
    return path


def shuffled(seed: int) -> list[Registration]:
    registrations = list(default_registrations())
    Random(seed).shuffle(registrations)
    return registrations


def from_scratch(registrations: Sequence[Registration], snapshot: int = HEAD) -> bytes:
    run = consolidate(ledger(snapshot), registrations, snapshot)
    document = extend(None, run)
    return canonical_json.dumps({"graph": document.to_json(), "snapshot": run.snapshot.to_json()})


def cli(*argv: str | Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = main([str(a) for a in argv], stdout=out, stderr=err)
    return status, out.getvalue(), err.getvalue()


# --- The guarantee -------------------------------------------------------------------------------


def test_every_registered_consolidator_emits_over_the_archetype_ledger() -> None:
    run = consolidate(ledger(), default_registrations(), HEAD)
    assert [r.transform.consolidator_id for r in run.consolidations] == [
        "memory.configuration",
        "memory.calibration",
        "memory.coverage",
        "memory.events",
        "memory.identity",
        "memory.runs",
        "memory.episodes",
        "memory.time",
    ]
    assert all(r.claims for r in run.consolidations)
    assert not [
        f.code
        for r in run.consolidations
        for f in r.findings
        if f.code.startswith("consolidate.")  # the runner's own: a crash or a refused draft
    ]


@pytest.mark.parametrize("seeds", [(0, 1), (2, 3), (4, 5)])
def test_shuffled_registration_order_gives_a_byte_identical_graph(seeds: tuple[int, int]) -> None:
    first, second = (shuffled(seed) for seed in seeds)
    assert [r.consolidator_id for r in first] != [r.consolidator_id for r in second]
    assert from_scratch(first) == from_scratch(second)


def test_two_rebuild_processes_under_different_hash_seeds_dump_identical_bytes(
    ledger_file: Path, tmp_path: Path
) -> None:
    """No env-dependent ordering: set and dict iteration order varies with ``PYTHONHASHSEED``."""
    outputs = []
    for seed in ("0", "4242"):
        graphs = tmp_path / f"seed-{seed}"
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "TZ": {"0": "UTC", "4242": "Pacific/Chatham"}[seed],
        }
        for argv in (
            ["rebuild", "--ledger", str(ledger_file), "--snapshot", str(HEAD)],
            ["dump", "--out", str(graphs / "dump.jsonl")],
        ):
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "neptune_memory.cli",
                    "--graphs",
                    str(graphs),
                    "--tenant",
                    "fleet",
                    *argv,
                ],
                check=True,
                env=env,
                capture_output=True,
            )
        outputs.append(
            [
                (graphs / "dump.jsonl").read_bytes(),
                (graphs / "fleet" / "graph.json").read_bytes(),
                (graphs / "fleet" / "snapshots" / f"{HEAD}.json").read_bytes(),
            ]
        )
    assert outputs[0] == outputs[1]
    assert outputs[0][0].count(b"\n") > 300


def test_incremental_consolidation_holds_the_same_graph_as_a_full_rebuild(
    ledger_file: Path, tmp_path: Path
) -> None:
    for snapshot in sorted(ARCHETYPE):
        assert (
            cli(
                "--graphs",
                tmp_path,
                "--tenant",
                "inc",
                "consolidate",
                "--ledger",
                ledger_file,
                "--snapshot",
                str(snapshot),
            )[0]
            == OK
        )
    status, full_snapshot, _ = cli(
        "--graphs",
        tmp_path,
        "--tenant",
        "full",
        "rebuild",
        "--ledger",
        ledger_file,
        "--snapshot",
        str(HEAD),
    )
    assert status == OK
    written = (tmp_path / "inc" / "snapshots" / f"{HEAD}.json").read_bytes()
    record = canonical_json.loads(written.rstrip(b"\n"))
    assert isinstance(record, dict)
    assert canonical_json.dumps(record["snapshot"]) + b"\n" == full_snapshot.encode()
    current = {}
    for tenant in ("inc", "full"):
        status, _, _ = cli(
            "--graphs",
            tmp_path,
            "--tenant",
            tenant,
            "dump",
            "--as-of",
            str(HEAD),
            "--out",
            tmp_path / f"{tenant}.jsonl",
        )
        assert status == OK
        current[tenant] = [
            canonical_json.loads(line)
            for line in (tmp_path / f"{tenant}.jsonl").read_bytes().splitlines()
        ]
    content = {
        tenant: sorted(
            canonical_json.dumps({k: v for k, v in claim.items() if k != "recorded_at"})  # type: ignore[union-attr]
            for claim in claims
        )
        for tenant, claims in current.items()
    }
    assert content["inc"] == content["full"] and len(content["inc"]) > 300


def test_what_a_later_build_no_longer_emits_is_withdrawn_never_deleted() -> None:
    document = None
    emitted: dict[int, set[ClaimId]] = {}
    for snapshot in sorted(ARCHETYPE):
        run = consolidate(ledger(snapshot), default_registrations(), snapshot)
        document = extend(document, run)
        emitted[snapshot] = {c.id for c in run.claims}
    assert document is not None
    history = {c.id: c for c in document.resolution.claims}
    gone = emitted[1] - emitted[HEAD]
    assert gone, "the archetype withdraws something between its first and last snapshot"
    for claim_id in gone:
        assert claim_id in history and not history[claim_id].is_current
    head = {c.id for c in as_of(document.resolution, ledger_tx(HEAD)).claims if not is_closure(c)}
    assert head <= emitted[HEAD]
    retracted = [
        c for c in history.values() if c.predicate == "same_as" and c.id in gone
    ]  # the humanoid confirmation the operator retracted at tx 3
    assert [c.superseded_at for c in retracted] == [HEAD]


def test_consolidating_the_head_again_changes_nothing(ledger_file: Path, tmp_path: Path) -> None:
    argv = (
        "--graphs",
        tmp_path,
        "--tenant",
        "t",
        "consolidate",
        "--ledger",
        ledger_file,
        "--snapshot",
        "1",
    )
    assert cli(*argv)[0] == OK
    before = {p.name: p.read_bytes() for p in (tmp_path / "t").rglob("*.json")}
    assert cli(*argv)[0] == OK
    assert {p.name: p.read_bytes() for p in (tmp_path / "t").rglob("*.json")} == before


# --- The MemorySnapshot -------------------------------------------------------------------------


def test_the_snapshot_records_the_consolidator_set_and_the_claim_set_hash() -> None:
    run = consolidate(ledger(2), shuffled(9), 2)
    snapshot: dict[str, Any] = dict(run.snapshot.to_json())
    assert snapshot["kind"] == "memory.snapshot" and snapshot["ledger_snapshot"] == 2
    entries = snapshot["consolidators"]
    assert isinstance(entries, list)
    assert sorted(e["consolidator_id"] for e in entries) == list(CONSOLIDATORS)
    for entry in entries:
        assert set(entry) >= {"consolidator_id", "version", "config_hash", "after", "priority"}
    claims = run.claims
    assert snapshot["claim_count"] == len(claims) == sum(len(r.claims) for r in run.consolidations)
    expected = content_id(
        canonical_json.dumps([{"claim": c.content_json(), "id": c.id} for c in claims])
    )
    assert snapshot["claims_hash"] == expected
    assert [p["package_id"] for p in snapshot["packages"]] == sorted(packages_at(2))
    body = {k: v for k, v in snapshot.items() if k != "id"}
    assert snapshot["id"] == content_id(canonical_json.dumps(body))


def test_a_different_config_is_a_different_snapshot() -> None:
    registrations = [
        Registration(r.consolidator, {"co_occurrence": {"window_seconds": "9", "max_partners": 64}})
        if r.consolidator_id == "memory.events"
        else r
        for r in default_registrations()
    ]
    a = consolidate(ledger(), default_registrations(), HEAD).snapshot
    b = consolidate(ledger(), registrations, HEAD).snapshot
    assert a.id != b.id


# --- The plan -----------------------------------------------------------------------------------


class Spy:
    """Records the consolidator ids of the claims it is given as ``previous``."""

    model = None
    version = "1"

    def __init__(self, consolidator_id: str) -> None:
        self.consolidator_id = consolidator_id
        self.seen: set[str] = set()

    def consolidate(
        self, ledger: LedgerReader, previous: Sequence[Claim], config: Mapping[str, JsonValue]
    ) -> ConsolidatorOutput:
        self.seen = {c.provenance.consolidator_id for c in previous}
        return ConsolidatorOutput()


def test_a_consolidator_sees_only_the_claims_of_what_it_reads() -> None:
    reads_runs, reads_nothing = Spy("test.reads-runs"), Spy("test.zzz")
    registrations = [
        *default_registrations(),
        Registration(reads_runs, after=("memory.episodes",)),
        Registration(reads_nothing),
    ]
    consolidate(ledger(), registrations, HEAD)
    assert reads_runs.seen == {"memory.episodes", "memory.runs"}  # transitively
    assert reads_nothing.seen == set()


@pytest.mark.parametrize(
    ("after", "message"),
    [
        ({"a": ("b",), "b": ("a",)}, "cycle"),
        ({"a": ("a",)}, "cycle"),
        ({"a": ("nope",)}, "not registered"),
    ],
)
def test_plans_that_cannot_be_ordered_are_refused(
    after: dict[str, tuple[str, ...]], message: str
) -> None:
    registrations = [
        Registration(Spy(f"test.{cid}"), after=tuple(f"test.{a}" for a in deps))
        for cid, deps in after.items()
    ]
    with pytest.raises(PlanError, match=message):
        plan(registrations)


def test_a_consolidator_registered_twice_or_as_the_resolver_is_refused() -> None:
    with pytest.raises(PlanError, match="twice"):
        plan([Registration(Spy("test.a")), Registration(Spy("test.a"))])
    with pytest.raises(PlanError, match="reserved"):
        plan([Registration(Spy("memory.supersede"))])


def test_dropping_a_consolidator_takes_a_rebuild() -> None:
    first = extend(None, consolidate(ledger(1), default_registrations(), 1))
    fewer = [r for r in default_registrations() if r.consolidator_id != "memory.coverage"]
    with pytest.raises(GraphExtendError, match=r"memory\.coverage"):
        extend(first, consolidate(ledger(2), fewer, 2))


def test_adding_a_consolidator_extends_the_graph_under_a_new_generation() -> None:
    fewer = [r for r in default_registrations() if r.consolidator_id != "memory.coverage"]
    first = extend(None, consolidate(ledger(1), fewer, 1))
    second = extend(first, consolidate(ledger(2), default_registrations(), 2))
    assert first.generation != second.generation
    assert {b.consolidator_id for b in second.builds} == set(CONSOLIDATORS)


# --- Hostile input ------------------------------------------------------------------------------


def _export_with(change: Any) -> JsonValue:
    data: Any = canonical_json.loads(canonical_json.dumps(export().to_json()))  # type: ignore[arg-type]
    change(data)
    return data  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(kind="memory.graph"),
        lambda d: d.update(head=0),
        lambda d: d.update(head=True),
        lambda d: d.update(extra=1),
        lambda d: d["packages"].reverse(),
        lambda d: d["packages"].append(d["packages"][0]),
        lambda d: d["packages"][0].update(registered_at=99),
        lambda d: d["packages"][0].update(package_id=""),
        lambda d: d["packages"][0].update(package_id="bad\nid"),
        lambda d: d["packages"][0].update(records={}),
        lambda d: d["packages"][0]["records"].append({"no": "kind"}),
        lambda d: d["packages"][0]["records"].append([]),
        lambda d: d["packages"][0].pop("schema_version"),
        lambda d: d.update(catalog_api_version=""),
    ],
)
def test_a_malformed_ledger_export_is_refused(change: Any) -> None:
    with pytest.raises(ValueError):
        ledger_export_from_json(_export_with(change))


def test_the_ledger_export_round_trips_and_snapshots_are_cumulative() -> None:
    data = export().to_json()
    assert ledger_export_from_json(data) == export()
    for snapshot in sorted(ARCHETYPE):
        listed = [p.package_id for p in export().at(snapshot).list_packages()]
        assert listed == sorted(packages_at(snapshot))
    for bad in (0, HEAD + 1, -1):
        with pytest.raises(ValueError):
            export().at(bad)


def test_cli_refusals_and_usage_errors(ledger_file: Path, tmp_path: Path) -> None:
    def run(*argv: str | Path) -> int:
        return cli("--graphs", tmp_path, *argv)[0]

    consolidate_at = ("consolidate", "--ledger", ledger_file, "--snapshot")
    assert run("--tenant", "t", *consolidate_at, "2") == OK
    assert run("--tenant", "t", *consolidate_at, "1") == REFUSED  # not after the head
    assert run("--tenant", "t", *consolidate_at, str(HEAD + 1)) == USAGE  # beyond the export
    assert run("--tenant", "t", *consolidate_at, "0") == USAGE
    assert run("--tenant", "nobody", "dump") == REFUSED
    assert run("--tenant", "t", "dump", "--as-of", "9") == REFUSED
    assert run("--tenant", "../t", "dump") == REFUSED
    assert run("--tenant", "T", "dump") == REFUSED
    assert (
        run(
            "--tenant", "t", "consolidate", "--ledger", tmp_path / "missing.json", "--snapshot", "3"
        )
        == USAGE
    )
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    assert run("--tenant", "t", "consolidate", "--ledger", garbage, "--snapshot", "3") == USAGE
    assert run("--tenant", "t", "frobnicate") == USAGE


def test_a_rebuild_refuses_a_tenant_directory_it_did_not_write(
    ledger_file: Path, tmp_path: Path
) -> None:
    rebuild = (
        "--graphs",
        tmp_path,
        "--tenant",
        "t",
        "rebuild",
        "--ledger",
        ledger_file,
        "--snapshot",
        "1",
    )
    assert cli(*rebuild)[0] == OK
    (tmp_path / "t" / "notes.txt").write_text("mine", encoding="utf-8")
    assert cli(*rebuild)[0] == REFUSED
    assert (tmp_path / "t" / "notes.txt").exists() and (tmp_path / "t" / "graph.json").exists()


def test_a_symlinked_tenant_is_refused(ledger_file: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "graphs").mkdir()
    (tmp_path / "graphs" / "t").symlink_to(elsewhere)
    status = cli(
        "--graphs",
        tmp_path / "graphs",
        "--tenant",
        "t",
        "consolidate",
        "--ledger",
        ledger_file,
        "--snapshot",
        "1",
    )[0]
    assert status == REFUSED and not list(elsewhere.iterdir())


def test_an_interrupted_write_is_ignored_and_dropped(ledger_file: Path, tmp_path: Path) -> None:
    rebuild = (
        "--graphs",
        tmp_path,
        "--tenant",
        "t",
        "rebuild",
        "--ledger",
        ledger_file,
        "--snapshot",
        "1",
    )
    assert cli(*rebuild)[0] == OK
    (tmp_path / "t" / "snapshots" / "2.json.tmp").write_text("{", encoding="utf-8")
    (tmp_path / "t" / "graph.json.tmp").write_text("{", encoding="utf-8")
    assert cli("--graphs", tmp_path, "--tenant", "t", "dump", "--out", tmp_path / "d")[0] == OK
    assert cli(*rebuild)[0] == OK
    assert sorted(p.name for p in (tmp_path / "t").rglob("*")) == [
        "1.json",
        "graph.json",
        "snapshots",
    ]


def test_the_graph_file_is_a_strict_graph_document(ledger_file: Path, tmp_path: Path) -> None:
    assert (
        cli(
            "--graphs",
            tmp_path,
            "--tenant",
            "t",
            "rebuild",
            "--ledger",
            ledger_file,
            "--snapshot",
            "1",
        )[0]
        == OK
    )
    path = tmp_path / "t" / "graph.json"
    document = graph_from_json(canonical_json.loads(path.read_bytes().rstrip(b"\n")))
    assert document.head == 1 and document.builds
    path.write_text('{"kind": "memory.graph"}\n', encoding="utf-8")
    assert cli("--graphs", tmp_path, "--tenant", "t", "dump")[0] == REFUSED


# --- Partial success -----------------------------------------------------------------------------


class Crashing:
    """A consolidator that fails on this snapshot, standing in for one hostile record."""

    consolidator_id = "memory.runs"
    version = "1"
    model = None

    def consolidate(
        self, ledger: LedgerReader, previous: Sequence[Claim], config: Mapping[str, JsonValue]
    ) -> ConsolidatorOutput:
        raise RuntimeError("a malformed record")


def test_a_consolidator_that_crashes_withdraws_nothing_and_its_readers_do_not_run() -> None:
    first = extend(None, consolidate(ledger(1), default_registrations(), 1))
    crashing = [
        Registration(Crashing()) if r.consolidator_id == "memory.runs" else r
        for r in default_registrations()
    ]
    run = consolidate(ledger(2), crashing, 2)
    by_id = {r.transform.consolidator_id: r for r in run.consolidations}
    assert [f.code for f in by_id["memory.runs"].findings] == ["consolidate.failed"]
    assert [f.code for f in by_id["memory.episodes"].findings] == ["consolidate.dependency_failed"]
    entries = {e.transform["consolidator_id"]: e for e in run.snapshot.consolidators}
    assert not entries["memory.runs"].complete and not entries["memory.episodes"].complete
    assert entries["memory.identity"].complete
    second = extend(first, run)
    assert {b.consolidator_id for b in second.builds if b.recorded_at == 2} == set(
        CONSOLIDATORS
    ) - {"memory.runs", "memory.episodes"}
    held = {
        c.id
        for c in as_of(first.resolution, ledger_tx(1)).claims
        if c.provenance.consolidator_id in {"memory.runs", "memory.episodes"}
    }
    now = {c.id for c in as_of(second.resolution, ledger_tx(2)).claims}
    assert held and held <= now  # nothing of theirs withdrawn by a run that did not complete


def test_a_deeply_nested_ledger_export_is_a_usage_error(tmp_path: Path) -> None:
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    status, _, err = cli(
        "--graphs", tmp_path, "--tenant", "t", "consolidate", "--ledger", deep, "--snapshot", "1"
    )
    assert status == USAGE and "nested" in err


def test_a_stale_later_record_does_not_block_reconsolidating_the_head(
    ledger_file: Path, tmp_path: Path
) -> None:
    """A crash between a snapshot record and its graph leaves a record past the head."""
    argv = (
        "--graphs",
        tmp_path,
        "--tenant",
        "t",
        "consolidate",
        "--ledger",
        ledger_file,
        "--snapshot",
        "1",
    )
    assert cli(*argv)[0] == OK
    later = tmp_path / "t" / "snapshots" / "2.json"
    later.write_bytes((tmp_path / "t" / "snapshots" / "1.json").read_bytes())
    graph = (tmp_path / "t" / "graph.json").read_bytes()
    assert cli(*argv)[0] == OK
    assert (tmp_path / "t" / "graph.json").read_bytes() == graph


def test_a_corrupt_snapshot_record_is_refused(ledger_file: Path, tmp_path: Path) -> None:
    argv = (
        "--graphs",
        tmp_path,
        "--tenant",
        "t",
        "consolidate",
        "--ledger",
        ledger_file,
        "--snapshot",
        "1",
    )
    assert cli(*argv)[0] == OK
    (tmp_path / "t" / "snapshots" / "1.json").write_text("{", encoding="utf-8")
    assert cli(*argv)[0] == REFUSED
