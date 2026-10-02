"""Open-RMF tasks as ``Run`` declarations, and lane and zone maps as spatial records (ADR 0010 §5).

Tasks. A task log item declares a task: its id, the robot it was assigned to, when it started and
finished. Each item with a task id becomes one ``Run``, ``stated`` by that item (the compiler has
no ``Task`` kind: ADR 0010, compiler gap, and a ``Run`` is a session one piece of evidence
declares, which a task record does):

- ``logical_id`` is ``rmf.task`` over the id the item states (by default ``/booking/id``);
- ``machine`` is ``rmf.robot`` over ``<group>/<name>`` of ``/assigned_to`` (the fleet and the
  robot, as stated), or the name alone where no group is stated. It is never matched to another
  system's name for the robot;
- ``first`` and ``last`` are ``unix_millis_start_time`` and ``unix_millis_finish_time``, each on
  the clock of its own field (a ``TimestampDomain`` whose epoch, timescale and resolution are
  ``Unknown`` unless declared). Two fields are two clocks; nothing here compares them, or either
  with another system's clock.

Maps. A map document states levels with their vertices, lanes and zones. Each level is one
``SpatialArtifact`` (``vector_map``) citing the level's item, in a ``Frame`` named by the level as
the file names it, in one ``FrameGraph`` for the document. The geometry stays in the document's
bytes: no coordinate is moved, converted or projected, and the unit and CRS are ``NotCovered``.
The frame's axes and handedness are ``Unknown``: the file does not say.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from neptune.identity.provenance import evidence_record_id
from neptune.model.frames import FrameRef
from neptune.model.ids import LogicalId
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Known, NotCovered, Unknown
from neptune.model.provenance import Provenance, TransformRecord
from neptune.model.reference import Frame, FrameGraph, TimestampDomain
from neptune.model.run import Run
from neptune.model.time import Timestamp
from neptune.model.world import SpatialArtifact, SpatialCategory
from neptune_deploy.sources.fleet_ops.documents import Document, cite, stated

TASK_NAMESPACE: Final = "rmf.task"
ROBOT_NAMESPACE: Final = "rmf.robot"
DEFAULT_TASK_FIELDS: Final = {
    "id": "/booking/id",
    "group": "/assigned_to/group",
    "robot": "/assigned_to/name",
    "start": "unix_millis_start_time",
    "finish": "unix_millis_finish_time",
}
MAX_ID_BYTES: Final = 256
Report = Callable[[str, Any, dict[str, JsonValue]], None]


def at(item: Mapping[str, JsonValue], pointer: str) -> "JsonValue | None":
    """The value at an RFC 6901 pointer into ``item``, or ``None`` if the path is not there."""
    if pointer == "":
        return item
    node: JsonValue = item
    for raw in pointer.split("/")[1:]:
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isascii() and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None
    return node


def _usable(value: "JsonValue | None") -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8", "replace")) > MAX_ID_BYTES
    ):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return value


def _parts(pointer: str) -> list[str | int]:
    return [raw.replace("~1", "/").replace("~0", "~") for raw in pointer.split("/")[1:]]


def build_runs(
    document: Document,
    transform: TransformRecord,
    fields: Mapping[str, str],
    clocks: Mapping[str, TimestampDomain],
    report: Report,
) -> list[Run]:
    """One ``Run`` per task item that states an id, in item order."""
    runs: list[Run] = []
    skipped = 0
    for index, item in enumerate(document.items):
        task = _usable(at(item, fields["id"]))
        if task is None:
            skipped += 1
            continue
        where = cite(document, "items", index)
        group = _usable(at(item, fields["group"]))
        robot = _usable(at(item, fields["robot"]))
        if robot is None:
            machine: Any = Unknown(stated(document, transform, "items", index))
        else:
            name = f"{group}/{robot}" if group is not None else robot
            machine = Known(
                LogicalId(ROBOT_NAMESPACE, name),
                stated(document, transform, "items", index, *_parts(fields["robot"])),
            )
        times: dict[str, Any] = {}
        for role in ("start", "finish"):
            key = fields[role]
            value = item.get(key)
            domain = clocks.get(key)
            if domain is None or isinstance(value, bool) or not isinstance(value, int):
                times[role] = Unknown(stated(document, transform, "items", index, key))
                continue
            try:
                times[role] = Known(
                    Timestamp(value, domain.id), stated(document, transform, "items", index, key)
                )
            except ValueError:
                report("value_unreadable", cite(document, "items", index, key), {"field": key})
                times[role] = Unknown(stated(document, transform, "items", index, key))
        runs.append(
            Run(
                id=evidence_record_id(Run.kind, where, transform),
                provenance=stated(document, transform, "items", index),
                logical_id=Known(
                    LogicalId(TASK_NAMESPACE, task),
                    stated(document, transform, "items", index, *_parts(fields["id"])),
                ),
                machine=machine,
                first=times["start"],
                last=times["finish"],
            )
        )
    if skipped:
        report(
            "record_skipped", cite(document, "items"), {"count": skipped, "reason": "id_invalid"}
        )
    return runs


def build_map(
    document: Document, transform: TransformRecord, report: Report
) -> list[FrameGraph | Frame | SpatialArtifact]:
    """A ``FrameGraph`` for the document, then a ``Frame`` and a ``SpatialArtifact`` per level.

    A level item is ``{"level": <name>, "data": <the level as the file states it>, ...}``; one with
    no usable name has no frame to be in and builds nothing (a finding).
    """
    graph_where = cite(document, "items")
    graph = FrameGraph(
        id=evidence_record_id(FrameGraph.kind, graph_where, transform),
        provenance=Provenance(graph_where, transform.id, AssertionKind.STATED),
        scope=(),
    )
    records: list[FrameGraph | Frame | SpatialArtifact] = [graph]
    skipped = 0
    for index, item in enumerate(document.items):
        level = _usable(item.get("level"))
        if level is None:
            skipped += 1
            continue
        ref = FrameRef(level, graph.id)
        where = cite(document, "items", index)
        name = stated(document, transform, "items", index, "level")
        records.append(
            Frame(
                id=evidence_record_id(Frame.kind, where, transform),
                provenance=name,
                ref=ref,
                axes=Unknown(name),
                handedness=Unknown(name),
            )
        )
        records.append(
            SpatialArtifact(
                id=evidence_record_id(SpatialArtifact.kind, where, transform),
                provenance=stated(document, transform, "items", index),
                category=SpatialCategory.VECTOR_MAP,
                name=Known(level, name),
                unit=NotCovered(),
                crs=NotCovered(),
                frame=Known(ref, name),
            )
        )
    if skipped:
        report(
            "record_skipped", cite(document, "items"), {"count": skipped, "reason": "level_invalid"}
        )
    return records if len(records) > 1 else []
