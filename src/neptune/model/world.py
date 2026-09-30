"""World and record context: places, things, geometry, photos, documents and tables as declared.

Every record here is an evidence record (ADR 0017) of the ``world`` family, specified by ADR 0020:

- ``Site`` and ``Asset``: a place and a physical thing that one declaration names (a site
  register's row, a manifest entry, a GeoJSON feature), with every id and name it gives them.
- ``SpatialArtifact``: a mesh, CAD model, point cloud, map or scene, by reference. The geometry
  stays in the source bytes; the record holds what the file declares about its units, CRS and frame.
- ``Image`` and ``Video``: a standalone photo, or one video track of a standalone video file:
  dimensions, encoding and what the file declares about its capture. Regions are cited with
  ``ImageRegion``, under a ``VideoFrame`` for video.
- ``DocumentRecord`` and ``DocumentBlock``: a document with its pages, and each unit of text the
  transform extracts from it, citing its exact span and, on a page, its region.
- ``StructuredTable`` and ``StructuredRecord``: a table and each of its rows, cell by cell, as
  declared.

None of them turns structure into text: an image stays pixels, geometry stays geometry, and a
table stays cells. A timestamped table or a video inside a log is a series instead (ADR 0018).
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, TypeAlias

from neptune.model._fields import (
    Identifiers,
    Names,
    check_identifiers,
    check_names,
    check_text_values,
    check_type,
    enum_decoder,
    exact_object,
    identifiers_from_json,
    identifiers_to_json,
    is_int,
    json_array,
    json_int,
    json_str,
    names_from_json,
    names_to_json,
    text_decoder,
    unit_json,
    values_of,
)
from neptune.model.frames import ANGLE, LENGTH, FrameRef, frame_ref_from_json
from neptune.model.ids import LogicalId, RecordId, check_text, logical_id_from_json, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    Ambiguous,
    Inherited,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    from_json,
    to_json,
)
from neptune.model.provenance import (
    NO_HEADER,
    EvidenceRef,
    NoHeader,
    Provenance,
    Row,
    RowCell,
    check_evidence_record,
    evidence_record_json,
    evidence_record_object,
    evidence_ref_from_json,
    provenance_from_json,
)
from neptune.model.record import Family
from neptune.model.scalars import NonFinite, Real, real_from_json, real_to_json
from neptune.model.spatial import (
    CrsCode,
    GeodeticPosition,
    crs_code_from_json,
    geodetic_position_from_json,
)
from neptune.model.time import (
    INT64_MAX,
    Duration,
    Timestamp,
    duration_from_json,
    timestamp_from_json,
)
from neptune.model.units import Unit, unit_from_json

# EXIF Orientation (tag 0x0112) takes the values 1 to 8.
EXIF_ORIENTATIONS: Final = range(1, 9)
# Coordinates are lengths (a mesh, a projected map) or angles (a geographic CRS).
_COORDINATE_DIMENSIONS: Final = (LENGTH, ANGLE)


def _check_count(field: str, value: int, low: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int, got {value!r}")
    if not low <= value <= INT64_MAX:
        raise ValueError(f"{field} must be in [{low}, 2^63): {value}")


def _check_counts(field: str, knowledge: Knowledge[int], low: int) -> None:
    check_type(field, knowledge, int)
    for value in values_of(knowledge):
        _check_count(field, value, low)


def _check_named(identifiers: Identifiers, name: Knowledge[str]) -> None:
    if not identifiers and not isinstance(name, Known | Ambiguous):
        raise ValueError("a declaration names what it declares: an identifier or a stated name")


def _position(data: JsonValue) -> GeodeticPosition:
    return geodetic_position_from_json(data, provenance_from_json)


def _float(data: JsonValue) -> float:
    if not isinstance(data, float):
        raise ValueError(f"expected a float, got {data!r}")
    return data


def _ints(what: str) -> Callable[[JsonValue], int]:
    def decode(data: JsonValue) -> int:
        return json_int(data, what)

    return decode


# --- Sites and assets --------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """A place that one declaration names: a facility, a field, a building, a berth (ADR 0020 §1).

    ``provenance`` cites the declaration: a site register's row or a manifest entry (``stated``), a
    GeoJSON feature. What else the row says stays in its ``StructuredRecord``.

    - ``identifiers``: every id the declaration gives the site (``("register", "S-007")``).
    - ``name`` and ``aliases``: the name it gives the site and any other names, each cited.
    - ``parent``: the declared id of the site it is part of (a building's campus).
    - ``location``: the position the declaration gives it.

    A declaration names what it declares: a site has an identifier or a stated name.
    """

    kind: ClassVar[str] = "site"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    identifiers: Identifiers
    name: Knowledge[str]
    aliases: Names
    parent: Knowledge[LogicalId]
    location: Knowledge[GeodeticPosition]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_identifiers("identifiers", self.identifiers)
        check_text_values("name", self.name)
        check_names("aliases", self.aliases)
        _check_named(self.identifiers, self.name)
        check_type("parent", self.parent, LogicalId)
        check_type("location", self.location, GeodeticPosition)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "aliases": names_to_json(self.aliases),
                "identifiers": identifiers_to_json(self.identifiers),
                "location": to_json(self.location, GeodeticPosition.to_json),
                "name": to_json(self.name),
                "parent": to_json(self.parent, LogicalId.to_json),
            },
        )


def site_from_json(data: JsonValue) -> Site:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, Site.kind, {"aliases", "identifiers", "location", "name", "parent"}
    )
    return Site(
        id=record_id,
        provenance=provenance,
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        aliases=names_from_json(obj["aliases"], "aliases", provenance_from_json),
        parent=from_json(obj["parent"], logical_id_from_json, provenance_from_json),
        location=from_json(obj["location"], _position, provenance_from_json),
    )


@dataclass(frozen=True)
class Asset:
    """A physical thing in the world that one declaration names: a pump, a valve, a panel string,
    a pallet, a door (ADR 0020 §1).

    As ``Site``, plus what the declaration says about what it is and where it belongs:
    ``category`` is its declared type, verbatim (``centrifugal pump``); ``site`` and ``parent``
    are the declared ids of its site and of the asset it is part of.
    """

    kind: ClassVar[str] = "asset"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    identifiers: Identifiers
    name: Knowledge[str]
    aliases: Names
    category: Knowledge[str]
    site: Knowledge[LogicalId]
    parent: Knowledge[LogicalId]
    location: Knowledge[GeodeticPosition]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_identifiers("identifiers", self.identifiers)
        check_text_values("name", self.name)
        check_names("aliases", self.aliases)
        _check_named(self.identifiers, self.name)
        check_text_values("category", self.category)
        check_type("site", self.site, LogicalId)
        check_type("parent", self.parent, LogicalId)
        check_type("location", self.location, GeodeticPosition)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "aliases": names_to_json(self.aliases),
                "category": to_json(self.category),
                "identifiers": identifiers_to_json(self.identifiers),
                "location": to_json(self.location, GeodeticPosition.to_json),
                "name": to_json(self.name),
                "parent": to_json(self.parent, LogicalId.to_json),
                "site": to_json(self.site, LogicalId.to_json),
            },
        )


def asset_from_json(data: JsonValue) -> Asset:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        Asset.kind,
        {"aliases", "category", "identifiers", "location", "name", "parent", "site"},
    )
    return Asset(
        id=record_id,
        provenance=provenance,
        identifiers=identifiers_from_json(obj["identifiers"], provenance_from_json),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        aliases=names_from_json(obj["aliases"], "aliases", provenance_from_json),
        category=from_json(obj["category"], text_decoder("category"), provenance_from_json),
        site=from_json(obj["site"], logical_id_from_json, provenance_from_json),
        parent=from_json(obj["parent"], logical_id_from_json, provenance_from_json),
        location=from_json(obj["location"], _position, provenance_from_json),
    )


# --- Geometry ----------------------------------------------------------------------------------


class SpatialCategory(StrEnum):
    """What kind of geometry the file holds. Structural: the adapter knows what it read."""

    MESH = "mesh"  # triangles or polygons: OBJ, STL, PLY with faces, glTF
    CAD = "cad"  # solid or building models: STEP, IGES, IFC
    POINT_CLOUD = "point_cloud"  # points: PCD, LAS / LAZ, PLY without faces
    RASTER_MAP = "raster_map"  # a gridded map: a ROS occupancy map, a GeoTIFF, an orthophoto
    VECTOR_MAP = "vector_map"  # features: GeoJSON, a shapefile, a lanelet map
    SCENE = "scene"  # several placed objects: USD, an SDF world


@dataclass(frozen=True)
class SpatialArtifact:
    """A geometry file, by reference (ADR 0020 §2).

    ``provenance`` cites the file, or the part of a container that holds it. The geometry stays in
    those bytes: nothing is converted, re-meshed or re-projected. Objects inside it are cited with
    ``ObjectLocator`` by the ids the file gives them.

    - ``name``: the name the file gives the model, scene or map.
    - ``unit``: the unit its coordinates are written in, where the file or its format states one
      for all of them (glTF: metres). ``NotApplicable`` where a CRS gives each axis its own
      (longitude and latitude in degrees, height in metres).
    - ``crs``: the coordinate reference system it declares; ``NotCovered`` where the format has
      no place for one (STL).
    - ``frame``: the frame its coordinates are in, in a declared ``FrameGraph``; the ``Frame``
      record says what its axes are.
    """

    kind: ClassVar[str] = "spatial_artifact"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    category: SpatialCategory
    name: Knowledge[str]
    unit: Knowledge[Unit]
    crs: Knowledge[CrsCode]
    frame: Knowledge[FrameRef]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        if not isinstance(self.category, SpatialCategory):
            raise TypeError(f"category must be a SpatialCategory, got {self.category!r}")
        check_text_values("name", self.name)
        check_type("unit", self.unit, Unit)
        for unit in values_of(self.unit):
            if unit.dimension not in _COORDINATE_DIMENSIONS:
                raise ValueError(f"coordinates are lengths or angles, not {unit.symbol!r}")
        check_type("crs", self.crs, CrsCode)
        check_type("frame", self.frame, FrameRef)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "category": str(self.category),
                "crs": to_json(self.crs, CrsCode.to_json),
                "frame": to_json(self.frame, FrameRef.to_json),
                "name": to_json(self.name),
                "unit": to_json(self.unit, unit_json),
            },
        )


def spatial_artifact_from_json(data: JsonValue) -> SpatialArtifact:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, SpatialArtifact.kind, {"category", "crs", "frame", "name", "unit"}
    )
    return SpatialArtifact(
        id=record_id,
        provenance=provenance,
        category=enum_decoder(SpatialCategory)(obj["category"]),
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        unit=from_json(obj["unit"], unit_from_json, provenance_from_json),
        crs=from_json(obj["crs"], crs_code_from_json, provenance_from_json),
        frame=from_json(obj["frame"], frame_ref_from_json, provenance_from_json),
    )


# --- Images and video --------------------------------------------------------------------------


@dataclass(frozen=True)
class Capture:
    """What a media file declares about how it was made: EXIF, XMP, a container's metadata.

    ``time`` is the capture time on the clock the file states it on (usually the camera's own,
    zone often undeclared). ``position`` is the declared position (EXIF GPS). The capturing device
    is ``device_manufacturer``, ``device_model`` and ``device_identifiers`` (a body serial), which
    relate a photo to a machine's camera without guessing.
    """

    time: Knowledge[Timestamp]
    position: Knowledge[GeodeticPosition]
    device_manufacturer: Knowledge[str]
    device_model: Knowledge[str]
    device_identifiers: Identifiers

    def __post_init__(self) -> None:
        check_type("time", self.time, Timestamp)
        check_type("position", self.position, GeodeticPosition)
        check_text_values("device_manufacturer", self.device_manufacturer)
        check_text_values("device_model", self.device_model)
        check_identifiers("device_identifiers", self.device_identifiers)

    def to_json(self) -> JsonObject:
        return {
            "device_identifiers": identifiers_to_json(self.device_identifiers),
            "device_manufacturer": to_json(self.device_manufacturer),
            "device_model": to_json(self.device_model),
            "position": to_json(self.position, GeodeticPosition.to_json),
            "time": to_json(self.time, Timestamp.to_json),
        }


def capture_from_json(data: JsonValue) -> Capture:
    obj = exact_object(
        data,
        "capture",
        {"device_identifiers", "device_manufacturer", "device_model", "position", "time"},
    )
    return Capture(
        time=from_json(obj["time"], timestamp_from_json, provenance_from_json),
        position=from_json(obj["position"], _position, provenance_from_json),
        device_manufacturer=from_json(
            obj["device_manufacturer"], text_decoder("device_manufacturer"), provenance_from_json
        ),
        device_model=from_json(
            obj["device_model"], text_decoder("device_model"), provenance_from_json
        ),
        device_identifiers=identifiers_from_json(obj["device_identifiers"], provenance_from_json),
    )


def _check_raster(width: int, height: int, encoding: str) -> None:
    _check_count("width", width, 1)
    _check_count("height", height, 1)
    if not isinstance(encoding, str):
        raise TypeError(f"encoding must be a str, got {type(encoding).__name__}")
    check_text("encoding", encoding)


@dataclass(frozen=True)
class Image:
    """A standalone still image (ADR 0020 §3).

    ``provenance`` cites the image's bytes. ``width`` and ``height`` are the stored raster's, in
    pixels, and ``encoding`` is the format the adapter decoded (``jpeg``, ``png``, ``tiff``). A
    region is cited as ``ImageRegion`` in the stored raster: origin top-left, EXIF orientation not
    applied. ``orientation`` is the EXIF Orientation value the file declares (1 to 8), kept and
    never applied. What a detector or a model sees in the image is derived.
    """

    kind: ClassVar[str] = "image"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    width: int
    height: int
    encoding: str
    orientation: Knowledge[int]
    capture: Capture

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        _check_raster(self.width, self.height, self.encoding)
        check_type("orientation", self.orientation, int)
        for value in values_of(self.orientation):
            if isinstance(value, bool) or value not in EXIF_ORIENTATIONS:
                raise ValueError(f"EXIF orientation is 1 to 8, got {value!r}")
        if not isinstance(self.capture, Capture):
            raise TypeError(f"capture must be a Capture, got {self.capture!r}")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "capture": self.capture.to_json(),
                "encoding": self.encoding,
                "height": self.height,
                "orientation": to_json(self.orientation),
                "width": self.width,
            },
        )


def image_from_json(data: JsonValue) -> Image:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, Image.kind, {"capture", "encoding", "height", "orientation", "width"}
    )
    return Image(
        id=record_id,
        provenance=provenance,
        width=json_int(obj["width"], "width"),
        height=json_int(obj["height"], "height"),
        encoding=json_str(obj["encoding"], "encoding"),
        orientation=from_json(obj["orientation"], _ints("orientation"), provenance_from_json),
        capture=capture_from_json(obj["capture"]),
    )


@dataclass(frozen=True)
class Video:
    """One video track of a standalone video file (ADR 0020 §3).

    ``provenance`` cites the track's declaration (an MP4 ``trak`` box). ``track`` is its 0-based
    position among the container's tracks, as in ``VideoFrame``. ``width``, ``height`` and
    ``encoding`` (the codec, as the sample entry names it: ``avc1``) are declared. ``clock`` is the
    ``TimestampDomain`` of its presentation times, and ``frame_count`` and ``duration`` are what the
    container declares. A frame is cited as ``VideoFrame(track, index, pts)``, and a region of it
    as that step followed by an ``ImageRegion``. A video inside a log is a stream (ADR 0018).
    """

    kind: ClassVar[str] = "video"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    track: int
    width: int
    height: int
    encoding: str
    clock: RecordId
    frame_count: Knowledge[int]
    duration: Knowledge[Duration]
    capture: Capture

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        _check_count("track", self.track, 0)
        _check_raster(self.width, self.height, self.encoding)
        parse_record_id(self.clock)
        _check_counts("frame_count", self.frame_count, 0)
        check_type("duration", self.duration, Duration)
        for duration in values_of(self.duration):
            if duration.domain_id != self.clock:
                raise ValueError(f"duration is on a clock other than the track's: {duration}")
        if not isinstance(self.capture, Capture):
            raise TypeError(f"capture must be a Capture, got {self.capture!r}")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "capture": self.capture.to_json(),
                "clock": self.clock,
                "duration": to_json(self.duration, Duration.to_json),
                "encoding": self.encoding,
                "frame_count": to_json(self.frame_count),
                "height": self.height,
                "track": self.track,
                "width": self.width,
            },
        )


def video_from_json(data: JsonValue) -> Video:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data,
        Video.kind,
        {"capture", "clock", "duration", "encoding", "frame_count", "height", "track", "width"},
    )
    return Video(
        id=record_id,
        provenance=provenance,
        track=json_int(obj["track"], "track"),
        width=json_int(obj["width"], "width"),
        height=json_int(obj["height"], "height"),
        encoding=json_str(obj["encoding"], "encoding"),
        clock=parse_record_id(json_str(obj["clock"], "clock")),
        frame_count=from_json(obj["frame_count"], _ints("frame_count"), provenance_from_json),
        duration=from_json(obj["duration"], duration_from_json, provenance_from_json),
        capture=capture_from_json(obj["capture"]),
    )


# --- Documents ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentPage:
    """One page as the document declares it; its position in ``pages`` is its ``Page`` index.

    ``label`` is the page label the document declares (``iv``, ``A-3``), never the index.
    ``width`` and ``height`` are in the page's own coordinate system, the one ``PageRegion`` uses
    (PDF default user space, as stored). ``rotation`` is the declared clockwise rotation in
    degrees (PDF ``/Rotate``), kept and never applied.
    """

    label: Knowledge[str]
    width: Knowledge[float]
    height: Knowledge[float]
    rotation: Knowledge[int]

    def __post_init__(self) -> None:
        check_text_values("label", self.label)
        for name in ("width", "height"):
            check_type(name, getattr(self, name), float)
        check_type("rotation", self.rotation, int)
        if any(isinstance(value, bool) for value in values_of(self.rotation)):
            raise ValueError("rotation must be an integer number of degrees")

    def to_json(self) -> JsonObject:
        return {
            "height": to_json(self.height),
            "label": to_json(self.label),
            "rotation": to_json(self.rotation),
            "width": to_json(self.width),
        }


def document_page_from_json(data: JsonValue) -> DocumentPage:
    obj = exact_object(data, "document page", {"height", "label", "rotation", "width"})
    return DocumentPage(
        label=from_json(obj["label"], text_decoder("label"), provenance_from_json),
        width=from_json(obj["width"], _float, provenance_from_json),
        height=from_json(obj["height"], _float, provenance_from_json),
        rotation=from_json(obj["rotation"], _ints("rotation"), provenance_from_json),
    )


@dataclass(frozen=True)
class DocumentRecord:
    """A document as a whole (ADR 0020 §4): a PDF, a Markdown file, a text file.

    ``provenance`` cites its bytes. ``format`` is the format the adapter decoded (``pdf``,
    ``markdown``, ``text``). ``title`` is the title it declares (PDF ``/Title``, front matter).
    ``pages`` lists every page of a paged document in document order, and is empty for a document
    that has none. Its text is in ``DocumentBlock`` records, each citing its exact span.
    """

    kind: ClassVar[str] = "document_record"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    format: str
    title: Knowledge[str]
    pages: tuple[DocumentPage, ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        if not isinstance(self.format, str):
            raise TypeError(f"format must be a str, got {type(self.format).__name__}")
        check_text("format", self.format)
        check_text_values("title", self.title)
        if not isinstance(self.pages, tuple):
            raise TypeError(f"pages must be a tuple, got {type(self.pages).__name__}")
        for page in self.pages:
            if not isinstance(page, DocumentPage):
                raise TypeError(f"not a DocumentPage: {page!r}")

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "format": self.format,
                "pages": [page.to_json() for page in self.pages],
                "title": to_json(self.title),
            },
        )


def document_record_from_json(data: JsonValue) -> DocumentRecord:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, DocumentRecord.kind, {"format", "pages", "title"}
    )
    return DocumentRecord(
        id=record_id,
        provenance=provenance,
        format=json_str(obj["format"], "format"),
        title=from_json(obj["title"], text_decoder("title"), provenance_from_json),
        pages=tuple(document_page_from_json(page) for page in json_array(obj["pages"], "pages")),
    )


class BlockRole(StrEnum):
    """What a block is, where the format declares it: Markdown syntax, a tagged PDF's structure,
    a word processor's paragraph style. A guess from font size or position is derived."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"  # its cells are a StructuredTable's, citing the same page or span
    FIGURE = "figure"
    CAPTION = "caption"
    CODE = "code"
    QUOTE = "quote"
    FORMULA = "formula"
    HEADER = "header"  # a running page header
    FOOTER = "footer"
    FOOTNOTE = "footnote"


@dataclass(frozen=True)
class DocumentBlock:
    """One unit of text the transform extracts from a document, in reading order (ADR 0020 §4).

    ``provenance`` cites exactly where the text is: ``[Page(p), Span(start, end)]`` in a page's
    extracted text, or ``[Span(start, end)]`` in an unpaged document's. How the text is split into
    blocks is the adapter's documented, deterministic rule (CommonMark's blocks; the pinned PDF
    extractor's). ``document`` is the ``DocumentRecord`` it belongs to, and ``order`` its 0-based
    position in the transform's reading order.

    - ``role`` and ``level`` (a heading's or list's depth, from 1): what the format declares the
      block to be. ``Unknown`` for an untagged PDF's text, whose roles are derived.
    - ``text``: the block's text exactly as the cited span holds it; ``NotApplicable`` for a figure.
    - ``region``: where the block is drawn: ``[Page(p), PageRegion(…)]`` in the page's own
      coordinates; ``NotApplicable`` in an unpaged document.
    """

    kind: ClassVar[str] = "document_block"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    document: RecordId
    order: int
    role: Knowledge[BlockRole]
    level: Knowledge[int]
    text: Knowledge[str]
    region: Knowledge[EvidenceRef]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.document)
        _check_count("order", self.order, 0)
        check_type("role", self.role, BlockRole)
        _check_counts("level", self.level, 1)
        check_text_values("text", self.text)
        check_type("region", self.region, EvidenceRef)

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "document": self.document,
                "level": to_json(self.level),
                "order": self.order,
                "region": to_json(self.region, EvidenceRef.to_json),
                "role": to_json(self.role, str),
                "text": to_json(self.text),
            },
        )


