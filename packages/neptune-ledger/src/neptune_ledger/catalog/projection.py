"""Kind-specific projection columns, generated from the compiler's JSON Schema (ADRs 0009, 0011).

The package schema's JSON Schema export (``contracts/package-schema/v<version>/schema.json``, from
``neptune.model.schema``) names every record kind and its top-level fields. ``projection_spec``
reads it into a ``Spec``: the kinds, the hot-filter fields (machine, site, run, stream, clock) each
kind states, and the free-form objects no pointer walk may enter. ``render_migration`` turns the
difference between two specs into a migration: columns and indexes per new hot-filter shape. It
never creates a partition: a kind without one of its own lives in ``record_default`` (ADR 0008),
and splitting it out is a rebuild, not a migration step (ADR 0009 §6). Neither touches a database,
so both are pure functions of their inputs.

The Ledger keeps one spec per package-schema version it reads, the schema-version registry
(``Registry``, ADR 0011). It is committed beside the migrations as ``projections.json``, and
``index`` reads that file, never the compiler's live schema: indexing is a function of the package
and the Ledger version alone. Each record is projected with the spec of the version it states, so
packages of every version are indexed side by side, and a package of a version the registry does
not hold is refused. Adding a version is one command over the registry's version directory, which
appends its spec and writes the next migration when a kind gains a projection::

    uv run python -m neptune_ledger.catalog.projection contracts/package-schema/v4.0.0/schema.json

A hot filter in a shape this module does not know, a kind or projection that disappears, or an
indexed version whose mapping would change raises ``ProjectionError``: a new shape, a removal or a
re-mapping is a decision for an ADR, not a guess.
"""

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Final, Literal

from neptune.identity import canonical_json

Shape = Literal["logical_id", "record_id", "record_ids"]

# Hot filter -> the top-level field names that state it. A field is matched by exact name.
HOT_FILTERS: Final[Mapping[str, tuple[str, ...]]] = {
    "clock": ("clock", "clocks"),
    "machine": ("machine",),
    "run": ("run",),
    "site": ("site",),
    "stream": ("stream",),
}

# The schema shapes a hot-filter field may have, and how each is projected (ADR 0009 §3).
_SHAPES: Final[Mapping[str, Shape]] = {
    json.dumps({"$ref": "#/$defs/Knowledge_LogicalId"}, sort_keys=True): "logical_id",
    json.dumps({"$ref": "#/$defs/RecordId"}, sort_keys=True): "record_id",
    json.dumps({"items": {"$ref": "#/$defs/RecordId"}, "type": "array"}, sort_keys=True): (
        "record_ids"
    ),
}

# The kinds migration 0001 partitions: package schema 1's kinds when the catalog was first cut.
BASELINE_KINDS: Final = (
    "asset",
    "calibration",
    "document_block",
    "document_record",
    "frame",
    "frame_graph",
    "frame_transform",
    "hardware_component",
    "hardware_configuration",
    "image",
    "ingest_finding",
    "machine",
    "run",
    "site",
    "software_configuration",
    "source_absence",
    "source_artifact",
    "source_revision",
    "spatial_artifact",
    "stream",
    "structured_record",
    "structured_table",
    "timestamp_domain",
    "transform_record",
    "video",
)

# A partition is record_<kind>: an unquoted identifier within PostgreSQL's 63-byte limit.
_KIND: Final = re.compile(r"[a-z][a-z0-9_]{0,55}")
SPEC_FILE: Final = "projections.json"
_MIGRATION: Final = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
# A package-schema id; version 0 names only BASELINE, the catalog before any projection.
_SCHEMA_ID: Final = re.compile(r"urn:neptune:schema:canonical:(0|[1-9][0-9]{0,8})")
# Field and filter names are spliced into SQL and comments: plain lower-case identifiers only.
_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,55}")
_SHAPE_NAMES: Final = frozenset({"logical_id", "record_id", "record_ids"})


class ProjectionError(ValueError):
    """The schema cannot be projected without a decision this module does not make."""


@dataclass(frozen=True, order=True)
class Projection:
    """One kind's field that states a hot filter, and its shape."""

    kind: str
    field: str
    filter: str
    shape: Shape

    @property
    def columns(self) -> tuple[str, ...]:
        """The ``record`` columns this projection fills."""
        return column_names(self.filter, self.shape)


