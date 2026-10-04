"""The schema-version registry and cross-version indexing of packages (Ledger ADR 0011, MVL-93).

The registry (``catalog/projections.json``) holds one projection spec per package-schema version,
each pinned here to the published ``contracts/package-schema`` version it was generated from. The
two-version fixture is the drone ingested by a schema-1 compiler (the worked example) and by a
schema-2 compiler (``at_schema_2``: the same flight log re-identified under adapter 2.0.0, plus the
configuration kinds schema 2 adds). Both are indexed side by side, as is the schema-4 manipulator
cell (deployment lifecycle kinds) beside the schema-1 drone; a version the registry does not hold
is refused before anything is written.
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

from conftest import new_database
from ledger_catalog_rows import add_package
from neptune.identity import canonical_json
from neptune.model.kinds import kinds_at
from neptune.model.knowledge import Known
from neptune.model.record import SCHEMA_VERSION
from neptune_ledger.api import codec
from neptune_ledger.catalog import check, projection
from neptune_ledger.catalog.index import (
    UnindexedVersion,
    package_rows,
    projected,
    projection_columns,
)
from neptune_ledger.catalog.migrate import apply_migrations, migrations
from neptune_ledger.catalog.projection import (
    Projection,
    ProjectionError,
    Registry,
    SchemaVersion,
    Spec,
    add_version,
    read_registry,
    registry_bytes,
    render_migration,
    schema_version_from,
    shipped_registry,
)
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.contract_tests.examples import (
    WorkedPackage,
    at_schema_1,
    at_schema_2,
    evidence_anchor,
    examples_dir,
    materialise,
    reparse,
    write,
)

Conn = psycopg.Connection[tuple[object, ...]]
REPO: Final = Path(__file__).resolve().parents[3]
PUBLISHED: Final = REPO / "contracts" / "package-schema"
CATALOG: Final = Path(projection.__file__).resolve().parent
DIGEST: Final = "sha256:" + "0" * 64
REGISTRY_TABLES: Final = ("schema_version", "schema_version_projection")


def fresh(uri: str) -> PostgresCatalog:
    with psycopg.connect(uri, autocommit=True) as conn:
        apply_migrations(conn, "acme")
    return PostgresCatalog(uri, "acme", package_roots=None)


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


@pytest.fixture
def older(tmp_path: Path) -> WorkedPackage:
    """The drone as a schema-1 compiler wrote it."""
    return write("drone-v1", tmp_path / "drone-v1", at_schema_1("drone"))


@pytest.fixture
def newer(tmp_path: Path) -> WorkedPackage:
    """The same flight log as a schema-2 compiler writes it, beside the parameter file."""
    return write("drone-v2", tmp_path / "drone-v2", at_schema_2("drone", "2.0.0", {}))


def rows(conn: Conn, query: str, *args: object) -> list[tuple[Any, ...]]:
    return [tuple(row) for row in conn.execute(query, args).fetchall()]


def entry(version: int, spec: Spec) -> SchemaVersion:
    return SchemaVersion(f"{version}.0.0", DIGEST, spec)


# --- the registry is generated from the published package-schema versions ----------------------


def test_each_version_is_generated_from_its_published_package_schema() -> None:
    registry = shipped_registry()
    assert registry.numbers == tuple(range(1, SCHEMA_VERSION + 1))  # every version it writes
    for shipped in registry.versions:
        directory = PUBLISHED / f"v{shipped.contract_version}"
        assert shipped == schema_version_from(directory / "schema.json")
        recorded = json.loads((directory / "version.json").read_bytes())["schema_sha256"]
        assert shipped.schema_sha256 == recorded
    assert (CATALOG / "projections.json").read_bytes() == registry_bytes(registry)


def test_each_version_holds_exactly_the_compilers_kinds_of_that_version() -> None:
    for shipped in shipped_registry().versions:
        assert set(shipped.spec.kinds) == set(kinds_at(shipped.version))
        assert set(shipped.spec.kinds) == check.kinds_of(shipped.version)


def test_each_version_only_adds_to_the_one_before() -> None:
    """The model only grows (root ADR 0037 §1), so the newest spec's columns are every column."""
    specs = [e.spec for e in shipped_registry().versions]
    for older, newer in pairwise(specs):
        assert set(older.kinds) < set(newer.kinds)
        assert set(older.projections) <= set(newer.projections)
        assert set(older.opaque) <= set(newer.opaque)
    assert specs[0].projections == specs[1].projections  # version 2 adds no hot filter
    assert projection_columns(shipped_registry()) == projection_columns(specs[-1])