def document_block_from_json(data: JsonValue) -> DocumentBlock:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, DocumentBlock.kind, {"document", "level", "order", "region", "role", "text"}
    )
    return DocumentBlock(
        id=record_id,
        provenance=provenance,
        document=parse_record_id(json_str(obj["document"], "document")),
        order=json_int(obj["order"], "order"),
        role=from_json(obj["role"], enum_decoder(BlockRole), provenance_from_json),
        level=from_json(obj["level"], _ints("level"), provenance_from_json),
        text=from_json(obj["text"], text_decoder("text"), provenance_from_json),
        region=from_json(obj["region"], evidence_ref_from_json, provenance_from_json),
    )


# --- Tables ------------------------------------------------------------------------------------

# A cell as its source types it: text for CSV, and a spreadsheet's or a typed file's own numbers
# and booleans. Nothing is inferred: "3.5" in a CSV stays text.
CellValue: TypeAlias = str | int | bool | Real


def _check_cell_value(value: CellValue) -> None:
    if isinstance(value, str):
        check_text("cell", value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"a non-finite cell is a NonFinite, got {value!r}")
    elif not isinstance(value, int | NonFinite):
        raise TypeError(f"a cell holds text, an int, a bool or a real, got {value!r}")


def _cell_json(value: CellValue) -> JsonValue:
    if isinstance(value, NonFinite | float):
        return real_to_json(value)
    return value


