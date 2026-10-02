"""Projection columns generated from the package schema's JSON Schema export (ADR 0008 §3).

The committed spec and migration 0004 are pinned to the generator's output over the published
package-schema v1.0.0 export. A schema-bump fixture adds a record kind, and the migration the
generator writes for it applies on top of the shipped ones and files the new kind's rows.
"""

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

from ledger_catalog_rows import add_package
from neptune.model.kinds import RECORD_KINDS
from neptune_ledger.catalog import projection
from neptune_ledger.catalog.index import projected, projection_columns
from neptune_ledger.catalog.migrate import Migration, apply_migrations, migrations
from neptune_ledger.catalog.projection import (
    BASELINE,
    BASELINE_KINDS,
    Projection,
    ProjectionError,
    Spec,
    generate,
    projection_spec,
    read_spec,
    render_migration,
    shipped_spec,
    spec_bytes,
)

Conn = psycopg.Connection[tuple[object, ...]]
REPO: Final = Path(__file__).resolve().parents[3]
SCHEMA_V1: Final = REPO / "contracts" / "package-schema" / "v1.0.0" / "schema.json"
CATALOG: Final = Path(projection.__file__).resolve().parent
RECORD: Final = "rec:sha256:" + "a" * 64
STREAM: Final = "rec:sha256:" + "b" * 64
CLOCK: Final = "rec:sha256:" + "d" * 64


