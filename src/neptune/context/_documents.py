"""Briefs, procedures, work orders, requirements: labelled lines in documents (ADR 0063 §3 to §5).

A document's blocks are read line by line, each line citing its exact span inside its block's.
Only explicit labels are read; the labels and their patterns are fixed and case-insensitive:

- ``<Label>: <value>`` for the labels of ``_LABELS`` (``Task ID: TB-117``, ``Site: WH-3``).
- ``Requirement <id>: <statement>`` (or ``REQ <id>: …``) anywhere: one ``Requirement``.
- ``Step <number>: <title>`` as a block's first line (``.``, ``)`` or a dash may stand for ``:``):
  one ``SOPSection`` spanning that block and its body.

A document with a ``Task ID`` label is a brief, one with a ``Work Order`` label a work order, one
with a ``Procedure ID`` label a procedure whose steps name it. Code blocks are never read. What only
looks like a step or a requirement becomes a derived candidate.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from neptune.context._emit import Output, as_id, field_key, identifiers, split_items, sub_span
from neptune.model._fields import Identifiers
from neptune.model.finding import FindingCategory
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Knowledge, Known, Unknown
from neptune.model.provenance import EvidenceRef, Provenance
from neptune.model.task import Requirement, SOPSection, TaskBrief, WorkOrder
from neptune.model.world import BlockRole, DocumentBlock, DocumentRecord

# Label (``field_key``) → what it states.
_LABELS: Final = {
    "task_id": "task",
    "procedure_id": "procedure",
    "sop_id": "procedure",
    "work_order": "work_order",
    "work_order_id": "work_order",
    "title": "title",
    "objective": "objective",
    "status": "status",
    "site": "site",
    "site_id": "site",
    "asset": "assets",
    "assets": "assets",
    "asset_id": "assets",
    "asset_ids": "assets",
    "robot": "machines",
    "robots": "machines",
    "robot_id": "machines",
    "machine": "machines",
    "machines": "machines",
    "machine_id": "machines",
}
_LIST_LABELS: Final = frozenset({"assets", "machines"})
# Bounded quantifiers only: a hostile line costs time linear in its length.
_LABEL: Final = re.compile(r"(?P<label>[A-Za-z][A-Za-z ._\-]{0,30}?)[ \t]*:[ \t]+(?P<value>\S.*)")
_REQUIREMENT: Final = re.compile(
    r"(?i:requirement|req)[ \t]+(?!(?i:id|no|number)[ \t]*:)"  # "Requirement ID:" names no id
    r"(?P<id>[A-Za-z0-9][A-Za-z0-9._/\-]{0,63})[ \t]*:(?P<text>.*)"
)
_STEP: Final = re.compile(
    r"(?i:step)[ \t]+(?P<number>\d{1,9}(?:\.\d{1,9}){0,8})(?!\.?\d)"  # the whole number
    r"(?:[ \t]*[:.)\-\u2013\u2014][ \t]*|[ \t]+)(?P<title>\S.*)"
)
_NUMBERED: Final = re.compile(r"\d{1,9}(?:\.\d{1,9}){0,8}[.)][ \t]+\S")
_MODAL: Final = re.compile(r"\b(?:shall|must)\b", re.IGNORECASE)
# Blocks never read line by line: code is not prose, and a table's cells are its
# ``StructuredTable``'s, read as a register.
_UNREAD: Final = frozenset({BlockRole.CODE, BlockRole.TABLE})
# Blocks whose lines are not sentences: a heading names, a table's rows are its cells' (read as
# a ``StructuredTable``), a figure has no text.
_NOT_PROSE: Final = frozenset({BlockRole.HEADING, BlockRole.TABLE, BlockRole.FIGURE})
# How sure each candidate rule is (ADR 0063 §7).
NUMBERED_HEADING_CONFIDENCE: Final = 0.5
MODAL_SENTENCE_CONFIDENCE: Final = 0.4


@dataclass(frozen=True)
class _Line:
    block: DocumentBlock
    text: str  # the line, without its line ending
    start: int  # its offset in the block's text

    def cite(self, start: int = 0, end: int | None = None) -> EvidenceRef:
        stop = len(self.text) if end is None else end
        return sub_span(self.block.provenance.evidence, self.start + start, self.start + stop)


@dataclass(frozen=True)
class _Label:
    line: _Line
    value: str
    start: int  # the value's offset in the line


def _lines(block: DocumentBlock) -> Iterator[_Line]:
    if not isinstance(block.text, Known) or _role(block) in _UNREAD:
        return
    text = block.text.value
    offset = 0
    for raw in text.split("\n"):
        line = raw.removesuffix("\r")
        yield _Line(block, line, offset)
        offset += len(raw) + 1


def _trim(line: _Line) -> _Line:
    """The line without surrounding spaces, still citing exactly what it holds."""
    lead = len(line.text) - len(line.text.lstrip())
    return _Line(line.block, line.text.strip(), line.start + lead)


def _role(block: DocumentBlock) -> BlockRole | None:
    return block.role.value if isinstance(block.role, Known) else None


def _level(block: DocumentBlock) -> int | None:
    return block.level.value if isinstance(block.level, Known) else None


class _Reader:
    def __init__(
        self, out: Output, document: DocumentRecord, blocks: Sequence[DocumentBlock]
    ) -> None:
        self.out, self.document, self.blocks = out, document, blocks
        self.labels: dict[str, list[_Label]] = {}
        self.requirements: list[tuple[_Line, re.Match[str]]] = []
        self.steps: list[tuple[int, _Line, re.Match[str]]] = []
        for index, block in enumerate(blocks):
            for number, line in enumerate(_lines(block)):
                body = _trim(line)
                if not body.text:
                    continue
                if number == 0 and (step := _STEP.fullmatch(body.text)):
                    self.steps.append((index, body, step))
                    continue
                if requirement := _REQUIREMENT.fullmatch(body.text):
                    self.requirements.append((body, requirement))
                elif (label := _LABEL.fullmatch(body.text)) and (
                    role := _LABELS.get(field_key(label["label"]))
                ):
                    self.labels.setdefault(role, []).append(
                        _Label(body, label["value"], label.start("value"))
                    )
                elif _MODAL.search(body.text) and _role(block) not in _NOT_PROSE:
                    out.candidate(
                        block.id,
                        Requirement.kind,
                        "modal_sentence_without_label",
                        MODAL_SENTENCE_CONFIDENCE,
                        body.text,
                        body.cite(),
                    )

    # --- labels ---------------------------------------------------------------------------------

    def label(self, role: str) -> _Label | None:
        """The first value a label states; a second, different one is a finding."""
        found = self.labels.get(role, [])
        if not found:
            return None
        first = found[0]
        others = [item for item in found[1:] if item.value != first.value]
        if others:
            self.out.finding(
                "label_repeated",
                FindingCategory.INCONSISTENT,
                first.line.cite(),
                f"the document states its {role} more than once, differently; the first is read",
                {"label": role, "values": [first.value, *(item.value for item in others)]},
                related=[item.line.cite() for item in others],
            )
        return first

    def text(self, role: str) -> Knowledge[str]:
        found = self.label(role)
        if found is None:
            return Unknown(self.out.prov(self.document.provenance.evidence))
        value = found.value
        return self.out.known(value, found.line.cite(found.start, found.start + len(value)))

    def ref(self, role: str, namespace: str) -> Knowledge[LogicalId]:
        return as_id(self.text(role), namespace)

    def refs(self, role: str, namespace: str) -> Identifiers:
        found: list[Known[LogicalId]] = []
        for item in self.labels.get(role, []):
            for start, end in split_items(item.value):
                cited = item.line.cite(item.start + start, item.start + end)
                found.append(self.out.known(LogicalId(namespace, item.value[start:end]), cited))
        return identifiers(found)

    def own(self, role: str, namespace: str) -> tuple[EvidenceRef, Identifiers] | None:
        """Where the document declares its own id of ``role``, and that id."""
        found = self.label(role)
        if found is None:
            return None
        state = self.ref(role, namespace)
        assert isinstance(state, Known)
        return found.line.cite(), (state,)

    def title(self) -> Knowledge[str]:
        """The ``Title:`` label, else the title the document declares (front matter, PDF
        ``/Title``), re-cited as this transform's statement."""
        if "title" in self.labels:
            return self.text("title")
        declared = self.document.title
        if isinstance(declared, Known):
            grounding = declared.provenance
            evidence = (
                grounding.evidence
                if isinstance(grounding, Provenance)
                else self.document.provenance.evidence
            )
            return self.out.known(declared.value, evidence)
        return Unknown(self.out.prov(self.document.provenance.evidence))

    # --- records --------------------------------------------------------------------------------

    def read(self) -> None:
        order = self.own("work_order", "work_order")
        # A work order's ``Task ID`` names the task it serves; only a brief declares one.
        task = None if order is not None else self.own("task", "task")
        task_ref = self.ref("task", "task")
        if task is not None:
            evidence, ids = task
            self.out.add(
                TaskBrief(
                    self.out.record_id(TaskBrief.kind, evidence),
                    self.out.prov(evidence),
                    self.document.id,
                    ids,
                    self.title(),
                    self.text("objective"),
                    self.ref("site", "site"),
                    self.refs("assets", "asset"),
                    self.refs("machines", "machine"),
                )
            )
        if order is not None:
            evidence, ids = order
            self.out.add(
                WorkOrder(
                    self.out.record_id(WorkOrder.kind, evidence),
                    self.out.prov(evidence),
                    self.document.id,
                    ids,
                    self.title(),
                    self.text("status"),
                    self.ref("site", "site"),
                    self.refs("assets", "asset"),
                    task_ref,
                )
            )
        for line, match in self.requirements:
            self.requirement(line, match, task_ref)
        procedure = self.ref("procedure", "procedure")
        self.sections(procedure)

    def requirement(self, line: _Line, match: re.Match[str], task: Knowledge[LogicalId]) -> None:
        evidence = line.cite()
        ident = match["id"]
        id_state = self.out.known(
            LogicalId("requirement", ident), line.cite(match.start("id"), match.end("id"))
        )
        raw = match["text"]
        statement = raw.strip()
        text: Knowledge[str]
        if statement:
            lead = match.start("text") + (len(raw) - len(raw.lstrip()))
            text = self.out.known(statement, line.cite(lead, lead + len(statement)))
        else:
            text = Unknown(self.out.prov(evidence))
            self.out.finding(
                "requirement_without_text",
                FindingCategory.MISSING,
                evidence,
                f"requirement {ident} is labelled but states nothing; its text is unknown",
                {"requirement": ident},
            )
        self.out.add(
            Requirement(
                self.out.record_id(Requirement.kind, evidence),
                self.out.prov(evidence),
                self.document.id,
                (id_state,),
                text,
                task,
            )
        )

    def sections(self, procedure: Knowledge[LogicalId]) -> None:
        starts = {index for index, _, _ in self.steps}
        for order, (index, line, match) in enumerate(self.steps):
            block = self.blocks[index]
            level = _level(block) if _role(block) is BlockRole.HEADING else None
            body = []
            for position in range(index + 1, len(self.blocks)):
                following = self.blocks[position]
                if position in starts:
                    break
                if _role(following) is BlockRole.HEADING:
                    other = _level(following)
                    if level is None or other is None or other <= level:
                        break
                body.append(following.id)
            evidence = block.provenance.evidence
            self.out.add(
                SOPSection(
                    self.out.record_id(SOPSection.kind, evidence),
                    self.out.prov(evidence),
                    self.document.id,
                    procedure,
                    self.out.known(
                        match["number"], line.cite(match.start("number"), match.end("number"))
                    ),
                    self.out.known(
                        match["title"].rstrip(),
                        line.cite(
                            match.start("title"),
                            match.start("title") + len(match["title"].rstrip()),
                        ),
                    ),
                    order,
                    (block.id, *body),
                )
            )
        if not self.steps and "procedure" not in self.labels:
            return
        for index, block in enumerate(self.blocks):  # a numbered heading that is not a step
            if index in starts or _role(block) is not BlockRole.HEADING:
                continue
            for line in _lines(block):
                trimmed = _trim(line)
                if _NUMBERED.match(trimmed.text):
                    self.out.candidate(
                        block.id,
                        SOPSection.kind,
                        "numbered_heading_in_procedure",
                        NUMBERED_HEADING_CONFIDENCE,
                        trimmed.text,
                        trimmed.cite(),
                    )
                break  # a heading's first line is its text


def read_document(out: Output, document: DocumentRecord, blocks: Sequence[DocumentBlock]) -> None:
    _Reader(out, document, sorted(blocks, key=lambda b: (b.order, b.id))).read()
