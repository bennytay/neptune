"""The catalog migrations apply to a clean PostgreSQL 16, per tenant, idempotently (ADR 0002)."""

import re
from pathlib import Path

import psycopg
import pytest

from ledger_catalog_rows import add_package
from neptune.model.kinds import RECORD_KINDS
from neptune_ledger.catalog import migrate
from neptune_ledger.catalog.migrate import (
    MigrationError,
    apply_migrations,
    check_collation,
    migrations,
    tenant_schema,
)

Conn = psycopg.Connection[tuple[object, ...]]
TX_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
ROBOT = "rec:sha256:" + "a" * 64
PACKAGE = "sha256:" + "b" * 64
DIGEST = "sha256:" + "f" * 64  # a record body digest (ADR 0005 §2)


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
    add_package(pg, "tenant_acme", PACKAGE, 1)
    with pytest.raises(psycopg.errors.CheckViolation):  # no partition for the value
        pg.execute(
            "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id,"
            " registration_key, line, schema_version, body_digest)"
            f" VALUES ('acme', 'telepathy', %s, %s, 1, 1, 1, '{DIGEST}')",
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
    pg.execute("SELECT tenant_acme.replay_tx(4, %s)", (future,))
    assert pg.execute("SELECT * FROM tenant_acme.next_tx()").fetchone() == (5, future)


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
        pg.execute("SELECT tenant_acme.replay_tx(1, %s)", (bad,))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO tenant_acme.registration_log VALUES ('acme', 1, %s, %s, 'root', '0.0.1')",
            (bad, PACKAGE),
        )


def test_a_rebuild_replays_logged_ticks_and_live_ticks_continue_after_them(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    logged = [(1, "2026-10-01T09:00:00.000000Z"), (2, "2026-10-01T09:00:00.000000Z")]
    for seq, time in logged:
        pg.execute("SELECT tenant_acme.replay_tx(%s, %s)", (seq, time))
    tick = pg.execute("SELECT * FROM tenant_acme.next_tx()").fetchone()
    assert tick is not None and tick[0] == 3 and str(tick[1]) >= logged[-1][1]


@pytest.mark.parametrize(
    "tick",
    [
        (2, "2026-10-01T08:59:59.999999Z"),  # earlier than the clock's time
        (2, "2026-10-01T09:00:00Z"),  # not the tx_time shape
        (1, "2026-10-01T10:00:00.000000Z"),  # not after the clock's sequence
    ],
)
def test_a_replayed_tick_must_follow_the_clock(pg: Conn, tick: tuple[int, str]) -> None:
    apply_migrations(pg, "acme")
    pg.execute("SELECT tenant_acme.replay_tx(1, '2026-10-01T09:00:00.000000Z')")
    with pytest.raises((psycopg.errors.RaiseException, psycopg.errors.CheckViolation)):
        pg.execute("SELECT tenant_acme.replay_tx(%s, %s)", tick)
    assert pg.execute("SELECT last_seq FROM tenant_acme.tx_clock").fetchone() == (1,)


@pytest.mark.parametrize(
    ("kind", "key"),
    [("source_artifact", ROBOT), ("run", PACKAGE), ("run", "rec:sha256:" + "A" * 64)],
)
def test_a_record_key_must_have_its_kinds_shape(pg: Conn, kind: str, key: str) -> None:
    apply_migrations(pg, "acme")
    add_package(pg, "tenant_acme", PACKAGE, 1)
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(
            "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id,"
            " registration_key, line, schema_version, body_digest)"
            f" VALUES ('acme', %s, %s, %s, 1, 1, 1, '{DIGEST}')",
            (kind, key, PACKAGE),
        )


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["0001_catalog.sql", "0002_add-index.sql"], "not named"),
        (["0001_catalog.sql", "0003_later.sql"], "without gaps"),
    ],
)
def test_misnamed_or_missing_migration_files_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str], message: str
) -> None:
    (tmp_path / "migrations").mkdir()
    for name in names:
        (tmp_path / "migrations" / name).write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "migrations" / "README.txt").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(migrate, "files", lambda _: tmp_path)
    with pytest.raises(MigrationError, match=message):
        migrations()


