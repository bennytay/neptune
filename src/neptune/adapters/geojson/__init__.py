"""GeoJSON (RFC 7946, and the 2008 dialect with a ``crs`` member) as sites, features and bounds
(MVL-31, ADR 0057).

A file becomes one ``SpatialArtifact`` (category ``vector_map``, its CRS as the file states it),
two tables, and a ``Site`` or ``Asset`` for each feature that names one:

- ``features``: one ``StructuredRecord`` per feature: its id, geometry type, position count and
  plain bounds (``min_x`` ... ``max_z``). Bounds are ``observed`` (read from the bytes); a
  geometry's type and a feature's id are ``stated``. Each cell cites its bytes.
- ``properties``: one row per leaf of a feature's ``properties``, ``(feature, key, value)``: the
  key a JSON pointer inside ``properties``, the value as the JSON types it (``stated``).
- ``Site`` / ``Asset``: a feature with a ``site_id`` or ``asset_id`` property. Those, and the
  feature's own ``id``, are identity *hints* kept as the source writes them; nothing is resolved
  or merged (MVL-35). A Point in a geographic CRS is the location; anything else is Unknown.

**The CRS is never invented** (ADR 0057 §3). A ``crs`` member is stated as written (verbatim
authority and code); two different ones are Ambiguous; a null, link or unreadable one is Unknown.
With no ``crs`` member RFC 7946's default (``OGC:CRS84``, longitude then latitude) is stated, cited
at the root ``type``, only if nothing contradicts it: positions outside longitude and latitude's
range, a ``crs`` on a feature, or a file that breaks off first make it Unknown, with a finding.
Coordinates are validated (range for a known geographic CRS, ring closure and winding) and never
changed: no reprojection, no wrapping, no repair.

Probing reads content: a root object whose ``type`` is a GeoJSON type and which holds the member
that type needs (``features``, ``geometry``, ``coordinates`` or ``geometries``) is ``VERIFIED``, so
a site file is never left to the configuration or text adapters. ``plan`` walks the file once
through a bounded window, cuts the features into blocks, and works out the file's CRS from the
whole file; each ``ingest`` reads one block.
"""

from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    VERIFIED,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
)
from neptune.adapters.geojson import _crs
from neptune.adapters.geojson._common import (
    ADAPTER_ID,
    BOM,
    CODES,
    GEOJSON_TYPES,
    GEOMETRY_TYPES,
    Limits,
    cite,
    context_int,
    context_text,
    finding,
    shown,
)
from neptune.adapters.geojson._geometry import measure
from neptune.adapters.geojson._records import Shared, feature_output, root_output
from neptune.adapters.geojson._scan import (
    DEEP,
    ArrayState,
    Reader,
    ScanError,
    decode_value,
    read_string,
    skip_ws,
)
from neptune.model.finding import IngestFinding
from neptune.model.jsonvalue import JsonObject

# Blocks are cut between features by constants of the adapter's version, never by a setting.
BLOCK_FEATURES: Final = 2048
BLOCK_BYTES: Final = 1024 * 1024

DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="GeoJSON as a vector map: features, plain bounds, properties, site and asset hints,"
    " and a CRS that is stated, defaulted by RFC 7946 or Unknown, never invented.",
    formats=(
        FormatSpec(
            "GeoJSON",
            media_types=("application/geo+json",),
            extensions=(".geojson", ".json"),
        ),
    ),
    record_kinds=(
        "asset",
        "site",
        "spatial_artifact",
        "structured_record",
        "structured_table",
    ),
    config=(
        ConfigOption(
            "max_depth",
            32,
            "GeometryCollections nesting deeper, or properties nesting deeper, are not read",
        ),
        ConfigOption(
            "max_feature_bytes",
            8 * 1024 * 1024,
            "a feature (or a root member) longer than this, and what follows it, is not read",
        ),
        ConfigOption(
            "max_features",
            10_000_000,
            "features past this many are not read",
        ),
        ConfigOption(
            "max_positions",
            500_000,
            "a geometry holding more positions is not walked: its bounds are Unknown",
        ),
        ConfigOption(
            "max_properties",
            4096,
            "property values of a feature past this many have no rows",
        ),
        ConfigOption(
            "max_source_bytes",
            1024 * 1024 * 1024,
            "a source longer than this is not read",
        ),
    ),
    libraries=(),
    finding_codes=tuple(
        Documented(f"{ADAPTER_ID}.{name}", f"{text} ({category}, {severity})")
        for name, (category, severity, text) in sorted(CODES.items())
    ),
    locator_steps=(
        Documented(
            "geojson:properties",
            "after the byte range of the features array: the table of the features' properties,"
            " columns feature, key, value; a row is cited by the bytes of its member",
        ),
    ),
    conventions=(
        Documented(
            "bounds",
            "plain minimum and maximum of the finite coordinates per axis in the CRS's own"
            " numbers, from the geometry's bytes; never reprojected or wrapped at the"
            " antimeridian; Unknown if the geometry is invalid, over budget or empty; z is"
            " NotCovered when no position has a third number",
        ),
        Documented(
            "chunks",
            "chunk 0 holds the artifact and the two tables; every other chunk holds whole"
            " features (at most 2,048 or 1 MiB of them) and its context gives the byte range, the"
            " first feature's index, the features array and the file's CRS decision",
        ),
        Documented(
            "crs",
            "root crs member: stated, authority and code verbatim from an OGC URN or URI or"
            " AUTHORITY:CODE; none: RFC 7946's default OGC:CRS84 stated by the specification and"
            " cited at the root type, unless a position is outside longitude and latitude's"
            " range, a feature has a crs, or the file breaks off first (Unknown); null, link or"
            " unreadable: Unknown; two different: Ambiguous",
        ),
        Documented(
            "features",
            "row i of the features table is the i-th element of the features array (a Feature or"
            " a bare geometry root is row 0); columns "
            + ", ".join(
                (
                    "id",
                    "geometry_type",
                    "positions",
                    "min_x",
                    "min_y",
                    "max_x",
                    "max_y",
                    "min_z",
                    "max_z",
                )
            )
            + "; the header is the adapter's, not the file's",
        ),
        Documented(
            "geographic",
            "range, winding and a Site or Asset location apply only where the CRS is the default,"
            " OGC:CRS84/CRS83/CRS27 or EPSG:4326/4269/4258: positions are longitude, latitude"
            " (GeoJSON's order, whatever the authority's)",
        ),
        Documented(
            "identity",
            "a feature with an asset_id property is an Asset, else with a site_id a Site; the"
            " identifiers are (asset_id, v), (site_id, v) and (geojson.feature_id, id), strings"
            " (an integer as written); name and category are the properties of that name; never"
            " resolved or merged",
        ),
        Documented(
            "probe",
            "a root object whose type is a GeoJSON type and which has the member it needs"
            " (features, geometry, coordinates or geometries) in the head: VERIFIED; anything else"
            " is declined",
        ),
        Documented(
            "properties",
            "one row per leaf of the properties object in document order, key a JSON pointer"
            " inside it, row the leaf's position within its feature (the table is keyed by"
            " feature and row); null is KnownAbsent citing itself; '', {} and [] are Unknown; a"
            " repeated name is Unknown; an integer outside int64 keeps its literal text",
        ),
    ),
    resources=Resources(max_memory=512 * 1024 * 1024, streaming=True),
    security=(
        "Decodes UTF-8 only; a feature that is not UTF-8 has no record.",
        "Reads through a window bounded by max_feature_bytes; a value nested deeper than the"
        " interpreter reads is skipped by counting brackets, never by recursion.",
        "Geometries are walked with a stack and bounded by max_positions and max_depth; NaN,"
        " infinities and huge numbers are findings, not exceptions; no CRS link is followed.",
    ),
)


class HeadInfo:
    """What the head of a source says about its root object."""

    def __init__(self, kind: str | None, openers: dict[str, str]) -> None:
        self.kind = kind
        self.openers = openers  # member name -> the first character of its value

    @property
    def shaped(self) -> bool:
        """Whether the root has the member its GeoJSON type needs."""
        kind, openers = self.kind, self.openers
        if kind == "FeatureCollection":
            return openers.get("features") == "["
        if kind == "Feature":
            return openers.get("geometry") in ("{", "n")
        if kind == "GeometryCollection":
            return openers.get("geometries") == "["
        if kind in GEOMETRY_TYPES:
            return openers.get("coordinates") == "["
        return False