def test_versions_3_and_4_share_the_guard_migration_0006() -> None:
    """Neither adds a column: 3's run filters and 4's lifecycle sites fill columns 0005 made. 0006
    was generated from version 2 to version 4 in one step; the registry keeps them apart."""
    registry = shipped_registry()
    two, four = registry.spec(2), registry.spec(4)
    assert two is not None and four is not None
    shipped = (CATALOG / "migrations" / "0006_projections_schema_4.sql").read_text(encoding="utf-8")
    assert render_migration(two, four, 6) == shipped
    assert "ADD COLUMN" not in shipped
    assert {m.name for m in migrations()} >= {"projections_schema_1", "projections_schema_4"}
    assert not any(m.name in ("projections_schema_2", "projections_schema_3") for m in migrations())


def test_the_registry_round_trips_byte_for_byte() -> None:
    data = (CATALOG / "projections.json").read_bytes()
    assert registry_bytes(read_registry(data)) == data
    assert data.endswith(b"}\n") and b"\n" not in data[:-1]
    assert read_registry(data) == shipped_registry()


@pytest.mark.parametrize(
    "versions",
    [(), (2,), (1, 3), (1, 1)],
    ids=["empty", "no_version_1", "gap", "twice"],
)
def test_a_registry_with_a_gap_is_refused(versions: tuple[int, ...]) -> None:
    spec = shipped_registry().latest.spec
    entries = tuple(
        entry(v, Spec(f"urn:neptune:schema:canonical:{v}", spec.kinds, (), ())) for v in versions
    )
    with pytest.raises(ProjectionError, match=r"1\.\.n without gaps"):
        Registry(entries)


@pytest.mark.parametrize(
    ("contract", "digest", "message"),
    [
        ("1.0.0", DIGEST, "registry major is the schema version"),
        ("2", DIGEST, "registry major is the schema version"),
        ("2.0.0", "md5:00", "not a sha256 content id"),
    ],
)
def test_an_entry_must_name_its_own_package_schema_version(
    contract: str, digest: str, message: str
) -> None:
    with pytest.raises(ProjectionError, match=message):
        SchemaVersion(contract, digest, shipped_registry().versions[1].spec)  # version 2


def test_a_published_version_whose_digest_disagrees_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "v2.0.0"
    directory.mkdir()
    schema = (PUBLISHED / "v2.0.0" / "schema.json").read_bytes()
    (directory / "schema.json").write_bytes(schema + b" ")
    (directory / "version.json").write_bytes((PUBLISHED / "v2.0.0" / "version.json").read_bytes())
    with pytest.raises(ProjectionError, match="not the package-schema version"):
        schema_version_from(directory / "schema.json")
    (directory / "version.json").unlink()
    with pytest.raises(ProjectionError, match="not a published package-schema version"):
        schema_version_from(directory / "schema.json")