@dataclass(frozen=True)
class Spec:
    """What the catalog indexes per kind, as read from one schema."""

    schema_id: str
    kinds: tuple[str, ...]
    projections: tuple[Projection, ...]
    opaque: tuple[tuple[str, str], ...]  # (kind, field) of every free-form object

    def to_json(self) -> dict[str, Any]:
        return {
            "kinds": list(self.kinds),
            "opaque": [list(pair) for pair in self.opaque],
            "projections": [
                {"field": p.field, "filter": p.filter, "kind": p.kind, "shape": p.shape}
                for p in self.projections
            ],
            "schema_id": self.schema_id,
        }

    @staticmethod
    def from_json(value: Any) -> "Spec":
        """A spec from its JSON; every name it would splice into SQL is checked first."""
        spec = Spec(
            schema_id=value["schema_id"],
            kinds=tuple(value["kinds"]),
            projections=tuple(
                Projection(p["kind"], p["field"], p["filter"], p["shape"])
                for p in value["projections"]
            ),
            opaque=tuple((kind, field) for kind, field in value["opaque"]),
        )
        names = [
            *spec.kinds,
            *(name for p in spec.projections for name in (p.kind, p.field)),
            *(name for pair in spec.opaque for name in pair),
        ]
        bad = [n for n in names if not isinstance(n, str) or not _NAME.fullmatch(n)]
        if bad or not isinstance(spec.schema_id, str) or not _SCHEMA_ID.fullmatch(spec.schema_id):
            raise ProjectionError(f"spec names outside the identifier rule: {bad[:3]!r}")
        for p in spec.projections:
            if p.filter not in HOT_FILTERS or p.shape not in _SHAPE_NAMES:
                raise ProjectionError(f"spec projection {p.kind}.{p.field} is not a known shape")
        return spec

    def columns(self) -> tuple[tuple[str, str], ...]:
        """Every projection column ``(name, SQL type)``, sorted by name."""
        found = {
            (name, sql_type)
            for p in self.projections
            for name, sql_type in zip(p.columns, column_types(p.shape), strict=True)
        }
        return tuple(sorted(found))

    def opaque_fields(self, kind: str) -> frozenset[str]:
        return frozenset(field for k, field in self.opaque if k == kind)

    @property
    def major(self) -> int:
        """The package-schema version the spec was read from."""
        match = _SCHEMA_ID.fullmatch(self.schema_id)
        if match is None:
            raise ProjectionError(f"spec {self.schema_id!r} names no package-schema version")
        return int(match.group(1))


BASELINE: Final = Spec("urn:neptune:schema:canonical:0", BASELINE_KINDS, (), ())
_SEMVER: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class SchemaVersion:
    """One package-schema version the Ledger reads, and the spec its records are indexed with.

    ``contract_version`` and ``schema_sha256`` name the package-schema registry version the spec
    was generated from (``contracts/package-schema/v<contract_version>/schema.json`` and the
    sha256 of its bytes): the schema is referenced by digest, never copied (ADR 0011 §1).
    """

    contract_version: str
    schema_sha256: str
    spec: Spec

    def __post_init__(self) -> None:
        match = _SEMVER.fullmatch(self.contract_version)
        if match is None or int(match.group(1)) != self.version:
            raise ProjectionError(
                f"{self.spec.schema_id} cannot come from package-schema {self.contract_version!r}:"
                " the registry major is the schema version"
            )
        if not _DIGEST.fullmatch(self.schema_sha256):
            raise ProjectionError(f"{self.schema_sha256!r} is not a sha256 content id")

    @property
    def version(self) -> int:
        return self.spec.major

    @property
    def mapping(self) -> str:
        """The projection mapping as canonical JSON text: what the catalog stores per version."""
        return canonical_json.dumps(self.spec.to_json()).decode("utf-8")

    @property
    def mapping_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.mapping.encode("utf-8")).hexdigest()

    def to_json(self) -> dict[str, Any]:
        return {
            **self.spec.to_json(),
            "contract_version": self.contract_version,
            "schema_sha256": self.schema_sha256,
        }

    @staticmethod
    def from_json(value: Any) -> "SchemaVersion":
        spec = {k: v for k, v in value.items() if k not in ("contract_version", "schema_sha256")}
        return SchemaVersion(
            value["contract_version"], value["schema_sha256"], Spec.from_json(spec)
        )


