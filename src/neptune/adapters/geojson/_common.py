"""What the GeoJSON adapter's parts share: finding codes, limits, citations and one block's issues.

Every finding about features is collected per chunk (``Issues``): one per code per chunk, citing
the bytes from the first affected feature to the last, with a count and the first features it
names. A chunk's bounds depend only on the bytes and this version's constants, so the findings are
as deterministic as the records.
"""

import json
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import AdapterConfig, ContractError, SourceReader
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    EvidenceRef,
    Locator,
    Provenance,
    adapter_locator,
)

ADAPTER_ID: Final = "geojson"
BOM: Final = b"\xef\xbb\xbf"
EXAMPLES: Final = 10  # how many features a finding names; it counts them all

GEOMETRY_TYPES: Final = frozenset(
    {
        "GeometryCollection",
        "LineString",
        "MultiLineString",
        "MultiPoint",
        "MultiPolygon",
        "Point",
        "Polygon",
    }
)
GEOJSON_TYPES: Final = GEOMETRY_TYPES | {"Feature", "FeatureCollection"}
# CRSs this adapter reads as geographic, in the order GeoJSON's positions give (longitude, then
# latitude), without looking anything up: the RFC 7946 default and the codes a legacy file most
# often names. Any other CRS is stated as written and its range is never checked.
GEOGRAPHIC: Final = frozenset(
    {
        ("EPSG", "4258"),
        ("EPSG", "4269"),
        ("EPSG", "4326"),
        ("OGC", "CRS27"),
        ("OGC", "CRS83"),
        ("OGC", "CRS84"),
    }
)
DEFAULT_CRS: Final = ("OGC", "CRS84")

_C, _S = FindingCategory, Severity
# name: (category, severity, what it means). The descriptor lists them as ``geojson.<name>``.
CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "coordinate_out_of_range": (
        _C.INCONSISTENT,
        _S.WARNING,
        "a longitude or latitude lies outside [-180, 180] or [-90, 90] in a geographic CRS: the"
        " position is kept as written and a Site or Asset does not take it as its location",
    ),
    "crs_ambiguous": (
        _C.AMBIGUOUS,
        _S.WARNING,
        "the file names two different CRSs: the artifact's CRS is Ambiguous, each candidate cited",
    ),
    "crs_defaulted": (
        _C.MISSING,
        _S.INFO,
        "the file has no crs member and positions all fit longitude and latitude: the CRS is"
        " RFC 7946's default, stated by the specification, not by the file",
    ),
    "crs_legacy": (
        _C.INCONSISTENT,
        _S.INFO,
        "the file has a crs member, which RFC 7946 removed: it is recorded as the file states it",
    ),
    "crs_unknown": (
        _C.MISSING,
        _S.WARNING,
        "the CRS cannot be stated: a null, link or unreadable crs, a default the positions"
        " contradict, a crs on a feature, or a file that breaks off before it could be told",
    ),
    "duplicate_member": (
        _C.AMBIGUOUS,
        _S.WARNING,
        "an object repeats a member name: that member is Unknown and nothing is read from it",
    ),
    "feature_invalid": (
        _C.CORRUPT,
        _S.WARNING,
        "an element of features is not a Feature object: it has no record",
    ),
    "feature_limit": (
        _C.LIMIT,
        _S.ERROR,
        "the collection holds more than max_features features: the rest are not read",
    ),
    "feature_too_large": (
        _C.LIMIT,
        _S.ERROR,
        "a feature or root member is longer than max_feature_bytes: it and what follows are not"
        " read",
    ),
    "features_missing": (
        _C.MISSING,
        _S.WARNING,
        "a FeatureCollection has no features array: there are no features to read",
    ),
    "geometry_invalid": (
        _C.CORRUPT,
        _S.WARNING,
        "a geometry has an unknown type, no coordinates, or coordinates that do not fit its type:"
        " the positions that fit are counted, and its bounds are Unknown",
    ),
    "identity_hint_ignored": (
        _C.INCONSISTENT,
        _S.INFO,
        "an id, site_id or asset_id is not a string or an integer: it is not kept as an identifier",
    ),
    "invalid_utf8": (
        _C.UNREPRESENTABLE,
        _S.WARNING,
        "a feature's bytes are not UTF-8: it has no record",
    ),
    "json_bom": (
        _C.INCONSISTENT,
        _S.INFO,
        "a JSON text starts with a byte-order mark, which RFC 8259 forbids: it is skipped",
    ),
    "json_syntax": (
        _C.CORRUPT,
        _S.ERROR,
        "the JSON breaks its grammar: nothing after the last whole feature is read",
    ),
    "json_truncated": (
        _C.CORRUPT,
        _S.ERROR,
        "the JSON breaks off before its end: nothing after the last whole feature is read",
    ),
    "nested_crs": (
        _C.UNSUPPORTED,
        _S.WARNING,
        "a feature or geometry has its own crs member, which GeoJSON never defined: it is not"
        " read, and that feature's location is Unknown",
    ),
    "non_finite_coordinate": (
        _C.UNREPRESENTABLE,
        _S.WARNING,
        "a coordinate is NaN, infinite or too large for a double: it is left out of the bounds",
    ),
    "not_geojson": (
        _C.UNSUPPORTED,
        _S.ERROR,
        "the root is not a GeoJSON object: nothing is read",
    ),
    "position_budget": (
        _C.LIMIT,
        _S.WARNING,
        "a geometry holds more than max_positions positions: it is not walked and its bounds"
        " are Unknown",
    ),
    "properties_invalid": (
        _C.CORRUPT,
        _S.WARNING,
        "a feature's properties is not an object or null: its properties have no rows",
    ),
    "properties_truncated": (
        _C.LIMIT,
        _S.WARNING,
        "a feature's properties hold more than max_properties values: the rest have no rows",
    ),
    "ring_not_closed": (
        _C.INCONSISTENT,
        _S.WARNING,
        "a polygon ring's first and last positions differ (RFC 7946 §3.1.6)",
    ),
    "ring_too_short": (
        _C.INCONSISTENT,
        _S.WARNING,
        "a polygon ring has fewer than four positions (RFC 7946 §3.1.6)",
    ),
    "ring_winding": (
        _C.INCONSISTENT,
        _S.INFO,
        "a polygon's exterior ring is not counterclockwise, or a hole is not clockwise, in x and"
        " y (RFC 7946 §3.1.6 advises it; earlier GeoJSON did not)",
    ),
    "source_too_large": (
        _C.LIMIT,
        _S.ERROR,
        "the source is longer than max_source_bytes: nothing is read",
    ),
    "too_deep": (
        _C.LIMIT,
        _S.ERROR,
        "a value nests deeper than the interpreter reads, or a GeometryCollection deeper than"
        " max_depth: it is not decoded",
    ),
}


