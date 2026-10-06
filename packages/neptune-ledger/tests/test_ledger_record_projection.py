"""Projection columns generated from the package schema's JSON Schema export (ADR 0009 §3).

The committed registry's newest spec is pinned to the newest published package-schema export, and
migrations 0005 and 0006 to the generator's output over the exports they were written from (each
version's entry is pinned in test_ledger_schema_registry.py). A schema-bump fixture adds a record
kind, and the migration the generator writes for it applies on top of the shipped ones and files the
new kind's rows.
"""

import copy
import hashlib
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

from ledger_catalog_rows import add_package
from neptune.model.record import SCHEMA_VERSION
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
    read_registry,
    read_spec,
    render_migration,
    shipped_registry,
    shipped_spec,
    spec_bytes,
)

Conn = psycopg.Connection[tuple[object, ...]]
REPO: Final = Path(__file__).resolve().parents[3]
EXPORTS: Final = REPO / "contracts" / "package-schema"
BUMPED: Final = shipped_registry().latest.version + 1  # the schema-bump fixture's version
CATALOG: Final = Path(projection.__file__).resolve().parent
RECORD: Final = "rec:sha256:" + "a" * 64
STREAM: Final = "rec:sha256:" + "b" * 64
CLOCK: Final = "rec:sha256:" + "d" * 64


def schema_export(major: int) -> dict[str, Any]:
    loaded = json.loads((EXPORTS / f"v{major}.0.0" / "schema.json").read_bytes())
    assert isinstance(loaded, dict)
    return loaded


def schema_v1() -> dict[str, Any]:
    return schema_export(1)


def schema_declared() -> dict[str, Any]:
    """The export of the package-schema version the compiler declares."""
    return schema_export(SCHEMA_VERSION)


def bumped_schema() -> dict[str, Any]:
    """The declared package schema plus a contact-event kind that states a machine, a stream and
    a clock, under the version after the registry's newest."""
    schema = copy.deepcopy(schema_declared())
    schema["$id"] = f"urn:neptune:schema:canonical:{BUMPED}"
    schema["$defs"]["ContactEvent"] = {
        "additionalProperties": False,
        "properties": {
            "clock": {"$ref": "#/$defs/RecordId"},
            "details": {"type": "object"},
            "id": {"$ref": "#/$defs/RecordId"},
            "kind": {"const": "contact_event"},
            "machine": {"$ref": "#/$defs/Knowledge_LogicalId"},
            "provenance": {"$ref": "#/$defs/Provenance"},
            "schema_version": {"const": BUMPED},
            "stream": {"$ref": "#/$defs/RecordId"},
        },
        "required": ["clock", "details", "id", "kind", "machine", "provenance", "stream"],
        "type": "object",
    }
    schema["anyOf"].append({"$ref": "#/$defs/ContactEvent"})
    return schema


def widened_schema() -> dict[str, Any]:
    """``bumped_schema`` whose contact event also states a run by logical id: a projection no
    shipped version has, so its bump migration adds ``run_namespace`` and ``run_value``."""
    schema = bumped_schema()
    event = schema["$defs"]["ContactEvent"]
    event["properties"]["run"] = {"$ref": "#/$defs/Knowledge_LogicalId"}
    event["required"] = sorted([*event["required"], "run"])
    return schema


def migration(version: int, text: str) -> Migration:
    data = text.encode("utf-8")
    return Migration(version, "bump", text, "sha256:" + hashlib.sha256(data).hexdigest())


# --- the committed spec and migration are the generator's output -------------------------------


