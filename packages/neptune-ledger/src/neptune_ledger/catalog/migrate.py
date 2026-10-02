"""Apply the catalog migrations to one tenant's schema (ADR 0002).

Each tenant is a PostgreSQL schema, ``tenant_<tenant_id>``. ``apply_migrations`` creates it if
needed, runs every migration under ``migrations/`` that it has not run yet, in version order, with
``search_path`` set to that schema alone, and records each one in ``schema_migration``. Everything
happens in one transaction under an advisory lock, so a failure leaves the schema as it was and two
runners for the same tenant never interleave. Running it again is a no-op.

The database must use the libc ``C`` collation (Ledger ADR 0005 §4): the catalog API orders ids and
kinds as UTF-8 bytes, and only ``C`` makes the default text order, and so every B-tree, that order.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from typing import Final

import psycopg
from psycopg import sql

# Tenant ids are short lower-case tokens, so "tenant_" + id is a valid, unquoted-safe identifier
# within PostgreSQL's 63-byte limit. The tenant table re-checks the same pattern.
_TENANT_ID: Final = re.compile(r"[a-z][a-z0-9_]{0,47}")
_MIGRATION: Final = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
SCHEMA_PREFIX: Final = "tenant_"
BYTE_ORDER_COLLATIONS: Final = frozenset({"C", "POSIX"})


@dataclass(frozen=True)
class Migration:
    """One migration file: its version, name, SQL text and the sha256 of its bytes."""

    version: int
    name: str
    sql: str
    sha256: str


class MigrationError(RuntimeError):
    """The tenant schema disagrees with the migrations this package ships."""


def tenant_schema(tenant_id: str) -> str:
    """The schema that isolates ``tenant_id``'s catalog."""
    if not isinstance(tenant_id, str) or not _TENANT_ID.fullmatch(tenant_id):
        raise ValueError(f"tenant id must match {_TENANT_ID.pattern}, got {tenant_id!r}")
    return SCHEMA_PREFIX + tenant_id


def check_collation(provider: str, collation: str) -> None:
    """Refuse a database whose default text order is not byte order (ADR 0005 §4).

    ``provider`` and ``collation`` are ``pg_database.datlocprovider`` and ``datcollate``: libc
    (``c``) with ``C`` or ``POSIX``. ICU and every locale-aware libc collation sort by language
    rules that differ from UTF-8 byte order and change between library versions.
    """
    if provider != "c" or collation not in BYTE_ORDER_COLLATIONS:
        raise MigrationError(
            f"the catalog database must use the libc C collation (byte order); it uses"
            f" provider {provider!r}, collation {collation!r}. Create it with"
            " TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"
        )


def migrations() -> tuple[Migration, ...]:
    """Every shipped migration, in version order. Versions run 1, 2, 3, ... with no gaps."""
    found: list[Migration] = []
    for entry in files("neptune_ledger.catalog").joinpath("migrations").iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _MIGRATION.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"migration file {entry.name!r} is not named NNNN_name.sql")
        data = entry.read_bytes()
        found.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=data.decode("utf-8"),
                sha256="sha256:" + hashlib.sha256(data).hexdigest(),
            )
        )
    found.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in found]
    if versions != list(range(1, len(found) + 1)):
        raise MigrationError(f"migration versions must be 1..n without gaps, got {versions}")
    return tuple(found)


def apply_migrations(
    conn: psycopg.Connection[tuple[object, ...]],
    tenant_id: str,
    *,
    shipped: Sequence[Migration] | None = None,
) -> list[int]:
    """Bring ``tenant_id``'s schema up to date; return the versions applied by this call.

    ``conn`` must not be inside a transaction. A migration already recorded with different bytes
    raises ``MigrationError``: shipped migrations are never edited, a change is a new migration.
    ``shipped`` replaces this package's migrations, in version order; it is for testing a
    generated migration before it is committed (ADR 0009 §3).
    """
    schema = tenant_schema(tenant_id)
    shipped = migrations() if shipped is None else tuple(shipped)
    applied: list[int] = []
    with conn.transaction():
        locale = conn.execute(
            "SELECT datlocprovider::text, datcollate FROM pg_database"
            " WHERE datname = current_database()"
        ).fetchone()
        if locale is None:
            raise MigrationError("cannot read the current database's collation")
        check_collation(str(locale[0]), str(locale[1]))
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (schema,))
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        recorded = _recorded(conn, schema)
        for migration in shipped:
            if migration.version in recorded:
                if recorded[migration.version] != migration.sha256:
                    raise MigrationError(
                        f"migration {migration.version} ({migration.name}) in {schema} was"
                        " applied from different bytes; migrations are never edited"
                    )
                continue
            conn.execute(migration.sql.encode("utf-8"))
            if migration.version == 1:
                _seed_tenant(conn, tenant_id)
            conn.execute(
                "INSERT INTO schema_migration (tenant_id, version, name, sha256)"
                " VALUES (%s, %s, %s, %s)",
                (tenant_id, migration.version, migration.name, migration.sha256),
            )
            applied.append(migration.version)
        unknown = sorted(set(recorded) - {migration.version for migration in shipped})
        if unknown:
            raise MigrationError(f"{schema} has migrations this Ledger does not ship: {unknown}")
        row = conn.execute("SELECT tenant_id FROM tenant").fetchone()
        if row is None or row[0] != tenant_id:
            raise MigrationError(
                f"{schema} belongs to tenant {row and row[0]!r}, not {tenant_id!r}"
            )
    return applied


def _recorded(conn: psycopg.Connection[tuple[object, ...]], schema: str) -> dict[int, str]:
    exists = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.schema_migration",)
    ).fetchone()
    if exists is None or not exists[0]:
        return {}
    rows = conn.execute("SELECT version, sha256 FROM schema_migration").fetchall()
    return {int(str(version)): str(sha256) for version, sha256 in rows}


def _seed_tenant(conn: psycopg.Connection[tuple[object, ...]], tenant_id: str) -> None:
    conn.execute("INSERT INTO tenant (tenant_id) VALUES (%s)", (tenant_id,))
    conn.execute("INSERT INTO tx_clock (tenant_id, last_seq) VALUES (%s, 0)", (tenant_id,))