@dataclass(frozen=True)
class Limits:
    """The settings that bound what a source, a feature or a geometry may cost."""

    max_source_bytes: int
    max_feature_bytes: int
    max_features: int
    max_positions: int
    max_depth: int
    max_properties: int

    @staticmethod
    def of(config: AdapterConfig) -> "Limits":
        return Limits(
            max_source_bytes=config.integer("max_source_bytes"),
            max_feature_bytes=config.integer("max_feature_bytes"),
            max_features=config.integer("max_features"),
            max_positions=config.integer("max_positions"),
            max_depth=config.integer("max_depth"),
            max_properties=config.integer("max_properties"),
        )


def cite(source: SourceReader, start: int, end: int, *steps: Locator) -> EvidenceRef:
    """Bytes ``[start, end)`` of the source, then ``steps`` inside them."""
    return EvidenceRef(source.content_id, (ByteRange(start, end - start), *steps))


def properties_step() -> AdapterLocator:
    """The step after the features' array that names the properties table."""
    return adapter_locator(f"{ADAPTER_ID}:properties", {})


def observed(evidence: EvidenceRef, config: AdapterConfig) -> Provenance:
    return Provenance(evidence, config.transform.id, AssertionKind.OBSERVED)


def stated(evidence: EvidenceRef, config: AdapterConfig) -> Provenance:
    return Provenance(evidence, config.transform.id, AssertionKind.STATED)


def record_id(kind: str, evidence: EvidenceRef, config: AdapterConfig) -> RecordId:
    return evidence_record_id(kind, evidence, config.transform)


def finding(
    config: AdapterConfig,
    name: str,
    subject: EvidenceRef,
    message: str,
    details: JsonObject,
    records: "tuple[RecordId, ...]" = (),
) -> IngestFinding:
    category, severity, _ = CODES[name]
    return ingest_finding(
        code=f"{ADAPTER_ID}.{name}",
        category=category,
        severity=severity,
        subject=subject,
        transform=config.transform,
        message=message,
        details=details,
        records=records,
    )


@dataclass
class _Issue:
    start: int
    end: int
    message: str
    details: JsonObject
    count: int = 0
    features: list[int] = field(default_factory=list)
    records: list[RecordId] = field(default_factory=list)


class Issues:
    """One chunk's findings about its features: one per code and label, however many features."""

    def __init__(self, source: SourceReader, config: AdapterConfig) -> None:
        self._source = source
        self._config = config
        self._found: dict[tuple[str, str], _Issue] = {}

    def add(
        self,
        name: str,
        start: int,
        end: int,
        feature: int,
        message: str,
        *,
        label: str = "",
        details: JsonObject | None = None,
        record: RecordId | None = None,
    ) -> None:
        """Feature ``feature``, bytes ``[start, end)``, has problem ``name``. ``message`` and
        ``details`` are the first such feature's; ``label`` separates problems of one code."""
        issue = self._found.setdefault(
            (name, label), _Issue(start, end, message, dict(details or {}))
        )
        issue.start, issue.end = min(issue.start, start), max(issue.end, end)
        issue.count += 1
        if len(issue.features) < EXAMPLES:
            issue.features.append(feature)
        if record is not None and len(issue.records) < EXAMPLES:
            issue.records.append(record)

    def findings(self) -> list[IngestFinding]:
        out = []
        for (name, _), issue in sorted(self._found.items()):
            more = f"; {issue.count} features in all" if issue.count > 1 else ""
            details: dict[str, JsonValue] = {
                **issue.details,
                "count": issue.count,
                "features": list(issue.features),
            }
            out.append(
                finding(
                    self._config,
                    name,
                    cite(self._source, issue.start, issue.end),
                    f"{issue.message}{more}",
                    details,
                    tuple(issue.records),
                )
            )
        return out


def context_int(context: JsonObject, key: str) -> int:
    value = context[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"chunk context {key} must be an integer, got {value!r}")
    return value


def context_text(context: JsonObject, key: str) -> str:
    value = context[key]
    if not isinstance(value, str):
        raise ContractError(f"chunk context {key} must be text, got {value!r}")
    return value


def context_object(context: JsonObject, key: str) -> JsonObject:
    value = context[key]
    if not isinstance(value, dict):
        raise ContractError(f"chunk context {key} must be an object, got {value!r}")
    return value


def shown(text: str, limit: int = 60) -> str:
    """Hostile text made safe and short for a message: escaped, at most ``limit`` characters."""
    escaped = json.dumps(text, ensure_ascii=True)
    return escaped if len(escaped) <= limit else escaped[: limit - 4] + '..."'
