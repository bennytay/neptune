"""PX4 ULog: probing, inspecting, planning and ingesting (ADR 0048).

One ``Walk`` reads the messages of a byte range and applies every rule. ``plan`` runs it over the
whole file without building rows: it counts them, finds the declarations and cuts the file into
pieces at message boundaries, and it is the only place findings are made (so they do not depend on
where pieces are cut). ``ingest`` runs the same walk over one piece with the plan's declarations and
builds the records and rows.
"""

import struct
from dataclasses import dataclass, field
from itertools import pairwise
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
    NOT_APPLICABLE,
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
    series_template,
    text_cell,
    text_value,
    time_field,
)
from neptune.adapters.flightlog.ulog_format import (
    DATA_APPENDED,
    DEFINED_TYPES,
    DEFINITION_TYPES,
    FLAG_BITS_SIZE,
    HEADER_SIZE,
    MAGIC,
    MAX_FORMATS,
    MAX_STREAMS,
    PLAUSIBLE_TYPES,
    SYNC_MAGIC,
    SYNC_MESSAGE,
    FormatError,
    KeyType,
    Layout,
    MessageFormat,
    decode_value,
    layout_of,
    parse_format,
    parse_key,
)
from neptune.identity.provenance import EvidenceRecord
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import LogicalId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    Unknown,
)
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import ColumnType, state_column, value_column
from neptune.model.time import INT64_MAX, MICROSECOND, ClockRole, Epoch, Timescale, Timestamp
from neptune.model.world import CellValue

FORMAT: Final = "ulog"
HEADER: Final[Place] = (0, HEADER_SIZE)
MAGIC_PLACE: Final[Place] = (0, len(MAGIC))
PREFIX: Final = "flightlog."
MAX_CELLS: Final = 1024  # elements of an array value that become cells

TABLES: Final = {
    (ord("B"), True): "flag_bits",
    (ord("I"), True): "info",
    (ord("M"), True): "info_multi",
    (ord("P"), True): "parameters",
    (ord("Q"), True): "parameters_default",
    (ord("I"), False): "info_data",
    (ord("M"), False): "info_multi_data",
    (ord("P"), False): "parameter_changes",
    (ord("Q"), False): "parameters_default_data",
}
LOGGED: Final = "logged"
DROPOUT: Final = "dropout"
# bytes before the key length: multi info has is_continued, default parameters their default types
KEY_PREFIX: Final = {ord("I"): 0, ord("P"): 0, ord("M"): 1, ord("Q"): 1}


# --- Probe -------------------------------------------------------------------------------------


def probe(head: bytes) -> ProbeResult:
    if not head.startswith(MAGIC):
        return ProbeResult(0.0, (ProbeReason("flightlog.no_ulog_magic", "not a ULog header"),))
    reasons = [ProbeReason("flightlog.ulog_magic", "the source starts with the ULog magic")]
    if len(head) >= HEADER_SIZE and head[7] <= 1:
        reasons.append(
            ProbeReason("flightlog.ulog_header", f"a complete header, file version {head[7]}")
        )
        if len(head) >= HEADER_SIZE + 3:
            kind = head[HEADER_SIZE + 2]
            if kind in DEFINED_TYPES or kind in PLAUSIBLE_TYPES:
                reasons.append(
                    ProbeReason("flightlog.ulog_message", "a plausible message follows the header")
                )
                return ProbeResult(VERIFIED, tuple(reasons), str(head[7]))
        return ProbeResult(SIGNATURE, tuple(reasons), str(head[7]))
    return ProbeResult(SIGNATURE, tuple(reasons))


# --- Declarations shared between the plan and its chunks ---------------------------------------


@dataclass
class Sub:
    msg_id: int
    multi_id: int
    name: str
    place: Place
    layout: Layout | None


@dataclass
class Format:
    name: str
    parsed: MessageFormat
    place: Place  # the whole F message
    text: str


@dataclass
class Shared:
    formats: dict[str, Format] = field(default_factory=dict)
    parsed: dict[str, MessageFormat] = field(default_factory=dict)
    layouts: dict[str, Layout | None] = field(default_factory=dict)
    subs: dict[int, Sub] = field(default_factory=dict)
    defs_end: int | None = None
    tables: dict[str, Place] = field(default_factory=dict)
    pseudo: dict[str, Place] = field(default_factory=dict)
    uuid: Place | None = None

    def layout(
        self, name: str, findings: Findings | None = None, at: Place = (0, 0)
    ) -> Layout | None:
        """The layout of format ``name``: cached, ``None`` (with a finding once) when unusable."""
        if name in self.layouts:
            return self.layouts[name]
        found = self.formats.get(name)
        layout: Layout | None = None
        if found is not None:
            try:
                layout = layout_of(name, self.parsed)
            except FormatError as exc:
                if findings is not None:
                    findings.aggregate(
                        "bad_format",
                        name,
                        FindingCategory.CORRUPT,
                        Severity.ERROR,
                        found.place,
                        "a message format cannot be laid out; its subscriptions get no rows",
                        {"reason": str(exc), "format": name},
                    )
        self.layouts[name] = layout
        return layout


