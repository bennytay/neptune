"""Reading one assertion file (format ``neptune.assertions``, version 1) into records (ADR 0062).

The file is read with the structured JSON reader the config and calibration adapters share, so
every value has its exact span and repeated keys are kept. Each entry of ``assertions`` becomes one
``Assertion``, and each ``authored_at`` one ``TimestampDomain``; anything that cannot be held is a
finding, and the field it fills is ``Unknown``.
"""

import calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from typing import Any, Final

from neptune.adapters.structured.tree import (
    Collection,
    Document,
    Issue,
    Node,
    Null,
    Unreadable,
    Value,
    pointer_token,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.assertion import Assertion, AssertionType, ScopeRef, is_iana_zone
from neptune.model.configuration import CollectionType, ScalarType
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, LogicalId, RecordId, parse_record_id
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import (
    AssertionKind,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.provenance import (
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    Span,
    TransformRecord,
)
from neptune.model.reference import TimestampDomain
from neptune.model.time import INT64_MAX, INT64_MIN, ClockRole, Epoch, Timescale, Timestamp

ADAPTER_ID: Final = "assertion"
FORMAT: Final = "neptune.assertions"
FORMAT_VERSION: Final = 1
TOP_KEYS: Final = ("assertions", "format", "version")
REQUIRED: Final = ("assertion_type", "author", "authored_at", "id", "scope")
OPTIONAL: Final = ("authored_zone", "payload", "rationale", "retracts", "signature", "ticket")
DAY: Final = 86_400

# RFC 3339's date-time (upper-case T and Z only), or a full date alone (ADR 0062 §3).
_DATE_TIME: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(?:(Z)|([+-])(\d{2}):(\d{2}))?)?"
)


def code(name: str) -> str:
    return f"{ADAPTER_ID}.{name}"


# --- The node tree -----------------------------------------------------------------------------


