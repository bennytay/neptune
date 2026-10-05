"""Where a canonical record came from and what produced it (ADR 0006, ADR 0016).

``Provenance = (EvidenceRef, transform id, assertion_kind)``. An ``EvidenceRef`` is a source's
content id plus a locator: a path of ``Locator`` steps from the outermost source inward. Step 0
addresses the source's stored bytes; every later step addresses inside what the transform decoded
from the step before it (a gzip member's JSON, an MCAP chunk's messages, one page's text).

Conventions for every step: indices are 0-based, ranges are half-open ``[start, end)``, text
offsets count Unicode code points, image pixels are in stored raster orientation. Names (channels,
column headers, object ids) are verbatim, and may be empty when the source's name is empty.

A ``TransformRecord`` describes what produced records: an adapter at one version and resolved
config, or a normaliser applied to another transform's output (``upstream``). Build and verify
one with ``neptune.identity.provenance``; this module only holds the shapes.

Every evidence record embeds one record-level ``Provenance`` beside its id (ADR 0017 §5); the
helpers at the end check and serialise that pair the same way for every record kind.
"""

import math
import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import ClassVar, Final, TypeAlias

from neptune.model._fields import exact_object, is_int, json_str
from neptune.model.frames import FrameRef, frame_ref_from_json
from neptune.model.ids import (
    EXTERNAL,
    ConfigHash,
    ContentId,
    ExternalObjectRef,
    RecordId,
    check_text,
    check_token,
    check_verbatim,
    parse_config_hash,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.record import OLDEST_READABLE_VERSION, Family, envelope, record_object
from neptune.model.source import external_object_ref_from_json
from neptune.model.time import INT64_MAX, Timestamp

# --- Field guards ------------------------------------------------------------------------------


def _index(field: str, value: int) -> None:
    """A non-negative position or length that fits a Parquet int64 column."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {type(value).__name__}")
    if not 0 <= value <= INT64_MAX:
        raise ValueError(f"{field} must be in [0, 2^63): {value}")


def _range(what: str, start: int, end: int) -> None:
    if start > end:
        raise ValueError(f"{what} is half-open [start, end) and needs start <= end: {start}, {end}")


def _coordinate(field: str, value: float) -> None:
    if not isinstance(value, float):
        # 1 and 1.0 are different canonical JSON; the adapter decides once, with float().
        raise TypeError(f"{field} must be a float, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{field} {value!r} is not finite")


def _same_domain(what: str, *stamps: Timestamp) -> None:
    for stamp in stamps:
        if not isinstance(stamp, Timestamp):
            raise TypeError(f"{what} needs Timestamps, got {type(stamp).__name__}")
    if len({stamp.domain_id for stamp in stamps}) > 1:
        raise ValueError(f"{what} start and end are in different clock domains")


# --- Locator steps (ADR 0006 §3, ADR 0016 §2) --------------------------------------------------


@dataclass(frozen=True)
class ByteRange:
    """``length`` bytes from ``offset`` of the scope's bytes: the stored bytes at step 0."""

    kind: ClassVar[str] = "byte_range"
    offset: int
    length: int

    def __post_init__(self) -> None:
        _index("offset", self.offset)
        _index("length", self.length)
        _index("offset + length", self.offset + self.length)

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "length": self.length, "offset": self.offset}


@dataclass(frozen=True)
class RecordRange:
    """The records of one channel or topic in a log whose ticks fall in ``[start, end)``.

    ``channel`` is the name as the log declares it (``/imu``, not ``imu``). ``start`` and ``end``
    are in one clock domain, the one the log indexes by (MCAP ``log_time``, rosbag record time).
    One record at tick ``t`` is ``[t, t + 1)``; records sharing a tick need a finer step.
    """

    kind: ClassVar[str] = "record_range"
    channel: str
    start: Timestamp
    end: Timestamp

    def __post_init__(self) -> None:
        check_verbatim("channel", self.channel)
        _same_domain("record range", self.start, self.end)
        _range("record range", self.start.ticks, self.end.ticks)

    def to_json(self) -> JsonObject:
        return {
            "channel": self.channel,
            "domain_id": self.start.domain_id,
            "end": self.end.ticks,
            "kind": self.kind,
            "start": self.start.ticks,
        }


