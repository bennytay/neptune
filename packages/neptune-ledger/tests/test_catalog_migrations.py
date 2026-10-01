"""The catalog migrations apply to a clean PostgreSQL 16, per tenant, idempotently (ADR 0002)."""

import re

import psycopg
import pytest

from neptune.model.kinds import RECORD_KINDS
from neptune_ledger.catalog.migrate import (
    MigrationError,
    apply_migrations,
    migrations,
    tenant_schema,
)

Conn = psycopg.Connection[tuple[object, ...]]
TX_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
ROBOT = "rec:sha256:" + "a" * 64
PACKAGE = "sha256:" + "b" * 64


def _columns(pg: Conn, schema: str) -> list[tuple[object, ...]]:
    return pg.execute(
        "SELECT table_name, column_name, data_type, is_nullable, ordinal_position"
        " FROM information_schema.columns WHERE table_schema = %s"
        " ORDER BY table_name, ordinal_position",
        (schema,),
    ).fetchall()


def test_server_is_postgres_16(pg: Conn) -> None:
    row = pg.execute("SHOW server_version_num").fetchone()
    assert row is not None
    assert 160000 <= int(str(row[0])) < 170000


def test_migrations_apply_to_a_clean_database_and_rerun_as_a_no_op(pg: Conn) -> None:
    assert apply_migrations(pg, "acme") == [m.version for m in migrations()]
    before = _columns(pg, "tenant_acme")
    assert apply_migrations(pg, "acme") == []
    assert _columns(pg, "tenant_acme") == before
    recorded = pg.execute(
        "SELECT version, name, sha256 FROM tenant_acme.schema_migration ORDER BY version"
    ).fetchall()
    assert recorded == [(m.version, m.name, m.sha256) for m in migrations()]
    assert pg.execute("SELECT tenant_id FROM tenant_acme.tenant").fetchall() == [("acme",)]


