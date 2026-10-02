"""One chunk's records: the artifact and its two tables (root chunk), or a block of features.

A feature is a row of the ``features`` table (its id, geometry type, position count and bounds),
its properties are rows of the ``properties`` table (one per leaf, cited by the bytes of the
member), and one that states ``asset_id`` or ``site_id`` is also an ``Asset`` or a ``Site``
carrying those ids as hints. Every cell cites its exact bytes; no row is a ``Row`` citation, so
every cell has its own provenance (ADR 0020 §5).
"""

from collections import Counter
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import AdapterConfig, ChunkOutput, ContractError, SourceReader
from neptune.adapters.geojson import _crs
from neptune.adapters.geojson._common import (
    Issues,
    Limits,
    cite,
    context_object,
    context_text,
    observed,
    properties_step,
    record_id,
    shown,
    stated,
)
from neptune.adapters.geojson._geometry import Measured, measure
from neptune.adapters.geojson._scan import (
    DEEP,
    ArrayState,
    Deep,
    Member,
    Reader,
    ScanError,
    array_items,
    decode_value,
    object_members,
)
from neptune.identity.provenance import EvidenceRecord
from neptune.model.ids import LogicalId, RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import (
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef
from neptune.model.scalars import Real, real
from neptune.model.spatial import GeodeticPosition, HeightReference
from neptune.model.units import unit_from_json
from neptune.model.world import (
    Asset,
    CellValue,
    Site,
    SpatialArtifact,
    SpatialCategory,
    StructuredRecord,
    StructuredTable,
)

FEATURE_COLUMNS: Final = (
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
PROPERTY_COLUMNS: Final = ("feature", "key", "value")
_INT64: Final = range(-(2**63), 2**63)
_DEGREE: Final = unit_from_json("deg")
_METRE: Final = unit_from_json("m")


class Offsets:
    """Byte offsets in a source of the characters of one feature's decoded text."""

    def __init__(self, text: str, base: int) -> None:
        self._text = text
        self._base = base
        self._ascii = text.isascii()
        self._char = 0
        self._byte = 0

    def at(self, char: int) -> int:
        if self._ascii:
            return self._base + char
        if char < self._char:
            self._char = self._byte = 0
        self._byte += len(self._text[self._char : char].encode("utf-8"))
        self._char = char
        return self._base + self._byte


def text_ok(value: str) -> bool:
    """Whether the model can hold ``value``: non-empty, with no lone surrogate."""
    if not value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@dataclass(frozen=True)
class Shared:
    """What every chunk of a source needs: the source, config, limits and the file's CRS."""

    source: SourceReader
    config: AdapterConfig
    limits: Limits
    decision: _crs.Decision
    array: tuple[int, int]
    mode: str

    @staticmethod
    def of(
        source: SourceReader, config: AdapterConfig, limits: Limits, context: JsonObject
    ) -> "Shared":
        raw = context["array"]
        if not isinstance(raw, list) or len(raw) != 2:
            raise ContractError("chunk context array is a byte range")
        first, second = raw
        if not (isinstance(first, int) and isinstance(second, int)):
            raise ContractError("chunk context array is a byte range")
        return Shared(
            source,
            config,
            limits,
            _crs.decision_from_json(context_object(context, "crs")),
            (first, second),
            context_text(context, "mode"),
        )

    def features_table(self) -> RecordId:
        return record_id(StructuredTable.kind, cite(self.source, *self.array), self.config)

    def properties_table(self) -> RecordId:
        evidence = cite(self.source, *self.array, properties_step())
        return record_id(StructuredTable.kind, evidence, self.config)


# --- Root: the artifact and the tables ---------------------------------------------------------


def root_output(
    shared: Shared, context: JsonObject, name: tuple[int, int] | None, whole: tuple[int, int]
) -> ChunkOutput:
    source, config = shared.source, shared.config
    evidence = cite(source, *whole)
    title: Knowledge[str] = Unknown(observed(evidence, config))
    if name is not None:
        raw = source.read(name[0], name[1] - name[0])
        try:
            decoded, _ = decode_value(raw.decode("utf-8"), 0)
        except (ScanError, UnicodeDecodeError):
            decoded = None
        if isinstance(decoded, str) and text_ok(decoded):
            title = Known(decoded, stated(cite(source, *name), config))
    crs = _crs.knowledge(shared.decision, source, config)
    known_crs = shared.decision.kind in ("stated", "default")
    unit: Knowledge[object] = NotApplicable() if known_crs else Unknown(observed(evidence, config))
    artifact = SpatialArtifact(
        id=record_id(SpatialArtifact.kind, evidence, config),
        provenance=observed(evidence, config),
        category=SpatialCategory.VECTOR_MAP,
        name=title,
        unit=unit,  # type: ignore[arg-type]
        crs=crs,
        frame=NotCovered(),
    )
    records: list[EvidenceRecord] = [artifact]
    if shared.mode != "none":
        at = cite(source, *shared.array)
        features_name = Known(
            "features", observed(at, config)
        )  # the table's own name, not the file's: see ADR 0057 §4
        records.append(
            StructuredTable(
                id=shared.features_table(),
                provenance=observed(at, config),
                name=features_name,
                header=Known(FEATURE_COLUMNS, observed(at, config)),
            )
        )
        pat = cite(source, *shared.array, properties_step())
        records.append(
            StructuredTable(
                id=shared.properties_table(),
                provenance=observed(pat, config),
                name=Known("properties", observed(pat, config)),
                header=Known(PROPERTY_COLUMNS, observed(pat, config)),
            )
        )
    return ChunkOutput(records=tuple(records), findings=())


# --- Features ----------------------------------------------------------------------------------

_NOT_READ: Final = object()  # a leaf that is a container (empty, repeated or too deep): not read


@dataclass
class _Leaf:
    pointer: str
    start: int  # char offsets in the feature's text: the member (its name), its value and its end
    value_start: int
    end: int
    value: object
    repeated: bool


def _escape(name: str) -> str:
    return name.replace("~", "~0").replace("/", "~1")


def _children(text: str, member: Member) -> list[Member]:
    try:
        if isinstance(member.value, dict):
            return object_members(text, member.start)[0]
        return array_items(text, member.start)[0]
    except ScanError:
        return []


def _leaves(text: str, top: list[Member], limits: Limits) -> tuple[list[_Leaf], bool, bool]:
    """Every leaf of a properties object in document order, whether it was cut at
    ``max_properties`` and whether something nested deeper than ``max_depth``."""
    found: list[_Leaf] = []
    truncated = too_deep = False
    # (members left, pointer prefix, depth, is an array, how often each name occurs)
    stack = [(iter(top), "", 1, False, Counter(m.name for m in top))]
    while stack:
        items, prefix, depth, is_array, counts = stack[-1]
        member = next(items, None)
        if member is None:
            stack.pop()
            continue
        pointer = f"{prefix}/{member.name if is_array else _escape(member.name)}"
        repeated = not is_array and counts[member.name] > 1
        value = member.value
        container = isinstance(value, dict | list) and bool(value)
        if container and not repeated and depth < limits.max_depth:
            children = _children(text, member)
            counted = Counter(m.name for m in children) if isinstance(value, dict) else Counter()
            stack.append((iter(children), pointer, depth + 1, isinstance(value, list), counted))
            continue
        too_deep = too_deep or (container and not repeated)
        if len(found) >= limits.max_properties:
            truncated = True
            break
        leaf_value = _NOT_READ if isinstance(value, dict | list | Deep) else value
        found.append(
            _Leaf(pointer, member.name_start, member.start, member.end, leaf_value, repeated)
        )
    return found, truncated, too_deep


def _property_cell(
    leaf: _Leaf, text: str, evidence: EvidenceRef, config: AdapterConfig
) -> tuple[Knowledge[CellValue], str]:
    """The cell of a property value, and a problem it has (``""`` if none)."""
    provenance = stated(evidence, config)
    value = leaf.value
    if leaf.repeated:
        return Unknown(provenance), "duplicate"
    if value is None:
        return KnownAbsent(provenance), ""
    if value is _NOT_READ:
        return Unknown(provenance), ""
    if isinstance(value, bool):
        return Known(value, provenance), ""
    if isinstance(value, str):
        if not value:
            return Unknown(provenance), ""
        if not text_ok(value):
            return Unknown(provenance), "surrogate"
        return Known(value, provenance), ""
    cell = _cell_number(value, text[leaf.value_start : leaf.end])
    if cell is None:
        return Unknown(provenance), ""
    return Known(cell, provenance), ""


def _cell_number(value: object, text: str) -> int | str | Real | None:
    """A JSON number as the cell value its type gives it; ``None`` if it is not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, int):
        return value if value in _INT64 else text
    try:
        return real(float(value))
    except OverflowError:
        return text


def _hint(member: Member | None, text: str) -> tuple[str | None, bool]:
    """A declared id as the string the source writes, and whether one was there but unusable."""
    if member is None:
        return None, False
    value = member.value
    if isinstance(value, str):
        return (value, False) if text_ok(value) else (None, True)
    if isinstance(value, int) and not isinstance(value, bool):
        return text[member.start : member.end], False
    return None, True


def _named(members: list[Member], name: str) -> list[Member]:
    return [m for m in members if m.name == name]


@dataclass
class _Feature:
    """One feature being read: where it is and what it is made of."""

    shared: Shared
    text: str
    where: Offsets
    members: list[Member]
    index: int
    start: int
    end: int
    issues: Issues

    @property
    def evidence(self) -> EvidenceRef:
        return cite(self.shared.source, self.start, self.end)

    def span(self, member: Member, *, name: bool = False) -> EvidenceRef:
        first = member.name_start if name else member.start
        return cite(self.shared.source, self.where.at(first), self.where.at(member.end))

    def problem(self, code: str, message: str, label: str = "") -> None:
        self.issues.add(
            code, self.start, self.end, self.index, f"feature {self.index}: {message}", label=label
        )

    def one(self, name: str, among: list[Member] | None = None) -> Member | None:
        """The one member called ``name``; a repeated name has no reading."""
        found = _named(self.members if among is None else among, name)
        if len(found) > 1:
            self.problem(
                "duplicate_member",
                f"member {shown(name)} is repeated; it is Unknown",
                label=name if among is None else f"properties.{name}",
            )
            return None
        return found[0] if found else None


def feature_output(
    shared: Shared, context: JsonObject, start: int, end: int, first: int
) -> ChunkOutput:
    """Every feature of the elements in ``[start, end)``, the first being feature ``first``."""
    del context
    source, config, limits = shared.source, shared.config, shared.limits
    issues = Issues(source, config)
    records: list[EvidenceRecord] = []
    reader = Reader(source, start, end, limits.max_feature_bytes)
    index = first
    for element in reader.elements(ArrayState(), until_end=True):
        raw = source.read(element.start, element.end - element.start)
        records.extend(_read(shared, raw, element.start, index, issues))
        index += 1
    return ChunkOutput(records=tuple(records), findings=tuple(issues.findings()))


def _read(
    shared: Shared, raw: bytes, start: int, index: int, issues: Issues
) -> list[EvidenceRecord]:
    end = start + len(raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        issues.add(
            "invalid_utf8",
            start,
            end,
            index,
            f"feature {index} is not UTF-8 from byte {start + exc.start}; it has no record",
        )
        return []
    try:
        members, _ = object_members(text, 0) if text.startswith("{") else ([], 0)
        if shared.mode == "geometry":  # a bare geometry: the one feature is its own geometry
            value, _ = decode_value(text, 0)
            members = [
                Member("type", 0, 0, 0, "Feature"),
                Member("geometry", 0, 0, len(text), value),
            ]
    except ScanError as exc:
        issues.add("json_syntax", start, end, index, f"feature {index} is not valid JSON: {exc}")
        return []
    feature = _Feature(shared, text, Offsets(text, start), members, index, start, end, issues)
    kind = _named(members, "type")
    if len(kind) != 1 or kind[0].value != "Feature":
        feature.problem("feature_invalid", 'it has no type "Feature"; it has no record')
        return []
    return _records(feature)


def _id_cell(feature: _Feature, member: Member | None) -> Knowledge[CellValue]:
    config = feature.shared.config
    if member is None:
        return Unknown(observed(feature.evidence, config))
    provenance = stated(feature.span(member), config)
    value = member.value
    if value is None:
        return KnownAbsent(provenance)
    if isinstance(value, str):
        return Known(value, provenance) if text_ok(value) else Unknown(provenance)
    cell = _cell_number(value, feature.text[member.start : member.end])
    if cell is None:
        feature.problem(
            "identity_hint_ignored", "its id is neither a string nor a number", label="id"
        )
        return Unknown(provenance)
    return Known(cell, provenance)


def _records(feature: _Feature) -> list[EvidenceRecord]:
    shared = feature.shared
    source, config, limits = shared.source, shared.config, shared.limits
    evidence = feature.evidence
    geometry = feature.one("geometry")
    id_member = feature.one("id")
    cells: list[Knowledge[CellValue]] = [_id_cell(feature, id_member)]
    measured: Measured | None = None
    geometry_evidence: EvidenceRef | None = None
    if geometry is None:
        cells += [Unknown(observed(evidence, config))] * 8
    else:
        geometry_evidence = feature.span(geometry)
        if geometry.value is None:
            cells += [KnownAbsent(stated(geometry_evidence, config))] * 8
        elif geometry.value is DEEP:
            feature.problem("too_deep", "its geometry nests deeper than the interpreter reads")
            cells += [Unknown(observed(geometry_evidence, config))] * 8
        else:
            measured = measure(geometry.value, geographic=shared.decision.geographic, limits=limits)
            cells += _geometry_cells(measured, geometry_evidence, config)
            for code, message in measured.problems.items():
                feature.problem(code, message, label=code)
    if _named(feature.members, "crs") or (measured is not None and measured.nested_crs):
        feature.problem("nested_crs", "it has a crs member of its own; its location is Unknown")
    out: list[EvidenceRecord] = [
        StructuredRecord(
            id=record_id(StructuredRecord.kind, evidence, config),
            provenance=observed(evidence, config),
            table=shared.features_table(),
            row=feature.index,
            cells=tuple(cells),
        )
    ]
    top: list[Member] = []
    props = feature.one("properties")
    if props is not None and isinstance(props.value, dict):
        top = _children(feature.text, props)
        out.extend(_property_rows(feature, top))
    elif props is not None and props.value is DEEP:
        feature.problem(
            "too_deep", "its properties nest deeper than the interpreter reads", label="properties"
        )
    elif props is not None and props.value is not None:
        feature.problem("properties_invalid", "its properties is not an object or null")
    out.extend(_place(feature, top, id_member, measured, geometry_evidence))
    del source
    return out


def _property_rows(feature: _Feature, top: list[Member]) -> list[EvidenceRecord]:
    shared = feature.shared
    source, config = shared.source, shared.config
    leaves, cut, deep = _leaves(feature.text, top, shared.limits)
    if cut:
        feature.problem(
            "properties_truncated",
            f"it holds more than max_properties ({shared.limits.max_properties}) values; the rest"
            " have no rows",
        )
    if deep:
        feature.problem("too_deep", "its properties nest deeper than max_depth", label="properties")
    out: list[EvidenceRecord] = []
    for row, leaf in enumerate(leaves):
        at = feature.where.at
        member = cite(source, at(leaf.start), at(leaf.end))
        value = cite(source, at(leaf.value_start), at(leaf.end))
        cell, problem = _property_cell(leaf, feature.text, value, config)
        if problem == "duplicate":
            feature.problem(
                "duplicate_member",
                "a property name is repeated: those members are Unknown",
                label="properties",
            )
        elif problem == "surrogate":
            feature.problem(
                "invalid_utf8",
                "a property string has a lone surrogate: it is Unknown",
                label="surrogate",
            )
        key_at = stated(cite(source, at(leaf.start), at(leaf.value_start)), config)
        key: Knowledge[CellValue] = (
            Known(leaf.pointer, key_at) if text_ok(leaf.pointer) else Unknown(key_at)
        )
        out.append(
            StructuredRecord(
                id=record_id(StructuredRecord.kind, member, config),
                provenance=observed(member, config),
                table=shared.properties_table(),
                row=row,
                cells=(Known(feature.index, observed(feature.evidence, config)), key, cell),
            )
        )
    return out


def _geometry_cells(
    measured: Measured, evidence: EvidenceRef, config: AdapterConfig
) -> list[Knowledge[CellValue]]:
    seen = observed(evidence, config)
    kind: Knowledge[CellValue] = (
        Known(measured.type, stated(evidence, config))
        if measured.type and text_ok(measured.type)
        else Unknown(seen)
    )
    count: Knowledge[CellValue] = (
        Known(measured.positions, seen) if measured.positions is not None else Unknown(seen)
    )
    cells: list[Knowledge[CellValue]] = [kind, count]
    for value in (measured.x0, measured.y0, measured.x1, measured.y1):
        cells.append(
            Known(value, seen) if value is not None and measured.bounded else Unknown(seen)
        )
    for value in (measured.z0, measured.z1):
        if value is not None and measured.bounded:
            cells.append(Known(value, seen))
        elif measured.bounded and measured.positions:
            cells.append(NotCovered(seen))  # positions hold no third number
        else:
            cells.append(Unknown(seen))
    return cells


def _place(
    feature: _Feature,
    top: list[Member],
    id_member: Member | None,
    measured: Measured | None,
    geometry_evidence: EvidenceRef | None,
) -> list[EvidenceRecord]:
    """The ``Asset`` or ``Site`` a feature that states ``asset_id`` or ``site_id`` is."""
    shared = feature.shared
    config, text = shared.config, feature.text
    hints = {
        name: member
        for name in ("asset_id", "site_id", "name", "category")
        if (member := feature.one(name, top)) is not None
    }
    asset_id, bad_asset = _hint(hints.get("asset_id"), text)
    site_id, bad_site = _hint(hints.get("site_id"), text)
    feature_id, _ = _hint(id_member, text)
    for name, bad in (("asset_id", bad_asset), ("site_id", bad_site)):
        if bad:
            feature.problem(
                "identity_hint_ignored", f"its {name} is neither a string nor an integer", name
            )
    if asset_id is None and site_id is None:
        return []
    evidence = feature.evidence
    ids: list[Known[LogicalId]] = []
    if asset_id is not None:
        ids.append(
            Known(LogicalId("asset_id", asset_id), stated(feature.span(hints["asset_id"]), config))
        )
    if site_id is not None:
        ids.append(
            Known(LogicalId("site_id", site_id), stated(feature.span(hints["site_id"]), config))
        )
    if feature_id is not None and id_member is not None:
        ids.append(
            Known(
                LogicalId("geojson.feature_id", feature_id), stated(feature.span(id_member), config)
            )
        )
    ids.sort(key=lambda known: (known.value.namespace, known.value.value))
    unknown = Unknown(observed(evidence, config))

    def text_of(name: str) -> Knowledge[str]:
        member = hints.get(name)
        if member is not None and isinstance(member.value, str) and text_ok(member.value):
            return Known(member.value, stated(feature.span(member), config))
        return unknown

    location = _location(shared, measured, geometry_evidence, evidence)
    if asset_id is not None:
        site: Knowledge[LogicalId] = unknown
        if site_id is not None:
            site = Known(
                LogicalId("site_id", site_id), stated(feature.span(hints["site_id"]), config)
            )
        return [
            Asset(
                id=record_id(Asset.kind, evidence, config),
                provenance=observed(evidence, config),
                identifiers=tuple(ids),
                name=text_of("name"),
                aliases=(),
                category=text_of("category"),
                site=site,
                parent=unknown,
                location=location,
            )
        ]
    return [
        Site(
            id=record_id(Site.kind, evidence, config),
            provenance=observed(evidence, config),
            identifiers=tuple(ids),
            name=text_of("name"),
            aliases=(),
            parent=unknown,
            location=location,
        )
    ]


def _location(
    shared: Shared,
    measured: Measured | None,
    geometry_evidence: EvidenceRef | None,
    evidence: EvidenceRef,
) -> Knowledge[GeodeticPosition]:
    """The position of a Point in a geographic CRS; otherwise Unknown, never a guess."""
    source, config = shared.source, shared.config
    cited = geometry_evidence or evidence
    seen = observed(cited, config)
    decision = shared.decision
    if (
        measured is None
        or measured.type != "Point"
        or measured.positions != 1
        or measured.problems
        or measured.nested_crs
        or not decision.geographic
        or measured.x0 is None
        or measured.y0 is None
    ):
        return Unknown(seen)
    default = decision.kind == "default"
    first = decision.readings[0]
    spec = stated(cite(source, first.start, first.end), config)
    position = GeodeticPosition(
        latitude=measured.y0,
        longitude=measured.x0,
        height=Known(measured.z0, seen) if measured.z0 is not None else NotCovered(seen),
        crs=_crs.knowledge(decision, source, config),
        angle_unit=Known(_DEGREE, spec) if default else Unknown(seen),
        height_unit=Known(_METRE, spec) if default else Unknown(seen),
        height_reference=Known(HeightReference.ELLIPSOID, spec) if default else Unknown(seen),
    )
    return Known(position, seen)