@dataclass
class _Tree:
    """A document's nodes with each mapping's members by key, in source order."""

    document: Document
    text: str
    members: dict[int, dict[str | int, list[int]]] = field(default_factory=dict)
    children: dict[int, list[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for index, node in enumerate(self.document.nodes):
            if node.parent < 0:
                continue
            self.children.setdefault(node.parent, []).append(index)
            self.members.setdefault(node.parent, {}).setdefault(node.path[-1], []).append(index)

    def node(self, index: int) -> Node:
        return self.document.nodes[index]

    def is_a(self, index: int, kind: CollectionType) -> bool:
        value = self.node(index).value
        return isinstance(value, Collection) and value.type is kind

    def scalar(self, index: int, kind: ScalarType) -> Any:
        """The node's reading if it is one scalar of ``kind``, else ``None``."""
        value = self.node(index).value
        if isinstance(value, Value) and len(value.readings) == 1:
            reading = value.readings[0]
            if reading.type is kind:
                return reading.value
        return None

    def pointer(self, index: int) -> str:
        return "".join(f"/{pointer_token(segment)}" for segment in self.node(index).path)

    def locator(self, index: int) -> tuple[Locator, ...]:
        span = self.node(index).span
        return (Span(*span),) if span is not None else (JsonPointer(self.pointer(index)),)

    def written(self, index: int) -> str | None:
        """The value exactly as the file writes it, or ``None`` where its span is not known."""
        span = self.node(index).span
        return None if span is None else self.text[span[0] : span[1]]


# --- Reading -----------------------------------------------------------------------------------


@dataclass
class Output:
    records: list[Any] = field(default_factory=list)
    findings: list[IngestFinding] = field(default_factory=list)


class Reader:
    """Turns one parsed assertion file into records and findings."""

    def __init__(self, source: ContentId, transform: TransformRecord, max_assertions: int) -> None:
        self.source = source
        self.transform = transform
        self.max_assertions = max(0, max_assertions)
        self.out = Output()

    def ref(self, locator: tuple[Locator, ...]) -> EvidenceRef:
        return EvidenceRef(self.source, locator)

    def finding(
        self,
        name: str,
        category: FindingCategory,
        severity: Severity,
        where: tuple[Locator, ...],
        message: str,
        details: dict[str, JsonValue] | None = None,
        records: tuple[RecordId, ...] = (),
        related: tuple[EvidenceRef, ...] = (),
    ) -> None:
        self.out.findings.append(
            ingest_finding(
                code=code(name),
                category=category,
                severity=severity,
                subject=self.ref(where),
                transform=self.transform,
                message=message,
                details=details,
                related=related,
                records=records,
            )
        )

    # --- The file ------------------------------------------------------------------------------

    def file(self, document: Document, text: str) -> None:
        tree = _Tree(document, text)
        self._report_document(tree)
        if not document.nodes or not tree.is_a(0, CollectionType.MAPPING):
            self._not_assertions(tree, "the file's root is not a JSON object")
            return
        top = tree.members.get(0, {})
        unknown = [key for key in top if key not in TOP_KEYS]
        if unknown:
            self.unknown_keys(tree, 0, top, unknown)
        found: dict[str, int] = {}
        for key in TOP_KEYS:
            nodes = top.get(key, [])
            if len(nodes) != 1:
                state = "is missing" if not nodes else "repeats"
                self._not_assertions(tree, f"its {key!r} {state}")
                return
            found[key] = nodes[0]
        if tree.scalar(found["format"], ScalarType.STRING) != FORMAT:
            self._not_assertions(tree, f"its 'format' is not {FORMAT!r}")
            return
        version = tree.scalar(found["version"], ScalarType.INT)
        if version != FORMAT_VERSION:
            written = tree.written(found["version"]) or "?"
            self.finding(
                "version_unsupported",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                tree.locator(found["version"]),
                f"version {written} is not {FORMAT_VERSION}, the only version this adapter"
                " reads; nothing is read",
                {"version": written},
            )
            return
        entries = found["assertions"]
        if not tree.is_a(entries, CollectionType.SEQUENCE):
            self._not_assertions(tree, "its 'assertions' is not an array")
            return
        items = tree.children.get(entries, [])
        for position, index in enumerate(items[: self.max_assertions]):
            _Entry(self, tree, position, index).read()
        if len(items) > self.max_assertions:
            first = items[self.max_assertions]
            self.finding(
                "too_many_assertions",
                FindingCategory.LIMIT,
                Severity.ERROR,
                (JsonPointer(tree.pointer(first)),),
                f"the file holds {len(items)} assertions, over max_assertions"
                f" ({self.max_assertions}); those from {self.max_assertions} on are not read",
                {"assertions": len(items), "max_assertions": self.max_assertions},
            )

    def _not_assertions(self, tree: _Tree, why: str) -> None:
        self.finding(
            "not_assertions",
            FindingCategory.UNSUPPORTED,
            Severity.ERROR,
            (JsonPointer(""),),
            f"not a {FORMAT} file: {why}; nothing is read",
        )

    def unknown_keys(
        self, tree: _Tree, index: int, members: dict[str | int, list[int]], keys: list[str | int]
    ) -> None:
        names = sorted(str(key) for key in keys)
        self.finding(
            "unknown_key",
            FindingCategory.UNSUPPORTED,
            Severity.INFO,
            (JsonPointer(tree.pointer(index)),),
            f"keys format version {FORMAT_VERSION} does not define are not read: "
            + ", ".join(names),
            {"keys": list(names)},
            related=tuple(tree_ref for key in keys for tree_ref in self._refs(tree, members[key])),
        )

    def _refs(self, tree: _Tree, nodes: list[int]) -> Iterator[EvidenceRef]:
        for node in nodes:
            yield self.ref(tree.locator(node))

    def _report_document(self, tree: _Tree) -> None:
        for skipped in tree.document.skipped:
            self.finding(
                "value_not_read",
                FindingCategory.UNREPRESENTABLE,
                Severity.WARNING,
                (Span(*skipped.span),),
                f"a member no record can hold is not read: {skipped.reason}",
            )
        nonstandard = [
            index
            for index, node in enumerate(tree.document.nodes)
            if Issue.NONSTANDARD_JSON in node.issues
        ]
        if nonstandard:
            self.finding(
                "nonstandard_json",
                FindingCategory.INCONSISTENT,
                Severity.INFO,
                tree.locator(nonstandard[0]),
                f"{len(nonstandard)} numbers are NaN or Infinity, which RFC 8259 does not define;"
                " they are kept as written",
                {"values": len(nonstandard)},
            )


# --- One assertion -----------------------------------------------------------------------------


class _Absent:
    """A key the entry does not write."""


ABSENT: Final = _Absent()


class _Entry:
    """One entry of ``assertions``: its record, its clock and its findings."""

    def __init__(self, reader: Reader, tree: _Tree, position: int, index: int) -> None:
        self.reader = reader
        self.tree = tree
        self.position = position
        self.index = index
        self.pointer = f"/assertions/{position}"
        evidence = reader.ref((JsonPointer(self.pointer),))
        self.id = evidence_record_id(Assertion.kind, evidence, reader.transform)
        self.provenance = Provenance(evidence, reader.transform.id, AssertionKind.STATED)

    def stated(self, node: int) -> Provenance:
        return Provenance(
            self.reader.ref(self.tree.locator(node)), self.reader.transform.id, AssertionKind.STATED
        )

    def warn(
        self,
        name: str,
        category: FindingCategory,
        node: int | None,
        message: str,
        related: tuple[EvidenceRef, ...] = (),
    ) -> None:
        where = self.tree.locator(node) if node is not None else (JsonPointer(self.pointer),)
        self.reader.finding(
            name,
            category,
            Severity.WARNING,
            where,
            f"assertion {self.position}: {message}",
            {"assertion": self.position},
            (self.id,),
            related,
        )

    # --- Fields --------------------------------------------------------------------------------

    def read(self) -> None:
        tree = self.tree
        if not tree.is_a(self.index, CollectionType.MAPPING):
            self.reader.finding(
                "not_an_assertion",
                FindingCategory.CORRUPT,
                Severity.WARNING,
                tree.locator(self.index),
                f"entry {self.position} of 'assertions' is not a JSON object; it is not read",
                {"assertion": self.position},
            )
            return
        members = tree.members.get(self.index, {})
        unknown = [key for key in members if key not in (*REQUIRED, *OPTIONAL)]
        if unknown:
            self.reader.unknown_keys(tree, self.index, members, unknown)
        assertion_type = self.assertion_type()
        record = Assertion(
            id=self.id,
            provenance=self.provenance,
            identifier=self.logical_id("id", required=True),
            assertion_type=assertion_type,
            author=self.logical_id("author", required=True),
            authored_at=self.authored_at(),
            authored_zone=self.zone(),
            scope=self.scope(),
            retracts=self.retracts(assertion_type),
            payload=self.payload(),
            rationale=self.text("rationale"),
            signature=self.text("signature"),
            ticket=self.logical_id("ticket", required=False),
        )
        self.reader.out.records.append(record)

    def nodes(self, key: str) -> list[int]:
        """Every node the entry writes for ``key``, in source order."""
        return self.tree.members.get(self.index, {}).get(key, [])

    def is_null(self, node: int) -> bool:
        return isinstance(self.tree.node(node).value, Null)

    def member(self, key: str) -> int | _Absent | None:
        """The key's node, ``ABSENT``, or ``None`` where the key repeats (reported here)."""
        nodes = self.nodes(key)
        if not nodes:
            return ABSENT
        if len(nodes) > 1:
            self.warn(
                "duplicate_key",
                FindingCategory.INCONSISTENT,
                nodes[0],
                f"{key!r} is written {len(nodes)} times; none is chosen, so it is Unknown",
                tuple(self.reader.ref(self.tree.locator(node)) for node in nodes[1:]),
            )
            return None
        return nodes[0]

    def missing(self, key: str, node: int | None, why: str = "is missing") -> Unknown:
        self.warn("missing_field", FindingCategory.MISSING, node, f"{key!r} {why}")
        return Unknown() if node is None else Unknown(self.stated(node))

    def unreadable(self, node: int) -> str | None:
        """Why the node, or a value directly in it, cannot be held; ``None`` if it can."""
        for index in (node, *self.tree.children.get(node, [])):
            value = self.tree.node(index).value
            if isinstance(value, Unreadable):
                return value.reason
        return None

    def invalid(self, key: str, node: int, why: str) -> Unknown:
        """``Unknown`` with the finding a value that is not what its key needs gets."""
        reason = self.unreadable(node)
        if reason is not None:
            self.warn(
                "value_not_read",
                FindingCategory.UNREPRESENTABLE,
                node,
                f"{key!r} cannot be held: {reason}",
            )
        else:
            self.warn("invalid_value", FindingCategory.CORRUPT, node, f"{key!r} {why}")
        return Unknown(self.stated(node))

    def required(self, key: str) -> int | Unknown:
        """The key's node, or the ``Unknown`` a missing, null or repeated key gives."""
        node = self.member(key)
        if node is None:
            return Unknown()
        if isinstance(node, _Absent):
            return self.missing(key, None)
        if isinstance(self.tree.node(node).value, Null):
            return self.missing(key, node, "is null")
        return node

    def optional(self, key: str) -> int | Knowledge[Any]:
        """The key's node, ``KnownAbsent`` where the entry leaves it out or writes null, or the
        ``Unknown`` a repeated key gives: format version 1 defines a left-out part as none."""
        node = self.member(key)
        if node is None:
            return Unknown()
        if isinstance(node, _Absent):
            return KnownAbsent(self.provenance)
        if isinstance(self.tree.node(node).value, Null):
            return KnownAbsent(self.stated(node))
        return node

    def logical_id(self, key: str, *, required: bool) -> Knowledge[LogicalId]:
        node = self.required(key) if required else self.optional(key)
        if not isinstance(node, int):
            return node
        return self._logical_value(key, node)

    def _logical_value(self, key: str, node: int) -> Knowledge[LogicalId]:
        parsed = self._logical(node)
        if parsed is None:
            return self.invalid(key, node, "is not {namespace, value}: a token and text")
        return Known(parsed, self.stated(node))

    def _logical(self, node: int) -> LogicalId | None:
        tree = self.tree
        if not tree.is_a(node, CollectionType.MAPPING):
            return None
        members = tree.members.get(node, {})
        if set(members) != {"namespace", "value"} or any(len(n) != 1 for n in members.values()):
            return None
        namespace = tree.scalar(members["namespace"][0], ScalarType.STRING)
        value = tree.scalar(members["value"][0], ScalarType.STRING)
        if namespace is None or value is None:
            return None
        try:
            return LogicalId(namespace, value)
        except ValueError:
            return None

    def assertion_type(self) -> Knowledge[AssertionType]:
        node = self.required("assertion_type")
        if not isinstance(node, int):
            return node
        text = self.tree.scalar(node, ScalarType.STRING)
        if text not in set(AssertionType):
            names = ", ".join(member.value for member in AssertionType)
            return self.invalid("assertion_type", node, f"is not one of {names}")
        return Known(AssertionType(text), self.stated(node))

    def authored_at(self) -> Knowledge[Timestamp]:
        node = self.required("authored_at")
        if not isinstance(node, int):
            return node
        text = self.tree.scalar(node, ScalarType.STRING)
        parsed = _civil_ticks(text) if isinstance(text, str) else None
        if isinstance(parsed, _LeapSecond):
            self.warn(
                "value_not_read",
                FindingCategory.UNREPRESENTABLE,
                node,
                "'authored_at' is a leap second (second 60): ticks that count 86,400-second days"
                " (ADR 0023 §2) have none for it",
            )
            return Unknown(self.stated(node))
        if parsed is None:
            return self.invalid(
                "authored_at",
                node,
                "is not an RFC 3339 date-time (YYYY-MM-DDTHH:MM:SS[.fraction][Z|+HH:MM]) or a"
                " date (YYYY-MM-DD) that fits 64-bit ticks",
            )
        ticks, resolution, instant = parsed
        evidence = self.reader.ref((JsonPointer(f"{self.pointer}/authored_at"),))
        domain = TimestampDomain(
            id=evidence_record_id(TimestampDomain.kind, evidence, self.reader.transform),
            provenance=Provenance(evidence, self.reader.transform.id, AssertionKind.STATED),
            field="authored_at",
            scope=(self.pointer,),
            role=Known(ClockRole.DOCUMENT),
            resolution=Known(resolution),
            epoch=Known(Epoch.UNIX),
            timescale=Known(Timescale.POSIX) if instant else Unknown(),
            declared_monotonic=Unknown(),
        )
        self.reader.out.records.append(domain)
        return Known(Timestamp(ticks, domain.id), self.stated(node))

    def zone(self) -> Knowledge[str]:
        node = self.member("authored_zone")
        if node is None:
            return Unknown()
        if isinstance(node, _Absent):
            return Unknown()  # the format has a place for a zone; the entry does not state one
        if isinstance(self.tree.node(node).value, Null):
            return Unknown(self.stated(node))
        text = self.tree.scalar(node, ScalarType.STRING)
        if not isinstance(text, str) or not is_iana_zone(text):
            return self.invalid("authored_zone", node, "is not spelled as an IANA zone name")
        return Known(text, self.stated(node))

    def scope(self) -> Knowledge[tuple[ScopeRef, ...]]:
        node = self.required("scope")
        if not isinstance(node, int):
            return node
        if not self.tree.is_a(node, CollectionType.SEQUENCE):
            return self.invalid("scope", node, "is not an array")
        refs: list[ScopeRef] = []
        for item in self.tree.children.get(node, []):
            ref = self._scope_ref(item)
            if ref is None:
                entry = self.tree.node(item).path[-1]
                self.invalid(
                    f"scope entry {entry}",
                    item,
                    "is neither a record id (rec:sha256:…) nor {namespace, value}; the scope is"
                    " Unknown",
                )
                return Unknown(self.stated(node))
            refs.append(ref)
        return Known(tuple(refs), self.stated(node))

    def _scope_ref(self, node: int) -> ScopeRef | None:
        text = self.tree.scalar(node, ScalarType.STRING)
        if isinstance(text, str):
            try:
                return parse_record_id(text)
            except ValueError:
                return None
        return self._logical(node)

    def retracts(self, assertion_type: Knowledge[AssertionType]) -> Knowledge[LogicalId]:
        match assertion_type:
            case Known(value=AssertionType.RETRACT):
                return self.logical_id("retracts", required=True)
            case Known(value=other):
                stated = [n for n in self.nodes("retracts") if not self.is_null(n)]
                if stated:
                    self.warn(
                        "retracts_not_applicable",
                        FindingCategory.INCONSISTENT,
                        stated[0],
                        f"a {other} assertion names an assertion it retracts; only a retract"
                        " does, so it is NotApplicable and stays in the file",
                    )
                return NotApplicable()
            case _:
                # The type was not read, so a retract target may or may not apply: read one if
                # it is written, and leave it Unknown otherwise, with no finding of its own.
                nodes = self.nodes("retracts")
                if not nodes or all(self.is_null(n) for n in nodes):
                    return Unknown(self.stated(nodes[0])) if nodes else Unknown()
                node = self.member("retracts")
                if not isinstance(node, int):
                    return Unknown()
                return self._logical_value("retracts", node)

    def payload(self) -> Knowledge[str]:
        node = self.optional("payload")
        if not isinstance(node, int):
            return node
        written = self.tree.written(node)
        if written is None:
            return self.invalid("payload", node, "has no located text to keep as written")
        return Known(written, self.stated(node))

    def text(self, key: str) -> Knowledge[str]:
        node = self.optional(key)
        if not isinstance(node, int):
            return node
        text = self.tree.scalar(node, ScalarType.STRING)
        if not isinstance(text, str):
            return self.invalid(key, node, "is not a string")
        if not text.strip():
            return self.missing(key, node, "is blank")
        return Known(text, self.stated(node))


class _LeapSecond:
    """A valid RFC 3339 time at second 60, which a count of 86,400-second days cannot hold."""


LEAP_SECOND: Final = _LeapSecond()


def _civil_ticks(text: str) -> tuple[int, Fraction, bool] | _LeapSecond | None:
    """Ticks, resolution in seconds and whether the text names an instant (ADR 0023 §2).

    A date-time with ``Z`` or an offset counts POSIX seconds from 1970-01-01T00:00:00Z; one
    without counts the same way on its own civil clock; a date alone counts days. The resolution
    is the finest field written. ``None`` if the text is not one of these or overflows int64;
    ``LEAP_SECOND`` for second 60, which RFC 3339 allows and no such count holds.
    """
    match = _DATE_TIME.fullmatch(text)
    if match is None:
        return None
    year, month, day = (int(part) for part in match.group(1, 2, 3))
    if match.group(4) is None:
        try:
            days = (date(year, month, day) - date(1970, 1, 1)).days
        except ValueError:
            return None
        return days, Fraction(DAY), False
    hour, minute, second = (int(part) for part in match.group(4, 5, 6))
    try:
        # Range checks only: no zone is implied. Second 60 is checked as 59.
        datetime(year, month, day, hour, minute, min(second, 59))
    except ValueError:
        return None
    if second == 60:
        return LEAP_SECOND
    digits = match.group(7) or ""
    scale = 10 ** len(digits)
    offset = 0
    if match.group(9) is not None:
        hours, minutes = int(match.group(10)), int(match.group(11))
        if hours > 23 or minutes > 59:
            return None
        offset = (hours * 3600 + minutes * 60) * (-1 if match.group(9) == "-" else 1)
    seconds = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) - offset
    ticks = seconds * scale + int(digits or "0")
    if not INT64_MIN <= ticks <= INT64_MAX:
        return None
    instant = match.group(8) is not None or match.group(9) is not None
    return ticks, Fraction(1, scale), instant
