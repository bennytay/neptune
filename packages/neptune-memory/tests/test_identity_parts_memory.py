"""Identity joins a part two sources name only on a person's assertion (ADR 0028 §4).

Real records from the acceptance corpus: work order WO-26-0911 installs the finger set the CMMS
calls ``serial:FS-0340`` (``fixtures/declared_cell3.records.jsonl``, read by ``memory.events``), and
the vendor's bulletin SB-2026-117 applies to "finger set FS-0340", which ``memory.procedures``
reads as ``vendor.part:FS-0340`` (``fixtures/procedures_sb117.records.jsonl``). The same text in two
namespaces is never joined by Memory: only a ``same_identity`` assertion naming both is a ground,
and then the ``same_as`` is stated and cites it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from memory_identity_records import assertion, ledger
from memory_run_records import NS
from neptune.model.assertion import AssertionType
from neptune.model.ids import LogicalId
from neptune.model.time import Epoch, Timescale
from neptune_memory.consolidate.base import rebuild
from neptune_memory.consolidate.event_records import resolve_config
from neptune_memory.consolidate.events import EventConsolidator
from neptune_memory.consolidate.identity import SAME_AS, IdentityConsolidator
from neptune_memory.consolidate.procedures import ProcedureConsolidator, procedure_config
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune_memory.consolidate.base import Consolidation

FIXTURES: Final = Path(__file__).parent / "fixtures"
CELL: Final[list[dict[str, Any]]] = [
    json.loads(line)
    for line in (FIXTURES / "declared_cell3.records.jsonl").read_text("utf-8").splitlines()
]
BULLETIN: Final[list[dict[str, Any]]] = [
    json.loads(line)
    for line in (FIXTURES / "procedures_sb117.records.jsonl").read_text("utf-8").splitlines()
]
DECLARATION: Final[dict[str, Any]] = {
    "documents": [
        {
            "name": "vendor service bulletin",
            "procedure": {"heading": 1},
            "effective": "Issued",
            "parts": {
                "label": "Applies to",
                "pattern": "finger set ([A-Z0-9][A-Z0-9-]*)",
                "namespace": "vendor.part",
            },
            "steps": "Procedure",
        }
    ]
}
CMMS_PART: Final = LogicalId("serial", "FS-0340")
VENDOR_PART: Final = LogicalId("vendor.part", "FS-0340")
CIVIL: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, NS)
AUTHORED: Final = CIVIL.at(1_789_478_400 * 1_000_000_000)  # 2026-09-15, any instant will do


def asset(part: LogicalId) -> NodeRef:
    return NodeRef(NodeType.ASSET, f"{part.namespace}:{part.value}")


def run(*statements: dict[str, object]) -> Consolidation:
    packages: dict[str, Sequence[dict[str, object]]] = {
        "cmms": CELL,
        "vendor": BULLETIN,
        "review": list(statements),
    }
    *_, identity = rebuild(
        ledger(packages),
        [
            (EventConsolidator(), resolve_config({})),
            (ProcedureConsolidator(), procedure_config(DECLARATION)),
            (IdentityConsolidator(), {}),
        ],
        recorded_at=ledger_tx(2),
    )
    return identity


def same_as(result: Consolidation) -> list[tuple[str, str]]:
    return sorted(
        (c.subject.node_id, c.object.node_id)  # type: ignore[union-attr]
        for c in result.claims
        if c.predicate == SAME_AS
    )


def test_one_text_in_two_namespaces_is_never_joined_without_a_statement() -> None:
    assert same_as(run()) == []


def test_a_same_identity_assertion_joins_the_installed_serial_and_the_bulletins_part() -> None:
    stated = assertion(
        "ASR-PART", AssertionType.SAME_IDENTITY, (CMMS_PART, VENDOR_PART), authored_at=AUTHORED
    )
    result = run(stated)
    assert same_as(result) == [("serial:FS-0340", "vendor.part:FS-0340")]
    (claim,) = [c for c in result.claims if c.predicate == SAME_AS]
    assert str(claim.assertion_kind) == "stated"
    assert stated["id"] in claim.provenance.records
    # No window is stated: it holds from where the work order names the serial, open-ended.
    assert claim.valid_to == OPEN
    assert not [f for f in result.findings if f.code == "identity.dangling_link"]


def test_a_part_nothing_involves_is_no_node_and_dangles() -> None:
    other = LogicalId("vendor.part", "FS-9999")
    result = run(
        assertion("ASR-NONE", AssertionType.SAME_IDENTITY, (CMMS_PART, other), authored_at=AUTHORED)
    )
    assert same_as(result) == []
    assert "identity.dangling_link" in {f.code for f in result.findings}


def test_a_distinct_identity_assertion_joins_nothing() -> None:
    result = run(
        assertion(
            "ASR-APART",
            AssertionType.DISTINCT_IDENTITY,
            (CMMS_PART, VENDOR_PART),
            authored_at=AUTHORED,
        )
    )
    assert same_as(result) == []


def test_the_join_is_deterministic_whatever_the_package_names() -> None:
    stated = assertion(
        "ASR-PART", AssertionType.SAME_IDENTITY, (VENDOR_PART, CMMS_PART), authored_at=AUTHORED
    )
    first = run(stated)
    *_, again = rebuild(
        ledger({"z": list(BULLETIN), "a": [stated], "m": list(reversed(CELL))}),
        [
            (EventConsolidator(), resolve_config({})),
            (ProcedureConsolidator(), procedure_config(DECLARATION)),
            (IdentityConsolidator(), {}),
        ],
        recorded_at=ledger_tx(2),
    )
    assert [c.id for c in first.claims if c.predicate == SAME_AS] == [
        c.id for c in again.claims if c.predicate == SAME_AS
    ]
