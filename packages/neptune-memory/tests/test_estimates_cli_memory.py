"""``memory rebuild|consolidate --with-estimates`` (ADR 0017): the compiler's estimated clock
mappings join a tenant's graph only when asked, only as ``inferred`` claims, and a graph built with
them is not extended without them."""

from __future__ import annotations

import io
import json
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

from memory_time_records import MICRO, MILLI, domain, estimate
from neptune.identity import canonical_json
from neptune.model.time import Epoch, Timescale
from neptune_memory.cli import OK, REFUSED, main
from neptune_memory.derived.clocks import ESTIMATES_CONSOLIDATOR_ID
from neptune_memory.ledger import ExportedPackage, LedgerExport
from neptune_memory.schema.codec import graph_from_json

if TYPE_CHECKING:
    from pathlib import Path

ROBOT_CELL: Final = [  # a cell PC's boot clock fitted to the controller's GPS-disciplined clock
    domain("ipc boot", MICRO),
    domain("controller gps", MILLI, timescale=Timescale.GPS, epoch=Epoch.GPS),
    estimate(
        "fit ipc-controller",
        "ipc boot",
        "controller gps",
        anchor=(20_000_000, 1_400_000_008_001),
        rate=Fraction(1, 1000),
        start=12_000_000,
        end=900_000_001,
    ),
]
QUIET: Final = [domain("hmi clock", MICRO)]


def export(tmp_path: Path) -> Path:
    packages = (
        ExportedPackage("cell-3", 6, 1, tuple(ROBOT_CELL)),
        ExportedPackage("hmi", 6, 2, tuple(QUIET)),
    )
    path = tmp_path / "ledger.json"
    path.write_bytes(canonical_json.dumps(LedgerExport(2, "stub", packages).to_json()))  # type: ignore[arg-type]
    return path


def memory(tmp_path: Path, *argv: str) -> int:
    base = ["--graphs", str(tmp_path / "graphs"), "--tenant", "t"]
    return main([*base, *argv], stdout=io.StringIO(), stderr=io.StringIO())


def graph(tmp_path: Path) -> Any:
    return graph_from_json(json.loads((tmp_path / "graphs" / "t" / "graph.json").read_text()))


def test_without_the_flag_no_estimate_is_relayed(tmp_path: Path) -> None:
    ledger = str(export(tmp_path))
    assert memory(tmp_path, "rebuild", "--ledger", ledger, "--snapshot", "1") == OK
    document = graph(tmp_path)
    assert ESTIMATES_CONSOLIDATOR_ID not in {b.consolidator_id for b in document.builds}
    assert all(str(c.assertion_kind) != "inferred" for c in document.resolution.claims)


def test_with_the_flag_estimates_are_inferred_claims_of_the_derived_consolidator(
    tmp_path: Path,
) -> None:
    ledger = str(export(tmp_path))
    argv = ("rebuild", "--ledger", ledger, "--snapshot", "1", "--with-estimates")
    assert memory(tmp_path, *argv) == OK
    document = graph(tmp_path)
    assert ESTIMATES_CONSOLIDATOR_ID in {b.consolidator_id for b in document.builds}
    relayed = [
        c
        for c in document.resolution.claims
        if c.provenance.consolidator_id == ESTIMATES_CONSOLIDATOR_ID
    ]
    assert {c.predicate for c in relayed} == {"clock_map", "maps_to"}
    assert all(str(c.assertion_kind) == "inferred" for c in relayed)
    others = [c for c in document.resolution.claims if c not in relayed]
    assert all(str(c.assertion_kind) != "inferred" for c in others)


def test_a_graph_built_with_estimates_is_extended_only_with_them(tmp_path: Path) -> None:
    ledger = str(export(tmp_path))
    assert (
        memory(tmp_path, "consolidate", "--ledger", ledger, "--snapshot", "1", "--with-estimates")
        == OK
    )
    assert memory(tmp_path, "consolidate", "--ledger", ledger, "--snapshot", "2") == REFUSED
    argv = ("consolidate", "--ledger", ledger, "--snapshot", "2", "--with-estimates")
    assert memory(tmp_path, *argv) == OK
    assert graph(tmp_path).head == 2