def test_the_shipped_spec_is_generated_from_the_declared_package_schema() -> None:
    """The newest spec follows the declared version. Of the versions after 1 only 10 adds a
    projection column: 2 adds kinds with no hot filter, 3 and 4 fill columns 0005 made (their
    only migration is the guard 0006), and 10's status and safety-state kinds are the first to
    state a ``stream`` (root ADR 0071), so migration 0013 adds ``stream_ids``."""
    latest = shipped_registry().latest
    assert latest.version == SCHEMA_VERSION
    newest = EXPORTS / f"v{latest.contract_version}" / "schema.json"
    assert shipped_spec() == latest.spec == projection_spec(json.loads(newest.read_bytes()))
    added = {
        newer.contract_version: re.findall(r"ADD COLUMN (\w+)", text)
        for older, newer in pairwise(shipped_registry().versions)
        if "ADD COLUMN" in (text := render_migration(older.spec, newer.spec, 6))
    }
    assert added == {"10.0.0": ["stream_ids"]}
    assert set(shipped_spec().kinds) - set(BASELINE_KINDS) >= {
        "configuration_snapshot",
        "configuration_value",
    }


def test_each_bump_migration_is_the_generated_migration_for_its_package_schema() -> None:
    """Version 2 only adds kinds with no hot filter, so it needs no migration; versions 3 and 4
    (both unreleased in the Ledger when indexed) share migration 0006: a run filter on
    run_assembly and snapshot_binding and a site filter on the lifecycle kinds, all over
    existing columns, so 0006 is a guard."""
    assert (
        render_migration(projection_spec(schema_v1()), projection_spec(schema_export(2)), 6) == ""
    )
    (path,) = sorted((CATALOG / "migrations").glob("*_projections_schema_4.sql"))
    assert path.name == "0006_projections_schema_4.sql"
    expected = render_migration(
        projection_spec(schema_export(2)), projection_spec(schema_export(4)), 6
    )
    assert path.read_text(encoding="utf-8") == expected
    assert "ADD COLUMN" not in expected
    assert "'run_assembly', 'snapshot_binding'" in expected
    assert "'maintenance_event'" in expected


def test_migration_0005_is_the_generated_migration_for_package_schema_1() -> None:
    (path,) = sorted((CATALOG / "migrations").glob("*_projections_schema_1.sql"))
    assert path.name == "0005_projections_schema_1.sql"
    expected = render_migration(BASELINE, projection_spec(schema_v1()), 5)
    assert path.read_text(encoding="utf-8") == expected


def test_the_baseline_kinds_are_migration_0001s_partitions() -> None:
    text = (CATALOG / "migrations" / "0001_catalog.sql").read_text(encoding="utf-8")
    partitions = re.findall(r"PARTITION OF record\s+FOR VALUES IN \('([a-z_]+)'\)", text)
    assert tuple(sorted(partitions)) == BASELINE_KINDS


def test_a_record_of_an_older_version_without_the_field_projects_nothing() -> None:
    """A hot-filter field a later schema version adds is absent from older records: NULL."""
    spec = Spec(
        "urn:neptune:schema:canonical:3",
        ("stream",),
        (Projection("stream", "machine", "machine", "logical_id"),),
        (),
    )
    assert projected(spec, "stream", {"run": RECORD}) == (None, None)


def test_the_shipped_spec_follows_the_compilers_schema_version() -> None:
    """A schema bump cannot ship stale projections: regenerate projections.json with the bump."""
    assert shipped_spec().major == SCHEMA_VERSION
    exports = sorted(
        (int(p.parent.name.removeprefix("v").split(".")[0]), p)
        for p in (REPO / "contracts" / "package-schema").glob("v*/schema.json")
    )
    newest = exports[-1][1]
    assert shipped_spec() == projection_spec(json.loads(newest.read_bytes()))


@pytest.mark.parametrize(
    "damage",
    [
        lambda v: v["projections"][0].update(field="machine;DROP TABLE record"),
        lambda v: v["projections"][0].update(filter="machine_value, kind) --"),
        lambda v: v["projections"][0].update(shape="raw_sql"),
        lambda v: v["kinds"].append("Run"),
        lambda v: v["opaque"].append(["stream", "meta\ndata"]),
        lambda v: v.update(schema_id="urn:neptune:schema:canonical:1; --"),
    ],
)
def test_a_spec_with_names_outside_the_identifier_rule_is_refused(damage: Any) -> None:
    value = shipped_spec().to_json()
    damage(value)
    with pytest.raises(ProjectionError):
        Spec.from_json(value)