@dataclass(frozen=True)
class Page:
    """One page of a paged document, by position in document order (never by page label)."""

    kind: ClassVar[str] = "page"
    index: int

    def __post_init__(self) -> None:
        _index("index", self.index)

    def to_json(self) -> JsonObject:
        return {"index": self.index, "kind": self.kind}


@dataclass(frozen=True)
class PageRegion:
    """A box on one page, ``[x0, x1) x [y0, y1)``, in the page's own coordinate system as stored.

    For PDF that is default user space: points, origin and y-axis as the page defines them, with
    ``/Rotate`` not applied and no shift to the crop box. The adapter's descriptor names the system.
    """

    kind: ClassVar[str] = "page_region"
    page: int
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        _index("page", self.page)
        for name in ("x0", "y0", "x1", "y1"):
            _coordinate(name, getattr(self, name))
        if self.x0 > self.x1 or self.y0 > self.y1:
            raise ValueError(f"page region needs x0 <= x1 and y0 <= y1: {self}")

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "page": self.page,
            "x0": self.x0,
            "x1": self.x1,
            "y0": self.y0,
            "y1": self.y1,
        }


@dataclass(frozen=True)
class Span:
    """Code points ``[start, end)`` of the text the transform extracted from the scope.

    The offsets are only meaningful with that transform: another extractor may produce other text.
    """

    kind: ClassVar[str] = "span"
    start: int
    end: int

    def __post_init__(self) -> None:
        _index("start", self.start)
        _index("end", self.end)
        _range("span", self.start, self.end)

    def to_json(self) -> JsonObject:
        return {"end": self.end, "kind": self.kind, "start": self.start}


@dataclass(frozen=True)
class Row:
    """One row of a table, counting every record the transform parsed, header rows included."""

    kind: ClassVar[str] = "row"
    row: int

    def __post_init__(self) -> None:
        _index("row", self.row)

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "row": self.row}


@dataclass(frozen=True)
class NoHeader:
    """The table has no header row, so its columns have no declared names. Omitted from JSON."""


NO_HEADER: Final = NoHeader()


@dataclass(frozen=True)
class RowCell:
    """One cell: ``row`` as in ``Row``, ``column`` by position, and the column's declared name.

    ``column_name`` is the header cell's text verbatim (``""`` for a blank header cell), or
    ``NO_HEADER``. Position is the address; the name makes the citation readable and lets a
    consumer notice a reordered table.
    """

    kind: ClassVar[str] = "row_cell"
    row: int
    column: int
    column_name: str | NoHeader

    def __post_init__(self) -> None:
        _index("row", self.row)
        _index("column", self.column)
        if not isinstance(self.column_name, NoHeader):
            check_verbatim("column_name", self.column_name)

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"column": self.column, "kind": self.kind, "row": self.row}
        if not isinstance(self.column_name, NoHeader):
            out["column_name"] = self.column_name
        return out


@dataclass(frozen=True)
class ImageRegion:
    """Pixels ``[x0, x1) x [y0, y1)`` of the stored raster: origin top-left, x right, y down.

    EXIF orientation is not applied. Sub-pixel boxes come from detectors, which are ``derived/``.
    """

    kind: ClassVar[str] = "image_region"
    x0: int
    y0: int
    x1: int
    y1: int

    def __post_init__(self) -> None:
        for name in ("x0", "y0", "x1", "y1"):
            _index(name, getattr(self, name))
        _range("image region x", self.x0, self.x1)
        _range("image region y", self.y0, self.y1)

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "x0": self.x0, "x1": self.x1, "y0": self.y0, "y1": self.y1}


@dataclass(frozen=True)
class VideoFrame:
    """One frame: the container's track, the frame's position in presentation order, and its pts.

    ``pts`` is the presentation timestamp in the track's declared clock domain. Where a raw stream
    declares none, the adapter's domain counts frames.
    """

    kind: ClassVar[str] = "video_frame"
    track: int
    index: int
    pts: Timestamp

    def __post_init__(self) -> None:
        _index("track", self.track)
        _index("index", self.index)
        _same_domain("video frame", self.pts)

    def to_json(self) -> JsonObject:
        return {
            "domain_id": self.pts.domain_id,
            "index": self.index,
            "kind": self.kind,
            "pts": self.pts.ticks,
            "track": self.track,
        }


