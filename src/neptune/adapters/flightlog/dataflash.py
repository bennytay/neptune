"""ArduPilot DataFlash binary logs: probing, inspecting, planning and ingesting (ADR 0048).

The same split as ``ulog``: one ``Walk`` applies every rule; ``plan`` runs it over the whole file to
count rows, find the FMT table and the unit records, cut pieces at record boundaries and report
every finding; ``ingest`` runs it over one piece and builds the records and rows.
"""

from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import (
    SIGNATURE,
    VERIFIED,
    AdapterConfig,
    Chunk,
    ChunkOutput,
    InspectResult,
    Plan,
    ProbeReason,
    ProbeResult,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.adapters.flightlog.common import (
    KNOWN,
    NOT_COVERED,
    TABLE_ROW_WEIGHT,
    Cite,
    ColumnSpec,
    Findings,
    Place,
    Slot,
    Tables,
    Window,
    as_dict,
    as_int,
    as_list,
    as_str,
    base_columns,
    int_cell,
    place_of,
    real_cell,
    sample_time,
    series_template,
    status_report,
    text_cell,
    text_value,
    time_field,
)
from neptune.adapters.flightlog.dataflash_format import (
    BUILTIN_FMT,
    DEFINITIONS,
    FMT_LENGTH,
    FMT_PAYLOAD,
    FMT_TYPE,
    HEAD,
    HEADER,
    PARAMETERS,
    TIME_LABELS,
    DfLayout,
    layout_of,
)
from neptune.adapters.flightlog.ulog_format import FormatError
from neptune.identity.provenance import EvidenceRecord
from neptune.model.finding import FindingCategory, Severity
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Knowledge, Known, KnownAbsent, NotCovered, Unknown
from neptune.model.provenance import Row
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import ColumnType, state_column, value_column
from neptune.model.status import StatusConvention, StatusReport, StatusValue
from neptune.model.time import INT64_MAX, MICROSECOND, MILLISECOND, ClockRole, Epoch, Timescale
from neptune.model.world import CellValue

FORMAT: Final = "dataflash"
PREFIX: Final = "flightlog."
MAX_UNIT_RECORDS: Final = 1024
FIELD_UNITS: Final = "field_units"
PARAMETERS_TABLE: Final = "parameters"
UNIT_KINDS: Final = ("FMTU", "MULT", "UNIT")
# FMT payload: type (1), length (1), name (4), format (16), labels (64)
_NAME_AT, _FORMAT_AT, _LABELS_AT = 2, 6, 22


def probe(head: bytes) -> ProbeResult:
    if not head.startswith(HEAD + bytes([FMT_TYPE])):
        reason = ProbeReason("flightlog.no_dataflash_header", "no FMT record opens the source")
        return ProbeResult(0.0, (reason,))
    reasons = [ProbeReason("flightlog.dataflash_header", "the source opens with a FMT record")]
    if len(head) >= FMT_LENGTH and head[HEADER:FMT_LENGTH] == BUILTIN_FMT:
        reasons.append(ProbeReason("flightlog.dataflash_fmt", "the first FMT record describes FMT"))
        return ProbeResult(VERIFIED, tuple(reasons))
    return ProbeResult(SIGNATURE, tuple(reasons))


# --- Declarations --------------------------------------------------------------------------------


@dataclass
class Fmt:
    type: int
    length: int
    payload: bytes
    place: Place
    at: int
    name: str | None
    chars: str | None
    labels: tuple[str, ...]
    layout: DfLayout | None
    builtin: bool = False

    @property
    def stream(self) -> bool:
        return self.layout is not None and self.name not in DEFINITIONS and self.name != PARAMETERS


def _make_fmt(payload: bytes, place: Place, at: int) -> tuple[Fmt | None, str | None]:
    """A FMT payload as a type declaration; (None, why) when it cannot even be skipped by."""
    kind, length, name_b, chars_b, labels_b = FMT_PAYLOAD.unpack(payload)
    if length < HEADER:
        return None, "length"
    name, chars, labels = text_value(name_b), text_value(chars_b), text_value(labels_b)
    layout: DfLayout | None = None
    why: str | None = None
    if not name or chars is None or labels is None:
        why = "text"
    else:
        try:
            layout = layout_of(chars, tuple(labels.split(",")), length)
        except FormatError as exc:
            why = str(exc)
    parts = tuple(labels.split(",")) if labels else ()
    return Fmt(kind, length, payload, place, at, name or None, chars, parts, layout), why


@dataclass
class Shared:
    fmts: dict[int, Fmt] = field(default_factory=dict)
    tables: dict[str, Place] = field(default_factory=dict)
    units: dict[str, list[tuple[Place, bytes]]] = field(
        default_factory=lambda: {kind: [] for kind in UNIT_KINDS}
    )
    time_fmt: dict[str, Place] = field(default_factory=dict)

    def __post_init__(self) -> None:
        builtin, _ = _make_fmt(BUILTIN_FMT, (0, 0), 0)
        assert builtin is not None
        builtin.builtin = True
        self.fmts[FMT_TYPE] = builtin

    def by_name(self, name: str) -> Fmt | None:
        for found in self.fmts.values():
            if found.name == name and found.layout is not None:
                return found
        return None


# --- The walk ------------------------------------------------------------------------------------


@dataclass
class Piece:
    start: int
    end: int
    seq: dict[str, int]
    rows: dict[str, int]


class Walk:
    def __init__(
        self,
        source: SourceReader,
        cite: Cite,
        shared: Shared,
        findings: Findings,
        *,
        plan: bool,
        seq: dict[str, int] | None = None,
        rows: dict[str, int] | None = None,
        chunk_bytes: int = 0,
        max_rows: int = 0,
    ) -> None:
        self.source = source
        self.cite = cite
        self.shared = shared
        self.findings = findings
        self.plan = plan
        self.seq = dict(seq or {})
        self.table_rows = dict(rows or {})
        self.chunk_bytes = chunk_bytes
        self.max_rows = max_rows
        self.pieces: list[Piece] = []
        self.slots: dict[str, Slot] = {}
        self.tables: Tables | None = None
        self.piece_start = 0
        self.piece_weight = 0
        self.piece_seq: dict[str, int] = {}
        self.piece_rows: dict[str, int] = {}
        self.statuses: list[StatusReport] = []  # built in ingest mode only (ADR 0071)

    def run(self, start: int, end: int) -> None:
        window = Window(self.source, start, end)
        fmts = self.shared.fmts
        pos = start
        self._begin(pos)
        while pos < end:
            if self.plan and pos > self.piece_start and self._full(pos):
                self._close(pos)
                self._begin(pos)
            i = window.at(pos, HEADER)
            if i < 0:
                self._leftover(pos, end, -1)
                pos = end
                break
            buf = window.buf
            declared = fmts.get(buf[i + 2])
            if buf[i] != 0xA3 or buf[i + 1] != 0x95 or declared is None or pos < declared.at:
                pos = self._corrupt(window, pos, end, "header")
                continue
            total = declared.length
            if pos + total > end:
                if self._candidate(window, pos + 1, end) < 0:
                    self._leftover(pos, end, total)
                    pos = end
                    break
                pos = self._corrupt(window, pos, end, "overrun")
                continue
            i = window.at(pos, total)
            self._record(declared, pos, window.buf[i + HEADER : i + total])
            pos += total
        self._close(end)

    # -- pieces and damage ---------------------------------------------------------------------

    def _begin(self, pos: int) -> None:
        self.piece_start = pos
        self.piece_weight = 0
        self.piece_seq = dict(self.seq)
        self.piece_rows = dict(self.table_rows)

    def _full(self, pos: int) -> bool:
        return pos - self.piece_start >= self.chunk_bytes or self.piece_weight >= self.max_rows

    def _close(self, end: int) -> None:
        if self.plan and end > self.piece_start:
            self.pieces.append(Piece(self.piece_start, end, self.piece_seq, self.piece_rows))

    def _candidate(self, window: Window, offset: int, end: int) -> int:
        """The next offset at or after ``offset`` that starts a record of a declared type."""
        fmts = self.shared.fmts
        while True:
            found = window.find(HEAD, offset)
            if found < 0:
                return -1
            i = window.at(found, HEADER)
            if i < 0:
                return -1
            declared = fmts.get(window.buf[i + 2])
            if declared is not None and found >= declared.at and found + declared.length <= end:
                return found
            offset = found + 1

    def _corrupt(self, window: Window, pos: int, end: int, why: str) -> int:
        found = self._candidate(window, pos + 1, end)
        stop = found if found >= 0 else end
        self.findings.aggregate(
            "corrupt_bytes",
            why,
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (pos, stop - pos),
            "bytes that are not a record were skipped to the next record of a declared type"
            " or the end",
            {"resynced": found >= 0},
            amount=stop - pos,
        )
        return stop

    def _leftover(self, pos: int, end: int, total: int) -> None:
        self.findings.add(
            "truncated",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (pos, end - pos),
            "the log ends inside a record: every record before it is read",
            {"declared": total, "present": end - pos},
        )

    # -- one record ----------------------------------------------------------------------------

    def _record(self, fmt: Fmt, pos: int, payload: bytes) -> None:
        place = (pos, HEADER + len(payload))
        if fmt.type == FMT_TYPE:
            if self.plan:
                self._declare(place, payload)
            return
        layout = fmt.layout
        if layout is None:
            self.findings.aggregate(
                "unreadable_records",
                str(fmt.type),
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                "records of a type whose format cannot be laid out get no rows",
                {"type": fmt.type},
                amount=place[1],
            )
            return
        name = fmt.name or ""
        if name in DEFINITIONS:
            if self.plan and name in UNIT_KINDS:
                kept = self.shared.units[name]
                if len(kept) < MAX_UNIT_RECORDS:
                    kept.append((place, payload))
                else:
                    self.findings.aggregate(
                        "limit_exceeded",
                        name,
                        FindingCategory.LIMIT,
                        Severity.ERROR,
                        place,
                        f"more than {MAX_UNIT_RECORDS} {name} records; the rest are not read",
                        {"limit": MAX_UNIT_RECORDS},
                    )
            return
        if name == PARAMETERS:
            self._parameter(fmt, layout, place, payload)
            return
        key = str(fmt.type)
        status = status_kind(fmt) if name in STATUS_TYPES else None
        if self.plan:
            self.seq[key] = self.seq.get(key, 0) + 1
            # a row that is a record too weighs as a table row: a piece's records stay bounded
            self.piece_weight += TABLE_ROW_WEIGHT if status is not None else 1
            if layout.time_label is None:
                self.findings.aggregate(
                    "no_time_field",
                    name,
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    fmt.place,
                    "a message type has no TimeUS or TimeMS first column; its rows have no time",
                    {"type": name},
                )
            elif layout.time_char == "Q" and int.from_bytes(payload[:8], "little") > INT64_MAX:
                self.findings.aggregate(
                    "time_out_of_range",
                    name,
                    FindingCategory.UNREPRESENTABLE,
                    Severity.WARNING,
                    place,
                    "a timestamp past 2^63-1 does not fit a signed tick count; it is unknown",
                    {"stream": name},
                )
            for start, width in layout.strings:
                if text_value(payload[start : start + width]) is None:
                    self._utf8(name, place)
            return
        values = layout.struct.unpack(payload)
        cells: dict[str, object] = {}
        for column in layout.columns:
            cname = value_column(column.label)
            if column.string:
                text = text_value(values[column.start])
                cells[cname] = text
                cells[state_column(cname)] = KNOWN if text is not None else "unknown"
            elif column.repeated:
                cells[cname] = tuple(values[column.start : column.start + column.count])
            else:
                cells[cname] = values[column.start]
        ts = values[layout.time_index] if layout.time_index is not None else None
        self.slots[key].row(
            place, ts, cells, time_state=NOT_COVERED if layout.time_label is None else None
        )
        if status is not None:
            self._status(fmt, layout, status, place, ts, values)

    def _status(
        self,
        fmt: Fmt,
        layout: DfLayout,
        convention: StatusConvention,
        place: Place,
        ts: int | None,
        values: tuple[object, ...],
    ) -> None:
        """The status an ``MSG`` or ``ERR`` record reports (ADR 0071 §1): its text, or its
        subsystem and error code, as stored. ArduPilot states no level, name or hardware id."""
        label = layout.time_label
        clock = None
        if label is not None:
            where = self.shared.time_fmt.get(label, (0, HEADER))
            clock = self.cite.record_id(TimestampDomain.kind, where, time_field(label))
        by_label = {column: values[layout.starts[i]] for i, column in enumerate(layout.labels)}
        absent = NotCovered(self.cite.provenance(fmt.place))  # the FMT declares no such column
        message: Knowledge[str] = absent
        pairs: Knowledge[tuple[StatusValue, ...]] = absent
        if convention is StatusConvention.ARDUPILOT_MESSAGE:
            raw = by_label[MESSAGE_LABEL]
            text = text_value(raw) if isinstance(raw, bytes) else None
            message = Known(text) if text else Unknown()
        else:
            codes = [by_label[column] for column in ERROR_LABELS]
            pairs = Known(
                tuple(
                    StatusValue(column, code)
                    for column, code in zip(ERROR_LABELS, codes, strict=True)
                    if isinstance(code, int)
                )
            )
        self.statuses.append(
            status_report(
                self.cite,
                place,
                self.slots[str(fmt.type)].stream,
                convention,
                sample_time(ts, clock),
                level=absent,
                level_names=absent,
                name=absent,
                message=message,
                hardware_id=absent,
                values=pairs,
            )
        )

    def _utf8(self, what: str, place: Place) -> None:
        self.findings.aggregate(
            "invalid_utf8",
            what,
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            place,
            "text that is not UTF-8 is unknown in its row, never replaced",
            {"where": what},
        )

    def _declare(self, place: Place, payload: bytes) -> None:
        fmts = self.shared.fmts
        kind = payload[0]
        known = fmts.get(kind)
        if known is not None:
            if known.payload != payload:
                self.findings.aggregate(
                    "conflicting_format",
                    str(kind),
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    place,
                    "a message type is declared again with another format; the first is used",
                    {"type": kind},
                )
            return
        fmt, why = _make_fmt(payload, place, place[0])
        if fmt is None:
            self.findings.aggregate(
                "bad_format",
                "length",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                "a FMT record declares a record shorter than its header; the type is not declared",
                {"type": kind},
            )
            return
        fmts[kind] = fmt
        if why is not None:
            self.findings.aggregate(
                "bad_format",
                why,
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                "a FMT record cannot be laid out; its records are skipped by their declared length",
                {"type": kind, "reason": why},
            )
        elif fmt.layout is not None and fmt.layout.time_label is not None:
            self.shared.time_fmt.setdefault(fmt.layout.time_label, place)
        if fmt.layout is not None and fmt.name in STATUS_TYPES and status_kind(fmt) is None:
            self.findings.aggregate(
                "status_definition_unrecognised",
                fmt.name,
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                place,
                f"a {fmt.name} format without the columns ArduPilot defines for it"
                f" ({', '.join(STATUS_TYPES[fmt.name])}); its records are rows only, no status"
                " records",
                {"type": fmt.name},
            )

    def _parameter(self, fmt: Fmt, layout: DfLayout, place: Place, payload: bytes) -> None:
        if self.plan:
            self.shared.tables.setdefault(PARAMETERS_TABLE, place)
            self.table_rows[PARAMETERS_TABLE] = self.table_rows.get(PARAMETERS_TABLE, 0) + 1
            self.piece_weight += TABLE_ROW_WEIGHT
            for start, width in layout.strings:
                if text_value(payload[start : start + width]) is None:
                    self._utf8(PARAMETERS_TABLE, place)
            for index, char in enumerate(layout.chars):
                start = layout.offsets[index]
                if char == "Q" and int.from_bytes(payload[start : start + 8], "little") > INT64_MAX:
                    self.findings.aggregate(
                        "unreadable_value",
                        f"{PARAMETERS_TABLE}:range",
                        FindingCategory.UNREPRESENTABLE,
                        Severity.WARNING,
                        place,
                        "an integer past 2^63-1 does not fit a cell; it is unknown",
                        {"table": PARAMETERS_TABLE, "reason": "range"},
                    )
            return
        assert self.tables is not None
        values = layout.struct.unpack(payload)
        cells: list[Knowledge[CellValue]] = []
        for index, char in enumerate(layout.chars):
            provenance = self.cite.stated(
                (place[0] + HEADER + layout.offsets[index], layout.widths[index])
            )
            value = values[layout.starts[index]]
            if isinstance(value, bytes):
                cells.append(text_cell(value, provenance)[0])
            elif isinstance(value, float):
                cells.append(real_cell(value, provenance))
            elif char == "a":
                cells.append(Unknown(provenance))
            else:
                cells.append(int_cell(int(value), provenance))
        header = Known(layout.labels, self.cite.stated(_labels_place(fmt)))
        self.tables.add(PARAMETERS_TABLE, place, tuple(cells), header)


# The status records ArduPilot logs (ADR 0071 §1), and the columns each must declare.
MESSAGE_LABEL: Final = "Message"
ERROR_LABELS: Final = ("Subsys", "ECode")
STATUS_TYPES: Final = {"MSG": (MESSAGE_LABEL,), "ERR": ERROR_LABELS}
_TEXT_CHARS: Final = frozenset("nNZ")
_INTEGER_CHARS: Final = frozenset("bBhHiIqQM")


def status_kind(fmt: Fmt) -> StatusConvention | None:
    """What an ``MSG`` or ``ERR`` format's records report, when its FMT declares the columns
    ArduPilot gives them: a text ``Message``, or integer ``Subsys`` and ``ECode``."""
    layout = fmt.layout
    if layout is None:
        return None
    chars = dict(zip(layout.labels, layout.chars, strict=True))
    if fmt.name == "MSG" and chars.get(MESSAGE_LABEL, "") in _TEXT_CHARS:
        return StatusConvention.ARDUPILOT_MESSAGE
    if fmt.name == "ERR" and all(chars.get(label, "") in _INTEGER_CHARS for label in ERROR_LABELS):
        return StatusConvention.ARDUPILOT_ERROR
    return None


def _labels_place(fmt: Fmt) -> Place:
    return (fmt.place[0] + HEADER + _LABELS_AT, 64)


# --- Units ---------------------------------------------------------------------------------------


@dataclass
class UnitRow:
    message: str
    type: int
    label: str
    column: int
    fmtu: Place
    cells: tuple[Knowledge[CellValue], ...]
    unit_label: str | None
    unit_id: str
    mult_id: str
    mult: float | None


def _by_label(fmt: Fmt, payload: bytes) -> dict[str, object]:
    assert fmt.layout is not None
    values = fmt.layout.struct.unpack(payload)
    return {label: values[fmt.layout.starts[i]] for i, label in enumerate(fmt.layout.labels)}


def unit_rows(shared: Shared, cite: Cite, findings: Findings) -> list[UnitRow]:
    """One row per declared column: its unit id and unit text, multiplier id and value, each
    cited to the bytes that state it (`FMTU` ids, `UNIT` labels, `MULT` values)."""
    units: dict[str, tuple[str | None, Place]] = {}
    mults: dict[str, tuple[float, Place]] = {}
    unit_fmt, mult_fmt = shared.by_name("UNIT"), shared.by_name("MULT")
    if unit_fmt is not None and unit_fmt.layout is not None:
        for place, payload in shared.units["UNIT"]:
            found = _by_label(unit_fmt, payload)
            ident, label = found.get("Id"), found.get("Label")
            if isinstance(ident, int) and isinstance(label, bytes):
                where = (
                    place[0]
                    + HEADER
                    + unit_fmt.layout.offsets[unit_fmt.layout.labels.index("Label")],
                    64,
                )
                text = text_value(label)
                if text is None:
                    findings.aggregate(
                        "invalid_utf8",
                        "unit",
                        FindingCategory.UNREPRESENTABLE,
                        Severity.WARNING,
                        where,
                        "text that is not UTF-8 is unknown in its cell, never replaced",
                        {"where": "unit"},
                    )
                units.setdefault(chr(ident & 0xFF), (text, where))
    if mult_fmt is not None and mult_fmt.layout is not None:
        for place, payload in shared.units["MULT"]:
            found = _by_label(mult_fmt, payload)
            ident, number = found.get("Id"), found.get("Mult")
            if isinstance(ident, int) and isinstance(number, float):
                at = mult_fmt.layout.offsets[mult_fmt.layout.labels.index("Mult")]
                mults.setdefault(chr(ident & 0xFF), (number, (place[0] + HEADER + at, 8)))
    rows: list[UnitRow] = []
    fmtu_fmt = shared.by_name("FMTU")
    if fmtu_fmt is None or fmtu_fmt.layout is None:
        return rows
    layout = fmtu_fmt.layout
    for place, payload in shared.units["FMTU"]:
        found = _by_label(fmtu_fmt, payload)
        kind, unit_ids, mult_ids = found.get("FmtType"), found.get("UnitIds"), found.get("MultIds")
        if not (
            isinstance(kind, int) and isinstance(unit_ids, bytes) and isinstance(mult_ids, bytes)
        ):
            continue
        target = shared.fmts.get(kind)
        if target is None or target.layout is None or not target.name:
            continue
        ids_at = place[0] + HEADER + layout.offsets[layout.labels.index("UnitIds")]
        mult_at = place[0] + HEADER + layout.offsets[layout.labels.index("MultIds")]
        label_at = 0
        for column, label in enumerate(target.layout.labels):
            if column >= len(unit_ids) or unit_ids[column] == 0:
                break
            unit_char = chr(unit_ids[column])
            mult_char = chr(mult_ids[column]) if column < len(mult_ids) else "\0"
            unit_text, unit_where = units.get(unit_char, (None, place))
            number, mult_where = mults.get(mult_char, (None, place))
            if unit_char not in units:
                findings.aggregate(
                    "unit_undeclared",
                    unit_char,
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    place,
                    "a unit id has no UNIT record; the unit is unknown",
                    {"unit_id": unit_char},
                )
            if mult_char not in mults:
                findings.aggregate(
                    "unit_undeclared",
                    "multiplier:" + mult_char,
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    place,
                    "a multiplier id has no MULT record; the multiplier is unknown",
                    {"multiplier_id": mult_char},
                )
            unit_id_at = (ids_at + column, 1)
            mult_id_at = (mult_at + column, 1)
            name_at = (target.place[0] + HEADER + _NAME_AT, 4)
            format_at = (target.place[0] + HEADER + _FORMAT_AT + column, 1)
            labels_at = (target.place[0] + HEADER + _LABELS_AT + label_at, len(label.encode()))
            label_at += len(label.encode()) + 1
            if target.builtin:
                name_at = format_at = labels_at = place
            stated = cite.stated
            cells: tuple[Knowledge[CellValue], ...] = (
                Known(target.name, stated(name_at)),
                Known(label, stated(labels_at)),
                Known((target.chars or "")[column], stated(format_at)),
                Known(unit_char, stated(unit_id_at)),
                Known(unit_text, stated(unit_where))
                if unit_text
                else KnownAbsent(stated(unit_where))
                if unit_text == ""
                else Unknown(stated(unit_where)),
                Known(mult_char, stated(mult_id_at)),
                real_cell(number, stated(mult_where))
                if number is not None
                else Unknown(stated(mult_where)),
            )
            rows.append(
                UnitRow(
                    target.name,
                    kind,
                    label,
                    column,
                    place,
                    cells,
                    unit_text,
                    unit_char,
                    mult_char,
                    number,
                )
            )
    return rows


# --- Plan ----------------------------------------------------------------------------------------


def make_plan(source: SourceReader, config: AdapterConfig, chunk_bytes: int, max_rows: int) -> Plan:
    cite = Cite(source, config)
    findings = Findings(source, config, PREFIX)
    size = source.size
    head = b"".join(read_pieces(source, 0, min(size, 3))) if size else b""
    if not head.startswith(HEAD + bytes([FMT_TYPE])):
        findings.add(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (0, min(size, 3)),
            "the source does not start with a FMT record; nothing of it is read",
            {"size": size},
        )
        context: JsonObject = {"format": FORMAT, "part": "unreadable"}
        return Plan((make_chunk(source, config, context, 0),), findings.flush())
    shared = Shared()
    walk = Walk(
        source, cite, shared, findings, plan=True, chunk_bytes=chunk_bytes, max_rows=max_rows
    )
    walk.run(0, size)
    rows = unit_rows(shared, cite, findings)
    if not rows and any(f.stream for f in shared.fmts.values()):
        findings.add(
            "units_not_declared",
            FindingCategory.MISSING,
            Severity.INFO,
            (0, min(size, 3)),
            "the log declares no units (no usable FMTU record): every unit is unknown",
            {},
        )
    if rows:
        shared.tables[FIELD_UNITS] = rows[0].fmtu
    base = _base_context(shared)
    pieces = walk.pieces or [Piece(0, 0, {}, {})]
    chunks = []
    for number, piece in enumerate(pieces):
        context = {
            **base,
            "end": piece.end,
            "number": number,
            "rows": {k: v for k, v in sorted(piece.rows.items()) if v},
            "seq": {k: v for k, v in sorted(piece.seq.items()) if v},
            "start": piece.start,
        }
        chunks.append(make_chunk(source, config, context, piece.end - piece.start))
    return Plan(tuple(chunks), findings.flush())


def _base_context(shared: Shared) -> JsonObject:
    fmts: list[JsonValue] = [
        [f.type, f.place[0], f.place[1], f.payload.hex()]
        for f in sorted(shared.fmts.values(), key=lambda f: f.type)
        if not f.builtin
    ]
    units: JsonObject = {
        kind: [[p[0], p[1], payload.hex()] for p, payload in shared.units[kind]]
        for kind in UNIT_KINDS
    }
    return {
        "fmts": fmts,
        "format": FORMAT,
        "part": "data",
        "tables": {k: list(v) for k, v in sorted(shared.tables.items())},
        "time_fmt": {k: list(v) for k, v in sorted(shared.time_fmt.items())},
        "units": units,
    }


# --- Ingest --------------------------------------------------------------------------------------


def _shared_from(context: JsonObject) -> Shared:
    shared = Shared()
    for item in as_list(context["fmts"]):
        kind, offset, length, payload = as_list(item)
        place = (as_int(offset), as_int(length))
        fmt, _ = _make_fmt(bytes.fromhex(as_str(payload)), place, place[0])
        assert fmt is not None
        shared.fmts[as_int(kind)] = fmt
    shared.tables = {k: place_of(v) for k, v in as_dict(context["tables"]).items()}
    shared.time_fmt = {k: place_of(v) for k, v in as_dict(context["time_fmt"]).items()}
    for kind, items in as_dict(context["units"]).items():
        for entry in as_list(items):
            offset, length, payload = as_list(entry)
            shared.units[kind].append(
                ((as_int(offset), as_int(length)), bytes.fromhex(as_str(payload)))
            )
    return shared


def stream_columns(layout: DfLayout) -> list[ColumnSpec]:
    columns = base_columns()
    for column in layout.columns:
        name = value_column(column.label)
        columns.append((name, column.type, column.repeated))
        if column.string:
            columns.append((state_column(name), ColumnType.STRING, False))
    return columns


def ingest(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
    context = chunk.context
    cite = Cite(source, config)
    if as_str(context["part"]) == "unreadable":
        return ChunkOutput()
    shared = _shared_from(context)
    findings = Findings(source, config, PREFIX)  # the plan reports; a chunk's are discarded
    seq = {k: as_int(v) for k, v in as_dict(context["seq"]).items()}
    rows = {k: as_int(v) for k, v in as_dict(context["rows"]).items()}
    walk = Walk(source, cite, shared, findings, plan=False, seq=seq, rows=rows)
    walk.tables = Tables(
        cite, {k: list(v) for k, v in shared.tables.items() if k != FIELD_UNITS}, rows
    )
    first = as_int(context["number"]) == 0
    records: list[EvidenceRecord] = []
    unit_table: Tables | None = None
    if first:
        declared, unit_table = _declarations(source, cite, shared, findings)
        records.extend(declared)
    for fmt in shared.fmts.values():
        if fmt.stream:
            assert fmt.layout is not None
            walk.slots[str(fmt.type)] = Slot(
                cite.record_id(Stream.kind, fmt.place),
                stream_columns(fmt.layout),
                seq.get(str(fmt.type), 0),
            )
    walk.run(as_int(context["start"]), as_int(context["end"]))
    series = [slot.batch() for slot in walk.slots.values() if first or slot.rows]
    records.extend(walk.tables.records())
    if unit_table is not None:
        records.extend(unit_table.records())
    records.extend(walk.statuses)
    return ChunkOutput(records=tuple(records), series=tuple(series))


def _domain(cite: Cite, shared: Shared, label: str) -> TimestampDomain:
    where = shared.time_fmt.get(label, (0, HEADER))
    spec = cite.provenance(where)
    tick = MICROSECOND if label == "TimeUS" else MILLISECOND
    return TimestampDomain(
        id=cite.record_id(TimestampDomain.kind, where, time_field(label)),
        provenance=cite.provenance(where, time_field(label)),
        field=label,
        scope=(),
        role=Known(ClockRole.SAMPLE, spec),
        resolution=Known(tick, spec),
        epoch=Known(Epoch.BOOT, spec),
        timescale=Known(Timescale.MONOTONIC, spec),
        declared_monotonic=Unknown(),
    )


def _no_clock(cite: Cite) -> TimestampDomain:
    where = (0, HEADER)
    return TimestampDomain(
        id=cite.record_id(TimestampDomain.kind, where, time_field("none")),
        provenance=cite.provenance(where, time_field("none")),
        field="none",
        scope=(),
        role=Unknown(),
        resolution=Unknown(),
        epoch=Unknown(),
        timescale=Unknown(),
        declared_monotonic=Unknown(),
    )


def _declarations(
    source: SourceReader, cite: Cite, shared: Shared, findings: Findings
) -> tuple[list[EvidenceRecord], Tables | None]:
    start = (0, min(source.size, HEADER))
    run = Run(
        id=cite.record_id(Run.kind, start),
        provenance=cite.provenance(start),
        logical_id=Unknown(),
        machine=Unknown(),
        first=Unknown(),
        last=Unknown(),
    )
    timed = {
        f.layout.time_label
        for f in shared.fmts.values()
        if f.stream and f.layout and f.layout.time_label
    }
    domains = {label: _domain(cite, shared, label) for label in TIME_LABELS if label in timed}
    records: list[EvidenceRecord] = [run]
    records.extend(domains[label] for label in sorted(domains))
    untimed = any(f.stream and f.layout and not f.layout.time_label for f in shared.fmts.values())
    if untimed:  # a type with no time column still carries one clock slot, declared as nothing
        none = _no_clock(cite)
        domains[""] = none
        records.append(none)
    unit_rows_ = unit_rows(shared, cite, findings)
    by_type: dict[int, list[UnitRow]] = {}
    for row in unit_rows_:
        by_type.setdefault(row.type, []).append(row)
    template = series_template(source)
    for fmt in sorted(shared.fmts.values(), key=lambda f: f.type):
        if not fmt.stream or fmt.name is None or fmt.layout is None:
            continue
        label = fmt.layout.time_label or ""
        where = cite.provenance(fmt.place)
        metadata = {
            "format": fmt.chars or "",
            "labels": ",".join(fmt.layout.labels),
            "msg_type": str(fmt.type),
        }
        for row in by_type.get(fmt.type, []):
            metadata[f"unit_id.{row.label}"] = row.unit_id
            metadata[f"multiplier_id.{row.label}"] = row.mult_id
            if row.unit_label:
                metadata[f"unit.{row.label}"] = row.unit_label
            if row.mult is not None:
                metadata[f"multiplier.{row.label}"] = repr(row.mult)
        records.append(
            Stream(
                id=cite.record_id(Stream.kind, fmt.place),
                provenance=cite.provenance(fmt.place),
                run=run.id,
                topic=Known(fmt.name),
                schema_name=Known(fmt.name),
                schema_encoding=Known("dataflash_fmt", where),
                schema_definition=Known(cite.evidence(fmt.place), where),
                message_encoding=Known("dataflash", where),
                metadata=tuple(sorted(metadata.items())),
                clocks=(domains[label].id,),
                message_count=Unknown(),
                first=Unknown(),
                last=Unknown(),
                series=template,
            )
        )
    table: Tables | None = None
    if unit_rows_:
        table = Tables(cite, {FIELD_UNITS: list(unit_rows_[0].fmtu)}, {})
        for row in unit_rows_:
            table.add(FIELD_UNITS, row.fmtu, row.cells, None, Row(row.column))
    return records, table


# --- Inspect -------------------------------------------------------------------------------------


def inspect(source: SourceReader, config: AdapterConfig) -> InspectResult:
    """The FMT table of the first megabytes: types with their formats. No record is decoded."""
    size = source.size
    summary: dict[str, JsonValue] = {"format": FORMAT, "size": size}
    findings = Findings(source, config, PREFIX)
    head = b"".join(read_pieces(source, 0, min(size, 3))) if size else b""
    if not head.startswith(HEAD + bytes([FMT_TYPE])):
        findings.add(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (0, min(size, 3)),
            "the source does not start with a FMT record",
            {"size": size},
        )
        return InspectResult(summary, findings.flush())
    shared = Shared()
    limit = min(size, 1024 * 1024)
    walk = Walk(
        source,
        Cite(source, config),
        shared,
        findings,
        plan=True,
        chunk_bytes=limit,
        max_rows=1 << 30,
    )
    walk.run(0, limit)
    summary["types"] = sorted(f.name for f in shared.fmts.values() if f.name and not f.builtin)[
        :1000
    ]
    summary["scanned_bytes"] = limit
    return InspectResult(summary, ())
