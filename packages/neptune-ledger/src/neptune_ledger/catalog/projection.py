"""Kind-specific projection columns, generated from the compiler's JSON Schema (Ledger ADR 0008).

The package schema's JSON Schema export (``contracts/package-schema/v<version>/schema.json``, from
``neptune.model.schema``) names every record kind and its top-level fields. ``projection_spec``
reads it into a ``Spec``: the kinds, the hot-filter fields (machine, site, run, stream, clock) each
kind states, and the free-form objects no pointer walk may enter. ``render_migration`` turns the
difference between two specs into a migration: a partition per new kind, columns and indexes per
new hot-filter shape. Neither touches a database, so both are pure functions of their inputs.

The spec the Ledger indexes with is committed beside the migrations as ``projections.json``, and
``index`` reads that file, never the compiler's live schema: indexing is a function of the package
and the Ledger version alone. A schema bump is one command, which rewrites the spec and writes the
next migration::

    uv run python -m neptune_ledger.catalog.projection contracts/package-schema/v1.0.0/schema.json

A hot filter in a shape this module does not know, or a kind or projection that disappears, raises
``ProjectionError``: a new shape or a removal is a decision for an ADR, not a guess.
"""

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

# The schema shapes a hot-filter field may have, and how each is projected (ADR 0008 §3).
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
_FREE_FORM: Final = {"type": "object"}
SPEC_FILE: Final = "projections.json"
_MIGRATION: Final = re.compile(r"(\d{4})_[a-z0-9_]+\.sql")
_SCHEMA_ID: Final = re.compile(r"urn:neptune:schema:canonical:([1-9][0-9]{0,8})")


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
        return Spec(
            schema_id=value["schema_id"],
            kinds=tuple(value["kinds"]),
            projections=tuple(
                Projection(p["kind"], p["field"], p["filter"], p["shape"])
                for p in value["projections"]
            ),
            opaque=tuple((kind, field) for kind, field in value["opaque"]),
        )

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


BASELINE: Final = Spec("baseline: migration 0001", BASELINE_KINDS, (), ())


def column_names(filter_name: str, shape: Shape) -> tuple[str, ...]:
    """A logical id is two columns, namespace and value; record ids are one array column."""
    if shape == "logical_id":
        return (f"{filter_name}_namespace", f"{filter_name}_value")
    return (f"{filter_name}_ids",)


def column_types(shape: Shape) -> tuple[str, ...]:
    return ("text", "text") if shape == "logical_id" else ("text[]",)


def projection_spec(schema: Mapping[str, Any]) -> Spec:
    """The spec one package-schema JSON Schema gives. Raises ``ProjectionError`` when unsure."""
    try:
        defs = schema["$defs"]
        refs = [entry["$ref"] for entry in schema["anyOf"]]
        schema_id = schema["$id"]
    except (KeyError, TypeError) as exc:
        raise ProjectionError(f"not a package-schema export: {exc!r} is missing") from exc
    if not isinstance(schema_id, str) or not _SCHEMA_ID.fullmatch(schema_id):
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
            if field_schema == _FREE_FORM:
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
    added_kinds = sorted(set(new.kinds) - set(old.kinds))
    added = sorted(set(new.projections) - set(old.projections))
    groups = sorted(
        {(p.filter, p.columns) for p in new.projections}
        - {(p.filter, p.columns) for p in old.projections}
    )
    if not added_kinds and not added:
        return ""
    out = [
        f"-- {version:04d} record projections for {new.schema_id} (Ledger ADR 0008).",
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
    # turned into a fact. Records of an older schema version do not state the field, so only
    # rows of this version or later make the migration refuse; the catalog is then rebuilt from
    # its packages and registration log (ADR 0002 §4) by a Ledger that ships this migration.
    existing = sorted({p.kind for p in added} & set(old.kinds))
    if existing:
        out += [
            "",
            "DO $$",
            "BEGIN",
            f"  IF EXISTS (SELECT 1 FROM record WHERE schema_version >= {new.major} AND kind IN (",
            ",\n".join(f"      '{kind}'" for kind in existing) + ")) THEN",
            "    RAISE EXCEPTION 'record rows of a kind gaining a projection would read as not'",
            "      ' Known; rebuild this catalog from its packages and registration log"
            " (ADR 0008)';",
            "  END IF;",
            "END",
            "$$;",
        ]
    for kind in added_kinds:
        out += ["", f"CREATE TABLE record_{kind} PARTITION OF record FOR VALUES IN ('{kind}');"]
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
def shipped_spec() -> Spec:
    """The spec this Ledger version indexes with: ``projections.json`` beside the migrations."""
    return read_spec(files("neptune_ledger.catalog").joinpath(SPEC_FILE).read_bytes())


def read_spec(data: bytes) -> Spec:
    """A spec from ``projections.json`` bytes, as ``spec_bytes`` writes them."""
    return Spec.from_json(canonical_json.loads(data.removesuffix(b"\n")))


def spec_bytes(spec: Spec) -> bytes:
    """``projections.json`` as committed: canonical JSON and a final newline."""
    return canonical_json.dumps(spec.to_json()) + b"\n"


def generate(schema_path: Path, catalog_dir: Path) -> Path | None:
    """Regenerate ``projections.json`` from ``schema_path`` and write the next migration.

    Returns the new migration's path, or None when the schema adds nothing.
    """
    schema = json.loads(schema_path.read_bytes())
    old = read_spec((catalog_dir / SPEC_FILE).read_bytes())
    new = projection_spec(schema)
    existing = sorted(
        int(match.group(1))
        for path in (catalog_dir / "migrations").iterdir()
        if (match := _MIGRATION.fullmatch(path.name))
    )
    version = (existing[-1] if existing else 0) + 1
    text = render_migration(old, new, version)
    target = catalog_dir / "migrations" / f"{version:04d}_projections_schema_{new.major}.sql"
    if text:
        # The migration first, never over an existing file: a spec naming columns no migration
        # creates would make every registration fail.
        with target.open("x", encoding="utf-8") as out:
            out.write(text)
    (catalog_dir / SPEC_FILE).write_bytes(spec_bytes(new))
    return target if text else None


def main(argv: Sequence[str] | None = None) -> int:
    """Run from the source tree (the workspace's editable install), never from an installed
    wheel: it writes into this package's directory."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: python -m neptune_ledger.catalog.projection SCHEMA_JSON\n")
        return 2
    written = generate(Path(args[0]), Path(__file__).resolve().parent)
    sys.stdout.write(f"{written or 'no new kinds or projections'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