_JSON_POINTER = re.compile(r"(/([^~/]|~[01])*)*")


@dataclass(frozen=True)
class JsonPointer:
    """An RFC 6901 pointer into a JSON or YAML document as parsed. ``""`` is the whole document."""

    kind: ClassVar[str] = "json_pointer"
    pointer: str

    def __post_init__(self) -> None:
        check_verbatim("pointer", self.pointer)
        if not _JSON_POINTER.fullmatch(self.pointer):
            raise ValueError(f"not an RFC 6901 JSON pointer: {self.pointer!r}")

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "pointer": self.pointer}


@dataclass(frozen=True)
class FrameLocator:
    """A coordinate frame the source declares (ADR 0007). JSON kind ``frame``.

    Lineage-scoped: the frame graph id is a tier-2 id. A citation that must outlive a lineage
    cites the bytes that declare the frame instead.
    """

    kind: ClassVar[str] = "frame"
    ref: FrameRef

    def __post_init__(self) -> None:
        if not isinstance(self.ref, FrameRef):
            raise TypeError(f"ref must be a FrameRef, got {type(self.ref).__name__}")

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "ref": self.ref.to_json()}


@dataclass(frozen=True)
class ObjectLocator:
    """An object in a mesh, CAD or spatial artifact, by the id the artifact gives it, verbatim.

    JSON kind ``object``. The adapter's descriptor says which id fills it (a glTF node name, an
    IFC GlobalId). An object with no id is addressed by a finer step (``JsonPointer``, bytes).
    """

    kind: ClassVar[str] = "object"
    object_id: str

    def __post_init__(self) -> None:
        check_verbatim("object_id", self.object_id)

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "object_id": self.object_id}


Scalar: TypeAlias = str | int | float | bool
_ADAPTER_KIND = re.compile(r"([a-z][a-z0-9_.\-]*):([a-z][a-z0-9_]*)")


@dataclass(frozen=True)
class AdapterLocator:
    """An adapter-specific step, kind ``<adapter id>:<name>`` (ADR 0006 §3).

    Its meaning is documented in the adapter's descriptor. Fields are flat scalars, sorted by name;
    anything deeper is a further step. Build one with ``adapter_locator``.
    """

    kind: str
    fields: tuple[tuple[str, Scalar], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _ADAPTER_KIND.fullmatch(self.kind):
            raise ValueError(f"adapter locator kind must be '<adapter id>:<name>': {self.kind!r}")
        names = [name for name, _ in self.fields]
        if names != sorted(set(names)):
            raise ValueError(f"adapter locator fields must be unique and sorted: {names}")
        for name, value in self.fields:
            check_token("field name", name)
            if name == "kind":
                raise ValueError("'kind' is reserved for the step's own kind")
            if not isinstance(value, str | int | float):
                raise TypeError(f"field {name} must be a JSON scalar, got {type(value).__name__}")
            if isinstance(value, str):
                check_verbatim(name, value)
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"field {name} {value!r} is not finite")

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, **dict(self.fields)}


def adapter_locator(kind: str, fields: Mapping[str, Scalar]) -> AdapterLocator:
    return AdapterLocator(kind, tuple(sorted(fields.items())))


Locator: TypeAlias = (
    ByteRange
    | RecordRange
    | Page
    | PageRegion
    | Span
    | Row
    | RowCell
    | ImageRegion
    | VideoFrame
    | JsonPointer
    | FrameLocator
    | ObjectLocator
    | AdapterLocator
)
_CORE_STEPS: Final = (
    ByteRange,
    RecordRange,
    Page,
    PageRegion,
    Span,
    Row,
    RowCell,
    ImageRegion,
    VideoFrame,
    JsonPointer,
    FrameLocator,
    ObjectLocator,
)


# --- Evidence and provenance -------------------------------------------------------------------

EvidenceSource: TypeAlias = ContentId | ExternalObjectRef


