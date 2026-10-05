"""Compiler-shaped configuration records for tests, built with the compiler's own types.

Every lifecycle record, ``run``, snapshot and ``snapshot_binding`` here is constructed as the
compiler model class and serialised with its ``to_json``, so a test can never feed the
configuration lineage consolidator a shape the compiler would not write (root ADRs 0050 and 0051).
Configuration and run threads are ``ledger_thread`` stand-ins (ADR 0003 §1): a snapshot's thread
cites the snapshot's own evidence, as the Ledger keys anchored threads (ADR 0010 §1).

The real worked examples (``tests/fixtures/model/warehouse_amr`` and ``manipulator_cell``) are read
as their record lines, as the golden generator reads the others.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from memory_identity_records import TRANSFORM, cite, thread
from neptune.identity.provenance import evidence_record_id
from neptune.model.alignment import SnapshotBinding, SnapshotKind, ValidityWindow
from neptune.model.ids import LogicalId
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.lifecycle import (
    AuthorisationEnvelope,
    ChangeRecord,
    CommissioningBaseline,
    Decision,
    MaintenanceEvent,
    Quantity,
    RequalificationRecord,
)
from neptune.model.machine import HardwareConfiguration
from neptune.model.provenance import Provenance
from neptune.model.run import Run
from neptune_memory.schema.nodes import NodeType

if TYPE_CHECKING:
    from collections.abc import Sequence

    from neptune.model.ids import RecordId
    from neptune.model.knowledge import Knowledge
    from neptune.model.time import Timestamp

Record = dict[str, object]
STATED, OBSERVED = AssertionKind.STATED, AssertionKind.OBSERVED
EXAMPLES: Final = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "model"


def worked_example(name: str) -> list[Record]:
    """A compiler worked example's record lines, in file-name then line order."""
    return [
        json.loads(line)
        for path in sorted((EXAMPLES / name / "records").glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _stated(name: str) -> Provenance:
    return Provenance(cite(f"forms/{name}"), TRANSFORM.id, STATED)


def _id_of(kind: str, provenance: Provenance) -> RecordId:
    return evidence_record_id(kind, provenance.evidence, TRANSFORM)


def _ids(ids: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]]) -> object:
    if isinstance(ids, (list, tuple)):
        return Known(tuple(Known(i) for i in sorted(ids, key=lambda i: (i.namespace, i.value))))
    return ids


def _one(value: LogicalId | Knowledge[LogicalId] | None) -> Knowledge[LogicalId]:
    if value is None:
        return Unknown()
    return Known(value) if isinstance(value, LogicalId) else value


def _when(at: Timestamp | None) -> Knowledge[Timestamp]:
    return Known(at) if at is not None else Unknown()


_NO_DECISION: Final = Decision(Unknown(), Unknown(), Unknown())


def _common(
    name: str,
    machines: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]],
    configuration: LogicalId | Knowledge[LogicalId] | None,
    site: LogicalId | None,
) -> dict[str, Any]:
    return {
        "identifiers": Known(()),
        "site": _one(site) if site is not None else NotCovered(),
        "machines": _ids(machines),
        "configuration": _one(configuration),
        "related": Known(()),
    }


def commissioning(
    name: str,
    machines: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]],
    configuration: LogicalId | Knowledge[LogicalId] | None,
    at: Timestamp | None,
    site: LogicalId | None = None,
) -> Record:
    provenance = _stated(name)
    return CommissioningBaseline(
        id=_id_of("commissioning_baseline", provenance),
        provenance=provenance,
        **_common(name, machines, configuration, site),
        commissioned=_when(at),
        hardware=Known(()),
        software=Known(()),
        calibrations=Known(()),
        tests=Known(()),
        constraints=Known(()),
        sign_off=_NO_DECISION,
    ).to_json()  # type: ignore[return-value]


def maintenance(
    name: str,
    machines: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]],
    configuration: LogicalId | Knowledge[LogicalId] | None,
    at: Timestamp | None,
    site: LogicalId | None = None,
) -> Record:
    """A work order; ``configuration`` is the as-maintained configuration it states resulted."""
    provenance = _stated(name)
    return MaintenanceEvent(
        id=_id_of("maintenance_event", provenance),
        provenance=provenance,
        **_common(name, machines, configuration, site),
        performed=_when(at),
        diagnosis=Unknown(),
        actions=Known((Known(name),)),
        parts=Known(()),
    ).to_json()  # type: ignore[return-value]


def change(
    name: str,
    machines: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]],
    configuration: LogicalId | Knowledge[LogicalId] | None,
    at: Timestamp | None,
    site: LogicalId | None = None,
) -> Record:
    provenance = _stated(name)
    return ChangeRecord(
        id=_id_of("change_record", provenance),
        provenance=provenance,
        **_common(name, machines, configuration, site),
        changes=Known(()),
        approval=_NO_DECISION,
        effective=_when(at),
        rollback=NotCovered(),
    ).to_json()  # type: ignore[return-value]


