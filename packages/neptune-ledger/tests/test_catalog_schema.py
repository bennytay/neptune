"""Rules every catalog table obeys (ADR 0002): tenancy, no interpretation, two clocks apart."""

import re

import psycopg
import pytest

from ledger_catalog_rows import add_package
from neptune_ledger.catalog.migrate import apply_migrations

Conn = psycopg.Connection[tuple[object, ...]]
SCHEMA = "tenant_acme"

# Column-name fragments that would mean a table stores interpretation: inferred or model-made
# meaning, scores, or semantic grouping. The Ledger stores evidence, declared links and its own
# rebuildable indexes only (root ADR 0016 §6; Ledger ADR 0001 §3, ADR 0002 §9).
INTERPRETATION = re.compile(
    r"infer|confidence|probab|score|embedding|vector|label|predict|summary|caption|semantic"
    r"|interpret|cluster|similar|guess|estimate|model_|llm|classif|tag|merged|canonical_entity"
)


@pytest.fixture
def catalog(pg: Conn) -> Conn:
    apply_migrations(pg, "acme")
    return pg


def _tables(pg: Conn) -> list[str]:
    """Every table in the tenant schema, partitions and the partitioned parent included."""
    rows = pg.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname",
        (SCHEMA,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _columns(pg: Conn) -> list[tuple[str, str, str, str]]:
    rows = pg.execute(
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns"
        " WHERE table_schema = %s ORDER BY table_name, ordinal_position",
        (SCHEMA,),
    ).fetchall()
    return [(str(t), str(c), str(d), str(n)) for t, c, d, n in rows]


def test_the_schema_has_the_tables_the_adr_names(catalog: Conn) -> None:
    top_level = {table for table in _tables(catalog) if not table.startswith("record_")}
    assert top_level == {
        "clock",
        "package",
        "package_source",
        "record",
        "registration_log",
        "schema_migration",
        "source",
        "source_location",
        "tenant",
        "transform",
        "transform_upstream",
        "tx_clock",
    }
    assert "record_logical_id" in _tables(catalog)


def test_every_table_has_a_non_null_tenant_id(catalog: Conn) -> None:
    tables = _tables(catalog)
    assert len(tables) > 25
    tenant_columns = {t for t, c, _, nullable in _columns(catalog) if c == "tenant_id"}
    assert tenant_columns == set(tables)
    nullable = {t for t, c, _, null in _columns(catalog) if c == "tenant_id" and null == "YES"}
    assert nullable == set()


def test_every_tenant_id_references_the_schemas_single_tenant(catalog: Conn) -> None:
    rows = catalog.execute(
        "SELECT DISTINCT cl.relname FROM pg_constraint con"
        " JOIN pg_class cl ON cl.oid = con.conrelid"
        " JOIN pg_namespace n ON n.oid = cl.relnamespace"
        " JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = ANY (con.conkey)"
        " WHERE n.nspname = %s AND con.contype = 'f' AND a.attname = 'tenant_id'"
        " AND con.confrelid = %s::regclass AND cardinality(con.conkey) = 1",
        (SCHEMA, f"{SCHEMA}.tenant"),
    ).fetchall()
    referencing = {str(row[0]) for row in rows}
    assert referencing == set(_tables(catalog)) - {"tenant"}


def test_no_table_stores_interpretation(catalog: Conn) -> None:
    offenders = [(t, c) for t, c, _, _ in _columns(catalog) if INTERPRETATION.search(c)]
    assert offenders == []
    offending_tables = [t for t in _tables(catalog) if INTERPRETATION.search(t)]
    assert offending_tables == []


def test_the_interpretation_pattern_would_catch_the_usual_suspects() -> None:
    for name in ("inferred_kind", "confidence", "embedding", "entity_label", "llm_summary"):
        assert INTERPRETATION.search(name), name


def test_assertion_kind_admits_only_the_canonical_kinds(catalog: Conn) -> None:
    package = add_package(catalog, SCHEMA, "sha256:" + "b" * 64, 1)
    insert = (
        f"INSERT INTO {SCHEMA}.record (tenant_id, kind, record_id, package_id,"
        " registration_key, line, schema_version, assertion_kind)"
        " VALUES ('acme', 'run', %s, %s, 1, %s, 1, %s)"
    )
    for line, kind in enumerate(("observed", "stated", None), start=1):
        catalog.execute(insert, ("rec:sha256:" + f"{line:064x}", package, line, kind))
    with pytest.raises(psycopg.errors.CheckViolation):
        catalog.execute(insert, ("rec:sha256:" + "d" * 64, package, 9, "inferred"))


def test_no_column_has_a_calendar_type(catalog: Conn) -> None:
    """World time is integer ticks on a named clock; transaction time is RFC 3339 UTC text."""
    calendar = [(t, c, d) for t, c, d, _ in _columns(catalog) if re.search("time|date|interval", d)]
    assert calendar == []


def test_transaction_time_and_world_time_never_share_a_table(catalog: Conn) -> None:
    columns = _columns(catalog)
    tx_tables = {t for t, c, _, _ in columns if c.startswith("tx_")}
    world_tables = {t for t, c, _, _ in columns if c.startswith("world_")}
    assert tx_tables == {"package", "registration_log"}  # package copies its log entry
    assert all(t == "record" or t.startswith("record_") for t in world_tables)
    assert tx_tables.isdisjoint(world_tables)
    tx_types = {(c, d) for t, c, d, _ in columns if t == "package" and c.startswith("tx_")}
    assert tx_types == {("tx_seq", "bigint"), ("tx_time", "text")}
    world_types = {(c, d) for t, c, d, _ in columns if t == "record" and c.startswith("world_")}
    assert world_types == {
        ("world_clock", "text"),
        ("world_first", "bigint"),
        ("world_last", "bigint"),
    }


def test_world_ticks_need_a_named_clock(catalog: Conn) -> None:
    package = add_package(catalog, SCHEMA, "sha256:" + "b" * 64, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        catalog.execute(
            f"INSERT INTO {SCHEMA}.record (tenant_id, kind, record_id, package_id,"
            " registration_key, line, schema_version, world_first)"
            " VALUES ('acme', 'run', %s, %s, 1, 1, 1, 5)",
            ("rec:sha256:" + "d" * 64, package),
        )


def test_nothing_references_outside_the_tenant_schema(catalog: Conn) -> None:
    """No foreign key leaves the schema: packages are external, other tenants unreachable."""
    rows = catalog.execute(
        "SELECT cl.relname, tn.nspname FROM pg_constraint con"
        " JOIN pg_class cl ON cl.oid = con.conrelid"
        " JOIN pg_namespace n ON n.oid = cl.relnamespace"
        " JOIN pg_class tc ON tc.oid = con.confrelid"
        " JOIN pg_namespace tn ON tn.oid = tc.relnamespace"
        " WHERE n.nspname = %s AND con.contype = 'f'",
        (SCHEMA,),
    ).fetchall()
    assert rows
    assert {str(row[1]) for row in rows} == {SCHEMA}


def _package(conn: Conn, seq: int) -> str:
    return add_package(conn, SCHEMA, "sha256:" + f"{seq:064x}", seq)


def test_the_registration_log_is_the_packages_in_sequence_order(catalog: Conn) -> None:
    packages = [_package(catalog, seq) for seq in (1, 2, 3)]
    rows = catalog.execute(
        f"SELECT tx_seq, package_id FROM {SCHEMA}.registration_log ORDER BY tx_seq"
    ).fetchall()
    assert rows == list(zip((1, 2, 3), packages, strict=True))
    copied = catalog.execute(
        f"SELECT tx_seq, tx_time, package_id, root_locator, ledger_version FROM {SCHEMA}.package"
        f" EXCEPT SELECT tx_seq, tx_time, package_id, root_locator, ledger_version"
        f" FROM {SCHEMA}.registration_log"
    ).fetchall()
    assert copied == []


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE {s}.package SET root_locator = 'elsewhere'",
        "DELETE FROM {s}.package",
        "TRUNCATE {s}.package CASCADE",
        "UPDATE {s}.record SET line = 2",
        "DELETE FROM {s}.record",
        "TRUNCATE {s}.record CASCADE",
    ],
)
def test_registered_rows_are_append_only(catalog: Conn, statement: str) -> None:
    package = _package(catalog, 1)
    catalog.execute(
        f"INSERT INTO {SCHEMA}.record (tenant_id, kind, record_id, package_id,"
        " registration_key, line, schema_version) VALUES ('acme', 'run', %s, %s, 1, 1, 1)",
        ("rec:sha256:" + "d" * 64, package),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        catalog.execute(statement.format(s=SCHEMA))
    row = catalog.execute(f"SELECT count(*) FROM {SCHEMA}.record").fetchone()
    assert row == (1,)


def test_a_records_registration_key_is_its_packages_sequence(catalog: Conn) -> None:
    package = _package(catalog, 1)
    _package(catalog, 2)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        catalog.execute(
            f"INSERT INTO {SCHEMA}.record (tenant_id, kind, record_id, package_id,"
            " registration_key, line, schema_version) VALUES ('acme', 'run', %s, %s, 2, 1, 1)",
            ("rec:sha256:" + "d" * 64, package),
        )


@pytest.mark.parametrize(
    "locator",
    [
        '[{"kind":"json_pointer","pointer":"/a\\u0000b"}]',  # an escaped NUL from hostile text
        '[{"kind":"json_pointer","pointer":"/' + "x" * 5000 + '"}]',  # past the B-tree limit
    ],
)
def test_hostile_locators_are_stored_as_stated(catalog: Conn, locator: str) -> None:
    package = _package(catalog, 1)
    source = "sha256:" + "e" * 64
    catalog.execute(
        f"INSERT INTO {SCHEMA}.record (tenant_id, kind, record_id, package_id, registration_key,"
        " line, schema_version, source_content_id, source_locator)"
        " VALUES ('acme', 'run', %s, %s, 1, 1, 1, %s, %s)",
        ("rec:sha256:" + "d" * 64, package, source, locator),
    )
    row = catalog.execute(
        f"SELECT source_locator FROM {SCHEMA}.record WHERE source_content_id = %s"
        " AND kind = 'run' AND md5(source_locator) = md5(%s)",
        (source, locator),
    ).fetchone()
    assert row == (locator,)