def head_info(head: bytes) -> HeadInfo:
    """The root object's ``type`` and member names read from a head, as far as it parses."""
    text = head.decode("latin-1")
    i = skip_ws(text, len(BOM) if head.startswith(BOM) else 0)
    if text[i : i + 1] != "{":
        return HeadInfo(None, {})
    i += 1
    kind: str | None = None
    openers: dict[str, str] = {}
    while True:
        i = skip_ws(text, i)
        if text[i : i + 1] != '"':
            break
        try:
            name, i = read_string(text, i)
        except ScanError:
            break
        i = skip_ws(text, i)
        if text[i : i + 1] != ":":
            break
        i = skip_ws(text, i + 1)
        openers.setdefault(name, text[i : i + 1])
        try:
            value, i = decode_value(text, i)
        except ScanError:
            break  # cut or damaged: what came before still counts
        if name == "type" and isinstance(value, str) and kind is None:
            kind = value
        i = skip_ws(text, i)
        if text[i : i + 1] != ",":
            break
        i += 1
    return HeadInfo(kind, openers)


class GeoJsonAdapter:
    """GeoJSON features, bounds and CRS. Blocks are fixed by the version."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        info = head_info(head)
        if info.kind is None:
            reason = ProbeReason("geojson.no_type", "the root is not an object with a type")
            return ProbeResult(0.0, (reason,))
        if info.kind not in GEOJSON_TYPES:
            reason = ProbeReason("geojson.other_type", "the root's type is not a GeoJSON type")
            return ProbeResult(0.0, (reason,))
        if not info.shaped:
            reason = ProbeReason(
                "geojson.shape", f"a {info.kind} root lacks the member that type needs"
            )
            return ProbeResult(0.0, (reason,))
        reason = ProbeReason("geojson.root", f"a {info.kind} root with the member that type needs")
        return ProbeResult(VERIFIED, (reason,))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        info = head_info(head)
        summary: JsonObject = {
            "bom": head.startswith(BOM),
            "crs_member": "crs" in info.openers,
            "root_type": info.kind or "",
            "size": source.size,
        }
        return InspectResult(summary)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return _plan(source, config)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        limits = Limits.of(config)
        shared = Shared.of(source, config, limits, chunk.context)
        part = context_text(chunk.context, "part")
        if part == "root":
            return _root(source, config, shared, chunk.context)
        return feature_output(
            shared,
            chunk.context,
            context_int(chunk.context, "start"),
            context_int(chunk.context, "end"),
            context_int(chunk.context, "first"),
        )


def _root(
    source: SourceReader, config: AdapterConfig, shared: Shared, context: JsonObject
) -> ChunkOutput:
    name_span = context["name"]
    name: tuple[int, int] | None = None
    if isinstance(name_span, list) and len(name_span) == 2:
        first, second = name_span
        if isinstance(first, int) and isinstance(second, int):
            name = (first, second)
    if shared.mode == "unread":
        return ChunkOutput()
    return root_output(shared, context, name, (0, source.size))


def _plan(source: SourceReader, config: AdapterConfig) -> Plan:
    limits = Limits.of(config)
    size = source.size
    findings: list[IngestFinding] = []
    whole = (0, size)

    def done(mode: str = "unread") -> Plan:
        context: JsonObject = {
            "array": [0, 0],
            "crs": _crs.Decision("unknown", (), whole, False).to_json(),
            "mode": mode,
            "name": [],
            "part": "root",
        }
        return Plan((make_chunk(source, config, context, 0),), tuple(findings))

    if size > limits.max_source_bytes:
        findings.append(
            finding(
                config,
                "source_too_large",
                cite(source, 0, size),
                f"the source holds {size} bytes, over max_source_bytes"
                f" ({limits.max_source_bytes}); nothing is read",
                {"max_source_bytes": limits.max_source_bytes, "size": size},
            )
        )
        return done()
    bom = source.read(0, len(BOM)) == BOM
    begin = len(BOM) if bom else 0
    if bom:
        findings.append(
            finding(
                config,
                "json_bom",
                cite(source, 0, len(BOM)),
                "the JSON text starts with a UTF-8 byte-order mark, which RFC 8259 forbids; it is"
                " skipped",
                {},
            )
        )
    reader = Reader(source, begin, size, limits.max_feature_bytes)
    if reader.peek() != "{":
        findings.append(
            finding(
                config,
                "not_geojson",
                cite(source, begin, size) if size > begin else cite(source, 0, size),
                "the root is not a JSON object",
                {},
            )
        )
        return done()
    return _walk(source, config, limits, reader, findings, whole)


def _walk(
    source: SourceReader,
    config: AdapterConfig,
    limits: Limits,
    reader: Reader,
    findings: list[IngestFinding],
    whole: tuple[int, int],
) -> Plan:
    size = source.size
    root_start = reader.offset
    reader.advance()
    members: list[_crs.RootMember] = []
    blocks: list[JsonObject] = []
    array: tuple[int, int] | None = None
    outside = nested = False
    broken: ScanError | None = None
    unread = False  # something stopped the walk before the file's end
    closed = False
    comma = False
    held: list[tuple[int, int]] = []  # (start, end) of the features of the open block
    held_first = 0
    count = 0

    def close_block() -> None:
        nonlocal held
        if held:
            blocks.append(
                {"end": held[-1][1], "first": held_first, "part": "features", "start": held[0][0]}
            )
            held = []

    def crs_seen() -> bool:
        return any(m.name == "crs" for m in members)

    def stop_with(code: str, at: int, message: str, details: JsonObject) -> None:
        findings.append(finding(config, code, cite(source, at, size), message, details))

    while True:
        char = reader.peek()
        if char == "}" and not comma:
            reader.advance()
            closed = True
            break
        if char != '"':
            broken = ScanError(
                "truncated" if not char else "syntax", reader.offset, "expected a member name"
            )
            break
        try:
            name = reader.string()
            if reader.peek() != ":":
                raise ScanError("syntax", reader.offset, "expected ':'")
            reader.advance()
            if name == "features" and reader.peek() == "[" and array is None:
                array_start = reader.offset
                reader.advance()
                state = ArrayState()
                last_end = array_start + 1
                for element in reader.elements(state):
                    if count >= limits.max_features:
                        stop_with(
                            "feature_limit",
                            element.start,
                            f"the collection holds more than max_features ({limits.max_features})"
                            f" features; features from {count} on are not read",
                            {"max_features": limits.max_features, "feature": count},
                        )
                        unread = True
                        break
                    if not crs_seen():  # what the CRS decision needs, until a crs member decides
                        outside, nested = _scan_feature(element.value, limits, outside, nested)
                    if held and (
                        len(held) >= BLOCK_FEATURES or element.end - held[0][0] > BLOCK_BYTES
                    ):
                        close_block()
                    if not held:
                        held_first = count
                    held.append((element.start, element.end))
                    last_end = element.end
                    count += 1
                close_block()
                array = (array_start, state.end if state.closed else last_end)
                if state.full:
                    stop_with(
                        "feature_too_large",
                        state.at,
                        f"the feature at byte {state.at} is longer than max_feature_bytes"
                        f" ({limits.max_feature_bytes}); it and what follows are not read",
                        {"max_feature_bytes": limits.max_feature_bytes, "feature": count},
                    )
                    unread = True
                if unread or state.full:
                    break
                if state.broken is not None:
                    broken = state.broken
                    break
            else:
                value, start, end = reader.value()
                if value is DEEP:
                    findings.append(
                        finding(
                            config,
                            "too_deep",
                            cite(source, start, end),
                            f"the root member {shown(name)} nests deeper than the interpreter"
                            " reads; it is not decoded",
                            {"name": name[:64]},
                        )
                    )
                if name == "features":
                    findings.append(
                        finding(
                            config,
                            "duplicate_member",
                            cite(source, start, end),
                            "the root repeats features: the later one is not read",
                            {"name": "features"},
                        )
                    )
                members.append(_crs.RootMember(name, start, end, value))
        except ScanError as exc:
            if exc.kind == "large":
                stop_with(
                    "feature_too_large",
                    exc.offset,
                    f"a root member at byte {exc.offset} is longer than max_feature_bytes"
                    f" ({limits.max_feature_bytes}); it and what follows are not read",
                    {"max_feature_bytes": limits.max_feature_bytes},
                )
                unread = True
            else:
                broken = exc
            break
        char = reader.peek()
        comma = False
        if char == ",":
            reader.advance()
            comma = True
        elif char != "}":
            broken = ScanError(
                "truncated" if not char else "syntax", reader.offset, "expected ',' or '}'"
            )
            break
    root_end = reader.offset if closed else size
    if broken is not None:
        code = "json_truncated" if broken.kind == "truncated" else "json_syntax"
        findings.append(
            finding(
                config,
                code,
                cite(source, min(broken.offset, size), size),
                f"at byte {broken.offset}: {broken.reason}; nothing after it is read",
                {"offset": broken.offset, "reason": broken.reason},
            )
        )
    return _chunks(
        source,
        config,
        limits,
        members,
        blocks,
        array,
        (root_start, root_end),
        whole,
        findings,
        outside=outside,
        nested=nested,
        complete=closed and not unread,
    )


def _scan_feature(value: object, limits: Limits, outside: bool, nested: bool) -> tuple[bool, bool]:
    """Fold one element's evidence about RFC 7946 into the file's: a position outside longitude
    and latitude's range, a ``crs`` member on a feature or its geometry."""
    if value is DEEP or not isinstance(value, dict):
        return outside, nested
    nested = nested or "crs" in value
    geometry = value.get("geometry")
    if geometry is None or geometry is DEEP:
        return outside, nested
    found = measure(geometry, geographic=True, limits=limits)
    return (
        outside or "coordinate_out_of_range" in found.problems,
        nested or found.nested_crs,
    )


