"""Task context: briefs, requirements, procedure steps and work orders as sources state them.

Every record here is an evidence record (ADR 0017) of the ``task`` family, specified by ADR 0063.
Each is one explicit declaration in a document, table or configuration that a transform already
parsed: a brief's ``Task ID:`` line, a requirements table's row, a procedure's ``Step 3:`` heading.
``provenance`` cites that declaration and is ``stated``: the source's authored assertion about the
work, never a reading of its prose.

- ``TaskBrief``: a task one declaration names, with its objective and the site, assets and
  machines it states the task involves.
- ``Requirement``: one requirement statement, verbatim, with the id it is labelled by.
- ``SOPSection``: one labelled step of a procedure and the document blocks it spans.
- ``WorkOrder``: a work-order document or row: the request, not the work done. What was done is a
  deployment lifecycle record (an intervention, a maintenance event), which refers to the order by
  its declared id.

``declared_in`` names the parsed record holding the declaration (a ``DocumentRecord``, a
``StructuredTable`` or a ``ConfigurationSnapshot``): the citations' spans and cells are in the text
or table that record's transform produced. References to other things (``site``, ``assets``,
``task``) are declared ids, never record ids: linking them is identity resolution's (MVL-35).
A heading that only looks like a step, or a sentence that only reads like a requirement, is a
derived candidate (``neptune.derived.context``), never one of these records.
"""

from dataclasses import dataclass
from typing import ClassVar, Final

from neptune.model._fields import (
    Identifiers,
    check_identifiers,
    check_text_values,
    check_type,
    identifiers_from_json,
    identifiers_to_json,
    json_array,
    json_int,
    json_str,
    text_decoder,
)
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Ambiguous, Knowledge, Known, from_json, to_json
from neptune.model.provenance import (
    Provenance,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    provenance_from_json,
)
from neptune.model.record import Family

# The schema version that added these kinds (ADR 0063, ADR 0037 §1). Provisional: the coordinator
# renumbers it at merge when another kind-adding change lands first.
TASK_SINCE: Final = 4


def _record_id(data: JsonValue) -> RecordId:
    return parse_record_id(json_str(data, "record id"))


def _check_count(field: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {value!r}")
    if value < 0:
        raise ValueError(f"{field} must be at least 0, got {value}")


def _check_named(identifiers: Identifiers, name: Knowledge[str], what: str) -> None:
    if not identifiers and not isinstance(name, Known | Ambiguous):
        raise ValueError(f"a declaration names what it declares: an identifier or a stated {what}")


def _logical(data: JsonValue) -> LogicalId:
    return logical_id_from_json(data)


@dataclass(frozen=True)
class TaskBrief:
    """A task one declaration names (ADR 0063 §2): a brief's labelled header, a task register's
    row, a manifest's ``tasks`` entry.

    - ``identifiers``: every id the declaration gives the task (``("task", "TB-117")``).
    - ``name``: its declared title. ``objective``: its declared objective, verbatim.
    - ``site``, ``assets`` and ``machines``: the declared ids of the site, the assets and the robots
      the declaration says the task involves.

    A declaration names what it declares: a task has an identifier or a stated name.
    """

    kind: ClassVar[str] = "task_brief"
    family: ClassVar[Family] = Family.TASK
    since: ClassVar[int] = TASK_SINCE
    id: RecordId
    provenance: Provenance
    declared_in: RecordId
    identifiers: Identifiers
    name: Knowledge[str]
    objective: Knowledge[str]
    site: Knowledge[LogicalId]
    assets: Identifiers
    machines: Identifiers

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.declared_in)
        check_identifiers("identifiers", self.identifiers)
        check_text_values("name", self.name)
        _check_named(self.identifiers, self.name, "name")
        check_text_values("objective", self.objective)
        check_type("site", self.site, LogicalId)
        check_identifiers("assets", self.assets)
        check_identifiers("machines", self.machines)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "assets": identifiers_to_json(self.assets),
                "declared_in": self.declared_in,
                "identifiers": identifiers_to_json(self.identifiers),
                "machines": identifiers_to_json(self.machines),
                "name": to_json(self.name),
                "objective": to_json(self.objective),
                "site": to_json(self.site, LogicalId.to_json),
            },
            self.since,
        )


def task_brief_from_json(data: JsonValue) -> TaskBrief:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        TaskBrief.kind,
        {"assets", "declared_in", "identifiers", "machines", "name", "objective", "site"},
        TaskBrief.since,
    )
    return TaskBrief(
        id=record_id,
        provenance=provenance,
        declared_in=_record_id(obj["declared_in"]),
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        objective=from_json(obj["objective"], text_decoder("objective"), provenance_from_json),
        site=from_json(obj["site"], _logical, provenance_from_json),
        assets=identifiers_from_json(obj["assets"], provenance_from_json),
        machines=identifiers_from_json(obj["machines"], provenance_from_json),
    )


@dataclass(frozen=True)
class Requirement:
    """One requirement statement as its source labels it (ADR 0063 §3): ``Requirement R-12: …``
    in a document, a requirements table's row, a manifest's ``requirements`` entry.

    - ``identifiers``: the id it is labelled by (``("requirement", "R-12")``).
    - ``text``: the statement, verbatim, citing exactly its span or cell. Nothing is paraphrased,
      and its modal verb (``shall``, ``should``) stays in the text.
    - ``task``: the declared id of the task the same declaration states it for: the document's own
      ``Task ID:`` label, a table's ``task_id`` cell. ``Unknown`` when it states none.
    """

    kind: ClassVar[str] = "requirement"
    family: ClassVar[Family] = Family.TASK
    since: ClassVar[int] = TASK_SINCE
    id: RecordId
    provenance: Provenance
    declared_in: RecordId
    identifiers: Identifiers
    text: Knowledge[str]
    task: Knowledge[LogicalId]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.declared_in)
        check_identifiers("identifiers", self.identifiers)
        check_text_values("text", self.text)
        _check_named(self.identifiers, self.text, "text")
        check_type("task", self.task, LogicalId)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "declared_in": self.declared_in,
                "identifiers": identifiers_to_json(self.identifiers),
                "task": to_json(self.task, LogicalId.to_json),
                "text": to_json(self.text),
            },
            self.since,
        )


