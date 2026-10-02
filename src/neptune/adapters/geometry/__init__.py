"""Meshes and scenes as referenced geometry objects: OBJ, STL, PLY, glTF/GLB and USD (ADR 0052).

Geometry is spatial evidence, kept as geometry. This adapter reads what a file declares and what
one bounded pass measures, and copies no vertex, face or texel:

- one ``SpatialArtifact`` per file citing the whole file, which is the lazy handle to the raw
  geometry (nothing is copied; the source's content id is its identity), with its category, the
  name the file gives, and its unit where the format or the file states one (glTF: metres, USD:
  ``metersPerUnit``; OBJ, STL and PLY have none, so it is Unknown). A CRS and a frame have no
  place in these formats: ``NotCovered``;
- a ``geometry properties`` table, one row per property, each citing the exact bytes it comes
  from: counts, bounds, up axis, scale, format version, encoding, and the header's own declared
  counts beside the measured ones;
- a ``geometry dependencies`` table, one row per material library, texture, buffer or sublayer the
  file names, with the scope its text implies (relative, parent, absolute, uri).

Vertices are never decoded into records: bounds are taken in one bounded pass and are in the
file's own coordinate units, unconverted. ``stated`` is what the file or its format's
specification declares; ``observed`` is what this adapter measured. A reference is only classified,
never opened or fetched: a path that cannot stay in the source root is a finding, and whether the
rest are present is a check across sources. Every problem is a finding; limits bound every scan.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Final

from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.adapters.geometry import _detect, _gltf, _obj, _ply, _stl, _usd
from neptune.adapters.geometry._context import Context
from neptune.adapters.geometry._emit import (
    FINDINGS,
    LIMIT_EXCEEDED,
    PROPERTY_STEP,
    TABLE_STEP,
    UNREADABLE,
    Emitter,
    Geometry,
    emit,
)
from neptune.adapters.geometry._scan import LimitHit, Scanner, Unreadable
from neptune.model.world import SpatialArtifact

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

DEFAULT_MAX_SCAN_BYTES: Final = 256 * 1024 * 1024
DEFAULT_MAX_VERTICES: Final = 2_000_000
DEFAULT_MAX_HEADER_BYTES: Final = 1024 * 1024
DEFAULT_MAX_JSON_BYTES: Final = 16 * 1024 * 1024
DEFAULT_MAX_JSON_DEPTH: Final = 64
DEFAULT_MAX_ENTRIES: Final = 100_000
DEFAULT_MAX_VALUE_BYTES: Final = 4096

DESCRIPTOR: Final = AdapterDescriptor(
    id="geometry",
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Meshes and scenes by reference: units, up axis, counts, bounds and dependencies.",
    formats=(
        FormatSpec(
            "glTF binary",
            media_types=("model/gltf-binary",),
            extensions=(".glb",),
            magic=(Magic(0, b"glTF"),),
        ),
        FormatSpec("glTF JSON", media_types=("model/gltf+json",), extensions=(".gltf",)),
        FormatSpec(
            "PLY",
            media_types=("application/x-ply", "model/ply"),
            extensions=(".ply",),
            magic=(Magic(0, b"ply\n"),),
        ),
        FormatSpec("STL", media_types=("model/stl",), extensions=(".stl",)),
        FormatSpec(
            "USD ASCII",
            media_types=("model/vnd.usda",),
            extensions=(".usd", ".usda"),
            magic=(Magic(0, b"#usda "),),
        ),
        FormatSpec(
            "USD crate",
            media_types=("model/vnd.usd",),
            extensions=(".usdc",),
            magic=(Magic(0, b"PXR-USDC"),),
        ),
        FormatSpec("Wavefront OBJ", media_types=("model/obj",), extensions=(".obj",)),
    ),
    record_kinds=("spatial_artifact", "structured_record", "structured_table"),
    config=(
        ConfigOption(
            "max_entries",
            DEFAULT_MAX_ENTRIES,
            "dependencies, elements and accessors one source may name; past it parsing"
            " stops with geometry.limit_exceeded",
        ),
        ConfigOption(
            "max_header_bytes",
            DEFAULT_MAX_HEADER_BYTES,
            "the largest PLY header or USD layer metadata block read; a longer one stops"
            " parsing with geometry.limit_exceeded",
        ),
        ConfigOption(
            "max_json_bytes",
            DEFAULT_MAX_JSON_BYTES,
            "the largest glTF JSON document or GLB JSON chunk parsed",
        ),
        ConfigOption(
            "max_json_depth",
            DEFAULT_MAX_JSON_DEPTH,
            "the deepest JSON nesting parsed (glTF) or metadata nesting tokenised (USD),"
            " counted before any parse",
        ),
        ConfigOption(
            "max_scan_bytes",
            DEFAULT_MAX_SCAN_BYTES,
            "the bytes one source may have read in all; past it a scan stops and its"
            " counts and bounds are NotCovered",
        ),
        ConfigOption(
            "max_value_bytes",
            DEFAULT_MAX_VALUE_BYTES,
            "the longest reference text copied into a cell; a longer one is NotCovered",
        ),
        ConfigOption(
            "max_vertices",
            DEFAULT_MAX_VERTICES,
            "the vertices one source may have scanned; a file declaring or holding more"
            " is not scanned and its counts and bounds are NotCovered",
        ),
    ),
    libraries=(),
    finding_codes=tuple(
        Documented(code, f"{description} ({category}, {severity})")
        for code, (category, severity, description) in sorted(FINDINGS.items())
    ),
    locator_steps=(
        Documented(
            PROPERTY_STEP,
            "the property named by `name` of the bytes the previous step"
            " cites: a count, a bound, an axis or a scale read from them",
        ),
        Documented(
            TABLE_STEP,
            "one of the source's two tables, `properties` or `dependencies`,"
            " standing for the whole file the previous step cites",
        ),
    ),
    conventions=(
        Documented(
            "assertion_kinds",
            "stated: what the file or its format's specification declares (a header count,"
            " glTF's metres and +Y, USD upAxis, metersPerUnit and defaultPrim, glTF"
            " accessor min and max, every reference); observed: what the adapter decoded"
            " (counts of statements, array lengths and facets, bounds of the vertices,"
            " the format and encoding, a reference's scope)",
        ),
        Documented(
            "chunks",
            "one chunk per source, context {part: geometry}: the reads are"
            " bounded passes of one file and nothing carries between them",
        ),
        Documented(
            "dependencies",
            "table `geometry dependencies`, header (kind, target, scope), one row per"
            " reference citing its exact bytes: material_library (OBJ mtllib, the rest of"
            " the line), texture (PLY TextureFile comment, glTF images), buffer (glTF),"
            " sublayer (USD). scope is relative, parent, absolute, uri or embedded; data:"
            " URIs are counted (embedded_resource_count), not listed. Nothing is opened",
        ),
        Documented(
            "objects",
            "an object inside the artifact is cited by the artifact's citation then an"
            " ObjectLocator: OBJ o name, glTF node or mesh name, USD prim path",
        ),
        Documented(
            "properties",
            "table `geometry properties`, header (property, v0, v1, v2): one row per"
            " property, cells in order. encoding, format_version; vertex_count,"
            " face_count, facet_count, object_count and glTF array counts; declared_*"
            " counts (stated) beside measured ones; bounds_min and bounds_max (x, y, z"
            " in the file's own units, finite vertices only); up_axis (the file's token,"
            " or glTF's Y); meters_per_unit. Missing values are explicit: Unknown (the"
            " file could have said), NotCovered (the format has no place, or the scan"
            " was cut), NotApplicable (no vertices)",
        ),
        Documented(
            "units",
            "glTF: metres by the specification. USD: metersPerUnit mapped to a unit only"
            " for 1, 0.01, 0.001, 0.0254, 0.3048, 1000 and 1e-6; absent or another scale:"
            " Unknown. OBJ, STL, PLY: Unknown. A binary USD crate: NotCovered",
        ),
    ),
    resources=Resources(max_memory=512 * 1024 * 1024, streaming=True),
    security=(
        "Decodes no geometry into records: vertices are scanned once for bounds, never kept.",
        "Bounds every scan: max_scan_bytes, max_vertices, a 64 KiB line cap, max_header_bytes.",
        "Checks every declared count against the file's size before reading rows; reads only"
        " the rows that fit.",
        "Counts JSON nesting outside strings before parsing glTF (max_json_depth), refuses NaN"
        " and Infinity, and parses no more than max_json_bytes.",
        "Never opens, resolves or fetches a reference; classifies its text only.",
        "Keeps hostile text out of finding messages.",
    ),
)

_READ: Final[dict[str, Callable[[Context], Geometry]]] = {
    _detect.OBJ: _obj.read,
    _detect.STL_BINARY: _stl.read_binary,
    _detect.STL_ASCII: _stl.read_ascii,
    _detect.PLY: _ply.read,
    _detect.GLTF: lambda ctx: _gltf.read(ctx, glb=False),
    _detect.GLB: lambda ctx: _gltf.read(ctx, glb=True),
    _detect.USDA: _usd.read_layer,
    _detect.USDC: _usd.read_crate,
}


def _context(source: SourceReader, config: AdapterConfig) -> Context:
    return Context(
        out=Emitter(source, config),
        scan=Scanner(source, config.integer("max_scan_bytes")),
        size=source.size,
        max_vertices=config.integer("max_vertices"),
        max_header_bytes=config.integer("max_header_bytes"),
        max_json_bytes=config.integer("max_json_bytes"),
        max_json_depth=config.integer("max_json_depth"),
        max_entries=config.integer("max_entries"),
    )


def _read(source: SourceReader, config: AdapterConfig) -> Context:
    """Every record and finding of ``source``: the whole of this adapter's work."""
    ctx = _context(source, config)
    head = b"".join(read_pieces(source, 0, min(source.size, PROBE_HEAD_SIZE)))
    found = _detect.lenient(head, source.size)
    if found is None:
        ctx.out.finding(
            UNREADABLE,
            ctx.whole,
            "the bytes start no geometry format this adapter reads",
        )
        return ctx
    try:
        geometry = _READ[found.format](ctx)
    except Unreadable as exc:
        ctx.out.finding(
            UNREADABLE, ctx.span(exc.offset, exc.length if exc.length is not None else 1),
            f"the {found.format} file is not readable: {exc}",
        )  # fmt: skip
        return ctx
    except LimitHit as hit:
        ctx.out.finding(
            LIMIT_EXCEEDED, ctx.whole,
            f"{hit.option} ({hit.limit}) stopped parsing before any record could be made",
            {"limit": hit.limit, "option": hit.option},
        )  # fmt: skip
        return ctx
    emit(ctx.out, geometry, source.size, config.integer("max_value_bytes"))
    return ctx


class GeometryAdapter:
    """The geometry adapter. It has no planning granularity: every source is one chunk."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        found = _detect.detect(head, hints.size) or _detect.detect_by_name(hints.name, hints.size)
        if found is None:
            reason = ProbeReason(
                "geometry.no_signature", "the head starts no geometry format read here"
            )
            return ProbeResult(0.0, (reason,))
        return ProbeResult(
            found.confidence,
            (ProbeReason(f"geometry.{found.format}", found.reason),),
            found.version,
        )

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        """The format and its version from the head alone: no pass over the geometry."""
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        found = _detect.lenient(head, source.size)
        summary: dict[str, JsonValue] = {"size": source.size}
        if found is not None:
            summary["format"] = found.format
            if found.version is not None:
                summary["version"] = found.version
        return InspectResult(summary, ())

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return Plan((make_chunk(source, config, {"part": "geometry"}, source.size),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        ctx = _read(source, config)
        return ChunkOutput(records=ctx.out.records, findings=ctx.out.findings)


__all__ = ["DESCRIPTOR", "GeometryAdapter", "SpatialArtifact"]
