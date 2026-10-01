"""The catalog on PostgreSQL: ``register`` and ``verify`` (Ledger ADRs 0002, 0004, 0005, 0006).

``PostgresCatalog`` serves one tenant's schema. ``register`` verifies the whole package first,
outside any transaction and without following a link (``check``), then writes the
registration-log row, the package row and every index row in one READ COMMITTED transaction that
holds the ``tx_clock`` row lock from before the package lookup to commit (ADR 0004 §4). The
remaining calls (``resolve``, ``thread``, ``threads_of``, ``lineage``, ``query``) belong to
MVL-91, MVL-92 and MVL-98 and raise ``NotImplementedError`` until they land.
"""

import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

import psycopg
from psycopg import sql

import neptune_ledger
from neptune.model.knowledge import Knowledge, Known, NotApplicable, NotCovered, Unknown
from neptune_ledger.api.protocol import CatalogUnavailable
from neptune_ledger.api.types import (
    CatalogFinding,
    ClockMerge,
    EvidenceAnchor,
    KindCount,
    LineageGraph,
    Order,
    QuerySpec,
    Registration,
    Resolution,
    Thread,
    ThreadKey,
    ThreadPreference,
    ThreadsOf,
    TransactionKey,
    VerifyReport,
)
from neptune_ledger.catalog.check import Checked, check_package, open_root
from neptune_ledger.catalog.index import PackageRows, package_rows
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.catalog.sources import SourceReport, SourceStore, Stated, check_sources

Conn = psycopg.Connection[tuple[Any, ...]]
T = TypeVar("T")

# SQLSTATEs after which the whole transaction is retried (ADR 0004 §4): a retry either registers
# the package or finds it registered, so it is always safe.
RETRYABLE: Final = (psycopg.errors.SerializationFailure, psycopg.errors.DeadlockDetected)
# What a package's own rows can trip: a constraint, a value out of a column's range, or a
# catalog trigger (the body-digest backstop of ADR 0005 §2).
HOSTILE: Final = (
    psycopg.errors.IntegrityError,
    psycopg.errors.DataError,
    psycopg.errors.RaiseException,
)
UNREADABLE: Final = "no readable package directory at this root"
_MAX_SEQ: Final = 2**63 - 1
Verdict = Literal["damaged", "intact", "unknown_package", "unreachable"]

_RECORD_COLUMNS: Final = (
    "tenant_id, kind, record_id, package_id, registration_key, line, schema_version,"
    " source_content_id, source_locator, transform_id, assertion_kind,"
    " world_clock, world_first, world_last, ambiguous_pointers, body_digest"
)


class _Refused(Exception):
    """Inside the registration transaction: roll back and refuse with these findings."""

    def __init__(self, findings: list[CatalogFinding]) -> None:
        super().__init__("refused")
        self.findings = findings