@dataclass(frozen=True)
class Registry:
    """Every package-schema version this Ledger reads, 1..n without gaps (ADR 0011 §1).

    A version's spec never changes once shipped: its records were indexed with it, and the
    catalog is append-only. The record columns are the union of every version's projections.
    """

    versions: tuple[SchemaVersion, ...]

    def __post_init__(self) -> None:
        numbers = [entry.version for entry in self.versions]
        if not numbers or numbers != list(range(1, len(numbers) + 1)):
            raise ProjectionError(f"registry versions must be 1..n without gaps, got {numbers}")

    @property
    def numbers(self) -> tuple[int, ...]:
        return tuple(entry.version for entry in self.versions)

    @property
    def latest(self) -> SchemaVersion:
        return self.versions[-1]

    def entry(self, version: int) -> SchemaVersion | None:
        if isinstance(version, bool) or not isinstance(version, int):
            return None
        return self.versions[version - 1] if 1 <= version <= len(self.versions) else None

    def spec(self, version: int) -> Spec | None:
        entry = self.entry(version)
        return entry.spec if entry is not None else None

    def columns(self) -> tuple[tuple[str, str], ...]:
        """Every projection column ``(name, SQL type)`` any version fills, sorted by name."""
        return tuple(sorted({column for e in self.versions for column in e.spec.columns()}))

    def covered(self, kind: str, version: int, column: str) -> bool:
        """Whether a ``kind`` record of ``version`` states the field behind ``column``.

        False for a NULL column means NotCovered by that version (or never a field of the kind),
        never "absent"; the migration's ``projection_covered`` answers the same in SQL.
        """
        spec = self.spec(version)
        return spec is not None and any(
            p.kind == kind and column in p.columns for p in spec.projections
        )

    def to_json(self) -> dict[str, Any]:
        return {"versions": [entry.to_json() for entry in self.versions]}

    @staticmethod
    def from_json(value: Any) -> "Registry":
        return Registry(tuple(SchemaVersion.from_json(entry) for entry in value["versions"]))


def column_names(filter_name: str, shape: Shape) -> tuple[str, ...]:
    """A logical id is two columns, namespace and value; record ids are one array column."""
    if shape == "logical_id":
        return (f"{filter_name}_namespace", f"{filter_name}_value")
    return (f"{filter_name}_ids",)


def column_types(shape: Shape) -> tuple[str, ...]:
    return ("text", "text") if shape == "logical_id" else ("text[]",)


def _free_form(field_schema: Any) -> bool:
    """An object whose keys the schema does not name (``{"type": "object"}``, or a map with
    ``additionalProperties``): its content is data, never a record's fields (ADR 0009 §2)."""
    return (
        isinstance(field_schema, Mapping)
        and field_schema.get("type") == "object"
        and "properties" not in field_schema
        and "$ref" not in field_schema
    )


def projection_spec(schema: Mapping[str, Any]) -> Spec:
    """The spec one package-schema JSON Schema gives. Raises ``ProjectionError`` when unsure."""
    try:
        defs = schema["$defs"]
        refs = [entry["$ref"] for entry in schema["anyOf"]]
        schema_id = schema["$id"]
    except (KeyError, TypeError) as exc:
        raise ProjectionError(f"not a package-schema export: {exc!r} is missing") from exc
    if (
        not isinstance(schema_id, str)
        or not _SCHEMA_ID.fullmatch(schema_id)
        or schema_id[-2:] == ":0"
    ):
        raise ProjectionError(f"schema id {schema_id!r} does not name a package-schema version")
    kinds: list[str] = []
    projections: list[Projection] = []
    opaque: list[tuple[str, str]] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref[8:] not in defs:
            raise ProjectionError(f"record kind reference {ref!r} does not resolve")
        properties = defs[ref[8:]].get("properties", {})
        kind = properties.get("kind", {}).get("const")
        if not isinstance(kind, str) or not _KIND.fullmatch(kind):
            raise ProjectionError(f"{ref} has no record kind usable as a partition name")
        if kind in kinds:
            raise ProjectionError(f"record kind {kind!r} is defined twice")
        kinds.append(kind)
        for field, field_schema in sorted(properties.items()):
            if _free_form(field_schema):
                opaque.append((kind, field))
            for filter_name, names in HOT_FILTERS.items():
                if field not in names:
                    continue
                shape = _SHAPES.get(json.dumps(field_schema, sort_keys=True))
                if shape is None:
                    raise ProjectionError(
                        f"{kind}.{field} states hot filter {filter_name!r} in a shape this"
                        f" Ledger does not project: {json.dumps(field_schema, sort_keys=True)}"
                    )
                projections.append(Projection(kind, field, filter_name, shape))
    return Spec(schema_id, tuple(sorted(kinds)), tuple(sorted(projections)), tuple(sorted(opaque)))