def test_every_tenant_gets_an_identical_schema_of_its_own(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    apply_migrations(pg, "harbour_ops")
    assert _columns(pg, "tenant_acme") == _columns(pg, "tenant_harbour_ops")
    assert pg.execute("SELECT tenant_id FROM tenant_harbour_ops.tenant").fetchall() == [
        ("harbour_ops",)
    ]


def test_a_schema_holds_exactly_one_tenant(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("INSERT INTO tenant_acme.tenant (tenant_id) VALUES ('other')")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        pg.execute(
            "INSERT INTO tenant_acme.source (tenant_id, content_id, size) VALUES ('other', %s, 1)",
            (PACKAGE,),
        )


def test_public_has_no_access_to_a_tenant_schema(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    row = pg.execute("SELECT has_schema_privilege('public', 'tenant_acme', 'USAGE')").fetchone()
    assert row == (False,)


@pytest.mark.parametrize("tenant", ["a", "acme_2", "x" + "y" * 47])
def test_tenant_ids_that_make_valid_schemas(tenant: str) -> None:
    assert tenant_schema(tenant) == f"tenant_{tenant}"


@pytest.mark.parametrize(
    "tenant",
    ["", "Acme", "2acme", "acme-ops", "acme;drop schema public", "x" + "y" * 48, "ä", "acme\n"],
)
def test_malformed_tenant_ids_are_refused(tenant: str) -> None:
    with pytest.raises(ValueError, match="tenant id"):
        tenant_schema(tenant)


def test_a_malformed_tenant_id_never_reaches_the_database(pg: Conn) -> None:
    with pytest.raises(ValueError):
        apply_migrations(pg, 'acme"; DROP SCHEMA public; --')
    rows = pg.execute("SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_%'").fetchall()
    assert rows == []


def test_an_edited_migration_is_refused(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    pg.execute("UPDATE tenant_acme.schema_migration SET sha256 = %s WHERE version = 1", (PACKAGE,))
    with pytest.raises(MigrationError, match="never edited"):
        apply_migrations(pg, "acme")


def test_a_migration_this_ledger_does_not_ship_is_refused(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    pg.execute(
        "INSERT INTO tenant_acme.schema_migration VALUES ('acme', 99, 'future', %s)", (PACKAGE,)
    )
    with pytest.raises(MigrationError, match="does not ship"):
        apply_migrations(pg, "acme")


def test_a_failed_run_leaves_nothing_behind(pg: Conn) -> None:
    pg.execute("CREATE SCHEMA tenant_acme")
    pg.execute("CREATE TABLE tenant_acme.package (x int)")  # collides with migration 1
    with pytest.raises(psycopg.errors.DuplicateTable):
        apply_migrations(pg, "acme")
    tables = pg.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'tenant_acme'"
    ).fetchall()
    assert tables == [("package",)]


def test_migration_versions_are_contiguous_from_one() -> None:
    shipped = migrations()
    assert [m.version for m in shipped] == list(range(1, len(shipped) + 1))
    assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", m.sha256) for m in shipped)


def test_record_partitions_are_exactly_the_package_schema_kinds(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    rows = pg.execute(
        "SELECT pg_get_expr(c.relpartbound, c.oid) FROM pg_inherits i"
        " JOIN pg_class c ON c.oid = i.inhrelid"
        " WHERE i.inhparent = 'tenant_acme.record'::regclass"
    ).fetchall()
    kinds = {re.fullmatch(r"FOR VALUES IN \('(\w+)'\)", str(row[0])).group(1) for row in rows}  # type: ignore[union-attr]
    assert kinds == set(RECORD_KINDS)
    assert len(rows) == len(RECORD_KINDS)  # and no DEFAULT partition


def test_a_record_of_an_unknown_kind_is_refused(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    pg.execute(
        "INSERT INTO tenant_acme.package VALUES ('acme', %s, 1, %s, 'root', '0.0.1', 1, %s)",
        (PACKAGE, ROBOT, "2026-10-02T00:00:00.000000Z"),
    )
    with pytest.raises(psycopg.errors.CheckViolation):  # no partition for the value
        pg.execute(
            "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id, line,"
            " schema_version) VALUES ('acme', 'telepathy', %s, %s, 1, 1)",
            (ROBOT, PACKAGE),
        )


def test_the_transaction_clock_is_strictly_sequenced_and_never_goes_back(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    ticks = [pg.execute("SELECT * FROM tenant_acme.next_tx()").fetchone() for _ in range(3)]
    assert [tick[0] for tick in ticks if tick] == [1, 2, 3]
    times = [str(tick[1]) for tick in ticks if tick]
    assert all(TX_TIME.fullmatch(t) for t in times)
    assert times == sorted(times)
    # A host clock that has gone backwards cannot move transaction time back.
    future = "2999-01-01T00:00:00.000000Z"
    pg.execute("UPDATE tenant_acme.tx_clock SET last_time = %s", (future,))
    assert pg.execute("SELECT * FROM tenant_acme.next_tx()").fetchone() == (4, future)


def test_the_transaction_clock_lives_in_its_tenants_schema(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    apply_migrations(pg, "harbour_ops")
    pg.execute("SELECT tenant_acme.next_tx()")
    pg.execute("SELECT tenant_acme.next_tx()")
    assert pg.execute("SELECT * FROM tenant_harbour_ops.next_tx()").fetchone() is not None
    assert pg.execute("SELECT last_seq FROM tenant_harbour_ops.tx_clock").fetchone() == (1,)
    assert pg.execute("SELECT last_seq FROM tenant_acme.tx_clock").fetchone() == (2,)


@pytest.mark.parametrize(
    "bad", ["2026-10-02T00:00:00Z", "2026-10-02 00:00:00.000000Z", "2026-10-02T00:00:00.000000+00"]
)
def test_transaction_time_is_rfc3339_utc_with_microseconds_only(pg: Conn, bad: str) -> None:
    apply_migrations(pg, "acme")
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO tenant_acme.package VALUES ('acme', %s, 1, %s, 'root', '0.0.1', 1, %s)",
            (PACKAGE, ROBOT, bad),
        )