def test_adding_a_version_appends_it_and_never_remaps_an_indexed_one() -> None:
    registry = shipped_registry()
    latest, n = registry.latest.spec, registry.latest.version

    def at(version: int, spec: Spec) -> Spec:
        return Spec(f"urn:neptune:schema:canonical:{version}", spec.kinds, spec.projections, ())

    assert add_version(registry, entry(n + 1, at(n + 1, latest))).numbers == (
        *registry.numbers,
        n + 1,
    )
    with pytest.raises(ProjectionError, match=f"skips a version; add {n + 1} first"):
        add_version(registry, entry(n + 2, at(n + 2, latest)))
    remapped = Spec(latest.schema_id, latest.kinds, latest.projections[1:], latest.opaque)
    with pytest.raises(ProjectionError, match="already indexed with another mapping"):
        add_version(registry, entry(n, remapped))
    # A later registry version of schema n with the same mapping only re-points the provenance.
    repointed = add_version(registry, SchemaVersion(f"{n}.1.0", DIGEST, latest))
    assert repointed.numbers == registry.numbers
    assert repointed.latest.contract_version == f"{n}.1.0"
    assert repointed.latest.mapping_digest == registry.latest.mapping_digest
    assert add_version(registry, registry.latest) == registry  # the same version again
    with pytest.raises(ProjectionError, match="only to a later registry version, not"):
        add_version(repointed, SchemaVersion(f"{n}.0.0", DIGEST, latest))


def test_the_mapping_is_canonical_json_and_its_digest_pins_it() -> None:
    for shipped in shipped_registry().versions:
        assert shipped.mapping == canonical_json.dumps(shipped.spec.to_json()).decode()
        expected = "sha256:" + hashlib.sha256(shipped.mapping.encode()).hexdigest()
        assert shipped.mapping_digest == expected
    digests = [e.mapping_digest for e in shipped_registry().versions]
    assert len(set(digests)) == len(digests)


# --- which versions the Ledger reads -------------------------------------------------------------


def test_the_ledger_reads_the_registry_versions_the_compiler_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert check.readable_versions() == shipped_registry().numbers
    monkeypatch.setattr(check, "SCHEMA_VERSION", 1)
    assert check.readable_versions() == (1,)


# --- coverage: a field a newer version adds is NotCovered for older records ---------------------


def _grown() -> Registry:
    """Two versions in which version 2 gives ``stream`` a ``machine`` field it lacked."""
    run = Projection("stream", "run", "run", "record_id")
    machine = Projection("stream", "machine", "machine", "logical_id")
    return Registry(
        (
            entry(1, Spec("urn:neptune:schema:canonical:1", ("stream",), (run,), ())),
            entry(2, Spec("urn:neptune:schema:canonical:2", ("stream",), (machine, run), ())),
        )
    )


def test_a_field_a_newer_version_adds_is_not_covered_for_older_records() -> None:
    registry = _grown()
    assert registry.covered("stream", 2, "machine_value")
    assert not registry.covered("stream", 1, "machine_value")
    assert registry.covered("stream", 1, "run_ids")
    assert not registry.covered("stream", 3, "run_ids")  # a version the registry lacks
    assert not registry.covered("run", 2, "machine_value")  # never a field of the kind


def test_each_record_is_projected_with_its_own_versions_spec() -> None:
    """A version-1 and a version-2 record of one kind, in one version-2 package."""
    registry = _grown()
    machine: Any = {"knowledge": "known", "value": {"namespace": "serial", "value": "qx-7"}}
    run = "rec:sha256:" + "1" * 64
    old: Any = {"id": "rec:sha256:" + "a" * 64, "kind": "stream", "run": run, "schema_version": 1}
    new: Any = {**old, "id": "rec:sha256:" + "b" * 64, "machine": machine, "schema_version": 2}
    # A machine field under version 1 is not version 1's: the spec of the record's own version
    # decides what is projected.
    stray: Any = {**old, "id": "rec:sha256:" + "c" * 64, "machine": machine}
    manifest = {"receipt": "rec:sha256:" + "d" * 64, "schema_version": 2, "sources": []}
    lines = {"stream": tuple(canonical_json.dumps(r) for r in (old, new, stray))}
    built = package_rows("sha256:" + "e" * 64, manifest, lines, registry)
    columns = projection_columns(registry)
    assert columns == ("machine_namespace", "machine_value", "run_ids")
    projected_by_id = {
        r.record_id: dict(zip(columns, r.projected, strict=True)) for r in built.records
    }
    assert projected_by_id[old["id"]] == {
        "machine_namespace": None,
        "machine_value": None,
        "run_ids": [run],
    }
    assert projected_by_id[stray["id"]] == projected_by_id[old["id"]]
    assert projected_by_id[new["id"]] == {
        "machine_namespace": "serial",
        "machine_value": "qx-7",
        "run_ids": [run],
    }
    assert built.schema_versions == (1, 2)
    first = registry.spec(1)
    assert first is not None
    assert projected(first, "stream", old) == ([run],)