def render_migration(old: Spec, new: Spec, version: int) -> str:
    """The migration that takes a catalog indexed by ``old`` to ``new``; empty when equal.

    Only additions: a kind or projection that ``new`` drops raises ``ProjectionError``, because the
    catalog is append-only and a removal needs its own ADR.
    """
    gone_kinds = sorted(set(old.kinds) - set(new.kinds))
    gone = sorted(set(old.projections) - set(new.projections))
    gone_opaque = sorted(set(old.opaque) - set(new.opaque))
    if gone_kinds or gone or gone_opaque:
        raise ProjectionError(
            f"the new schema drops kinds {gone_kinds}, projections"
            f" {[f'{p.kind}.{p.field}' for p in gone]} or free-form fields"
            f" {[f'{kind}.{field}' for kind, field in gone_opaque]}; removals need an ADR"
        )
    added = sorted(set(new.projections) - set(old.projections))
    groups = sorted(
        {(p.filter, p.columns) for p in new.projections}
        - {(p.filter, p.columns) for p in old.projections}
    )
    if not added:
        return ""  # a new kind without hot filters needs no migration: it lives in record_default
    out = [
        f"-- {version:04d} record projections for {new.schema_id} (Ledger ADR 0009).",
        "--",
        "-- GENERATED by neptune_ledger.catalog.projection from the package schema's JSON Schema",
        "-- export; do not edit. A schema bump regenerates projections.json and writes the next",
        "-- migration; this one is never rewritten. Columns go on the partitioned parent, so",
        "-- every partition has them; only the kinds listed below fill them, and NULL means the",
        "-- record does not state the value as Known (the package keeps its state).",
        "--",
        "-- Hot-filter projections this migration adds (kind.field -> columns):",
    ]
    out += [f"--   {p.kind}.{p.field} -> {', '.join(p.columns)}" for p in added]
    # Rows already filed for a kind that gains a projection would read as "not Known": a blank
    # turned into a fact. A kind new to the spec states the field in every row; an older kind's
    # rows state it from some version after the old spec's on (a bump may skip versions). Either
    # makes the migration refuse, and the catalog is rebuilt from its packages and registration
    # log (ADR 0002 §4) by a Ledger that ships it.
    fresh = sorted({p.kind for p in added} - set(old.kinds))
    grown = sorted({p.kind for p in added} & set(old.kinds))
    tests = []
    if fresh:
        tests.append(f"kind IN ({', '.join(repr(k) for k in fresh)})")
    if grown:
        listed = ",\n".join(f"      '{kind}'" for kind in grown)
        tests.append(f"(schema_version > {old.major} AND kind IN (\n{listed}))")
    if tests:
        out += [
            "",
            "DO $$",
            "BEGIN",
            f"  IF EXISTS (SELECT 1 FROM record WHERE {' OR '.join(tests)}) THEN",
            "    RAISE EXCEPTION 'record rows of a kind gaining a projection would read as not'",
            "      ' Known; rebuild this catalog from its packages and registration log"
            " (ADR 0009)';",
            "  END IF;",
            "END",
            "$$;",
        ]
    for filter_name, columns in groups:
        if len(columns) == 1:
            (name,) = columns
            out += [
                "",
                f"ALTER TABLE record ADD COLUMN {name} text[];",
                f"CREATE INDEX record_by_{name} ON record USING gin ({name})",
                f"  WHERE {name} IS NOT NULL;",
            ]
            continue
        namespace, value = columns
        out += [
            "",
            f"ALTER TABLE record ADD COLUMN {namespace} text, ADD COLUMN {value} text,",
            f"  ADD CONSTRAINT record_{filter_name}_whole",
            f"  CHECK (({namespace} IS NULL) = ({value} IS NULL));",
            f"CREATE INDEX record_by_{filter_name} ON record ({namespace}, {value}, kind)",
            f"  WHERE {value} IS NOT NULL;",
        ]
    return "\n".join(out) + "\n"


@cache
def shipped_registry() -> Registry:
    """The registry this Ledger version indexes with: ``projections.json`` beside the migrations."""
    return read_registry(files("neptune_ledger.catalog").joinpath(SPEC_FILE).read_bytes())


def shipped_spec() -> Spec:
    """The spec of the newest package-schema version this Ledger reads."""
    return shipped_registry().latest.spec