def _cell_from_json(data: JsonValue) -> CellValue:
    if isinstance(data, str | bool):
        return data
    if is_int(data):
        return data
    return real_from_json(data)


def _header(data: JsonValue) -> tuple[str, ...]:
    return tuple(json_str(name, "header cell") for name in json_array(data, "header"))


@dataclass(frozen=True)
class StructuredTable:
    """A table as its source declares it (ADR 0020 §5): a CSV file, a spreadsheet's sheet, a
    table in a document. Its rows are ``StructuredRecord`` records.

    ``provenance`` cites the table. ``name`` is the name it declares (a sheet's name, a caption).
    ``header`` is the header row's cells, verbatim (``""`` for a blank header cell), citing that
    row; ``NotApplicable`` when the table has no header row, and ``Unknown`` when the source does
    not say whether its first row is one (a CSV, unless the adapter's config says).
    """

    kind: ClassVar[str] = "structured_table"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    name: Knowledge[str]
    header: Knowledge[tuple[str, ...]]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        check_text_values("name", self.name)
        check_type("header", self.header, tuple)
        for header in values_of(self.header):
            if not header:
                raise ValueError("a header row has at least one cell")
            for name in header:
                if not isinstance(name, str):
                    raise TypeError(f"header cells are text, got {name!r}")
                if name:
                    check_text("header cell", name)

    def column_name(self, column: int) -> str | NoHeader:
        """The declared name of column ``column``, as ``RowCell`` cites it."""
        match self.header:
            case Known(value=header) if column < len(header):
                return header[column]
            case _:
                return NO_HEADER

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {"header": to_json(self.header, list), "name": to_json(self.name)},
        )