@pytest.mark.parametrize(("record_version", "package_version"), [(3, 3), (2, 1), (0, 1)])
def test_a_record_of_a_version_the_registry_or_its_package_lacks_is_unindexed(
    record_version: int, package_version: int
) -> None:
    record: Any = {
        "id": "rec:sha256:" + "a" * 64,
        "kind": "stream",
        "schema_version": record_version,
    }
    manifest = {"receipt": "rec:sha256:" + "d" * 64, "schema_version": package_version}
    with pytest.raises(UnindexedVersion, match="this Ledger indexes 1, 2"):
        package_rows(
            DIGEST,
            {**manifest, "sources": []},
            {"stream": (canonical_json.dumps(record),)},
            _grown(),
        )


# --- catalog-api names kinds by the package-schema contract (ADR 0011 §4) -----------------------


def test_the_catalog_api_export_does_not_depend_on_the_compilers_kinds() -> None:
    """A compiler with one more record kind exports byte-identical catalog-api schema."""
    script = (
        "import hashlib, neptune.model.kinds as k\n"
        "k.RECORD_KINDS = {**k.RECORD_KINDS, 'contact_event': k.RECORD_KINDS['run']}\n"
        "from neptune_ledger.api.codec import _catalog_schema_text\n"
        "print(hashlib.sha256(_catalog_schema_text()).hexdigest())\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    from neptune_ledger.api.codec import _catalog_schema_text

    assert done.stdout.strip() == hashlib.sha256(_catalog_schema_text()).hexdigest()
    definition = codec.catalog_schema()["$defs"]["RecordKind"]
    assert "enum" not in definition
    assert definition["pattern"] == "^[a-z][a-z0-9_]*$"
    assert "contracts/package-schema" in definition["description"]


# --- the database: two versions side by side -----------------------------------------------------


def test_packages_of_schema_1_and_2_are_indexed_side_by_side(
    catalog: PostgresCatalog, pg: Conn, older: WorkedPackage, newer: WorkedPackage
) -> None:
    """Acceptance (MVL-93): both versions' records are lineage siblings in one record space, and
    what the older schema has no table for reads NotCovered, never "none found"."""
    first, second = catalog.register(older.root), catalog.register(newer.root)
    assert (first.outcome, second.outcome) == ("registered", "registered")
    assert (first.schema_version, second.schema_version) == (Known(1), Known(2))

    registered = rows(
        pg,
        "SELECT schema_version, schema_id, contract_version, schema_sha256, mapping_digest,"
        " first_registration_key FROM tenant_acme.schema_version ORDER BY 1",
    )
    assert registered == [
        (e.version, e.spec.schema_id, e.contract_version, e.schema_sha256, e.mapping_digest, seq)
        for e, seq in zip(shipped_registry().versions[:2], (1, 2), strict=True)
    ]

    # Lineage siblings (ADR 0003 §4.1): same kind and evidence anchor, another transform.
    siblings = rows(
        pg,
        "SELECT a.kind, a.schema_version, b.schema_version FROM tenant_acme.record a"
        " JOIN tenant_acme.record b ON b.kind = a.kind"
        "  AND b.source_content_id = a.source_content_id AND b.source_locator = a.source_locator"
        "  AND b.transform_id <> a.transform_id"
        " WHERE a.package_id = %s AND b.package_id = %s ORDER BY 1",
        older.package_id,
        newer.package_id,
    )
    cited = sorted(
        kind
        for kind, _, record in older.every_record()
        if kind != "ingest_finding" and evidence_anchor(record) is not None
    )
    assert [kind for kind, _, _ in siblings] == cited and len(cited) > 5
    assert {(old, new) for _, old, new in siblings} == {(1, 1)}  # version-1 kinds stay version 1

    # Kind coverage: the schema-2 package holds configuration tables; the schema-1 package's
    # version does not cover them (NotCovered), which is not the same as an empty table.
    covered = rows(
        pg,
        "SELECT p.schema_version, k.kind = ANY(v.kinds) FROM tenant_acme.package p"
        " JOIN tenant_acme.schema_version v USING (tenant_id, schema_version)"
        " CROSS JOIN (VALUES ('configuration_snapshot'), ('run')) AS k(kind)"
        " ORDER BY 1, k.kind",
    )
    assert covered == [(1, False), (1, True), (2, True), (2, True)]
    held = rows(
        pg,
        "SELECT schema_version, count(*) FROM tenant_acme.record"
        " WHERE package_id = %s AND kind LIKE 'configuration%%' GROUP BY 1",
        newer.package_id,
    )
    assert held == [(2, 5)]

    # Column coverage, in SQL: a run's machine is a version-1 field; no configuration kind has one.
    assert rows(
        pg,
        "SELECT tenant_acme.projection_covered('run', 1, 'machine_value'),"
        " tenant_acme.projection_covered('run', 2, 'machine_value'),"
        " tenant_acme.projection_covered('configuration_snapshot', 2, 'machine_value'),"
        " tenant_acme.projection_covered('run', 99, 'machine_value')",
    ) == [(True, True, False, False)]
    projections = rows(
        pg,
        "SELECT schema_version, kind, field, column_name FROM tenant_acme.schema_version_projection"
        " ORDER BY 1, 2, 3, 4",
    )
    assert projections == sorted(
        (e.version, p.kind, p.field, column)
        for e in shipped_registry().versions[:2]
        for p in e.spec.projections
        for column in p.columns
    )


def test_a_lifecycle_package_is_indexed_beside_a_schema_1_package(
    catalog: PostgresCatalog, pg: Conn, older: WorkedPackage, tmp_path: Path
) -> None:
    """The schema-4 manipulator cell beside the schema-1 drone: each version is recorded by the
    registration that first states it, a lifecycle record's site fills the site columns, and the
    schema-1 package's version does not cover the lifecycle kinds (NotCovered, not "none")."""
    cell = materialise("manipulator_cell", tmp_path / "manipulator_cell")
    assert cell.schema_version == 4
    assert catalog.register(older.root).outcome == "registered"
    assert catalog.register(cell.root).outcome == "registered"
    # The cell's records state version 1 (sources, transforms, clocks) and 4 (lifecycle kinds).
    assert rows(
        pg,
        "SELECT schema_version, first_registration_key FROM tenant_acme.schema_version ORDER BY 1",
    ) == [(1, 1), (4, 2)]
    covered = rows(
        pg,
        "SELECT p.schema_version, 'maintenance_event' = ANY(v.kinds) FROM tenant_acme.package p"
        " JOIN tenant_acme.schema_version v USING (tenant_id, schema_version) ORDER BY 1",
    )
    assert covered == [(1, False), (4, True)]
    sites = rows(
        pg,
        "SELECT kind, site_namespace, site_value FROM tenant_acme.record"
        " WHERE package_id = %s AND schema_version = 4 ORDER BY 1, record_id",
        cell.package_id,
    )
    expected = sorted(
        (kind, record["site"]["value"]["namespace"], record["site"]["value"]["value"])
        for kind, _, record in cell.every_record()
        if record.get("schema_version") == 4
    )
    assert sorted(sites) == expected and len(expected) >= 4
    assert rows(
        pg,
        "SELECT tenant_acme.projection_covered('maintenance_event', 4, 'site_value'),"
        " tenant_acme.projection_covered('maintenance_event', 1, 'site_value'),"
        " tenant_acme.projection_covered('run', 4, 'site_value')",
    ) == [(True, False, False)]


def test_the_sql_coverage_agrees_with_the_registry_everywhere(
    catalog: PostgresCatalog, pg: Conn, newer: WorkedPackage
) -> None:
    """``projection_covered`` and ``Registry.covered`` answer alike for every kind, version and
    column the catalog has seen, so the Python and SQL readings of NotCovered cannot drift. A
    version it has not seen covers nothing: no record of it is filed."""
    assert catalog.register(newer.root).outcome == "registered"
    registry = Registry(shipped_registry().versions[:2])  # what the schema-2 package brought
    cases = [
        (kind, version, column)
        for version in (*registry.numbers, registry.latest.version + 1)
        for kind in registry.latest.spec.kinds
        for column in projection_columns(registry)
    ]
    answers = rows(
        pg,
        "SELECT tenant_acme.projection_covered(c.kind, c.version, c.column_name)"
        " FROM unnest(%s::text[], %s::int[], %s::text[]) WITH ORDINALITY"
        "  AS c(kind, version, column_name, n) ORDER BY n",
        [k for k, _, _ in cases],
        [v for _, v, _ in cases],
        [c for _, _, c in cases],
    )
    assert [a for (a,) in answers] == [registry.covered(*case) for case in cases]
    assert any(a for (a,) in answers) and not all(a for (a,) in answers)


def test_a_version_is_recorded_once_by_the_registration_that_first_brings_it(
    catalog: PostgresCatalog, pg: Conn, older: WorkedPackage, tmp_path: Path
) -> None:
    files = reparse("drone", "1.0.0", {"profile": "b"}, up_to=1)
    sibling = write("drone-b", tmp_path / "drone-b", files)
    assert catalog.register(older.root).outcome == "registered"
    assert catalog.register(sibling.root).outcome == "registered"
    assert rows(
        pg, "SELECT schema_version, first_registration_key FROM tenant_acme.schema_version"
    ) == [(1, 1)]


def test_a_package_of_a_version_the_registry_lacks_is_refused_and_writes_nothing(
    catalog: PostgresCatalog, pg: Conn, newer: WorkedPackage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ledger whose registry stops at version 1 refuses a version-2 package, although the
    compiler reads it: newer than any projection the Ledger knows, so never guessed at."""
    monkeypatch.setattr(
        check, "shipped_registry", lambda: Registry(shipped_registry().versions[:1])
    )
    result = catalog.register(newer.root)
    assert result.outcome == "refused"
    assert [(f.code, f.subject) for f in result.findings] == [
        ("unsupported_schema_version", "manifest.json")
    ]
    assert result.findings[0].detail == "schema version 2; this Ledger reads 1"
    assert result.schema_version == Known(2)
    assert rows(pg, "SELECT last_seq FROM tenant_acme.tx_clock") == [(0,)]
    for table in ("package", *REGISTRY_TABLES):
        assert rows(pg, f"SELECT count(*) FROM tenant_acme.{table}") == [(0,)]


def test_the_newest_version_is_read_and_the_next_is_refused(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    """Boundary: the registry's newest version (the schema-5 assertion golden, root ADR 0062)
    registers; the same package one version past it is a future version."""
    goldens = examples_dir().parents[1] / "golden" / "assertion"
    cell = materialise("cell_baseline", tmp_path / "cell_baseline", goldens)
    assert cell.schema_version == shipped_registry().latest.version
    manifest = dict(cell.manifest)
    manifest["schema_version"] = shipped_registry().latest.version + 1
    future = tmp_path / "cell_baseline-next"
    future.mkdir()
    for path, data in cell.files.items():
        (future / path).parent.mkdir(parents=True, exist_ok=True)
        (future / path).write_bytes(
            canonical_json.dumps(manifest) if path == "manifest.json" else data
        )
    refused = catalog.register(future)
    assert [f.code for f in refused.findings] == ["unsupported_schema_version"]
    assert catalog.register(cell.root).outcome == "registered"


def test_a_catalog_that_indexed_a_version_another_way_is_not_extended(
    catalog: PostgresCatalog, pg: Conn, older: WorkedPackage, newer: WorkedPackage
) -> None:
    """A stored mapping for version 2 that differs from this Ledger's: refuse and say rebuild."""
    assert catalog.register(older.root).outcome == "registered"
    spec = shipped_registry().versions[1].spec  # version 2
    pg.execute(
        "INSERT INTO tenant_acme.schema_version VALUES"
        " ('acme', 2, %s, '2.0.0', %s, %s, '{}', %s, 1)",
        (spec.schema_id, DIGEST, list(spec.kinds), DIGEST),
    )
    result = catalog.register(newer.root)
    assert result.outcome == "refused"
    (finding,) = result.findings
    assert finding.code == "unsupported_schema_version"
    assert "rebuild the catalog" in finding.detail
    assert rows(pg, "SELECT count(*) FROM tenant_acme.package") == [(1,)]
    assert rows(pg, "SELECT last_seq FROM tenant_acme.tx_clock") == [(1,)]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE tenant_acme.schema_version SET contract_version = '1.1.0'",
        "DELETE FROM tenant_acme.schema_version",
        "TRUNCATE tenant_acme.schema_version CASCADE",
        "UPDATE tenant_acme.schema_version_projection SET field = 'site'",
        "DELETE FROM tenant_acme.schema_version_projection",
        "TRUNCATE tenant_acme.schema_version_projection",
    ],
)
def test_registry_rows_are_append_only(
    catalog: PostgresCatalog, pg: Conn, older: WorkedPackage, statement: str
) -> None:
    assert catalog.register(older.root).outcome == "registered"
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        pg.execute(statement)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("schema_id", "urn:neptune:schema:canonical:3"),
        ("contract_version", "3.0.0"),
        ("contract_version", "2.0"),
        ("kinds", []),
    ],
)
def test_a_registry_row_must_agree_with_its_version(
    catalog: PostgresCatalog, pg: Conn, older: WorkedPackage, column: str, value: object
) -> None:
    assert catalog.register(older.root).outcome == "registered"
    good: dict[str, object] = {
        "schema_id": "urn:neptune:schema:canonical:2",
        "contract_version": "2.0.0",
        "kinds": ["run"],
    }
    good[column] = value
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO tenant_acme.schema_version VALUES"
            " ('acme', 2, %s, %s, %s, %s, '{}', %s, 1)",
            (good["schema_id"], good["contract_version"], DIGEST, good["kinds"], DIGEST),
        )