def read_registry(data: bytes) -> Registry:
    """A registry from ``projections.json`` bytes, as ``registry_bytes`` writes them."""
    return Registry.from_json(canonical_json.loads(data.removesuffix(b"\n")))


def registry_bytes(registry: Registry) -> bytes:
    """``projections.json`` as committed: canonical JSON and a final newline."""
    return canonical_json.dumps(registry.to_json()) + b"\n"


def read_spec(data: bytes) -> Spec:
    """A spec from ``spec_bytes``."""
    return Spec.from_json(canonical_json.loads(data.removesuffix(b"\n")))


def spec_bytes(spec: Spec) -> bytes:
    """One spec as canonical JSON and a final newline."""
    return canonical_json.dumps(spec.to_json()) + b"\n"


def schema_version_from(schema_path: Path) -> SchemaVersion:
    """The registry entry one published package-schema version gives.

    ``schema_path`` is ``contracts/package-schema/v<version>/schema.json``; the ``version.json``
    beside it names the registry version and the schema's sha256, which must be its bytes'.
    """
    data = schema_path.read_bytes()
    try:
        published = json.loads((schema_path.parent / "version.json").read_bytes())
        contract, version = published["contract"], published["version"]
        recorded = published["schema_sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ProjectionError(
            f"{schema_path.parent} is not a published package-schema version: {exc!r}"
        ) from exc
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if contract != "package-schema" or recorded != digest or not isinstance(version, str):
        raise ProjectionError(
            f"{schema_path} is not the package-schema version its version.json records"
        )
    return SchemaVersion(version, digest, projection_spec(json.loads(data)))


def _semver(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def add_version(registry: Registry, entry: SchemaVersion) -> Registry:
    """``registry`` with ``entry``: the next version, or an indexed one whose mapping is unchanged.

    An indexed version's spec never changes (ADR 0011 §2): its records were indexed with it. A
    later registry version of the same schema version (``2.1.0`` after ``2.0.0``) only re-points
    the entry's provenance when its mapping is identical.
    """
    known = registry.entry(entry.version)
    if known == entry:
        return registry  # the same published version again: nothing changes
    if known is not None:
        if known.spec != entry.spec:
            raise ProjectionError(
                f"package-schema {entry.version} is already indexed with another mapping; a"
                " change to an indexed version needs an ADR and a rebuild"
            )
        if _semver(entry.contract_version) <= _semver(known.contract_version):
            raise ProjectionError(
                f"package-schema {entry.version} is indexed from {known.contract_version}; an"
                f" entry is re-pointed only to a later registry version, not"
                f" {entry.contract_version}"
            )
        versions = list(registry.versions)
        versions[entry.version - 1] = entry
        return Registry(tuple(versions))
    if entry.version != registry.latest.version + 1:
        raise ProjectionError(
            f"package-schema {entry.version} skips a version; add"
            f" {registry.latest.version + 1} first"
        )
    return Registry((*registry.versions, entry))


def generate(schema_path: Path, catalog_dir: Path) -> Path | None:
    """Add the package-schema version at ``schema_path`` to the registry; write its migration.

    Returns the new migration's path, or None when the version adds no projection.
    """
    entry = schema_version_from(schema_path)
    old = read_registry((catalog_dir / SPEC_FILE).read_bytes())
    new = add_version(old, entry)
    text = ""
    if old.entry(entry.version) is None:
        existing = sorted(
            int(match.group(1))
            for path in (catalog_dir / "migrations").iterdir()
            if (match := _MIGRATION.fullmatch(path.name))
        )
        number = (existing[-1] if existing else 0) + 1
        text = render_migration(old.latest.spec, entry.spec, number)
        target = catalog_dir / "migrations" / f"{number:04d}_projections_schema_{entry.version}.sql"
    if text:
        # The migration first, never over an existing file: a spec naming columns no migration
        # creates would make every registration fail.
        with target.open("x", encoding="utf-8") as out:
            out.write(text)
    (catalog_dir / SPEC_FILE).write_bytes(registry_bytes(new))
    return target if text else None


def main(argv: Sequence[str] | None = None) -> int:
    """Run from the source tree (the workspace's editable install), never from an installed
    wheel: it writes into this package's directory."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: python -m neptune_ledger.catalog.projection SCHEMA_JSON\n")
        return 2
    written = generate(Path(args[0]), Path(__file__).resolve().parent)
    sys.stdout.write(
        f"{written or 'registry rewritten; no new projection columns, so no migration'}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
