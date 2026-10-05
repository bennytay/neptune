"""Briefs, procedures, work orders, requirements: labelled lines in documents (ADR 0063 §3 to §5).

A document's blocks are read line by line, each line citing its exact span inside its block's.
Only explicit labels are read; the labels and their patterns are fixed and case-insensitive:

- ``<Label>: <value>`` for the labels of ``_LABELS`` (``Task ID: TB-117``, ``Site: WH-3``).
- ``Requirement <id>: <statement>`` (or ``REQ <id>: …``) anywhere, the id holding a digit: one
  ``Requirement``.
- ``Step <number>: <title>`` as a block's first line (``.``, ``)`` or a dash may stand for ``:``,
  nothing else): one ``SOPSection`` spanning that block and its body.

A statement or a title runs over the block's following lines up to the next labelled line (a PDF
wraps at every visual line end), joined by one space and citing its whole span, within bounds.

A document with a ``Task ID`` label is a brief, one with a ``Work Order`` label a work order, one
with a ``Procedure ID`` label a procedure whose steps name it. Code and table blocks are never
read (a table is its ``StructuredTable``); a quote's later lines are read without their markers.
What only looks like a step or a requirement becomes a derived candidate.
"""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from neptune.declared._emit import Output, as_id, field_key, identifiers, split_items, sub_span
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
    r"(?i:requirement|req)[ \t]+"
    r"(?P<id>(?=[A-Za-z0-9])[A-Za-z0-9._/\-]{0,63}\d[A-Za-z0-9._/\-]{0,63})[ \t]*:(?P<text>.*)"
)  # an id has a digit: "Requirement type: Functional" and "Req coverage: 80%" are not ids
_STEP: Final = re.compile(
    r"(?i:step)[ \t]+(?P<number>\d{1,9}(?:\.\d{1,9}){0,8})(?!\.?\d)"  # the whole number
    r"[ \t]*[:.)\-\u2013\u2014][ \t]*(?P<title>\S.*)"
)
# A blockquote's later lines keep their markers in the block's text; the first line's are outside.
_QUOTE_MARKERS: Final = re.compile(r"[ \t]{0,3}(?:>[ \t]?){1,16}")
# A requirement's statement or a step's title runs over its block's wrapped lines (a PDF wraps at
# every visual line end), joined by one space, up to the next labelled line: at most this many
# lines and code points, past which the rest is left unread with a finding.
MAX_CONTINUATION_LINES: Final = 64
MAX_STATEMENT_LENGTH: Final = 4096
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
    quoted = _role(block) is BlockRole.QUOTE
    offset = 0
    for number, raw in enumerate(text.split("\n")):
        line = raw.removesuffix("\r")
        marker = _QUOTE_MARKERS.match(line) if quoted and number else None
        skip = marker.end() if marker is not None else 0
        yield _Line(block, line[skip:], offset + skip)
        offset += len(raw) + 1


@dataclass(frozen=True)
class _Statement:
    """A labelled line and the wrapped lines that continue it, as one cited text."""

    first: _Line
    match: re.Match[str]
    group: str  # the match group the text starts at: a requirement's text, a step's title
    rest: tuple[_Line, ...]

    def text_and_span(self) -> tuple[str, EvidenceRef] | None:
        """The text joined by single spaces, citing from its first character to the end of its
        last line; ``None`` when there is none."""
        raw = self.match[self.group]
        head = raw.strip()
        lead = self.match.start(self.group) + (len(raw) - len(raw.lstrip()))
        parts = ([head] if head else []) + [line.text for line in self.rest]
        if not parts:
            return None
        start = self.first.start + lead if head else self.rest[0].start
        last = self.rest[-1] if self.rest else self.first
        end = last.start + len(last.text) if self.rest else self.first.start + lead + len(head)
        span = sub_span(self.first.block.provenance.evidence, start, end)
        return " ".join(parts), span

    def cite(self) -> EvidenceRef:
        """The whole statement: its labelled line through its last continuation line."""
        last = self.rest[-1] if self.rest else self.first
        end = last.start + len(last.text)
        return sub_span(self.first.block.provenance.evidence, self.first.start, end)


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
        self.requirements: list[_Statement] = []
        self.steps: list[tuple[int, _Statement]] = []
        for index, block in enumerate(blocks):
            lines = [line for line in map(_trim, _lines(block)) if line.text]
            kinds = [self._kind(line, number == 0) for number, line in enumerate(lines)]
            number = 0
            while number < len(lines):
                line, (kind, match) = lines[number], kinds[number]
                if kind in ("step", "requirement"):
                    assert match is not None
                    end = number + 1
                    while end < len(lines) and kinds[end][0] == "plain":
                        end += 1
                    rest = self._bounded(lines[number + 1 : end], match, line)
                    statement = _Statement(
                        line, match, "title" if kind == "step" else "text", tuple(rest)
                    )
                    if kind == "step":
                        self.steps.append((index, statement))
                    else:
                        self.requirements.append(statement)
                    number += 1 + len(rest)
                    continue
                if kind == "label":
                    assert match is not None
                    self.labels.setdefault(_LABELS[field_key(match["label"])], []).append(
                        _Label(line, match["value"], match.start("value"))
                    )
                elif _MODAL.search(line.text) and _role(block) not in _NOT_PROSE:
                    out.candidate(
                        block.id,
                        Requirement.kind,
                        "modal_sentence_without_label",
                        MODAL_SENTENCE_CONFIDENCE,
                        line.text,
                        line.cite(),
                    )
                number += 1

    @staticmethod
    def _kind(line: _Line, first: bool) -> tuple[str, re.Match[str] | None]:
        """What a line is: a step label (a block's first line only), a requirement label, a known
        ``Label:``, or plain text."""
        if first and (step := _STEP.fullmatch(line.text)):
            return "step", step
        if requirement := _REQUIREMENT.fullmatch(line.text):
            return "requirement", requirement
        label = _LABEL.fullmatch(line.text)
        if label and field_key(label["label"]) in _LABELS:
            return "label", label
        return "plain", None

    def _bounded(self, rest: list[_Line], match: re.Match[str], first: _Line) -> list[_Line]:
        """The continuation lines a statement takes, within its bounds; a finding past them."""
        kept: list[_Line] = []
        length = len(first.text)
        for line in rest:
            length += 1 + len(line.text)
            if len(kept) >= MAX_CONTINUATION_LINES or length > MAX_STATEMENT_LENGTH:
                self.out.finding(
                    "statement_too_long",
                    FindingCategory.LIMIT,
                    first.cite(),
                    "a labelled statement runs past its bounds; its later lines are not read"
                    " as part of it",
                    {"lines": MAX_CONTINUATION_LINES, "length": MAX_STATEMENT_LENGTH},
                )
                break
            kept.append(line)
        return kept

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
        for statement in self.requirements:
            self.requirement(statement, task_ref)
        procedure = self.ref("procedure", "procedure")
        self.sections(procedure)

    def requirement(self, statement: _Statement, task: Knowledge[LogicalId]) -> None:
        line, match = statement.first, statement.match
        evidence = statement.cite()
        ident = match["id"]
        id_state = self.out.known(
            LogicalId("requirement", ident), line.cite(match.start("id"), match.end("id"))
        )
        text: Knowledge[str]
        if (found := statement.text_and_span()) is not None:
            text = self.out.known(*found)
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
        starts = {index for index, _ in self.steps}
        for order, (index, statement) in enumerate(self.steps):
            line, match = statement.first, statement.match
            titled = statement.text_and_span()
            assert titled is not None  # the step pattern needs a title
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
                    self.out.known(*titled),
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