def structured_table_from_json(data: JsonValue) -> StructuredTable:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, StructuredTable.kind, {"header", "name"}
    )
    return StructuredTable(
        id=record_id,
        provenance=provenance,
        name=from_json(obj["name"], text_decoder("name"), provenance_from_json),
        header=from_json(obj["header"], _header, provenance_from_json),
    )


def _slots(knowledge: Knowledge[CellValue]) -> list[object]:
    """Every provenance slot a cell state has: its own, or each candidate's."""
    match knowledge:
        case NotApplicable():
            return []
        case Ambiguous(candidates=candidates):
            return [candidate.provenance for candidate in candidates]
        case _:
            return [knowledge.provenance]


@dataclass(frozen=True)
class StructuredRecord:
    """One row of a ``StructuredTable``, cell by cell as declared (ADR 0020 §5).

    ``provenance`` cites the row, ``table`` is its table, and ``row`` its 0-based position the way
    ``Row`` counts it (header rows included). ``cells`` holds each cell by column position; a
    short row keeps its length.

    - A blank cell is ``Unknown``, never ``""`` or ``"none"``. A token the source defines as
      "none" (a register's legend) is ``KnownAbsent`` citing that definition; the text ``none``
      with no such definition is ``Known("none")``.
    - Every cell cites its exact cell. In a row cited as ``Row(row)``, cell ``c`` is at
      ``RowCell(row, c, name)``: the row's evidence with its last step replaced
      (``cell_evidence``), so its states may inherit. A row cited otherwise (a JSON array element,
      a table on a PDF page) gives each cell its own provenance.
    """

    kind: ClassVar[str] = "structured_record"
    family: ClassVar[Family] = Family.WORLD
    id: RecordId
    provenance: Provenance
    table: RecordId
    row: int
    cells: tuple[Knowledge[CellValue], ...]

    def __post_init__(self) -> None:
        check_evidence_record(self.id, self.provenance)
        parse_record_id(self.table)
        _check_count("row", self.row, 0)
        if not isinstance(self.cells, tuple):
            raise TypeError(f"cells must be a tuple, got {type(self.cells).__name__}")
        by_row = self.provenance.evidence.locator[-1] == Row(self.row)
        for column, cell in enumerate(self.cells):
            check_type(f"cell {column}", cell, str | int | bool | float | NonFinite)
            for value in values_of(cell):
                _check_cell_value(value)
            if not by_row and any(isinstance(slot, Inherited) for slot in _slots(cell)):
                raise ValueError(
                    f"cell {column} needs its own provenance: its row is not cited as a Row step"
                )

    def cell_evidence(self, table: StructuredTable, column: int) -> EvidenceRef:
        """Where cell ``column`` of this row is: its ``RowCell`` when the row is cited as a ``Row``,
        otherwise the cell's own citation.

        This is the cell's place, not the grounds for its state: a ``KnownAbsent`` cell's
        provenance cites the definition that makes it "none". Such a cell in a row not cited as a
        ``Row`` has no citation of its own place, and resolves to the row.
        """
        if table.id != self.table:
            raise ValueError(f"row {self.id} is not in table {table.id}")
        if not 0 <= column < len(self.cells):
            raise ValueError(f"row {self.row} has {len(self.cells)} cells, so no cell {column}")
        evidence = self.provenance.evidence
        if evidence.locator[-1] == Row(self.row):
            step = RowCell(self.row, column, table.column_name(column))
            return EvidenceRef(evidence.source, (*evidence.locator[:-1], step))
        cell = self.cells[column]
        if not isinstance(cell, KnownAbsent):
            for slot in _slots(cell):
                if isinstance(slot, Provenance):
                    return slot.evidence
        return evidence

    def to_json(self) -> JsonObject:
        return evidence_record_json(
            self.kind,
            self.id,
            self.provenance,
            {
                "cells": [to_json(cell, _cell_json) for cell in self.cells],
                "row": self.row,
                "table": self.table,
            },
        )


def structured_record_from_json(data: JsonValue) -> StructuredRecord:
    """Parse strictly: unexpected or missing keys and wrongly typed values are errors."""
    obj, record_id, provenance = evidence_record_object(
        data, StructuredRecord.kind, {"cells", "row", "table"}
    )
    return StructuredRecord(
        id=record_id,
        provenance=provenance,
        table=parse_record_id(json_str(obj["table"], "table")),
        row=json_int(obj["row"], "row"),
        cells=tuple(
            from_json(cell, _cell_from_json, provenance_from_json)
            for cell in json_array(obj["cells"], "cells")
        ),
    )