def schema_v1() -> dict[str, Any]:
    loaded = json.loads(SCHEMA_V1.read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def bumped_schema() -> dict[str, Any]:
    """Package schema 1 plus a contact-event kind that states a machine, a stream and a clock."""
    schema = copy.deepcopy(schema_v1())
    schema["$id"] = "urn:neptune:schema:canonical:2"
    schema["$defs"]["ContactEvent"] = {
        "additionalProperties": False,
        "properties": {
            "clock": {"$ref": "#/$defs/RecordId"},
            "details": {"type": "object"},
            "id": {"$ref": "#/$defs/RecordId"},
            "kind": {"const": "contact_event"},
            "machine": {"$ref": "#/$defs/Knowledge_LogicalId"},
            "provenance": {"$ref": "#/$defs/Provenance"},
            "schema_version": {"const": 2},
            "stream": {"$ref": "#/$defs/RecordId"},
        },
        "required": ["clock", "details", "id", "kind", "machine", "provenance", "stream"],
        "type": "object",
    }
    schema["anyOf"].append({"$ref": "#/$defs/ContactEvent"})
    return schema


def migration(version: int, text: str) -> Migration:
    data = text.encode("utf-8")
    return Migration(version, "bump", text, "sha256:" + hashlib.sha256(data).hexdigest())


# --- the committed spec and migration are the generator's output -------------------------------


def test_the_shipped_spec_is_generated_from_package_schema_1() -> None:
    assert (CATALOG / "projections.json").read_bytes() == spec_bytes(projection_spec(schema_v1()))
    assert shipped_spec() == projection_spec(schema_v1())


def test_migration_0004_is_the_generated_migration_for_package_schema_1() -> None:
    (path,) = sorted((CATALOG / "migrations").glob("0004_*.sql"))
    assert path.name == "0004_projections_schema_1.sql"
    expected = render_migration(BASELINE, projection_spec(schema_v1()), 4)
    assert path.read_text(encoding="utf-8") == expected


def test_the_baseline_kinds_are_migration_0001s_partitions() -> None:
    text = (CATALOG / "migrations" / "0001_catalog.sql").read_text(encoding="utf-8")
    partitions = re.findall(r"PARTITION OF record\s+FOR VALUES IN \('([a-z_]+)'\)", text)
    assert tuple(sorted(partitions)) == BASELINE_KINDS


def test_the_shipped_spec_covers_exactly_the_compilers_kinds() -> None:
    """A canary: a kind the compiler adds must arrive through a regenerated spec and migration."""
    assert set(shipped_spec().kinds) == set(RECORD_KINDS)


def test_package_schema_1_projects_the_hot_filters_by_name_and_shape() -> None:
    spec = projection_spec(schema_v1())
    assert {(p.kind, p.field, p.filter, p.shape) for p in spec.projections} == {
        ("asset", "site", "site", "logical_id"),
        ("calibration", "machine", "machine", "logical_id"),
        ("hardware_configuration", "machine", "machine", "logical_id"),
        ("run", "machine", "machine", "logical_id"),
        ("software_configuration", "machine", "machine", "logical_id"),
        ("stream", "clocks", "clock", "record_ids"),
        ("stream", "run", "run", "record_id"),
        ("video", "clock", "clock", "record_id"),
    }
    assert spec.opaque == (("ingest_finding", "details"), ("transform_record", "config"))
    assert projection_columns(spec) == (
        "clock_ids",
        "machine_namespace",
        "machine_value",
        "run_ids",
        "site_namespace",
        "site_value",
    )


# --- generation is pure and refuses what it cannot decide --------------------------------------


def test_generation_is_deterministic_and_idempotent() -> None:
    first, second = projection_spec(schema_v1()), projection_spec(schema_v1())
    assert first == second
    assert spec_bytes(first) == spec_bytes(second)
    assert read_spec(spec_bytes(first)) == first
    assert render_migration(first, first, 5) == ""


def test_schema_key_order_does_not_change_the_spec() -> None:
    schema = schema_v1()
    reordered = json.loads(json.dumps(schema, sort_keys=True))
    reordered["anyOf"] = list(reversed(reordered["anyOf"]))
    assert projection_spec(reordered) == projection_spec(schema)


def test_a_bump_that_adds_a_kind_renders_its_partition_and_new_columns_only() -> None:
    text = render_migration(projection_spec(schema_v1()), projection_spec(bumped_schema()), 5)
    assert text.startswith("-- 0005 record projections for urn:neptune:schema:canonical:2")
    assert "CREATE TABLE record_contact_event PARTITION OF record" in text
    assert "ADD COLUMN stream_ids text[]" in text
    assert "--   contact_event.machine -> machine_namespace, machine_value" in text
    assert "ADD COLUMN machine_" not in text  # machine columns already exist
    assert "ADD COLUMN clock_ids" not in text


@pytest.mark.parametrize(
    "change",
    [
        "drop_kind",
        "drop_projection",
    ],
)
def test_a_removal_needs_an_adr(change: str) -> None:
    old = projection_spec(schema_v1())
    if change == "drop_kind":
        new = Spec(old.schema_id, old.kinds[1:], old.projections, old.opaque)
    else:
        new = Spec(old.schema_id, old.kinds, old.projections[1:], old.opaque)
    with pytest.raises(ProjectionError, match="removals need an ADR"):
        render_migration(old, new, 5)


def test_a_hot_filter_in_an_unknown_shape_is_refused() -> None:
    schema = schema_v1()
    schema["$defs"]["Run"]["properties"]["machine"] = {"$ref": "#/$defs/Knowledge_string"}
    with pytest.raises(ProjectionError, match=r"run\.machine states hot filter 'machine'"):
        projection_spec(schema)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda s: s.pop("$defs"), "not a package-schema export"),
        (lambda s: s.pop("anyOf"), "not a package-schema export"),
        (lambda s: s["anyOf"].append({"$ref": "#/$defs/Nowhere"}), "does not resolve"),
        (lambda s: s["anyOf"].append({"$ref": "https://elsewhere/x"}), "does not resolve"),
        (lambda s: s["anyOf"].append(s["anyOf"][0]), "defined twice"),
        (
            lambda s: s["$defs"]["Run"]["properties"]["kind"].update({"const": "Run; DROP"}),
            "no record kind usable",
        ),
        (
            lambda s: s["$defs"]["Run"]["properties"]["kind"].update({"const": "r" * 57}),
            "no record kind usable",
        ),
        (lambda s: s["$defs"]["Run"]["properties"].pop("kind"), "no record kind usable"),
    ],
)
def test_a_malformed_schema_is_refused(damage: Any, message: str) -> None:
    schema = schema_v1()
    damage(schema)
    with pytest.raises(ProjectionError, match=message):
        projection_spec(schema)


def test_the_longest_kind_name_still_makes_a_partition_name() -> None:
    schema = schema_v1()
    schema["$defs"]["Run"]["properties"]["kind"] = {"const": "r" * 56}
    spec = projection_spec(schema)
    assert "r" * 56 in spec.kinds
    assert len("record_" + "r" * 56) <= 63