def _chunks(
    source: SourceReader,
    config: AdapterConfig,
    limits: Limits,
    members: list[_crs.RootMember],
    blocks: list[JsonObject],
    array: tuple[int, int] | None,
    root: tuple[int, int],
    whole: tuple[int, int],
    findings: list[IngestFinding],
    *,
    outside: bool,
    nested: bool,
    complete: bool,
) -> Plan:
    """The root chunk and a chunk per block, once the whole walk's evidence is in."""
    kinds = [m for m in members if m.name == "type"]
    kind = kinds[0].value if len(kinds) == 1 and isinstance(kinds[0].value, str) else None
    mode = "unread"  # nothing of the file is GeoJSON: no records
    if len(kinds) > 1:
        findings.append(
            finding(
                config,
                "duplicate_member",
                cite(source, kinds[1].start, kinds[1].end),
                "the root repeats type: it is not read as GeoJSON",
                {"name": "type"},
            )
        )
    elif kind is None or kind not in GEOJSON_TYPES:
        findings.append(
            finding(
                config,
                "not_geojson",
                cite(source, *whole),
                "the root has no GeoJSON type: nothing is read",
                {},
            )
        )
    elif kind == "FeatureCollection":
        mode = "collection" if array is not None else "none"
        if array is None:
            findings.append(
                finding(
                    config,
                    "features_missing",
                    cite(source, *whole),
                    "the FeatureCollection has no features array",
                    {},
                )
            )
    else:
        mode = "none"  # a Feature or geometry that does not close is not read as features
        if complete:
            mode, array = ("feature" if kind == "Feature" else "geometry"), root
            by_name = {m.name: m.value for m in members}
            geometry = by_name.get("geometry") if kind == "Feature" else by_name
            if geometry is not None and geometry is not DEEP:
                found = measure(geometry, geographic=True, limits=limits)
                outside = outside or "coordinate_out_of_range" in found.problems
                nested = nested or found.nested_crs
    crs_given = any(m.name == "crs" for m in members)
    contradicted = ""
    if nested:
        contradicted = "a feature or geometry has its own crs member"
    elif outside:
        contradicted = (
            "positions lie outside longitude and latitude's range, so the file is not RFC 7946's"
        )
    elif not complete:
        contradicted = "the file is not read to its end, so a crs member cannot be ruled out"
    elif mode == "none":
        contradicted = "no features were read to check the default against"
    decision, crs_findings = _crs.decide(
        source, config, members, contradicted=contradicted, whole=whole
    )
    findings.extend(crs_findings)
    if crs_given:
        again = [m for m in members if m.name == "crs"]
        if len(again) > 1:
            findings.append(
                finding(
                    config,
                    "duplicate_member",
                    cite(source, again[1].start, again[1].end),
                    "the root repeats crs",
                    {"name": "crs"},
                )
            )
    name = next((m for m in members if m.name == "name" and isinstance(m.value, str)), None)
    context: JsonObject = {
        "array": list(array) if array is not None else [0, 0],
        "crs": decision.to_json(),
        "mode": mode,
        "name": [name.start, name.end] if name is not None else [],
        "part": "root",
    }
    chunks = [make_chunk(source, config, context, 0)]
    if mode in ("feature", "geometry"):
        blocks = [{"end": root[1], "first": 0, "part": "features", "start": root[0]}]
    if mode in ("collection", "feature", "geometry"):
        for block in blocks:
            block_context: JsonObject = {
                **block,
                "array": context["array"],
                "crs": context["crs"],
                "mode": mode,
            }
            cost = int(str(block["end"])) - int(str(block["start"]))
            chunks.append(make_chunk(source, config, block_context, cost))
    return Plan(tuple(chunks), tuple(findings))


__all__ = ["DESCRIPTOR", "GeoJsonAdapter"]