def requirement_from_json(data: JsonValue) -> Requirement:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, Requirement.kind, {"declared_in", "identifiers", "task", "text"}, Requirement.since
    )
    return Requirement(
        id=record_id,
        provenance=provenance,
        declared_in=_record_id(obj["declared_in"]),
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        text=from_json(obj["text"], text_decoder("text"), provenance_from_json),
        task=from_json(obj["task"], _logical, provenance_from_json),
    )


@dataclass(frozen=True)
class SOPSection:
    """One labelled step of a procedure (ADR 0063 §4): a block whose text starts ``Step 3:``.

    ``provenance`` cites the labelled block. ``blocks`` are the ``DocumentBlock`` records the step
    spans, in reading order: the labelled block first, then its body up to the next step or the
    next heading at its level or above. Its text stays in those blocks, each citing its span.

    - ``procedure``: the declared id of the procedure (the document's ``Procedure ID:`` or
      ``SOP ID:`` label); ``Unknown`` when the document declares none.
    - ``number``: the step's number as written (``3``, ``4.2``), never parsed into an int.
    - ``title``: the rest of the labelled line, verbatim.
    - ``order``: its 0-based position among the document's steps, in reading order.
    """

    kind: ClassVar[str] = "sop_section"
    family: ClassVar[Family] = Family.TASK
    since: ClassVar[int] = TASK_SINCE
    id: RecordId
    provenance: Provenance
    declared_in: RecordId
    procedure: Knowledge[LogicalId]
    number: Knowledge[str]
    title: Knowledge[str]
    order: int
    blocks: tuple[RecordId, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.declared_in)
        check_type("procedure", self.procedure, LogicalId)
        check_text_values("number", self.number)
        check_text_values("title", self.title)
        _check_count("order", self.order)
        if not isinstance(self.blocks, tuple) or not self.blocks:
            raise ValueError("a step spans at least its own labelled block")
        for block in self.blocks:
            parse_record_id(block)
        if len(set(self.blocks)) != len(self.blocks):
            raise ValueError(f"a step names each block once: {self.blocks}")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "blocks": list(self.blocks),
                "declared_in": self.declared_in,
                "number": to_json(self.number),
                "order": self.order,
                "procedure": to_json(self.procedure, LogicalId.to_json),
                "title": to_json(self.title),
            },
            self.since,
        )


def sop_section_from_json(data: JsonValue) -> SOPSection:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        SOPSection.kind,
        {"blocks", "declared_in", "number", "order", "procedure", "title"},
        SOPSection.since,
    )
    return SOPSection(
        id=record_id,
        provenance=provenance,
        declared_in=_record_id(obj["declared_in"]),
        procedure=from_json(obj["procedure"], _logical, provenance_from_json),
        number=from_json(obj["number"], text_decoder("number"), provenance_from_json),
        title=from_json(obj["title"], text_decoder("title"), provenance_from_json),
        order=json_int(obj["order"], "order"),
        blocks=tuple(_record_id(block) for block in json_array(obj["blocks"], "blocks")),
    )


@dataclass(frozen=True)
class WorkOrder:
    """A work order as its document or row states it (ADR 0063 §5): what is asked, of what,
    where. Not the work done: an intervention or maintenance record (ADR 0051) says that, and
    refers to the order by its declared id.

    - ``identifiers``: the order's ids (``("work_order", "WO-5531")``).
    - ``name``: its declared title. ``status``: its declared status, verbatim (``Open``), never
      mapped onto a vocabulary.
    - ``site``, ``assets`` and ``task``: the declared ids it names.
    """

    kind: ClassVar[str] = "work_order"
    family: ClassVar[Family] = Family.TASK
    since: ClassVar[int] = TASK_SINCE
    id: RecordId
    provenance: Provenance
    declared_in: RecordId
    identifiers: Identifiers
    name: Knowledge[str]
    status: Knowledge[str]
    site: Knowledge[LogicalId]
    assets: Identifiers
    task: Knowledge[LogicalId]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.declared_in)
        check_identifiers("identifiers", self.identifiers)
        check_text_values("name", self.name)
        _check_named(self.identifiers, self.name, "name")
        check_text_values("status", self.status)
        check_type("site", self.site, LogicalId)
        check_identifiers("assets", self.assets)
        check_type("task", self.task, LogicalId)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "assets": identifiers_to_json(self.assets),
                "declared_in": self.declared_in,
                "identifiers": identifiers_to_json(self.identifiers),
                "name": to_json(self.name),
                "site": to_json(self.site, LogicalId.to_json),
                "status": to_json(self.status),
                "task": to_json(self.task, LogicalId.to_json),
            },
            self.since,
        )


def work_order_from_json(data: JsonValue) -> WorkOrder:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        WorkOrder.kind,
        {"assets", "declared_in", "identifiers", "name", "site", "status", "task"},
        WorkOrder.since,
    )
    return WorkOrder(
        id=record_id,
        provenance=provenance,
        declared_in=_record_id(obj["declared_in"]),
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        status=from_json(obj["status"], text_decoder("status"), provenance_from_json),
        site=from_json(obj["site"], _logical, provenance_from_json),
        assets=identifiers_from_json(obj["assets"], provenance_from_json),
        task=from_json(obj["task"], _logical, provenance_from_json),
    )