def test_generate_writes_the_spec_and_numbers_the_next_migration(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    (catalog / "migrations").mkdir(parents=True)
    for path in (CATALOG / "migrations").glob("*.sql"):
        (catalog / "migrations" / path.name).write_bytes(path.read_bytes())
    (catalog / "projections.json").write_bytes((CATALOG / "projections.json").read_bytes())
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(schema_v1()), encoding="utf-8")
    assert generate(schema, catalog) is None  # nothing new
    schema.write_text(json.dumps(bumped_schema()), encoding="utf-8")
    written = generate(schema, catalog)
    assert (
        written == catalog / "migrations" / f"{len(migrations()) + 1:04d}_projections_schema_2.sql"
    )
    assert read_spec((catalog / "projections.json").read_bytes()) == projection_spec(
        bumped_schema()
    )


def test_the_generator_command_needs_one_schema_path() -> None:
    assert projection.main([]) == 2
    assert projection.main(["a", "b"]) == 2


# --- the generated bump migration applies and files the new kind -------------------------------


def test_a_schema_bump_migration_applies_and_files_the_new_kind(pg: Conn) -> None:
    new = projection_spec(bumped_schema())
    shipped = migrations()
    bump = migration(len(shipped) + 1, render_migration(shipped_spec(), new, len(shipped) + 1))
    apply_migrations(pg, "acme", shipped=(*shipped, bump))
    package = add_package(pg, "tenant_acme", "sha256:" + "e" * 64, 1)
    record = {
        "clock": CLOCK,
        "details": {"machine": {"knowledge": "known", "value": {"namespace": "x", "value": "y"}}},
        "id": RECORD,
        "kind": "contact_event",
        "machine": {"knowledge": "known", "value": {"namespace": "serial", "value": "arm-7"}},
        "stream": STREAM,
    }
    columns = projection_columns(new)
    values = projected(new, "contact_event", record)
    assert dict(zip(columns, values, strict=True)) == {
        "clock_ids": [CLOCK],
        "machine_namespace": "serial",
        "machine_value": "arm-7",
        "run_ids": None,
        "site_namespace": None,
        "site_value": None,
        "stream_ids": [STREAM],
    }
    pg.execute(
        "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id, registration_key,"
        f" line, schema_version, body_digest, body, {', '.join(columns)})"
        f" VALUES ('acme', 'contact_event', %s, %s, 1, 1, 2, %s, %s::jsonb,"
        f" {', '.join(['%s'] * len(columns))})",
        (RECORD, package, "sha256:" + "0" * 64, json.dumps(record), *values),
    )
    row = pg.execute(
        "SELECT tableoid::regclass::text, stream_ids, body ->> 'kind' FROM tenant_acme.record"
        " WHERE stream_ids @> ARRAY[%s]",
        (STREAM,),
    ).fetchone()
    assert row == ("tenant_acme.record_contact_event", [STREAM], "contact_event")
    indexes = {
        str(r[0])
        for r in pg.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'tenant_acme'"
            " AND tablename = 'record'"
        ).fetchall()
    }
    assert {"record_by_stream_ids", "record_by_machine", "record_by_clock_ids"} <= indexes


def test_without_the_bump_the_new_kind_is_refused(pg: Conn) -> None:
    """No default partition (ADR 0002 §5): a kind the migrations do not name is never filed."""
    apply_migrations(pg, "acme")
    package = add_package(pg, "tenant_acme", "sha256:" + "e" * 64, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id,"
            " registration_key, line, schema_version, body_digest)"
            " VALUES ('acme', 'contact_event', %s, %s, 1, 1, 2, %s)",
            (RECORD, package, "sha256:" + "0" * 64),
        )


def test_a_logical_id_projection_is_both_columns_or_neither(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    package = add_package(pg, "tenant_acme", "sha256:" + "e" * 64, 1)
    with pytest.raises(psycopg.errors.CheckViolation, match="record_machine_whole"):
        pg.execute(
            "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id,"
            " registration_key, line, schema_version, body_digest, machine_value)"
            " VALUES ('acme', 'run', %s, %s, 1, 1, 1, %s, 'arm-7')",
            (RECORD, package, "sha256:" + "0" * 64),
        )


def test_a_projection_is_ordered_by_its_fields() -> None:
    first = Projection("run", "machine", "machine", "logical_id")
    second = Projection("stream", "run", "run", "record_id")
    assert sorted([second, first]) == [first, second]
    assert first.columns == ("machine_namespace", "machine_value")
    assert second.columns == ("run_ids",)