@dataclass(frozen=True)
class EvidenceRef:
    """A source's bytes and an exact place in them: the durable way to cite evidence (ADR 0006 §2).

    ``source`` is a tier-1 content id, or the external identity of an object whose bytes have not
    been fetched. ``locator`` is a non-empty path, outermost step first. Cite a whole source as
    ``ByteRange(0, size)``.
    """

    source: EvidenceSource
    locator: tuple[Locator, ...]

    def __post_init__(self) -> None:
        if isinstance(self.source, str):
            parse_content_id(self.source)
        elif not isinstance(self.source, ExternalObjectRef):
            raise TypeError(f"source must be a content id or ExternalObjectRef: {self.source!r}")
        if not isinstance(self.locator, tuple) or not self.locator:
            raise ValueError("locator must be a non-empty tuple of steps, outermost first")
        for step in self.locator:
            if not isinstance(step, (*_CORE_STEPS, AdapterLocator)):
                raise TypeError(f"not a locator step: {step!r}")

    def locator_json(self) -> list[JsonValue]:
        """The locator as it enters tier-2 ids (ADR 0003) and JSON."""
        return [step.to_json() for step in self.locator]

    def to_json(self) -> JsonObject:
        source = self.source if isinstance(self.source, str) else self.source.to_json()
        return {"locator": self.locator_json(), "source": source}


@dataclass(frozen=True)
class Provenance:
    """Which evidence a record or value came from, what produced it, and how (ADR 0006 §1).

    ``transform`` is a ``TransformRecord`` id. Implements ``Grounding``, so it fills the
    provenance slot of any ``Knowledge`` state.
    """

    evidence: EvidenceRef
    transform: RecordId
    assertion_kind: AssertionKind

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceRef):
            raise TypeError(f"evidence must be an EvidenceRef, got {type(self.evidence).__name__}")
        parse_record_id(self.transform)
        if not isinstance(self.assertion_kind, AssertionKind):
            raise TypeError(
                f"assertion_kind must be observed or stated, got {self.assertion_kind!r};"
                " inferred provenance belongs in derived/"
            )

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": str(self.assertion_kind),
            "evidence": self.evidence.to_json(),
            "transform": self.transform,
        }


# --- Transforms (ADR 0006 §4, ADR 0016 §4) -----------------------------------------------------


@dataclass(frozen=True)
class TransformRecord:
    """What produced a set of records, with nothing host-specific in it.

    - ``adapter_id`` / ``adapter_version`` name the producer: an adapter, or a runtime component
      such as a normaliser (``neptune.si``).
    - ``config`` is the resolved config (defaults filled in); ``config_hash`` is its hash.
    - ``libraries`` are the output-affecting dependency versions the producer declares, by name.
    - ``upstream`` holds the ids of the transforms whose output this one consumed, in the order it
      consumed them; empty for an adapter reading source bytes. It makes the chain hash-linked.

    ``id`` is derived from everything else by ``neptune.identity.provenance``, which also checks
    ``config_hash``. Treat ``config`` as immutable.
    """

    kind: ClassVar[str] = "transform_record"
    family: ClassVar[Family] = Family.LINEAGE
    id: RecordId
    adapter_id: str
    adapter_version: str
    config_hash: ConfigHash
    config: JsonObject
    libraries: tuple[tuple[str, str], ...]
    upstream: tuple[RecordId, ...] = field(metadata=EXTERNAL)  # lineage, in any package (ADR 0069)

    def __post_init__(self) -> None:
        parse_record_id(self.id)
        check_token("adapter_id", self.adapter_id)
        check_text("adapter_version", self.adapter_version)
        parse_config_hash(self.config_hash)
        if not isinstance(self.config, Mapping):
            raise TypeError(f"config must be a JSON object, got {type(self.config).__name__}")
        names = [name for name, _ in self.libraries]
        if names != sorted(set(names)):
            raise ValueError(f"libraries must be unique and sorted by name: {names}")
        for name, version in self.libraries:
            check_text("library name", name)
            check_text("library version", version)
        if len(set(self.upstream)) != len(self.upstream):
            raise ValueError(f"upstream transforms repeat: {self.upstream}")
        for upstream in self.upstream:
            parse_record_id(upstream)
            if upstream == self.id:
                raise ValueError(f"a transform cannot consume its own output: {self.id}")

    def __hash__(self) -> int:
        return hash(self.id)

    def content_json(self) -> JsonObject:
        """Everything but ``id``: the input its id is derived from."""
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "config": self.config,
            "config_hash": self.config_hash,
            "libraries": dict(self.libraries),
            "upstream": list(self.upstream),
        }

    def to_json(self) -> JsonObject:
        return envelope(self.kind, {**self.content_json(), "id": self.id})