def test_registry_rows_are_a_function_of_the_registration_order(
    pg_server: str, older: WorkedPackage, newer: WorkedPackage
) -> None:
    """Determinism: the same order gives identical rows; another order moves only the
    registration that first brought each version (ADR 0002 §4: a rebuild replays the log). The
    schema-2 package states version 1 too, in its version-1 kinds' records."""
    dumps = []
    for order in ((older, newer), (older, newer), (newer, older)):
        uri = new_database(pg_server)
        with fresh(uri) as made:
            for package in order:
                assert made.register(package.root).outcome == "registered"
        with psycopg.connect(uri) as conn:
            dumps.append(
                [
                    rows(conn, f"SELECT * FROM tenant_acme.{table} ORDER BY 1, 2, 3")
                    for table in REGISTRY_TABLES
                ]
            )
    assert dumps[0] == dumps[1]
    first_seen = [[row[-1] for row in dump[0]] for dump in dumps]
    assert first_seen == [[1, 2], [1, 2], [1, 1]]
    without = [[[row[:-1] for row in dump[0]], dump[1]] for dump in dumps]
    assert without[0] == without[2]


def test_the_registry_migration_refuses_a_catalog_that_already_holds_packages(pg: Conn) -> None:
    """Their versions would have no registry rows, so their NULLs could not be told apart."""
    (number,) = [m.version for m in migrations() if m.name == "schema_version_registry"]
    before = tuple(m for m in migrations() if m.version < number)
    apply_migrations(pg, "acme", shipped=before)
    add_package(pg, "tenant_acme", DIGEST, 1)
    with pytest.raises(psycopg.errors.RaiseException, match="rebuild this catalog"):
        apply_migrations(pg, "acme")
    assert rows(pg, "SELECT to_regclass('tenant_acme.schema_version') IS NULL") == [(True,)]