def _flags(head: bytes) -> tuple[list[int], list[int], list[int]] | None:
    """compat, incompat and appended offsets of a flag bits message at offset 16, else None."""
    if len(head) < HEADER_SIZE + 3 + FLAG_BITS_SIZE:
        return None
    size, kind = struct.unpack_from("<HB", head, HEADER_SIZE)
    if kind != ord("B") or size < FLAG_BITS_SIZE:
        return None
    body = HEADER_SIZE + 3
    return (
        list(head[body : body + 8]),
        list(head[body + 8 : body + 16]),
        list(struct.unpack_from("<3Q", head, body + 16)),
    )


# --- The walk ----------------------------------------------------------------------------------


@dataclass
class Piece:
    start: int
    end: int
    seq: dict[str, int]
    rows: dict[str, int]


class Walk:
    """Reads messages from byte ranges. ``plan`` mode counts and decides; ``ingest`` mode also
    builds records and rows. Both make the same decisions, byte for byte."""

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
        stop_at_data: bool = False,
    ) -> None:
        self.stop_at_data = stop_at_data
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

    # -- driving ---------------------------------------------------------------------------

    def run(self, start: int, end: int) -> None:
        window = Window(self.source, start, end)
        shared = self.shared
        pos = start
        self._begin(pos)
        while pos < end:
            if self.plan and pos > self.piece_start and self._full(pos):
                self._close(pos)
                self._begin(pos)
            i = window.at(pos, 3)
            if i < 0:
                self._leftover(pos, end, -1)
                pos = end
                break
            size, kind = struct.unpack_from("<HB", window.buf, i)
            total = 3 + size
            if kind not in DEFINED_TYPES and kind not in PLAUSIBLE_TYPES:
                pos = self._corrupt(window, pos, end, "header")
                continue
            if pos + total > end:
                found = window.find(SYNC_MESSAGE, pos + 1)
                if found < 0:
                    self._leftover(pos, end, total)
                    pos = end
                    break
                pos = self._corrupt(window, pos, end, "overrun", found)
                continue
            if kind not in DEFINED_TYPES and not self._header_follows(window, pos + total, end):
                # a type the format does not define is skipped by size only if the message after
                # it starts like a message; otherwise its type or size is the damage
                pos = self._corrupt(window, pos, end, "header")
                continue
            if shared.defs_end is None and kind not in DEFINITION_TYPES and self.plan:
                shared.defs_end = pos
                if self.stop_at_data:
                    break
            i = window.at(pos, total)
            self._message(kind, pos, total, window.buf[i + 3 : i + total])
            pos += total
        self._close(end)

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

    def _in_defs(self, pos: int) -> bool:
        end = self.shared.defs_end
        return end is None or pos < end

    @staticmethod
    def _header_follows(window: Window, pos: int, end: int) -> bool:
        if pos >= end:
            return True
        i = window.at(pos, 3)
        return i < 0 or window.buf[i + 2] in PLAUSIBLE_TYPES

    def _corrupt(
        self, window: Window, pos: int, end: int, why: str, found: int | None = None
    ) -> int:
        """A damaged header: skip to the next sync message, or to the end of the range."""
        if found is None:
            found = window.find(SYNC_MESSAGE, pos + 1)
        stop = found if found >= 0 else end
        self.findings.aggregate(
            "corrupt_bytes",
            why,
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (pos, stop - pos),
            "bytes that are not a message were skipped to the next sync message or the end",
            {"resynced": found >= 0},
            amount=stop - pos,
        )
        return stop

    def _leftover(self, pos: int, end: int, total: int) -> None:
        eof = end >= self.source.size
        if eof:
            self.findings.add(
                "truncated",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                (pos, end - pos),
                "the log ends inside a message: every message before it is read",
                {"declared": total, "present": end - pos},
            )
        else:
            self.findings.aggregate(
                "appended_misaligned",
                "end",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                (pos, end - pos),
                "bytes before an appended-data offset are not a whole message; they are skipped",
                {"declared": total},
                amount=end - pos,
            )

    # -- one message -----------------------------------------------------------------------

    def _message(self, kind: int, pos: int, total: int, payload: bytes) -> None:
        place = (pos, total)
        table = TABLES.get((kind, self._in_defs(pos)))
        if kind in KEY_PREFIX or kind == ord("B"):
            if kind == ord("B") and pos != HEADER_SIZE:
                self._misplaced("B", place)
                return
            self._table(table or "", kind, place, payload)
        elif kind == ord("F"):
            self._format(place, payload)
        elif kind == ord("A"):
            self._subscription(place, payload)
        elif kind == ord("D"):
            self._data(place, payload)
        elif kind in (ord("L"), ord("C")):
            self._logged(kind, place, payload)
        elif kind == ord("O"):
            self._dropout(place, payload)
        elif kind == ord("S"):
            if payload != SYNC_MAGIC:
                self.findings.aggregate(
                    "bad_sync",
                    "",
                    FindingCategory.CORRUPT,
                    Severity.WARNING,
                    place,
                    "a sync message does not hold the sync magic",
                    {},
                )
        elif kind == ord("R"):
            pass  # an unsubscription: ids are never reused, so nothing changes
        else:
            self.findings.aggregate(
                "unknown_message_type",
                chr(kind),
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                place,
                "messages of a type the format does not define were skipped by size",
                {},
                amount=total,
            )

    def _misplaced(self, key: str, place: Place) -> None:
        self.findings.aggregate(
            "misplaced_message",
            key,
            FindingCategory.INCONSISTENT,
            Severity.WARNING,
            place,
            "a message in a place the format does not allow was skipped",
            {"type": key},
        )

    def _malformed(self, key: str, place: Place, why: str) -> None:
        self.findings.aggregate(
            "malformed_message",
            key,
            FindingCategory.CORRUPT,
            Severity.ERROR,
            place,
            "a message is too short or not laid out as its type requires; it is skipped",
            {"type": key, "reason": why},
        )

    def _format(self, place: Place, payload: bytes) -> None:
        shared = self.shared
        if not self._in_defs(place[0]):
            self._misplaced("F", place)
            return
        if not self.plan:
            return  # the plan reports formats; chunks receive the ones streams use
        try:
            text = payload.decode("utf-8")
            parsed = parse_format(text)
        except (UnicodeDecodeError, FormatError) as exc:
            self.findings.aggregate(
                "bad_format",
                "parse",
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                "a format message does not parse; it defines no type",
                {"reason": str(exc)},
            )
            return
        known = shared.formats.get(parsed.name)
        if known is not None:
            if known.text != text:
                self.findings.aggregate(
                    "conflicting_format",
                    parsed.name,
                    FindingCategory.INCONSISTENT,
                    Severity.WARNING,
                    place,
                    "a format name is defined again with other fields; the first is used",
                    {"format": parsed.name},
                )
            return
        if len(shared.formats) >= MAX_FORMATS:
            self.findings.aggregate(
                "limit_exceeded",
                "formats",
                FindingCategory.LIMIT,
                Severity.ERROR,
                place,
                f"more than {MAX_FORMATS} formats; the rest are not read",
                {"limit": MAX_FORMATS},
            )
            return
        shared.formats[parsed.name] = Format(parsed.name, parsed, place, text)
        shared.parsed[parsed.name] = parsed

    def _subscription(self, place: Place, payload: bytes) -> None:
        if not self.plan:
            return
        shared = self.shared
        if len(payload) < 4:
            self._malformed("A", place, "short")
            return
        multi_id, msg_id = struct.unpack_from("<BH", payload)
        try:
            name = payload[3:].decode("utf-8")
        except UnicodeDecodeError:
            self._malformed("A", place, "name")
            return
        if msg_id in shared.subs:
            self.findings.aggregate(
                "duplicate_subscription",
                str(msg_id),
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                place,
                "a message id is subscribed again; the first subscription is used",
                {"msg_id": msg_id},
            )
            return
        if len(shared.subs) >= MAX_STREAMS:
            self.findings.aggregate(
                "limit_exceeded",
                "streams",
                FindingCategory.LIMIT,
                Severity.ERROR,
                place,
                f"more than {MAX_STREAMS} subscriptions; the rest get no streams",
                {"limit": MAX_STREAMS},
            )
            return
        layout = shared.layout(name, self.findings, place)
        if name not in shared.formats:
            self.findings.aggregate(
                "unknown_format",
                name,
                FindingCategory.CORRUPT,
                Severity.ERROR,
                place,
                "a subscription names a format no format message defines; it gets no stream",
                {"format": name},
            )
        shared.subs[msg_id] = Sub(msg_id, multi_id, name, place, layout)

    def _data(self, place: Place, payload: bytes) -> None:
        if len(payload) < 2:
            self._malformed("D", place, "short")
            return
        (msg_id,) = struct.unpack_from("<H", payload)
        sub = self.shared.subs.get(msg_id)
        if sub is None or sub.layout is None or place[0] < sub.place[0]:
            self.findings.aggregate(
                "unknown_message_id",
                str(msg_id),
                FindingCategory.CORRUPT,
                Severity.WARNING,
                place,
                "data messages name an id with no usable subscription; they get no rows",
                {"msg_id": msg_id},
                amount=place[1],
            )
            return
        layout = sub.layout
        have = len(payload) - 2
        if have < layout.min_size:
            self.findings.aggregate(
                "size_mismatch",
                f"short:{sub.name}",
                FindingCategory.INCONSISTENT,
                Severity.ERROR,
                place,
                "data messages are shorter than their format; they get no rows",
                {"format": sub.name, "expected": layout.min_size, "found": have},
            )
            return
        if have > layout.size:
            self.findings.aggregate(
                "size_mismatch",
                f"long:{sub.name}",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                place,
                "data messages are longer than their format; the format's fields are read",
                {"format": sub.name, "expected": layout.size, "found": have},
            )
        key = str(msg_id)
        if self.plan:
            self.seq[key] = self.seq.get(key, 0) + 1
            self.piece_weight += 1
            if layout.time_offset is None:
                self.findings.aggregate(
                    "no_time_field",
                    sub.name,
                    FindingCategory.MISSING,
                    Severity.WARNING,
                    sub.place,
                    "a format has no top-level uint64 timestamp; its rows have no time",
                    {"format": sub.name},
                )
            else:
                (ts,) = struct.unpack_from("<Q", payload, 2 + layout.time_offset)
                if ts > INT64_MAX:
                    self._time_range(sub.name, place)
            for start, width in layout.strings:
                if text_value(payload[2 + start : 2 + start + width]) is None:
                    self._utf8(sub.name, place)
            return
        if have < layout.size:  # the logger leaves trailing padding out of a message
            payload = payload + bytes(layout.size - have)
        values = layout.struct.unpack_from(payload, 2)
        ts = values[layout.time_index] if layout.time_index is not None else None
        cells: dict[str, object] = {}
        for item in layout.items:
            name = value_column(item.path)
            if item.string:
                text = text_value(values[item.start])
                cells[name] = text
                cells[state_column(name)] = KNOWN if text is not None else "unknown"
            elif item.repeated:
                cells[name] = tuple(values[item.start : item.start + item.count])
            else:
                cells[name] = values[item.start]
        self.slots[key].row(
            place, ts, cells, time_state=NOT_COVERED if layout.time_index is None else None
        )

    def _time_range(self, what: str, place: Place) -> None:
        self.findings.aggregate(
            "time_out_of_range",
            what,
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            place,
            "a timestamp past 2^63-1 does not fit a signed tick count; the row's time is unknown",
            {"stream": what},
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

    def _logged(self, kind: int, place: Place, payload: bytes) -> None:
        tagged = kind == ord("C")
        head = 11 if tagged else 9
        if len(payload) < head:
            self._malformed(chr(kind), place, "short")
            return
        if tagged:
            level, tag, ts = struct.unpack_from("<BHQ", payload)
        else:
            level, ts = struct.unpack_from("<BQ", payload)
            tag = None
        text = text_value(payload[head:])
        if self.plan:
            self.shared.pseudo.setdefault(LOGGED, place)
            self.seq[LOGGED] = self.seq.get(LOGGED, 0) + 1
            self.piece_weight += 1
            if ts > INT64_MAX:
                self._time_range(LOGGED, place)
            if text is None:
                self._utf8(LOGGED, place)
            return
        values: dict[str, object] = {
            value_column("level"): level,
            value_column("message"): text,
            state_column(value_column("message")): KNOWN if text is not None else "unknown",
            value_column("tag"): tag,
            state_column(value_column("tag")): KNOWN if tag is not None else NOT_APPLICABLE,
        }
        self.slots[LOGGED].row(place, ts, values)

    def _dropout(self, place: Place, payload: bytes) -> None:
        if len(payload) < 2:
            self._malformed("O", place, "short")
            return
        (duration,) = struct.unpack_from("<H", payload)
        if self.plan:
            self.shared.pseudo.setdefault(DROPOUT, place)
            self.seq[DROPOUT] = self.seq.get(DROPOUT, 0) + 1
            self.piece_weight += 1
            self.findings.aggregate(
                "dropout",
                "",
                FindingCategory.MISSING,
                Severity.WARNING,
                place,
                "the logger dropped data here; its dropouts are rows of the dropout stream",
                {},
                amount=duration,
            )
            return
        self.slots[DROPOUT].row(
            place, None, {value_column("duration"): duration}, time_state=NOT_COVERED
        )

    # -- tables ----------------------------------------------------------------------------

    def _table(self, name: str, kind: int, place: Place, payload: bytes) -> None:
        cells = self._cells(name, kind, place, payload)
        if cells is None:
            return
        if self.plan:
            self.shared.tables.setdefault(name, place)
            self.table_rows[name] = self.table_rows.get(name, 0) + 1
            self.piece_weight += TABLE_ROW_WEIGHT
            is_info = name in ("info", "info_data")
            if is_info and self.shared.uuid is None and _key_name(payload, kind) == "sys_uuid":
                self.shared.uuid = place
        else:
            assert self.tables is not None
            self.tables.add(name, place, cells)

    def _cells(
        self, name: str, kind: int, place: Place, payload: bytes
    ) -> tuple[Knowledge[CellValue], ...] | None:
        cite, base = self.cite, place[0] + 3
        if kind == ord("B"):
            return self._flag_cells(place, payload)
        prefix = KEY_PREFIX[kind]
        if len(payload) < prefix + 1:
            self._malformed(chr(kind), place, "short")
            return None
        key_len = payload[prefix]
        key_at = prefix + 1
        if key_len == 0 or key_at + key_len > len(payload):
            self._malformed(chr(kind), place, "key")
            return None
        try:
            key_type, key_name, name_at = parse_key(payload[key_at : key_at + key_len])
        except FormatError as exc:
            self._malformed(chr(kind), place, str(exc))
            return None
        cells: list[Knowledge[CellValue]] = []
        # name, then the declared type, then the prefix byte (multi info, default parameters)
        cells.append(Known(key_name, cite.stated((base + key_at + name_at, key_len - name_at))))
        cells.append(Known(key_type.text, cite.stated((base + key_at, name_at - 1))))
        if prefix:
            cells.append(Known(int(payload[0]), cite.stated((base, 1))))
        value_at = key_at + key_len
        cells.extend(self._value_cells(name, place, key_type, payload[value_at:], base + value_at))
        return tuple(cells)

    def _value_cells(
        self, table: str, place: Place, kind: KeyType, raw: bytes, at: int
    ) -> list[Knowledge[CellValue]]:
        cite = self.cite
        whole = cite.stated((at, len(raw)))
        try:
            value = decode_value(kind, raw)
        except FormatError:
            self._unreadable(table, place, "length")
            return [Unknown(whole)]
        if isinstance(value, bytes):
            cell, bad = text_cell(value, whole)
            if bad:
                self._utf8(table, place)
            return [cell]
        if len(value) > MAX_CELLS:
            self._unreadable(table, place, "array")
            return [Unknown(whole)]
        width = kind.size // (kind.count or 1)
        out: list[Knowledge[CellValue]] = []
        for index, element in enumerate(value):
            provenance = cite.stated((at + index * width, width))
            if isinstance(element, bool):
                out.append(Known(element, provenance))
            elif isinstance(element, int):
                cell = int_cell(element, provenance)
                if isinstance(cell, Unknown):
                    self._unreadable(table, place, "range")
                out.append(cell)
            else:
                assert isinstance(element, float)
                out.append(real_cell(element, provenance))
        return out

    def _unreadable(self, table: str, place: Place, why: str) -> None:
        self.findings.aggregate(
            "unreadable_value",
            f"{table}:{why}",
            FindingCategory.UNREPRESENTABLE,
            Severity.WARNING,
            place,
            "a value does not match its declared type; it is unknown",
            {"table": table, "reason": why},
        )

    def _flag_cells(self, place: Place, payload: bytes) -> tuple[Knowledge[CellValue], ...] | None:
        if len(payload) < FLAG_BITS_SIZE:
            self._malformed("B", place, "short")
            return None
        base, cite = place[0] + 3, self.cite
        cells: list[Knowledge[CellValue]] = [
            Known(payload[n], cite.stated((base + n, 1))) for n in range(16)
        ]
        for n in range(3):
            raw = struct.unpack_from("<Q", payload, 16 + 8 * n)[0]
            cell = int_cell(raw, cite.stated((base + 16 + 8 * n, 8)))
            if isinstance(cell, Unknown):
                self._unreadable("flag_bits", place, "range")
            cells.append(cell)
        return tuple(cells)


def _key_name(payload: bytes, kind: int) -> str | None:
    prefix = KEY_PREFIX[kind]
    if len(payload) <= prefix:
        return None
    key = payload[prefix + 1 : prefix + 1 + payload[prefix]]
    try:
        return parse_key(key)[1]
    except FormatError:
        return None


# --- Plan ---------------------------------------------------------------------------------------


def _unreadable_plan(source: SourceReader, config: AdapterConfig, findings: Findings) -> Plan:
    context: JsonObject = {"format": FORMAT, "part": "unreadable"}
    return Plan((make_chunk(source, config, context, 0),), findings.flush())


def make_plan(source: SourceReader, config: AdapterConfig, chunk_bytes: int, max_rows: int) -> Plan:
    cite = Cite(source, config)
    findings = Findings(source, config, PREFIX)
    size = source.size
    head = b"".join(_read(source, 0, min(size, HEADER_SIZE + 3 + FLAG_BITS_SIZE)))
    if not head.startswith(MAGIC) or size < HEADER_SIZE:
        findings.add(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (0, min(size, HEADER_SIZE)),
            "the source does not start with a complete ULog header; nothing of it is read",
            {"size": size},
        )
        return _unreadable_plan(source, config, findings)
    if head[7] > 1:
        findings.add(
            "unknown_version",
            FindingCategory.UNSUPPORTED,
            Severity.WARNING,
            (7, 1),
            "the file version is newer than the format this adapter knows; it is read as version 1",
            {"version": head[7]},
        )
    flags = _flags(head)
    appended: list[int] = []
    if flags is not None:
        _, incompat, offsets = flags
        unknown = incompat[0] & ~DATA_APPENDED or any(incompat[1:])
        if unknown:
            findings.add(
                "unknown_flags",
                FindingCategory.UNSUPPORTED,
                Severity.ERROR,
                (HEADER_SIZE + 3 + 8, 8),
                "incompatible flag bits this adapter does not know are set; the format requires"
                " refusing to parse the messages, so only the header is read",
                {"incompat_flags": incompat},
            )
            only_header: JsonObject = {"format": FORMAT, "part": "header", "size": size}
            return Plan((make_chunk(source, config, only_header, HEADER_SIZE),), findings.flush())
        if incompat[0] & DATA_APPENDED:
            last = HEADER_SIZE
            for offset in offsets:
                if offset == 0:
                    continue
                if offset <= last or offset >= size:
                    findings.aggregate(
                        "appended_misaligned",
                        "offset",
                        FindingCategory.INCONSISTENT,
                        Severity.WARNING,
                        (HEADER_SIZE + 3 + 16, 24),
                        "an appended-data offset is not inside the file after the previous"
                        " one; it is ignored",
                        {},
                    )
                    continue
                appended.append(offset)
                last = offset
    shared = Shared()
    walk = Walk(
        source, cite, shared, findings, plan=True, chunk_bytes=chunk_bytes, max_rows=max_rows
    )
    bounds = [HEADER_SIZE, *appended, size]
    for start, end in pairwise(bounds):
        walk.run(start, end)
    if shared.defs_end is None:
        shared.defs_end = bounds[1]
    base = _base_context(shared)
    chunks = []
    pieces = walk.pieces or [Piece(HEADER_SIZE, HEADER_SIZE, {}, {})]
    for number, piece in enumerate(pieces):
        piece_context: JsonObject = {
            **base,
            "end": piece.end,
            "number": number,
            "rows": {k: v for k, v in sorted(piece.rows.items()) if v},
            "seq": {k: v for k, v in sorted(piece.seq.items()) if v},
            "start": piece.start,
        }
        chunks.append(make_chunk(source, config, piece_context, piece.end - piece.start))
    return Plan(tuple(chunks), findings.flush())


def _base_context(shared: Shared) -> dict[str, JsonValue]:
    needed: set[str] = set()
    streams: list[JsonValue] = []
    for sub in sorted(shared.subs.values(), key=lambda s: s.msg_id):
        if sub.layout is None:
            continue
        needed.add(sub.name)
        needed.update(sub.layout.nested)
        streams.append([sub.msg_id, sub.multi_id, sub.name, sub.place[0], sub.place[1]])
    formats: list[JsonValue] = []
    for name in sorted(needed):
        found = shared.formats[name]
        formats.append([name, found.place[0], found.place[1], found.text])
    context: dict[str, JsonValue] = {
        "defs_end": shared.defs_end if shared.defs_end is not None else 0,
        "format": FORMAT,
        "formats": formats,
        "part": "data",
        "pseudo": {k: list(v) for k, v in sorted(shared.pseudo.items())},
        "streams": streams,
        "tables": {k: list(v) for k, v in sorted(shared.tables.items())},
    }
    if shared.uuid is not None:
        context["uuid"] = list(shared.uuid)
    return context


def _read(source: SourceReader, start: int, end: int) -> list[bytes]:
    return list(read_pieces(source, start, end)) if end > start else []


# --- Ingest -------------------------------------------------------------------------------------


def _shared_from(context: JsonObject) -> Shared:
    shared = Shared(defs_end=as_int(context["defs_end"]))
    for item in as_list(context["formats"]):
        name, offset, length, text = as_list(item)
        parsed = parse_format(as_str(text))
        place = (as_int(offset), as_int(length))
        shared.formats[as_str(name)] = Format(as_str(name), parsed, place, as_str(text))
        shared.parsed[as_str(name)] = parsed
    for item in as_list(context["streams"]):
        msg_id, multi_id, name, offset, length = as_list(item)
        sub = Sub(
            as_int(msg_id), as_int(multi_id), as_str(name), (as_int(offset), as_int(length)), None
        )
        sub.layout = shared.layout(sub.name)
        shared.subs[sub.msg_id] = sub
    shared.tables = {k: place_of(v) for k, v in as_dict(context["tables"]).items()}
    shared.pseudo = {k: place_of(v) for k, v in as_dict(context["pseudo"]).items()}
    if "uuid" in context:
        shared.uuid = place_of(context["uuid"])
    return shared


def stream_columns(layout: Layout) -> list[ColumnSpec]:
    columns = base_columns()
    for item in layout.items:
        name = value_column(item.path)
        columns.append((name, item.type, item.repeated))
        if item.string:
            columns.append((state_column(name), ColumnType.STRING, False))
    return columns


def _pseudo_columns(kind: str) -> list[ColumnSpec]:
    columns = base_columns()
    if kind == LOGGED:
        message = value_column("message")
        tag = value_column("tag")
        columns += [
            (value_column("level"), ColumnType.UINT8, False),
            (message, ColumnType.STRING, False),
            (state_column(message), ColumnType.STRING, False),
            (tag, ColumnType.UINT16, False),
            (state_column(tag), ColumnType.STRING, False),
        ]
    else:
        columns.append((value_column("duration"), ColumnType.UINT16, False))
    return columns


def ingest(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
    context = chunk.context
    cite = Cite(source, config)
    part = as_str(context["part"])
    if part == "unreadable":
        return ChunkOutput()
    if part == "header":
        return ChunkOutput(records=tuple(_declarations(source, cite, Shared(defs_end=0))[0]))
    shared = _shared_from(context)
    findings = Findings(source, config, PREFIX)  # the plan reports; a chunk's are discarded
    seq = {k: as_int(v) for k, v in as_dict(context["seq"]).items()}
    rows = {k: as_int(v) for k, v in as_dict(context["rows"]).items()}
    walk = Walk(source, cite, shared, findings, plan=False, seq=seq, rows=rows)
    walk.tables = Tables(cite, {k: list(v) for k, v in shared.tables.items()}, rows)
    first = as_int(context["number"]) == 0
    records: list[EvidenceRecord] = []
    if first:
        records.extend(_declarations(source, cite, shared)[0])
    # slots for every stream the piece may hold rows of
    for key, sub in _keyed(shared):
        assert sub.layout is not None
        walk.slots[key] = Slot(
            cite.record_id(Stream.kind, sub.place), stream_columns(sub.layout), seq.get(key, 0)
        )
    for kind, place in shared.pseudo.items():
        walk.slots[kind] = Slot(
            cite.record_id(Stream.kind, place), _pseudo_columns(kind), seq.get(kind, 0)
        )
    walk.run(as_int(context["start"]), as_int(context["end"]))
    series = [slot.batch() for slot in walk.slots.values() if first or slot.rows]
    records.extend(walk.tables.records())
    return ChunkOutput(records=tuple(records), series=tuple(series))


def _keyed(shared: Shared) -> list[tuple[str, Sub]]:
    return [
        (str(s.msg_id), s)
        for s in sorted(shared.subs.values(), key=lambda s: s.msg_id)
        if s.layout is not None
    ]


def _declarations(
    source: SourceReader, cite: Cite, shared: Shared
) -> tuple[list[EvidenceRecord], dict[str, str]]:
    """The clock, the run and a stream per subscription (and per kind of pseudo-stream)."""
    head = b"".join(_read(source, 0, HEADER_SIZE))
    (start,) = struct.unpack_from("<Q", head, 8)
    spec = cite.provenance(MAGIC_PLACE)
    domain = TimestampDomain(
        id=cite.record_id(TimestampDomain.kind, HEADER, time_field("timestamp")),
        provenance=cite.provenance(HEADER, time_field("timestamp")),
        field="timestamp",
        scope=(),
        role=Known(ClockRole.SAMPLE, spec),
        resolution=Known(MICROSECOND, spec),
        epoch=Known(Epoch.BOOT, spec),
        timescale=Known(Timescale.MONOTONIC, spec),
        declared_monotonic=Unknown(),
    )
    stated = cite.stated((8, 8))
    first: Knowledge[Timestamp] = (
        Known(Timestamp(start, domain.id), stated) if start <= INT64_MAX else Unknown(stated)
    )
    run = Run(
        id=cite.record_id(Run.kind, HEADER),
        provenance=cite.provenance(HEADER),
        logical_id=Unknown(),
        machine=_machine(source, cite, shared),
        first=first,
        last=Unknown(),
    )
    records: list[EvidenceRecord] = [domain, run]
    ids: dict[str, str] = {}
    template = series_template(source)
    absent = KnownAbsent(spec)
    encoding = Known("ulog", spec)
    for key, sub in _keyed(shared):
        assert sub.layout is not None
        found = shared.formats[sub.name]
        where = cite.provenance(found.place)
        definition = cite.evidence((found.place[0] + 3, found.place[1] - 3))
        metadata = {"msg_id": str(sub.msg_id), "multi_id": str(sub.multi_id)}
        for nested in sub.layout.nested:
            metadata[f"format.{nested}"] = shared.formats[nested].text
        stream = Stream(
            id=cite.record_id(Stream.kind, sub.place),
            provenance=cite.provenance(sub.place),
            run=run.id,
            topic=Known(sub.name),
            schema_name=Known(sub.name, where),
            schema_encoding=Known("ulog_format", spec),
            schema_definition=Known(definition, where),
            message_encoding=encoding,
            metadata=tuple(sorted(metadata.items())),
            clocks=(domain.id,),
            message_count=Unknown(),
            first=Unknown(),
            last=Unknown(),
            series=template,
        )
        records.append(stream)
        ids[key] = stream.id
    for kind, place in sorted(shared.pseudo.items()):
        stream = Stream(
            id=cite.record_id(Stream.kind, place),
            provenance=cite.provenance(place),
            run=run.id,
            topic=NotApplicable(),
            schema_name=absent,
            schema_encoding=absent,
            schema_definition=absent,
            message_encoding=encoding,
            metadata=(("message_type", "dropout" if kind == DROPOUT else "logged_message"),),
            clocks=(domain.id,),
            message_count=Unknown(),
            first=Unknown(),
            last=Unknown(),
            series=template,
        )
        records.append(stream)
        ids[kind] = stream.id
    return records, ids


def _machine(source: SourceReader, cite: Cite, shared: Shared) -> Knowledge[LogicalId]:
    """The `sys_uuid` the log states, as a declared machine id; unknown when it does not."""
    place = shared.uuid
    if place is None:
        return Unknown()
    raw = b"".join(_read(source, place[0], place[0] + place[1]))
    payload = raw[3:]
    key_len = payload[0]
    try:
        key_type, _, _ = parse_key(payload[1 : 1 + key_len])
        value = decode_value(key_type, payload[1 + key_len :])
    except FormatError:
        return Unknown()
    if not isinstance(value, bytes):
        return Unknown()
    at = place[0] + 3 + 1 + key_len
    provenance = cite.stated((at, len(value)))
    text = text_value(value)
    return Known(LogicalId("px4.sys_uuid", text), provenance) if text else Unknown(provenance)


# --- Inspect ------------------------------------------------------------------------------------


def inspect(source: SourceReader, config: AdapterConfig) -> InspectResult:
    """The header, the flag bits and the definitions section's counts. No data message is read."""
    size = source.size
    summary: dict[str, JsonValue] = {"format": FORMAT, "size": size}
    findings = Findings(source, config, PREFIX)
    head = b"".join(_read(source, 0, min(size, HEADER_SIZE + 3 + FLAG_BITS_SIZE)))
    if not head.startswith(MAGIC) or size < HEADER_SIZE:
        findings.add(
            "bad_magic",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            (0, min(size, HEADER_SIZE)),
            "the source does not start with a complete ULog header",
            {"size": size},
        )
        return InspectResult(summary, findings.flush())
    summary["version"] = head[7]
    summary["start_timestamp"] = struct.unpack_from("<Q", head, 8)[0]
    flags = _flags(head)
    if flags is not None:
        summary["compat_flags"] = flags[0]
        summary["incompat_flags"] = flags[1]
        summary["appended_offsets"] = flags[2]
    shared = Shared()
    limit = min(size, 8 * 1024 * 1024)
    walk = Walk(
        source,
        Cite(source, config),
        shared,
        findings,
        plan=True,
        chunk_bytes=limit,
        max_rows=1 << 30,
        stop_at_data=True,
    )
    walk.run(HEADER_SIZE, limit)
    summary["formats"] = sorted(shared.formats)[:1000]
    summary["parameters"] = walk.table_rows.get("parameters", 0)
    summary["info_messages"] = walk.table_rows.get("info", 0)
    summary["definitions_end"] = shared.defs_end if shared.defs_end is not None else limit
    return InspectResult(summary, findings.flush() if limit >= size else ())