def test_a_kind_outside_the_spec_has_no_projections() -> None:
    """The compiler's kind list is not closed: a kind the spec does not name is still indexed,
    with every projection column NULL (ADR 0009 §3)."""
    record = {"machine": {"knowledge": "known", "value": {"namespace": "a", "value": "b"}}}
    assert projected(shipped_spec(), "contact_event", record) == (None,) * len(projection_columns())


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
    assert spec.opaque == (
        ("ingest_finding", "details"),
        ("stream", "metadata"),
        ("transform_record", "config"),
        ("transform_record", "libraries"),
    )
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


def test_a_bump_that_adds_a_kind_renders_its_new_columns_only() -> None:
    """No partition: the new kind lives in record_default (ADR 0008; ADR 0009 §6). Every column
    the fixture's kind states exists already (``stream_ids`` since 0013), so the bump is a guard
    and adds none."""
    text = render_migration(shipped_spec(), projection_spec(bumped_schema()), 5)
    assert text.startswith(f"-- 0005 record projections for urn:neptune:schema:canonical:{BUMPED}")
    assert "CREATE TABLE" not in text
    assert "IF EXISTS (SELECT 1 FROM record WHERE kind IN ('contact_event')) THEN" in text
    assert "--   contact_event.machine -> machine_namespace, machine_value" in text
    assert "--   contact_event.stream -> stream_ids" in text
    assert "ADD COLUMN" not in text  # machine, clock and stream columns already exist


@pytest.mark.parametrize("change", ["drop_kind", "drop_projection", "drop_opaque"])
def test_a_removal_needs_an_adr(change: str) -> None:
    old = projection_spec(schema_v1())
    if change == "drop_kind":
        new = Spec(old.schema_id, old.kinds[1:], old.projections, old.opaque)
    elif change == "drop_projection":
        new = Spec(old.schema_id, old.kinds, old.projections[1:], old.opaque)
    else:
        new = Spec(old.schema_id, old.kinds, old.projections, old.opaque[1:])
    with pytest.raises(ProjectionError, match="removals need an ADR"):
        render_migration(old, new, 5)


def test_a_free_form_field_that_gains_named_properties_stops_being_free_form_loudly() -> None:
    schema = schema_v1()
    schema["$defs"]["TransformRecord"]["properties"]["config"] = {
        "properties": {"rate": {"type": "integer"}},
        "type": "object",
    }
    with pytest.raises(ProjectionError, match=r"free-form fields \['transform_record\.config'\]"):
        render_migration(projection_spec(schema_v1()), projection_spec(schema), 6)


def test_a_hot_filter_in_an_unknown_shape_is_refused() -> None:
    schema = schema_v1()
    schema["$defs"]["Run"]["properties"]["machine"] = {"$ref": "#/$defs/Knowledge_string"}
    with pytest.raises(ProjectionError, match=r"run\.machine states hot filter 'machine'"):
        projection_spec(schema)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda s: s.pop("$defs"), "not a package-schema export"),
        (
            lambda s: s.update({"$id": "urn:neptune:schema:canonical:x"}),
            "does not name a package-schema version",
        ),
        (
            lambda s: s.update({"$id": "urn:neptune:schema:canonical:0"}),
            "does not name a package-schema version",
        ),
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


def test_a_projection_added_to_an_existing_kind_guards_its_filed_rows() -> None:
    schema = schema_v1()
    schema["$id"] = "urn:neptune:schema:canonical:2"
    schema["$defs"]["Image"]["properties"]["stream"] = {"$ref": "#/$defs/RecordId"}
    text = render_migration(projection_spec(schema_v1()), projection_spec(schema), 6)
    # Rows of any version after the old spec's state the field, even when a bump skips versions.
    assert "IF EXISTS (SELECT 1 FROM record WHERE (schema_version > 1 AND kind IN (" in text
    assert "      'image'))) THEN" in text