class PostgresCatalog:
    """The catalog API for one tenant, on the catalog schema of ``tenant_id`` (ADR 0002 §2).

    ``conninfo`` names a PostgreSQL 16 database with C collation whose tenant schema
    ``apply_migrations`` has brought up to date. ``package_roots`` are the tenant's package roots
    (ADR 0006 §3): ``register`` accepts only a root that, fully resolved, lies inside one of them.
    ``None`` means no limit; it is for single-tenant hosts and tests, and ``access/`` (MVL-99)
    configures roots before any multi-tenant deployment. One connection is opened lazily and
    reused; ``close`` (or ``with``) releases it.
    """

    def __init__(
        self,
        conninfo: str,
        tenant_id: str,
        *,
        package_roots: Sequence[str | os.PathLike[str]] | None,
        ledger_version: str = neptune_ledger.__version__,
        attempts: int = 5,
    ) -> None:
        self._conninfo = conninfo
        self._tenant = tenant_id
        self._schema = tenant_schema(tenant_id)
        self._roots = None if package_roots is None else tuple(Path(root) for root in package_roots)
        self._ledger_version = ledger_version
        self._attempts = attempts
        self._conn: Conn | None = None

    def __enter__(self) -> "PostgresCatalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- register ------------------------------------------------------------------------------

    def register(self, package_root: str | os.PathLike[str]) -> Registration:
        """Catalogue the package at ``package_root``; see ``CatalogApi.register``."""
        raw = Path(package_root)
        given = str(raw.absolute())  # as named, unresolved: a refusal reveals nothing more
        # Every link and ".." resolved in order (ADR 0006 §3); realpath, unlike Path.resolve on
        # Python 3.11 and 3.12, does not raise on a link loop.
        root = os.path.realpath(raw)
        if raw.is_symlink() or not self._inside_roots(root):
            return self._unreadable(given)
        root_fd = open_root(root)  # no component followed: a link swapped in since is refused
        if root_fd is None:
            return self._unreadable(given)
        try:
            checked = check_package(root_fd, "register")
        finally:
            os.close(root_fd)
        if checked.findings:
            return self._refusal(root, checked, list(checked.findings))
        rows = package_rows(str(checked.package_id), checked.manifest, checked.lines)
        try:
            outcome, key, locator, version = self._run(
                lambda conn: self._write(conn, rows, root), refuse_as=rows.package_id
            )
        except _Refused as refused:
            return self._refusal(root, checked, refused.findings)
        return Registration(
            outcome=outcome,
            package_id=Known(rows.package_id),
            registration_key=Known(key),
            root_locator=locator,
            ledger_version=version,
            schema_version=Known(rows.schema_version),
            record_counts=_counts(checked),
            findings=(),
        )

    def _inside_roots(self, resolved: str) -> bool:
        """ADR 0006 §3: inside a tenant root, both fully resolved, compared by path components."""
        if self._roots is None:
            return True
        path = Path(resolved)
        return any(path.is_relative_to(os.path.realpath(root)) for root in self._roots)

    def _unreadable(self, given: str) -> Registration:
        """A refusal that reads the same for a missing root and one outside the tenant's roots."""
        return Registration(
            outcome="refused",
            package_id=Unknown(),
            registration_key=NotApplicable(),
            root_locator=given,
            ledger_version=self._ledger_version,
            schema_version=Unknown(),
            record_counts=(),
            findings=(CatalogFinding("package_unreadable", given, UNREADABLE),),
        )

    def _refusal(self, root: str, checked: Checked, findings: list[CatalogFinding]) -> Registration:
        version = checked.schema_version
        return Registration(
            outcome="refused",
            package_id=Known(checked.package_id) if checked.package_id else Unknown(),
            registration_key=NotApplicable(),
            root_locator=root,
            ledger_version=self._ledger_version,
            schema_version=Known(version) if version is not None else Unknown(),
            record_counts=(),
            findings=tuple(findings),
        )

    def _write(
        self, conn: Conn, rows: PackageRows, root: str
    ) -> tuple[Literal["already_registered", "registered"], TransactionKey, str, str]:
        """The registration transaction body (ADR 0002 §4, §6; ADR 0004 §4; ADR 0005 §2)."""
        conn.execute("SELECT 1 FROM tx_clock FOR UPDATE")  # held to commit: serialises the tenant
        stored = conn.execute(
            "SELECT tx_seq, tx_time, root_locator, ledger_version FROM package"
            " WHERE tenant_id = %s AND package_id = %s",
            (self._tenant, rows.package_id),
        ).fetchone()
        if stored is not None:
            seq, at, locator, version = stored
            return "already_registered", TransactionKey(int(seq), str(at)), locator, version
        conflicts, new_sources, new_transforms = self._compare(conn, rows)
        if conflicts:
            raise _Refused(conflicts)
        tick = conn.execute("SELECT tx_seq, tx_time FROM next_tx()").fetchone()
        assert tick is not None
        seq, at = int(tick[0]), str(tick[1])
        t, p = self._tenant, rows.package_id
        conn.execute(
            "INSERT INTO registration_log VALUES (%s, %s, %s, %s, %s, %s)",
            (t, seq, at, p, root, self._ledger_version),
        )
        conn.execute(
            "INSERT INTO package (tenant_id, package_id, schema_version, receipt_id)"
            " VALUES (%s, %s, %s, %s)",
            (t, p, rows.schema_version, rows.receipt_id),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO source VALUES (%s, %s, %s)",
                [(t, content, size) for content, size, _ in rows.sources if content in new_sources],
            )
            cur.executemany(
                "INSERT INTO package_source VALUES (%s, %s, %s, %s)",
                [(t, p, content, storage) for content, _, storage in rows.sources],
            )
            cur.executemany(
                "INSERT INTO source_location VALUES (%s, %s, %s, %s, %s, %s)",
                [(t, rev, p, content, loc, list(sup)) for rev, content, loc, sup in rows.locations],
            )
            cur.executemany(
                "INSERT INTO location_absence VALUES (%s, %s, %s, %s, %s)",
                [(t, absence, p, loc, list(sup)) for absence, loc, sup in rows.absences],
            )
            new = [x for x in rows.transforms if x.transform_id in new_transforms]
            cur.executemany(
                "INSERT INTO transform VALUES (%s, %s, %s, %s, %s, %s)",
                [
                    (t, x.transform_id, x.adapter_id, x.adapter_version, x.config_hash, x.libraries)
                    for x in new
                ],
            )
            cur.executemany(
                "INSERT INTO transform_upstream VALUES (%s, %s, %s)",
                [(t, x.transform_id, up) for x in new for up in x.upstream],
            )
            cur.executemany(
                "INSERT INTO clock VALUES (%s, %s, %s, %s, %s)",
                [(t, clock, p, field, list(scope)) for clock, field, scope in rows.clocks],
            )
            cur.executemany(
                f"INSERT INTO record ({_RECORD_COLUMNS}) VALUES ({', '.join(['%s'] * 16)})",
                [
                    (
                        t,
                        r.kind,
                        r.record_id,
                        p,
                        seq,
                        r.line,
                        r.schema_version,
                        r.source_content_id,
                        r.source_locator,
                        r.transform_id,
                        r.assertion_kind,
                        r.world_clock,
                        r.world_first,
                        r.world_last,
                        list(r.ambiguous_pointers),
                        r.body_digest,
                    )
                    for r in rows.records
                ],
            )
            cur.executemany(
                "INSERT INTO record_logical_id VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [
                    (t, r.kind, r.record_id, p, pointer, namespace, value)
                    for r in rows.records
                    for pointer, namespace, value in r.logical_ids
                ],
            )
        return "registered", TransactionKey(seq, at), root, self._ledger_version

    def _compare(
        self, conn: Conn, rows: PackageRows
    ) -> tuple[list[CatalogFinding], set[str], set[str]]:
        """Conflicting ids, and the source and transform ids not yet catalogued.

        A conflict is an existing id with other fields (ADR 0002 §6, ADR 0005 §2): a source of
        another size, a transform with another adapter, version, config hash, libraries or
        upstream set, or a record id (``source_artifact`` aside) with another body digest. The
        clock lock is held, so nothing changes between this comparison and the inserts.
        """
        found: list[CatalogFinding] = []
        sizes = {content: size for content, size, _ in rows.sources}
        stored_sizes: dict[str, int] = dict(
            conn.execute(
                "SELECT content_id, size FROM source WHERE tenant_id = %s AND content_id = ANY(%s)",
                (self._tenant, list(sizes)),
            ).fetchall()
        )
        for content, size in sorted(stored_sizes.items()):
            if int(size) != sizes[content]:
                detail = f"catalogued with size {size}, arrived with size {sizes[content]}"
                found.append(CatalogFinding("conflicting_id", content, detail))
        stored = self._stored_transforms(conn, [x.transform_id for x in rows.transforms])
        for x in rows.transforms:
            known = stored.get(x.transform_id)
            mine = (x.adapter_id, x.adapter_version, x.config_hash, x.libraries, sorted(x.upstream))
            if known is not None and known != mine:
                detail = "catalogued with another adapter, version, config, libraries or upstream"
                found.append(CatalogFinding("conflicting_id", x.transform_id, detail))
        bodies = [r for r in rows.records if r.kind != "source_artifact"]
        for kind, record_id, digest in conn.execute(
            "SELECT DISTINCT r.kind, r.record_id, r.body_digest FROM record r"
            " JOIN unnest(%s::text[], %s::text[], %s::text[]) AS n(kind, record_id, digest)"
            "   ON r.kind = n.kind AND r.record_id = n.record_id"
            " WHERE r.tenant_id = %s AND r.body_digest <> n.digest",
            (
                [r.kind for r in bodies],
                [r.record_id for r in bodies],
                [r.body_digest for r in bodies],
                self._tenant,
            ),
        ).fetchall():
            detail = f"{kind} is catalogued with body {digest}; this package brings another"
            found.append(CatalogFinding("conflicting_id", record_id, detail))
        found.sort(key=lambda f: (f.subject, f.detail))
        return (
            found,
            set(sizes) - set(stored_sizes),
            {x.transform_id for x in rows.transforms} - set(stored),
        )

    def _stored_transforms(
        self, conn: Conn, ids: list[str]
    ) -> dict[str, tuple[str, str, str, str, list[str]]]:
        out: dict[str, tuple[str, str, str, str, list[str]]] = {}
        for tid, adapter, version, config, libraries, upstream in conn.execute(
            "SELECT t.transform_id, t.adapter_id, t.adapter_version, t.config_hash, t.libraries,"
            "  coalesce(array_agg(u.upstream_id::text ORDER BY u.upstream_id)"
            "    FILTER (WHERE u.upstream_id IS NOT NULL), '{}'::text[])"
            " FROM transform t LEFT JOIN transform_upstream u USING (tenant_id, transform_id)"
            " WHERE t.tenant_id = %s AND t.transform_id = ANY(%s)"
            " GROUP BY 1, 2, 3, 4, 5",
            (self._tenant, ids),
        ).fetchall():
            out[tid] = (adapter, version, config, libraries, sorted(upstream))
        return out

    # --- verify --------------------------------------------------------------------------------

    def verify(self, package_id: str, *, as_of: int | None = None) -> VerifyReport:
        """Re-hash a registered package at its stored root (ADR 0004 §1, ADR 0006 §1, §2)."""
        point, row, problem = self._registered(package_id, as_of)
        if problem is not None or row is None:
            assert problem is not None
            return _report(package_id, "unknown_package", point, None, (problem,))
        root = row[1]
        # The stored locator is fully resolved and every component is opened without following
        # a link: a link put anywhere on it since makes the root unreachable (ADR 0006 §2, §3).
        root_fd = open_root(root)
        if root_fd is None:
            finding = CatalogFinding(
                "package_unreadable", root, "the stored root locator does not exist"
            )
            return _report(package_id, "unreachable", point, row, (finding,))
        try:
            checked = check_package(root_fd, "verify", package_id)
        finally:
            os.close(root_fd)
        verdict: Verdict = "damaged" if checked.findings else "intact"
        return _report(package_id, verdict, point, row, checked.findings, checked.files_checked)

    def _registered(
        self, package_id: str, as_of: int | None
    ) -> tuple[Knowledge[TransactionKey], tuple[TransactionKey, str] | None, CatalogFinding | None]:
        """The catalog point, and the package's key and root at it, or the finding why not."""
        if as_of is not None and (not isinstance(as_of, int) or as_of < 1):
            point, _, _ = self._run(lambda conn: self._lookup(conn, package_id, None))
            detail = "as_of is a tx_seq, at least 1"
            return point, None, CatalogFinding("invalid_request", str(as_of), detail)
        point, row, beyond = self._run(lambda conn: self._lookup(conn, package_id, as_of))
        if beyond:
            detail = "as_of is beyond the latest committed catalog point"
            return point, None, CatalogFinding("as_of_out_of_range", str(as_of), detail)
        if row is None:
            detail = "no package with this id is registered"
            return point, None, CatalogFinding("unknown_package", package_id, detail)
        return point, row, None

    def verify_sources(
        self, package_id: str, stores: Sequence[SourceStore], *, as_of: int | None = None
    ) -> SourceReport:
        """Re-hash the package's referenced sources in ``stores`` (MVL-90; ADR 0007).

        Not a catalog-API call: catalog-api 1.1.0 has no finding codes for source locations.
        Every current location (ADR 0006 §5) of every referenced source is reported, and one
        that is absent or changed never hides the others.
        """
        point, row, problem = self._registered(package_id, as_of)
        if problem is not None or row is None:
            assert problem is not None
            return SourceReport(package_id, point, (), (problem,))
        assert isinstance(point, Known)  # a package is registered, so the catalog has a point
        limit = point.value.tx_seq
        stated = self._run(lambda conn: self._stated(conn, package_id, limit))
        return SourceReport(package_id, point, check_sources(stated, stores), ())

    def _stated(self, conn: Conn, package_id: str, limit: int) -> list[Stated]:
        """The package's referenced sources with its current locations, and the current
        locations other packages registered by ``limit`` state for the same bytes."""
        sources = conn.execute(
            "SELECT ps.content_id, s.size FROM package_source ps"
            " JOIN source s USING (tenant_id, content_id)"
            " WHERE ps.tenant_id = %s AND ps.package_id = %s AND ps.storage = 'referenced'"
            " ORDER BY ps.content_id",
            (self._tenant, package_id),
        ).fetchall()
        contents = [str(content) for content, _ in sources]
        rows = conn.execute(
            "SELECT p.tx_seq, l.package_id, l.revision_id, l.content_id, l.location"
            " FROM source_location l JOIN package p USING (tenant_id, package_id)"
            " WHERE l.tenant_id = %s AND l.content_id = ANY(%s) AND p.tx_seq <= %s"
            " ORDER BY p.tx_seq, l.revision_id",
            (self._tenant, contents, limit),
        ).fetchall()
        packages = sorted({str(row[1]) for row in rows})
        superseded = {
            (str(pid), str(rid))
            for pid, rid in conn.execute(
                "SELECT package_id, unnest(supersedes)::text FROM source_location"
                " WHERE tenant_id = %s AND package_id = ANY(%s)"
                " UNION ALL SELECT package_id, unnest(supersedes)::text FROM location_absence"
                " WHERE tenant_id = %s AND package_id = ANY(%s)",
                (self._tenant, packages, self._tenant, packages),
            ).fetchall()
        }
        current = [
            (str(pid), str(content), str(location))
            for _, pid, rid, content, location in rows
            if (str(pid), str(rid)) not in superseded
        ]
        out = []
        for content, size in sources:
            mine = tuple(loc for pid, c, loc in current if c == content and pid == package_id)
            others = tuple(
                dict.fromkeys(loc for pid, c, loc in current if c == content and pid != package_id)
            )
            out.append(Stated(str(content), int(size), mine, others))
        return out

    def _lookup(
        self, conn: Conn, package_id: str, as_of: int | None
    ) -> tuple[Knowledge[TransactionKey], tuple[TransactionKey, str] | None, bool]:
        """The catalog point, the package's key and root at that point, and whether ``as_of`` is
        beyond the latest point. One statement, so one snapshot."""
        row = conn.execute(
            "SELECT c.last_seq, c.last_time, l.tx_time, p.tx_seq, p.tx_time, p.root_locator"
            " FROM tx_clock c"
            " LEFT JOIN registration_log l ON l.tenant_id = c.tenant_id AND l.tx_seq = %s"
            " LEFT JOIN package p ON p.tenant_id = c.tenant_id AND p.package_id = %s"
            " WHERE c.tenant_id = %s",
            (as_of if as_of is not None and as_of <= _MAX_SEQ else 0, package_id, self._tenant),
        ).fetchone()
        if row is None:
            raise CatalogUnavailable(f"{self._schema} has no transaction clock")
        last_seq, last_time, as_of_time, seq, at, root = row
        latest: Knowledge[TransactionKey] = (
            Known(TransactionKey(int(last_seq), str(last_time))) if last_seq else NotCovered()
        )
        if as_of is not None and as_of > int(last_seq):
            return latest, None, True
        point = Known(TransactionKey(as_of, str(as_of_time))) if as_of is not None else latest
        limit = as_of if as_of is not None else int(last_seq)
        if seq is None or int(seq) > limit:
            return point, None, False
        return point, (TransactionKey(int(seq), str(at)), str(root)), False

    # --- the rest of the API (MVL-91, MVL-92, MVL-98) -------------------------------------------

    def _missing(self, name: str) -> NotImplementedError:
        return NotImplementedError(f"catalog API {name}() is not implemented yet")

    def resolve(self, evidence_ref: EvidenceAnchor, *, as_of: int | None = None) -> Resolution:
        raise self._missing("resolve")

    def thread(
        self,
        key: ThreadKey,
        order: Order,
        preference: ThreadPreference,
        *,
        merge: ClockMerge | None = None,
        as_of: int | None = None,
    ) -> Thread:
        raise self._missing("thread")

    def threads_of(self, record_id: str, *, as_of: int | None = None) -> ThreadsOf:
        raise self._missing("threads_of")

    def lineage(self, record_id: str, *, as_of: int | None = None) -> LineageGraph:
        raise self._missing("lineage")

    def query(self, spec: QuerySpec) -> Any:
        raise self._missing("query")

    # --- transactions --------------------------------------------------------------------------

    def _connection(self) -> Conn:
        if self._conn is None or self._conn.closed:
            conn: Conn = psycopg.connect(self._conninfo, autocommit=True)
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self._schema)))
            self._conn = conn
        return self._conn

    def _run(self, body: Callable[[Conn], T], *, refuse_as: str | None = None) -> T:
        """Run ``body`` in one transaction; retry it on 40001/40P01 (ADR 0004 §4).

        A ``_Refused`` rolls the transaction back and propagates. With ``refuse_as`` (a package
        id), a row the catalog's constraints or triggers refuse is that package's fault, so it
        becomes ``_Refused`` with a ``record_invalid`` finding, not an outage. Any other store
        failure, or retries running out, is ``CatalogUnavailable``: nothing was written.
        """
        for attempt in range(self._attempts):
            try:
                conn = self._connection()
                with conn.transaction():
                    return body(conn)
            except RETRYABLE:
                time.sleep(0.01 * (attempt + 1))
                continue
            except HOSTILE as exc:
                if refuse_as is None:
                    raise CatalogUnavailable(f"the catalog store refused the call: {exc}") from exc
                detail = f"the catalog refuses a row of it: {str(exc).splitlines()[0][:300]}"
                raise _Refused([CatalogFinding("record_invalid", refuse_as, detail)]) from exc
            except psycopg.OperationalError as exc:
                self.close()
                raise CatalogUnavailable(f"the catalog store is unreachable: {exc}") from exc
            except psycopg.Error as exc:
                raise CatalogUnavailable(f"the catalog store refused the call: {exc}") from exc
        raise CatalogUnavailable(f"the transaction kept failing after {self._attempts} attempts")


def _report(
    package_id: str,
    verdict: Verdict,
    point: Knowledge[TransactionKey],
    row: tuple[TransactionKey, str] | None,
    findings: tuple[CatalogFinding, ...],
    files_checked: int = 0,
) -> VerifyReport:
    return VerifyReport(
        package_id=package_id,
        verdict=verdict,
        registration_key=Known(row[0]) if row else NotCovered(),
        root_locator=Known(row[1]) if row else NotCovered(),
        files_checked=files_checked,
        as_of=point,
        findings=findings,
    )


def _counts(checked: Checked) -> tuple[KindCount, ...]:
    tables = checked.manifest["tables"]
    return tuple(KindCount(kind, tables[kind]) for kind in sorted(tables) if tables[kind])