# --- JSON --------------------------------------------------------------------------------------


def _int(obj: Mapping[str, JsonValue], key: str) -> int:
    value = obj[key]
    if not is_int(value):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    return value


def _float(obj: Mapping[str, JsonValue], key: str) -> float:
    value = obj[key]
    if not isinstance(value, float):
        raise ValueError(f"{key} must be a float, got {value!r}")
    return value


def _domain(obj: Mapping[str, JsonValue]) -> RecordId:
    return parse_record_id(json_str(obj["domain_id"], "domain_id"))


_STEP_KEYS: Final[Mapping[str, set[str]]] = {
    ByteRange.kind: {"kind", "length", "offset"},
    RecordRange.kind: {"channel", "domain_id", "end", "kind", "start"},
    Page.kind: {"index", "kind"},
    PageRegion.kind: {"kind", "page", "x0", "x1", "y0", "y1"},
    Span.kind: {"end", "kind", "start"},
    Row.kind: {"kind", "row"},
    ImageRegion.kind: {"kind", "x0", "x1", "y0", "y1"},
    VideoFrame.kind: {"domain_id", "index", "kind", "pts", "track"},
    JsonPointer.kind: {"kind", "pointer"},
    FrameLocator.kind: {"kind", "ref"},
    ObjectLocator.kind: {"kind", "object_id"},
}


def step_keys(kind: str, present: AbstractSet[str]) -> set[str]:
    """The JSON keys a core step of ``kind`` has, ``kind`` included; unknown kinds raise.

    ``present`` only matters for ``row_cell``, whose ``column_name`` is omitted when the table has
    no header (``NO_HEADER``).
    """
    if kind == RowCell.kind:
        return {"column", "kind", "row"} | ({"column_name"} & present)
    if kind not in _STEP_KEYS:
        raise ValueError(f"unknown locator kind: {kind!r}")
    return set(_STEP_KEYS[kind])


def locator_from_json(data: JsonValue) -> Locator:
    """Parse one step strictly: unknown kinds, missing or extra keys and wrong types are errors."""
    if not isinstance(data, Mapping):
        raise ValueError(f"locator step must be a JSON object, got {type(data).__name__}")
    kind = data.get("kind")
    if not isinstance(kind, str):
        raise ValueError(f"locator step needs a string kind, got {kind!r}")
    if ":" in kind:
        return _adapter_locator_from_json(kind, data)
    obj = exact_object(data, kind, step_keys(kind, data.keys()))
    if kind == RowCell.kind:
        name = json_str(obj["column_name"], "column_name") if "column_name" in obj else NO_HEADER
        return RowCell(_int(obj, "row"), _int(obj, "column"), name)
    match kind:
        case ByteRange.kind:
            return ByteRange(_int(obj, "offset"), _int(obj, "length"))
        case RecordRange.kind:
            domain = _domain(obj)
            return RecordRange(
                json_str(obj["channel"], "channel"),
                Timestamp(_int(obj, "start"), domain),
                Timestamp(_int(obj, "end"), domain),
            )
        case Page.kind:
            return Page(_int(obj, "index"))
        case PageRegion.kind:
            return PageRegion(
                _int(obj, "page"),
                *(_float(obj, key) for key in ("x0", "y0", "x1", "y1")),
            )
        case Span.kind:
            return Span(_int(obj, "start"), _int(obj, "end"))
        case Row.kind:
            return Row(_int(obj, "row"))
        case ImageRegion.kind:
            return ImageRegion(*(_int(obj, key) for key in ("x0", "y0", "x1", "y1")))
        case VideoFrame.kind:
            return VideoFrame(
                _int(obj, "track"), _int(obj, "index"), Timestamp(_int(obj, "pts"), _domain(obj))
            )
        case JsonPointer.kind:
            return JsonPointer(json_str(obj["pointer"], "pointer"))
        case FrameLocator.kind:
            return FrameLocator(frame_ref_from_json(obj["ref"]))
        case _:
            return ObjectLocator(json_str(obj["object_id"], "object_id"))