def published(directory: Path, schema: dict[str, Any], version: str) -> Path:
    """``schema`` published as package-schema ``version`` in ``directory`` (as the registry
    lays a version out: schema.json and the version.json recording its sha256)."""
    directory.mkdir(parents=True, exist_ok=True)
    data = json.dumps(schema).encode("utf-8")
    (directory / "schema.json").write_bytes(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    record = {"contract": "package-schema", "schema_sha256": digest, "version": version}
    (directory / "version.json").write_text(json.dumps(record), encoding="utf-8")
    return directory / "schema.json"


def test_generate_writes_the_spec_and_numbers_the_next_migration(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog"
    (catalog / "migrations").mkdir(parents=True)
    for path in (CATALOG / "migrations").glob("*.sql"):
        (catalog / "migrations" / path.name).write_bytes(path.read_bytes())
    (catalog / "projections.json").write_bytes((CATALOG / "projections.json").read_bytes())
    # The newest published version again: nothing new, and the registry bytes do not change.
    newest = EXPORTS / f"v{shipped_registry().latest.contract_version}" / "schema.json"
    assert generate(newest, catalog) is None
    assert (catalog / "projections.json").read_bytes() == (
        CATALOG / "projections.json"
    ).read_bytes()
    written = generate(published(tmp_path / "bumped", bumped_schema(), f"{BUMPED}.0.0"), catalog)
    number = len(migrations()) + 1
    assert written == catalog / "migrations" / f"{number:04d}_projections_schema_{BUMPED}.sql"
    registry = read_registry((catalog / "projections.json").read_bytes())
    assert registry.numbers == (*shipped_registry().numbers, BUMPED)
    assert registry.latest.spec == projection_spec(bumped_schema())
    assert registry.versions[:-1] == shipped_registry().versions


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
        f" VALUES ('acme', 'contact_event', %s, %s, 1, 1, %s, %s, %s::jsonb,"
        f" {', '.join(['%s'] * len(columns))})",
        (RECORD, package, BUMPED, "sha256:" + "0" * 64, json.dumps(record), *values),
    )
    row = pg.execute(
        "SELECT tableoid::regclass::text, stream_ids, body ->> 'kind' FROM tenant_acme.record"
        " WHERE stream_ids @> ARRAY[%s]",
        (STREAM,),
    ).fetchone()
    assert row == ("tenant_acme.record_default", [STREAM], "contact_event")
    indexes = {
        str(r[0])
        for r in pg.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'tenant_acme'"
            " AND tablename = 'record'"
        ).fetchall()
    }
    assert {"record_by_stream_ids", "record_by_machine", "record_by_clock_ids"} <= indexes


def test_a_bump_migration_refuses_rows_of_its_kind_already_filed(pg: Conn) -> None:
    """Rows of the new kind filed in record_default before its projections exist would read as
    not Known; the generated migration refuses and the catalog is rebuilt (ADR 0009 §3). The
    migration is one transaction: the columns it would add are not left behind."""
    new = projection_spec(widened_schema())
    shipped = migrations()
    apply_migrations(pg, "acme")
    package = add_package(pg, "tenant_acme", "sha256:" + "e" * 64, 1)
    pg.execute(
        "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id,"
        " registration_key, line, schema_version, body_digest)"
        " VALUES ('acme', 'contact_event', %s, %s, 1, 1, %s, %s)",
        (RECORD, package, BUMPED, "sha256:" + "0" * 64),
    )
    text = render_migration(shipped_spec(), new, len(shipped) + 1)
    assert "ADD COLUMN run_namespace text" in text and "ADD COLUMN run_value text" in text
    bump = migration(len(shipped) + 1, text)
    with pytest.raises(psycopg.errors.RaiseException, match="rebuild this catalog"):
        apply_migrations(pg, "acme", shipped=(*shipped, bump))
    columns = pg.execute(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'tenant_acme'"
        " AND table_name = 'record' AND column_name IN ('run_namespace', 'run_value')"
    ).fetchone()
    assert columns == (0,)
    applied = pg.execute(
        "SELECT count(*) FROM tenant_acme.schema_migration WHERE version = %s",
        (len(shipped) + 1,),
    ).fetchone()
    assert applied == (0,)
    filed = pg.execute(
        "SELECT stream_ids FROM tenant_acme.record WHERE kind = 'contact_event'"
    ).fetchall()
    assert filed == [(None,)]


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