@pytest.mark.parametrize(
    "update",
    [
        "SET last_seq = 1, last_time = NULL",  # forget the time
        "SET last_seq = 2",  # reuse a sequence
        "SET last_seq = 1",  # go back
        "SET last_seq = 4, last_time = '2026-10-01T08:00:00.000000Z'",  # earlier time
    ],
)
def test_the_clock_refuses_any_update_that_does_not_move_it_forward(pg: Conn, update: str) -> None:
    apply_migrations(pg, "acme")
    pg.execute("SELECT tenant_acme.replay_tx(2, '2026-10-01T09:00:00.000000Z')")
    with pytest.raises(psycopg.errors.RaiseException, match="only moves forward"):
        pg.execute(f"UPDATE tenant_acme.tx_clock {update}")
    with pytest.raises(psycopg.errors.RaiseException, match="only moves forward"):
        pg.execute("DELETE FROM tenant_acme.tx_clock")
    assert pg.execute("SELECT last_seq, last_time FROM tenant_acme.tx_clock").fetchone() == (
        2,
        "2026-10-01T09:00:00.000000Z",
    )


def test_a_registration_older_than_the_last_is_refused(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    add_package(pg, "tenant_acme", PACKAGE, 2)
    with pytest.raises(psycopg.errors.RaiseException, match="never goes backwards"):
        pg.execute(
            "INSERT INTO tenant_acme.registration_log VALUES"
            " ('acme', 1, '2026-10-02T00:00:01.000000Z', %s, 'root', '0.0.1')",
            ("sha256:" + "e" * 64,),
        )


def test_a_registration_must_use_the_tick_just_allocated(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    pg.execute("SELECT tenant_acme.next_tx()")
    with pytest.raises(psycopg.errors.RaiseException, match="not the tick just allocated"):
        pg.execute(
            "INSERT INTO tenant_acme.registration_log VALUES"
            " ('acme', 2, '2999-01-01T00:00:00.000000Z', %s, 'root', '0.0.1')",
            (PACKAGE,),
        )


def test_a_package_older_than_the_last_registered_is_refused(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    older, newer = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    for seq, package in ((1, older), (2, newer)):
        time = f"2026-10-02T00:00:0{seq}.000000Z"
        pg.execute("SELECT tenant_acme.replay_tx(%s, %s)", (seq, time))
        pg.execute(
            "INSERT INTO tenant_acme.registration_log VALUES ('acme', %s, %s, %s, 'root', '0.0.1')",
            (seq, time, package),
        )
    insert = (
        "INSERT INTO tenant_acme.package (tenant_id, package_id, schema_version, receipt_id)"
        " VALUES ('acme', %s, 1, %s)"
    )
    pg.execute(insert, (newer, ROBOT))
    with pytest.raises(psycopg.errors.RaiseException, match="never goes backwards"):
        pg.execute(insert, (older, ROBOT))


def test_a_package_must_match_its_log_entry(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    with pytest.raises(psycopg.errors.RaiseException, match="not in the registration log"):
        pg.execute(
            "INSERT INTO tenant_acme.package (tenant_id, package_id, schema_version, receipt_id)"
            " VALUES ('acme', %s, 1, %s)",
            (PACKAGE, ROBOT),
        )
    pg.execute("SELECT tenant_acme.replay_tx(1, '2026-10-02T00:00:01.000000Z')")
    pg.execute(
        "INSERT INTO tenant_acme.registration_log VALUES"
        " ('acme', 1, '2026-10-02T00:00:01.000000Z', %s, 'root', '0.0.1')",
        (PACKAGE,),
    )
    with pytest.raises(psycopg.errors.RaiseException, match="disagrees"):
        pg.execute(
            "INSERT INTO tenant_acme.package (tenant_id, package_id, schema_version, receipt_id,"
            " root_locator) VALUES ('acme', %s, 1, %s, 'elsewhere')",
            (PACKAGE, ROBOT),
        )


@pytest.mark.parametrize(
    ("provider", "collation"),
    [("c", "en_US.UTF-8"), ("c", "C.UTF-8"), ("i", "C"), ("c", "")],
)
def test_a_database_without_byte_order_collation_is_refused(provider: str, collation: str) -> None:
    with pytest.raises(MigrationError, match="C collation"):
        check_collation(provider, collation)


@pytest.mark.parametrize("collation", ["C", "POSIX"])
def test_byte_order_collations_are_accepted(collation: str) -> None:
    check_collation("c", collation)


def test_the_test_database_sorts_ids_as_bytes(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    rows = pg.execute(
        "SELECT v FROM (VALUES ('a_b'), ('aB'), ('a-b'), ('ab'), ('A_b')) AS t (v) ORDER BY v"
    ).fetchall()
    values = [str(row[0]) for row in rows]
    assert values == sorted(values, key=lambda v: v.encode("utf-8"))


def test_a_registration_costs_the_same_whatever_the_catalog_size(pg: Conn) -> None:
    """ADR 0005 §1: the clock checks read the one clock row, not every earlier registration."""
    apply_migrations(pg, "acme")
    sources = {
        str(row[0]): str(row[1])
        for row in pg.execute(
            "SELECT p.proname, p.prosrc FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE n.nspname = 'tenant_acme'"
            " AND p.proname IN ('registration_log_follows_clock', 'package_from_log')"
        ).fetchall()
    }
    assert set(sources) == {"registration_log_follows_clock", "package_from_log"}
    for name, body in sources.items():
        assert re.search(r"FROM (registration_log|package)\s+WHERE tx_seq", body) is None, name
    assert (
        "tenant_id = NEW.tenant_id AND package_id = NEW.package_id" in sources["package_from_log"]
    )


def test_a_record_id_keeps_one_body(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    first, second = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    add_package(pg, "tenant_acme", first, 1)
    insert = (
        "INSERT INTO tenant_acme.record (tenant_id, kind, record_id, package_id,"
        " registration_key, line, schema_version, body_digest) VALUES ('acme', 'run', %s, %s, %s,"
        " 1, 1, %s)"
    )
    pg.execute(insert, (ROBOT, first, 1, DIGEST))
    add_package(pg, "tenant_acme", second, 2)
    with pytest.raises(psycopg.errors.RaiseException, match="catalogued with body"):
        pg.execute(insert, (ROBOT, second, 2, "sha256:" + "e" * 64))
    pg.execute(insert, (ROBOT, second, 2, DIGEST))  # the same body from another package
    assert pg.execute("SELECT count(*) FROM tenant_acme.record").fetchone() == (2,)


def test_location_absences_are_append_only(pg: Conn) -> None:
    apply_migrations(pg, "acme")
    add_package(pg, "tenant_acme", PACKAGE, 1)
    pg.execute(
        "INSERT INTO tenant_acme.location_absence VALUES ('acme', %s, %s, %s, %s)",
        (ROBOT, PACKAGE, '{"kind":"local","path":"flight.ulg"}', ["rec:sha256:" + "9" * 64]),
    )
    for statement in (
        "UPDATE tenant_acme.location_absence SET location = '{}'",
        "DELETE FROM tenant_acme.location_absence",
        "TRUNCATE tenant_acme.location_absence",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            pg.execute(statement)


def test_migrations_commit_on_a_connection_outside_autocommit(pg: Conn) -> None:
    """The collation check runs inside the migration transaction, so nothing is left open."""
    pg.autocommit = False
    apply_migrations(pg, "acme")
    assert pg.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    pg.rollback()  # nothing to roll back: the migrations were committed
    pg.autocommit = True
    assert pg.execute("SELECT count(*) FROM tenant_acme.schema_migration").fetchone() == (
        len(migrations()),
    )
