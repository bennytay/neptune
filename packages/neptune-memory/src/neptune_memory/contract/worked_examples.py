"""Run records of the compiler's worked examples, parsed strictly for the golden graph.

A compiler ``run`` record (package-schema v1) carries its id, its provenance (assertion kind and
one evidence ref), ``first`` and ``last`` timestamps and ``machine``, each a ``Knowledge`` state.
Only ``Known`` values are used; nothing else is read, and a malformed record raises
``ValueError`` (the golden builder turns that into a hard failure: golden inputs must be clean).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune_memory.schema.interval import OPEN, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import is_declared_value

if TYPE_CHECKING:
    from collections.abc import Iterator

    from neptune_memory.ledger import LedgerReader

RUN: Final = "run"


@dataclass(frozen=True)
class RunRecord:
    package_id: str
    record: RecordId
    assertion_kind: AssertionKind
    evidence: EvidenceRef
    first: Timestamp
    last: Timestamp | Open  # OPEN when the record states no end
    machine: LogicalId | None  # only when Known and a declared value
    machine_evidence: EvidenceRef | None

    @property
    def node(self) -> NodeRef:
        return run_node(self.record)


def run_node(record: RecordId) -> NodeRef:
    """A run without a declared logical id is keyed by its Ledger record: the log is the run."""
    return NodeRef(NodeType.RUN, f"record:{record}")


def machine_node(machine: LogicalId) -> NodeRef:
    return NodeRef(NodeType.MACHINE, f"{machine.namespace}:{machine.value}")


def _known(field: object) -> Mapping[str, object] | None:
    if not isinstance(field, Mapping):
        raise ValueError(f"expected a knowledge state, got {type(field).__name__}")
    return field if field.get("knowledge") == "known" else None


def _evidence(provenance: object) -> tuple[AssertionKind, EvidenceRef]:
    if not isinstance(provenance, Mapping):
        raise ValueError("provenance must be an object")
    kind = provenance.get("assertion_kind")
    return AssertionKind(str(kind)), evidence_ref_from_json(provenance.get("evidence"))  # type: ignore[arg-type]


def parse_run(package_id: str, record: Mapping[str, object]) -> RunRecord:
    rid = record.get("id")
    if not isinstance(rid, str):
        raise ValueError("run record has no id")
    kind, evidence = _evidence(record.get("provenance"))
    first = _known(record.get("first"))
    if first is None:
        raise ValueError(f"run {rid} states no first timestamp")
    last = _known(record.get("last"))
    machine = _known(record.get("machine"))
    machine_id = logical_id_from_json(machine["value"]) if machine is not None else None  # type: ignore[arg-type]
    if machine_id is not None and not is_declared_value(machine_id.value):
        machine_id = None
    machine_evidence = None
    if machine is not None and "provenance" in machine:
        machine_evidence = _evidence(machine["provenance"])[1]
    return RunRecord(
        package_id=package_id,
        record=parse_record_id(rid),
        assertion_kind=kind,
        evidence=evidence,
        first=timestamp_from_json(first["value"]),  # type: ignore[arg-type]
        last=OPEN if last is None else timestamp_from_json(last["value"]),  # type: ignore[arg-type]
        machine=machine_id,
        machine_evidence=machine_evidence if machine_id is not None else None,
    )


def runs(ledger: LedgerReader) -> Iterator[RunRecord]:
    """Every run record in every package, in package then file order."""
    for ref in ledger.list_packages():
        for record in ledger.read_records(ref.package_id, RUN) or ():
            yield parse_run(ref.package_id, record)