def requalification(
    name: str,
    machines: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]],
    configuration: LogicalId | Knowledge[LogicalId] | None,
    at: Timestamp | None,
    site: LogicalId | None = None,
) -> Record:
    provenance = _stated(name)
    return RequalificationRecord(
        id=_id_of("requalification_record", provenance),
        provenance=provenance,
        **_common(name, machines, configuration, site),
        performed=_when(at),
        cause=Unknown(),
        corrective_actions=Known(()),
        tests=Known(()),
        result=Known("pass"),
        return_to_service=_NO_DECISION,
    ).to_json()  # type: ignore[return-value]


def envelope(
    name: str,
    site: LogicalId | None,
    configuration: LogicalId | Knowledge[LogicalId] | None,
    valid_from: Timestamp | None,
    valid_until: Timestamp | None = None,
    machines: Sequence[LogicalId] = (),
) -> Record:
    provenance = _stated(name)
    unknown = Quantity(Unknown(), Unknown())
    return AuthorisationEnvelope(
        id=_id_of("authorisation_envelope", provenance),
        provenance=provenance,
        **_common(name, machines, configuration, site),
        missions=Known(()),
        payload_min=unknown,
        payload_max=unknown,
        zones=Known(()),
        supervision=Unknown(),
        dependencies=Known(()),
        valid_from=_when(valid_from),
        valid_until=_when(valid_until),
        approval=_NO_DECISION,
    ).to_json()  # type: ignore[return-value]


# --- Runs, snapshots and bindings ---------------------------------------------------------------


def _observed(name: str) -> Provenance:
    return Provenance(cite(name), TRANSFORM.id, OBSERVED)


def run(
    name: str,
    logical_id: LogicalId | None = None,
    first: Timestamp | None = None,
    last: Timestamp | None = None,
    machine: LogicalId | None = None,
) -> Record:
    """A recording ``name``; without ``logical_id`` its thread is anchored on ``cite(name)``."""
    provenance = _observed(name)
    return Run(
        id=_id_of("run", provenance),
        provenance=provenance,
        logical_id=_one(logical_id),
        machine=_one(machine),
        first=_when(first),
        last=_when(last),
    ).to_json()  # type: ignore[return-value]


def hardware(name: str, machine: LogicalId | None = None) -> Record:
    """A hardware configuration declared by source ``name`` (a URDF, a manifest entry)."""
    provenance = _observed(name)
    return HardwareConfiguration(
        id=_id_of("hardware_configuration", provenance),
        provenance=provenance,
        machine=_one(machine) if machine is not None else NotCovered(),
        name=Known(name),
        revision=Unknown(),
    ).to_json()  # type: ignore[return-value]


def binding(
    name: str,
    run_record: Record,
    snapshot: Record,
    *,
    start: Timestamp | None = None,
    end: Timestamp | None = None,
    clock: RecordId | None = None,
    kind: AssertionKind = OBSERVED,
) -> Record:
    """A ``SnapshotBinding``; a window when ``clock`` is given, else ``NotCovered`` (whole run)."""
    provenance = Provenance(cite(f"bindings/{name}"), TRANSFORM.id, kind)
    validity: Knowledge[ValidityWindow] = (
        Known(ValidityWindow(clock, _when(start), _when(end)))
        if clock is not None
        else NotCovered()
    )
    return SnapshotBinding(
        id=_id_of("snapshot_binding", provenance),
        provenance=provenance,
        run=run_record["id"],  # type: ignore[arg-type]
        snapshot=snapshot["id"],  # type: ignore[arg-type]
        snapshot_kind=SnapshotKind(str(snapshot["kind"])),
        validity=validity,
    ).to_json()  # type: ignore[return-value]


# --- Threads ------------------------------------------------------------------------------------


def configuration_thread(node: LogicalId, *sources: str) -> Record:
    """A configuration thread; each source is a snapshot it is anchored on (``cite(source)``)."""
    return thread(node, *(sources or (f"threads/{node.value}",)), node_type=NodeType.CONFIGURATION)


def threads(node_type: NodeType, *nodes: LogicalId) -> list[Record]:
    return [thread(node, f"threads/{node.value}", node_type=node_type) for node in nodes]


def run_thread(node: LogicalId | None, source: str, start: Timestamp | None = None) -> Record:
    """A run thread: declared by ``node``, or anchored on ``cite(source)`` with a stand-in id."""
    return thread(node or LogicalId("anchor", source), source, node_type=NodeType.RUN, start=start)