def _adapter_locator_from_json(kind: str, data: Mapping[str, JsonValue]) -> AdapterLocator:
    fields: dict[str, Scalar] = {}
    for name, value in data.items():
        if name == "kind":
            continue
        if not isinstance(value, str | int | float):
            raise ValueError(f"adapter locator field {name} must be a JSON scalar, got {value!r}")
        fields[name] = value
    return adapter_locator(kind, fields)


def evidence_ref_from_json(data: JsonValue) -> EvidenceRef:
    obj = exact_object(data, "evidence ref", {"locator", "source"})
    source = obj["source"]
    parsed: EvidenceSource = (
        parse_content_id(source)
        if isinstance(source, str)
        else external_object_ref_from_json(source)
    )
    steps = obj["locator"]
    if not isinstance(steps, list | tuple):
        raise ValueError("locator must be an array of steps")
    return EvidenceRef(parsed, tuple(locator_from_json(step) for step in steps))


def provenance_from_json(data: JsonValue) -> Provenance:
    """Parse strictly; ``"inferred"`` is rejected. Use as ``from_json``'s ``decode_provenance``."""
    obj = exact_object(data, "provenance", {"assertion_kind", "evidence", "transform"})
    kind = json_str(obj["assertion_kind"], "assertion_kind")
    if kind not in AssertionKind.__members__.values():
        raise ValueError(f"assertion_kind must be observed or stated in model/, got {kind!r}")
    return Provenance(
        evidence_ref_from_json(obj["evidence"]),
        parse_record_id(json_str(obj["transform"], "transform")),
        AssertionKind(kind),
    )


def transform_record_from_json(data: JsonValue) -> TransformRecord:
    """Parse the shape strictly; ``identity.provenance.check_transform_record`` checks hashes."""
    obj = record_object(
        data,
        TransformRecord.kind,
        {"adapter_id", "adapter_version", "config", "config_hash", "id", "libraries", "upstream"},
    )
    config = obj["config"]
    if not isinstance(config, Mapping):
        raise ValueError("config must be a JSON object")
    libraries = obj["libraries"]
    if not isinstance(libraries, Mapping):
        raise ValueError("libraries must be a JSON object of name to version")
    upstream = obj["upstream"]
    if not isinstance(upstream, list | tuple):
        raise ValueError("upstream must be an array of transform ids")
    return TransformRecord(
        id=parse_record_id(json_str(obj["id"], "id")),
        adapter_id=json_str(obj["adapter_id"], "adapter_id"),
        adapter_version=json_str(obj["adapter_version"], "adapter_version"),
        config_hash=parse_config_hash(json_str(obj["config_hash"], "config_hash")),
        config=config,
        libraries=tuple(
            sorted((name, json_str(version, name)) for name, version in libraries.items())
        ),
        upstream=tuple(parse_record_id(json_str(u, "upstream")) for u in upstream),
    )


# --- Evidence records (ADR 0017 §5) ------------------------------------------------------------


def check_evidence_record(record_id: RecordId, provenance: Provenance) -> None:
    """An evidence record's envelope: a tier-2 id and one canonical, record-level provenance."""
    parse_record_id(record_id)
    if not isinstance(provenance, Provenance):
        raise TypeError(
            f"record provenance must be a Provenance (observed or stated), got {provenance!r};"
            " inferred records belong in derived/"
        )


def evidence_record_json(
    kind: str,
    record_id: RecordId,
    provenance: Provenance,
    body: Mapping[str, JsonValue],
    version: int = OLDEST_READABLE_VERSION,
) -> JsonObject:
    """An evidence record's JSON: its fields plus ``id``, ``provenance`` and the envelope.

    ``version`` is the schema version that added ``kind`` (ADR 0037 §1).
    """
    body = {**body, "id": record_id, "provenance": provenance.to_json()}
    return envelope(kind, body, version)


def evidence_record_object(
    data: JsonValue, kind: str, keys: set[str], since: int = OLDEST_READABLE_VERSION
) -> tuple[Mapping[str, JsonValue], RecordId, Provenance]:
    """Check an evidence record's JSON strictly; return its object, id and provenance."""
    obj = record_object(data, kind, keys | {"id", "provenance"}, since)
    return (
        obj,
        parse_record_id(json_str(obj["id"], "id")),
        provenance_from_json(obj["provenance"]),
    )
