"""Rebuild a tenant's catalog from its registry manifest and packages, and dump it canonically
(Ledger ADR 0012; the guarantee is docs/guarantees.md).

``rebuild`` drops the tenant's schema, migrates a fresh one and replays every manifest entry
through registration at its logged tick, all in one transaction: either every package registers
again and the new catalog commits, or nothing changes. Readers wait on the dropped schema's locks
meanwhile; they never see a partial catalog.

``dump`` writes every catalog table as canonical JSON Lines: per table a header naming its
columns, then one object per row mapping each column to its PostgreSQL text form, a NULL column
left out (canonical JSON has no null), rows ordered by their column texts in byte order. It
leaves out the columns that record *when and in which order* the Ledger learned of each package
(and ``tenant_id``, constant within a schema), so two catalogs of the same packages and Ledger
version dump to the same bytes whatever order they were registered in. Every left-out value
follows from the registry manifest (a row's registration key is its package's ``tx_seq``), so a
dump and a manifest together determine the catalog.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Final, Literal

import psycopg
from psycopg import sql

import neptune_ledger
from neptune.identity import canonical_json
from neptune_ledger.api import codec
from neptune_ledger.api.protocol import CatalogUnavailable
from neptune_ledger.api.types import Registration, TransactionKey
from neptune_ledger.catalog.manifest import Entry, Manifest
from neptune_ledger.catalog.migrate import apply_migrations, tenant_schema
from neptune_ledger.catalog.registry import PostgresCatalog

DUMP_FORMAT: Final = "neptune-ledger/catalog-dump"
DUMP_FORMAT_VERSION: Final = 1
# Columns that hold a transaction key or a transaction time: when, and in which order, a package
# was registered (ADR 0002 §4). A schema test holds every such column of every table to this set.
TRANSACTION_COLUMNS: Final = frozenset(
    {"first_registration_key", "last_seq", "last_time", "registration_key", "tx_seq", "tx_time"}
)
LEFT_OUT: Final = TRANSACTION_COLUMNS | {"tenant_id"}
_FETCH: Final = 2000

Conn = psycopg.Connection[tuple[Any, ...]]


@dataclass(frozen=True)
class RebuildReport:
    """What a rebuild did. ``refused`` changed nothing: ``failed`` is the manifest entry whose
    registration did not succeed and ``registration`` its answer, or ``unlisted`` names the
    registered packages the manifest leaves out (and ``prune`` was not given)."""

    outcome: Literal["rebuilt", "refused"]
    tenant_id: str
    packages: int
    ledger_version: str
    failed: Entry | None = None
    registration: Registration | None = None
    unlisted: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ledger_version": self.ledger_version,
            "outcome": self.outcome,
            "packages": self.packages,
            "tenant_id": self.tenant_id,
        }
        if self.failed is not None:
            out["failed"] = self.failed.to_json()
        if self.registration is not None:
            out["registration"] = codec.to_json(self.registration)
        if self.unlisted:
            out["unlisted"] = list(self.unlisted)
        return out


class _Stop(Exception):
    """Roll the rebuild back and report this."""

    def __init__(self, report: RebuildReport) -> None:
        super().__init__(report.outcome)
        self.report = report


def rebuild(
    conninfo: str,
    manifest: Manifest,
    *,
    package_roots: Sequence[str] | None,
    ledger_version: str = neptune_ledger.__version__,
    prune: bool = False,
) -> RebuildReport:
    """Replace ``manifest.tenant_id``'s catalog with one rebuilt from the manifest's packages.

    Entries are replayed in ``tx_seq`` order at their logged ticks, from their logged roots, which
    must lie inside ``package_roots`` (ADR 0006 §3; ``None`` for no limit). With the Ledger version
    every entry names, the result is the original catalog byte for byte, transaction times included
    (ADR 0002 §4). With another version it is a new catalog lineage over the same transaction keys.
    A registered package the manifest does not list is refused unless ``prune``: a stale manifest
    must not silently drop packages. Store failures raise ``CatalogUnavailable``.
    """
    tenant = manifest.tenant_id
    schema = tenant_schema(tenant)
    try:
        with psycopg.connect(conninfo, autocommit=True) as conn:
            try:
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (schema,))
                    unlisted = [] if prune else _unlisted(conn, schema, manifest)
                    if unlisted:
                        raise _Stop(
                            RebuildReport(
                                "refused", tenant, 0, ledger_version, unlisted=tuple(unlisted)
                            )
                        )
                    conn.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                    )
                    apply_migrations(conn, tenant)
                    conn.execute(
                        sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
                    )
                    catalog = PostgresCatalog(
                        conninfo,
                        tenant,
                        package_roots=package_roots,
                        ledger_version=ledger_version,
                        connection=conn,
                    )
                    for entry in manifest.registrations:
                        answer = catalog.replay(
                            entry.root_locator, TransactionKey(entry.tx_seq, entry.tx_time)
                        )
                        if answer.outcome != "registered":
                            raise _Stop(
                                RebuildReport("refused", tenant, 0, ledger_version, entry, answer)
                            )
            except _Stop as stop:
                return stop.report
    except psycopg.Error as exc:
        raise CatalogUnavailable(f"the rebuild could not complete: {exc}") from exc
    return RebuildReport("rebuilt", tenant, len(manifest.registrations), ledger_version)


def _unlisted(conn: Conn, schema: str, manifest: Manifest) -> list[str]:
    """Package ids the existing catalog holds and the manifest does not, in byte order."""
    exists = conn.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.registration_log",)
    ).fetchone()
    if exists is None or not exists[0]:
        return []
    held = {
        str(row[0])
        for row in conn.execute(
            sql.SQL("SELECT package_id FROM {}.registration_log").format(sql.Identifier(schema))
        ).fetchall()
    }
    listed = {entry.package_id for entry in manifest.registrations}
    return sorted(held - listed)


def dump(conninfo: str, tenant_id: str, out: IO[bytes]) -> None:
    """Write the tenant's catalog to ``out`` canonically, from one snapshot (ADR 0012 §3)."""
    schema = tenant_schema(tenant_id)
    try:
        with psycopg.connect(conninfo) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            # Text forms are fixed whatever the server's or the session's defaults are.
            for setting, value in (
                ("bytea_output", "hex"),
                ("extra_float_digits", "1"),
                ("TimeZone", "UTC"),
            ):
                conn.execute("SELECT set_config(%s, %s, true)", (setting, value))
            tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
                    ' ORDER BY c.relname COLLATE "C"',
                    (schema,),
                ).fetchall()
            ]
            if not tables:
                raise CatalogUnavailable(f"{schema} holds no catalog; run `ledger migrate`")
            out.write(
                _line(
                    {
                        "format": DUMP_FORMAT,
                        "format_version": DUMP_FORMAT_VERSION,
                        "left_out": sorted(LEFT_OUT),
                    }
                )
            )
            for table in tables:
                _dump_table(conn, schema, table, out)
    except psycopg.Error as exc:
        raise CatalogUnavailable(f"the dump could not read the catalog: {exc}") from exc


def _dump_table(conn: Conn, schema: str, table: str, out: IO[bytes]) -> None:
    columns = [
        str(row[0])
        for row in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = %s::regclass"
            ' AND attnum > 0 AND NOT attisdropped ORDER BY attname COLLATE "C"',
            (f"{schema}.{table}",),
        ).fetchall()
        if row[0] not in LEFT_OUT
    ]
    out.write(_line({"columns": columns, "table": table}))
    source = sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))
    if not columns:  # every column left out: one empty object per row
        row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(source)).fetchone()
        out.write(b"{}\n" * (int(row[0]) if row else 0))
        return
    texts = [sql.SQL("({}::text)").format(sql.Identifier(c)) for c in columns]
    query = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
        sql.SQL(", ").join(texts),
        source,
        sql.SQL(", ").join(sql.SQL('{} COLLATE "C"').format(text) for text in texts),
    )
    with conn.cursor(name=f"dump_{table}") as cursor:
        cursor.itersize = _FETCH
        cursor.execute(query)
        for row in cursor:
            cells = zip(columns, row, strict=True)
            out.write(_line({column: text for column, text in cells if text is not None}))


def _line(value: Any) -> bytes:
    return canonical_json.dumps(value) + b"\n"
