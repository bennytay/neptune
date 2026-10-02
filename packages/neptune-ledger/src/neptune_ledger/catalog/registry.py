"""The catalog on PostgreSQL: ``register``, ``verify``, ``resolve`` (Ledger ADRs 0002, 0004-0009).

``PostgresCatalog`` serves one tenant's schema. ``register`` verifies the whole package first,
outside any transaction and without following a link (``check``), then writes the
registration-log row, the package row and every index row in one READ COMMITTED transaction that
holds the ``tx_clock`` row lock from before the package lookup to commit (ADR 0004 §4).
``resolve`` reads the source and record indexes registration wrote (ADR 0006 §5). ``thread``,
``threads_of`` and ``lineage`` read the derived thread index registration writes in the same
transaction (ADR 0003, ADR 0010). ``query`` belongs to MVL-98 and raises ``NotImplementedError``
until it lands. With a ``manifest`` path, every registration rewrites the registry manifest, and
``replay`` re-registers a package at its logged tick on a rebuild (ADR 0012).
"""

import hashlib
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, TypeVar

import psycopg
from psycopg import sql

import neptune_ledger
from neptune.identity import canonical_json
from neptune.model.knowledge import Knowledge, Known, NotApplicable, NotCovered, Unknown
from neptune.store.package import MANIFEST, blob_path
from neptune_ledger.api import codec
from neptune_ledger.api.protocol import CatalogUnavailable
from neptune_ledger.api.types import (
    AsRegisteredBy,
    CatalogFinding,
    ClockMerge,
    EvidenceAnchor,
    History,
    KindCount,
    LatestTransform,
    LineageGraph,
    Order,
    Pinned,
    QuerySpec,
    RecordRef,
    Region,
    Registration,
    Resolution,
    SourceLocation,
    Thread,
    ThreadKey,
    ThreadPreference,
    ThreadsOf,
    TransactionKey,
    VerifyReport,
)
from neptune_ledger.catalog.check import Checked, check_package, open_root
from neptune_ledger.catalog.index import (
    PackageRows,
    RecordRow,
    UnindexedVersion,
    package_rows,
    projection_columns,
)
from neptune_ledger.catalog.manifest import ManifestNotWritten, write_manifest
from neptune_ledger.catalog.migrate import tenant_schema
from neptune_ledger.catalog.projection import SchemaVersion, shipped_registry
from neptune_ledger.catalog.sources import SourceReport, SourceStore, Stated, check_sources
from neptune_ledger.lineage.graph import read_lineage, unknown_record
from neptune_ledger.threads.alignment import clock_mapping
from neptune_ledger.threads.membership import MembershipError, ThreadRows, thread_rows
from neptune_ledger.threads.merge import ClockMapping
from neptune_ledger.threads.read import (
    empty_thread,
    read_thread,
    read_threads_of,
    unknown_threads_of,
)

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
_CONTENT_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
# Region.addressing for the package schema's core locator steps; any other step is an adapter's.
_CORE_STEPS: Final = frozenset(
    {
        "byte_range",
        "frame",
        "image_region",
        "json_pointer",
        "object",
        "page",
        "page_region",
        "record_range",
        "row",
        "row_cell",
        "span",
        "video_frame",
    }
)
Verdict = Literal["damaged", "intact", "unknown_package", "unreachable"]

# Every record column registration writes (ADR 0002 §5, ADR 0005 §2, ADR 0009), in this order.
_RECORD_COLUMNS: Final = (
    "tenant_id",
    "kind",
    "record_id",
    "package_id",
    "registration_key",
    "line",
    "schema_version",
    "source_content_id",
    "source_locator",
    "transform_id",
    "assertion_kind",
    "world_clock",
    "world_first",
    "world_last",
    "ambiguous_pointers",
    "body_digest",
    "body",
    "unknown_pointers",
    *projection_columns(),
)
_INSERT_RECORD: Final = "INSERT INTO record ({}) VALUES ({})".format(
    ", ".join(_RECORD_COLUMNS),
    ", ".join("%s::jsonb" if column == "body" else "%s" for column in _RECORD_COLUMNS),
)
# Rows per executemany call (ADR 0009 §4): fixed, so a package is written the same way every time.
BATCH_ROWS: Final = 1000


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

    ``manifest`` names the tenant's registry manifest, rewritten after every registration that
    adds a package (ADR 0012 §1). ``connection`` is for a rebuild: an open connection inside the
    caller's transaction, which the catalog borrows, writes through with savepoints, and never
    opens again or closes.
    """

    def __init__(
        self,
        conninfo: str,
        tenant_id: str,
        *,
        package_roots: Sequence[str | os.PathLike[str]] | None,
        ledger_version: str = neptune_ledger.__version__,
        attempts: int = 5,
        manifest: str | os.PathLike[str] | None = None,
        connection: Conn | None = None,
    ) -> None:
        self._conninfo = conninfo
        self._tenant = tenant_id
        self._schema = tenant_schema(tenant_id)
        self._roots = None if package_roots is None else tuple(Path(root) for root in package_roots)
        self._ledger_version = ledger_version
        self._attempts = attempts
        self._manifest = manifest
        self._borrowed = connection
        self._conn: Conn | None = None

    def __enter__(self) -> "PostgresCatalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None  # a borrowed connection is never closed here: its owner ends it

    # --- register ------------------------------------------------------------------------------

    def register(self, package_root: str | os.PathLike[str]) -> Registration:
        """Catalogue the package at ``package_root``; see ``CatalogApi.register``.

        With a manifest path, a registration that is not refused then rewrites the manifest, so
        registering a package again repairs a manifest an earlier failure left stale. A failure
        writing it raises ``ManifestNotWritten``, which carries the committed registration.
        """
        registration = self._register(package_root, None)
        if self._manifest is not None and registration.outcome != "refused":
            try:
                write_manifest(self._connection(), self._tenant, self._manifest)
            except (OSError, psycopg.Error) as exc:
                raise ManifestNotWritten(registration, exc) from exc
        return registration

    def replay(self, package_root: str, tick: TransactionKey) -> Registration:
        """Register ``package_root`` at ``tick``, a registration-log entry, on a rebuild.

        The clock is advanced with ``replay_tx`` instead of ``next_tx`` (ADR 0002 §4), so the
        rebuilt log holds the logged transaction key. A logged root that now resolves elsewhere
        (a link put on one of its directories) is refused, since the rebuilt log would record
        another root; the manifest names the new root if the package really moved.
        """
        if os.path.realpath(package_root) != package_root:
            return self._unreadable(package_root)
        return self._register(package_root, tick)

    def _register(
        self, package_root: str | os.PathLike[str], tick: TransactionKey | None
    ) -> Registration:
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
        try:
            rows = package_rows(str(checked.package_id), checked.manifest, checked.lines)
        except UnindexedVersion as exc:
            finding = CatalogFinding("record_invalid", str(checked.package_id), str(exc))
            return self._refusal(root, checked, [finding])
        except (RecursionError, MemoryError) as exc:  # hostile depth or size the readers let by
            detail = f"the record index cannot be built: {type(exc).__name__}"
            finding = CatalogFinding("record_invalid", str(checked.package_id), detail)
            return self._refusal(root, checked, [finding])
        try:
            threads = thread_rows(checked.lines)
        except MembershipError as exc:  # a thread key the catalog API cannot express
            finding = CatalogFinding("record_invalid", exc.record_id, exc.detail)
            return self._refusal(root, checked, [finding])
        try:
            outcome, key, locator, version = self._run(
                lambda conn: self._write(conn, rows, threads, root, tick), refuse_as=rows.package_id
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
        self,
        conn: Conn,
        rows: PackageRows,
        threads: ThreadRows,
        root: str,
        replayed: TransactionKey | None,
    ) -> tuple[Literal["already_registered", "registered"], TransactionKey, str, str]:
        """The registration transaction body (ADR 0002 §4, §6; ADR 0004 §4; ADR 0005 §2).

        ``replayed`` is the logged tick a rebuild replays; otherwise the clock allocates one.
        """
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
        unseen, remapped = self._schema_versions(conn, rows)
        if conflicts or remapped:
            raise _Refused([*remapped, *conflicts])
        if replayed is None:
            tick = conn.execute("SELECT tx_seq, tx_time FROM next_tx()").fetchone()
            assert tick is not None
            seq, at = int(tick[0]), str(tick[1])
        else:
            seq, at = replayed.tx_seq, replayed.tx_time
            conn.execute("SELECT replay_tx(%s, %s)", (seq, at))
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
        self._write_schema_versions(conn, unseen, seq)
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
                "INSERT INTO transform_upstream (tenant_id, transform_id, upstream_id, position)"
                " VALUES (%s, %s, %s, %s)",
                [(t, x.transform_id, up, i) for x in new for i, up in enumerate(x.upstream)],
            )
            cur.executemany(
                "INSERT INTO clock VALUES (%s, %s, %s, %s, %s)",
                [(t, clock, p, field, list(scope)) for clock, field, scope in rows.clocks],
            )
            for batch in _batches(rows.records):
                cur.executemany(_INSERT_RECORD, [_record_values(t, p, seq, r) for r in batch])
            for batch in _batches(rows.records):
                cur.executemany(
                    "INSERT INTO record_logical_id VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    [
                        (t, r.kind, r.record_id, p, pointer, namespace, value)
                        for r in batch
                        for pointer, namespace, value in r.logical_ids
                    ],
                )
            self._write_threads(conn, cur, threads, p, seq)
        return "registered", TransactionKey(seq, at), root, self._ledger_version

    def _write_threads(
        self, conn: Conn, cur: psycopg.Cursor[Any], threads: ThreadRows, package: str, seq: int
    ) -> None:
        """The package's rows of the derived thread index (ADR 0010 §1), after its records.

        A thread row is written once per id: the id is the hash of the key it holds, so an
        existing row with that id holds the same key.
        """
        t = self._tenant
        stored = {
            str(row[0])
            for row in conn.execute(
                "SELECT thread_id FROM thread WHERE tenant_id = %s AND thread_id = ANY(%s)",
                (t, [x.thread_id for x in threads.threads]),
            ).fetchall()
        }
        new = [x for x in threads.threads if x.thread_id not in stored]
        for keys in _batches(new):
            cur.executemany(
                "INSERT INTO thread VALUES (%s, %s, %s, %s)",
                [(t, x.thread_id, x.kind, x.key) for x in keys],
            )
        for members in _batches(threads.members):
            cur.executemany(
                "INSERT INTO thread_member VALUES"
                " (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        t,
                        m.thread_id,
                        package,
                        m.record_id,
                        m.kind,
                        seq,
                        list(m.roles),
                        m.transform_id,
                        m.source_content_id,
                        m.world_clock,
                        m.world_first,
                        m.world_last,
                        m.world,
                    )
                    for m in members
                ],
            )
        for named in _batches(threads.unresolved):
            cur.executemany(
                "INSERT INTO thread_unresolved VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(t, u.thread_id, package, u.record_id, u.kind, u.pointer, seq) for u in named],
            )
        for links in _batches(threads.links):
            cur.executemany(
                "INSERT INTO thread_identity_link VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        t,
                        package,
                        x.record_id,
                        "identity_link",
                        seq,
                        x.left,
                        x.right,
                        x.state,
                        x.assertion_kind,
                    )
                    for x in links
                ],
            )
        for mappings in _batches(threads.mappings):
            cur.executemany(
                "INSERT INTO thread_clock_mapping VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (t, package, x.record_id, "clock_mapping", seq, x.source, x.target, x.mapping)
                    for x in mappings
                ],
            )

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

    def _schema_versions(
        self, conn: Conn, rows: PackageRows
    ) -> tuple[list[SchemaVersion], list[CatalogFinding]]:
        """The package's schema versions this catalog has not seen, and any it indexed with
        another projection mapping (ADR 0011 §2): a catalog built by a Ledger whose mapping of a
        version differs is rebuilt, never extended with rows indexed two ways."""
        registry = shipped_registry()
        stored: dict[int, str] = {
            int(version): str(digest)
            for version, digest in conn.execute(
                "SELECT schema_version, mapping_digest FROM schema_version"
                " WHERE tenant_id = %s AND schema_version = ANY(%s)",
                (self._tenant, list(rows.schema_versions)),
            ).fetchall()
        }
        unseen: list[SchemaVersion] = []
        remapped: list[CatalogFinding] = []
        for version in rows.schema_versions:
            entry = registry.entry(version)
            assert entry is not None  # package_rows refused every version the registry lacks
            digest = stored.get(version)
            if digest is None:
                unseen.append(entry)
            elif digest != entry.mapping_digest:
                detail = (
                    f"this catalog indexed schema version {version} with projection mapping"
                    f" {digest}, this Ledger with {entry.mapping_digest}; rebuild the catalog"
                    " from its packages and registration log (ADR 0011)"
                )
                remapped.append(CatalogFinding("unsupported_schema_version", MANIFEST, detail))
        return unseen, remapped

    def _write_schema_versions(self, conn: Conn, unseen: list[SchemaVersion], seq: int) -> None:
        """Record each version first seen in this registration, with its mapping (ADR 0011 §1)."""
        t = self._tenant
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO schema_version VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        t,
                        e.version,
                        e.spec.schema_id,
                        e.contract_version,
                        e.schema_sha256,
                        list(e.spec.kinds),
                        e.mapping,
                        e.mapping_digest,
                        seq,
                    )
                    for e in unseen
                ],
            )
            cur.executemany(
                "INSERT INTO schema_version_projection VALUES (%s, %s, %s, %s, %s)",
                [
                    (t, e.version, p.kind, p.field, column)
                    for e in unseen
                    for p in e.spec.projections
                    for column in p.columns
                ],
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
        if _bad_as_of(as_of):
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
        superseded = self._superseded(conn, packages)
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

    def _superseded(self, conn: Conn, packages: list[str]) -> set[tuple[str, str]]:
        """``(package id, record id)`` of every revision a revision or an absence in the same
        package supersedes: not a current location (ADR 0006 §5)."""
        return {
            (str(pid), str(rid))
            for pid, rid in conn.execute(
                "SELECT package_id, unnest(supersedes)::text FROM source_location"
                " WHERE tenant_id = %s AND package_id = ANY(%s)"
                " UNION ALL SELECT package_id, unnest(supersedes)::text FROM location_absence"
                " WHERE tenant_id = %s AND package_id = ANY(%s)",
                (self._tenant, packages, self._tenant, packages),
            ).fetchall()
        }

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

    # --- resolve -------------------------------------------------------------------------------

    def resolve(self, evidence_ref: EvidenceAnchor, *, as_of: int | None = None) -> Resolution:
        """The source's size, each package's route to it and the records citing this anchor
        exactly (ADR 0004 §1, ADR 0006 §5), at one catalog point."""
        steps = evidence_ref.locator if isinstance(evidence_ref.locator, tuple) else ()
        region = _region(steps[-1] if steps else {})
        locator = _anchor_locator(evidence_ref)
        if _bad_as_of(as_of):
            point, _, _ = self._run(lambda conn: self._lookup(conn, "", None))
            finding = CatalogFinding("invalid_request", str(as_of), "as_of is a tx_seq, at least 1")
            return _unresolved(evidence_ref, region, point, finding)
        if locator is None:
            point, _, _ = self._run(lambda conn: self._lookup(conn, "", None))
            detail = "an evidence anchor is a content id and a non-empty locator of JSON objects"
            finding = CatalogFinding("invalid_request", str(evidence_ref.source)[:200], detail)
            return _unresolved(evidence_ref, region, point, finding)
        return self._run(lambda conn: self._resolve(conn, evidence_ref, locator, region, as_of))

    def _resolve(
        self,
        conn: Conn,
        anchor: EvidenceAnchor,
        locator: str,
        region: Region,
        as_of: int | None,
    ) -> Resolution:
        point, _, beyond = self._lookup(conn, "", as_of)
        if beyond:
            detail = "as_of is beyond the latest committed catalog point"
            finding = CatalogFinding("as_of_out_of_range", str(as_of), detail)
            return _unresolved(anchor, region, point, finding)
        limit = point.value.tx_seq if isinstance(point, Known) else 0
        holders = conn.execute(
            "SELECT ps.package_id, ps.storage, s.size FROM package_source ps"
            " JOIN source s USING (tenant_id, content_id)"
            " JOIN package p USING (tenant_id, package_id)"
            " WHERE ps.tenant_id = %s AND ps.content_id = %s AND p.tx_seq <= %s"
            " ORDER BY p.tx_seq",
            (self._tenant, anchor.source, limit),
        ).fetchall()
        if not holders:
            detail = "no registered package holds this source"
            finding = CatalogFinding("unresolvable_evidence", anchor.source, detail)
            return _unresolved(anchor, region, point, finding)
        referenced = [str(h[0]) for h in holders if h[1] == "referenced"]
        stated = self._locations(conn, anchor.source, referenced) if referenced else {}
        fetch = tuple(
            SourceLocation(
                package_id=str(package_id),
                storage=storage,
                blob_path=Known(blob_path(anchor.source))  # type: ignore[arg-type]
                if storage == "materialised"
                else NotApplicable(),
                locations=stated.get(str(package_id), ()),
            )
            for package_id, storage, _ in holders
        )
        cited_by = tuple(
            RecordRef(str(package_id), str(kind), str(record_id), int(line))
            for kind, record_id, package_id, line in conn.execute(
                "SELECT kind, record_id, package_id, line FROM record"
                " WHERE tenant_id = %s AND source_content_id = %s"
                "   AND md5(source_locator) = md5(%s) AND source_locator = %s"
                "   AND kind <> 'ingest_finding' AND registration_key <= %s"
                " ORDER BY kind, record_id, package_id",
                (self._tenant, anchor.source, locator, locator, limit),
            ).fetchall()
        )
        return Resolution(
            evidence_ref=anchor,
            status="resolved",
            size=Known(int(holders[0][2])),
            region=region,
            fetch=fetch,
            cited_by=cited_by,
            as_of=point,
            findings=(),
        )

    def _locations(
        self, conn: Conn, content_id: str, packages: list[str]
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Per package, its revisions of ``content_id`` that no revision or absence in the same
        package supersedes, in table order (ADR 0006 §5)."""
        superseded = self._superseded(conn, packages)
        out: dict[str, list[dict[str, Any]]] = {}
        for package_id, revision_id, location in conn.execute(
            "SELECT l.package_id, l.revision_id, l.location FROM source_location l"
            " JOIN record r ON r.tenant_id = l.tenant_id AND r.kind = 'source_revision'"
            "  AND r.record_id = l.revision_id AND r.package_id = l.package_id"
            " WHERE l.tenant_id = %s AND l.content_id = %s AND l.package_id = ANY(%s)"
            " ORDER BY l.package_id, r.line",
            (self._tenant, content_id, packages),
        ).fetchall():
            if (str(package_id), str(revision_id)) not in superseded:
                loaded = canonical_json.loads(str(location).encode("utf-8"))
                assert isinstance(loaded, dict)
                out.setdefault(str(package_id), []).append(loaded)
        return {package_id: tuple(found) for package_id, found in out.items()}

    # --- thread, threads_of, lineage (ADR 0003, ADR 0010) --------------------------------------

    def thread(
        self,
        key: ThreadKey,
        order: Order,
        preference: ThreadPreference,
        *,
        merge: ClockMerge | None = None,
        as_of: int | None = None,
    ) -> Thread:
        """One thread at one catalog point (ADR 0003 §3-§5): history or a current view."""
        thread_id = _thread_id(key)
        # A rejection echoes the optional request parts only when they are inside the contract,
        # so it still encodes. A key or order outside it is echoed as given: such a request never
        # decodes from the wire, only an in-process caller can make one (ADR 0010 §5).
        chosen = preference if isinstance(preference, _PREFERENCES) and _valid(preference) else None
        echo = merge if isinstance(merge, ClockMerge) and _valid(merge) else None
        if _bad_as_of(as_of):
            point, _, _ = self._run(lambda conn: self._lookup(conn, "", None))
            finding = CatalogFinding("invalid_request", str(as_of), "as_of is a tx_seq, at least 1")
            return empty_thread(thread_id, key, order, point, (finding,), chosen, echo)

        def body(conn: Conn) -> Thread:
            point, _, beyond = self._lookup(conn, "", as_of)
            limit = point.value.tx_seq if isinstance(point, Known) else 0
            if beyond:
                return empty_thread(thread_id, key, order, point, (_beyond(as_of),), chosen, echo)
            findings, mappings = self._thread_request(conn, key, order, preference, merge, limit)
            if findings:
                return empty_thread(thread_id, key, order, point, findings, chosen, echo)
            assert chosen is not None
            return read_thread(conn, self._tenant, key, order, chosen, echo, mappings, limit, point)

        return self._run(body)

    def _thread_request(
        self,
        conn: Conn,
        key: ThreadKey,
        order: Order,
        preference: object,
        merge: ClockMerge | None,
        limit: int,
    ) -> tuple[tuple[CatalogFinding, ...], tuple[ClockMapping, ...]]:
        """Why a ``thread`` call is rejected, or nothing, and the clock mappings its merge names
        (ADR 0004 §2; ADR 0010 §5, §9)."""
        if preference is None:
            detail = "a thread call names its preference; there is no default (ADR 0003 §4.4)"
            return (CatalogFinding("preference_required", "preference", detail),), ()
        if not isinstance(preference, _PREFERENCES) or not _valid(preference):
            return (CatalogFinding("invalid_request", "preference", "not a thread preference"),), ()
        if order not in ("world", "transaction"):
            detail = "order is world or transaction"
            return (CatalogFinding("invalid_request", str(order)[:200] or "order", detail),), ()
        if not isinstance(key, ThreadKey) or not _valid(key):
            bad = CatalogFinding("invalid_request", "key", "not a thread key of the contract")
            return (bad,), ()
        if merge is None:
            return (), ()
        if not isinstance(merge, ClockMerge) or not _valid(merge) or order != "world":
            detail = "a merge is a reference clock and mapping ids, on world order only"
            return (CatalogFinding("invalid_request", "merge", detail),), ()
        found: list[CatalogFinding] = []
        if not conn.execute(
            "SELECT 1 FROM clock c JOIN package p USING (tenant_id, package_id)"
            " WHERE c.tenant_id = %s AND c.clock_id = %s AND p.tx_seq <= %s LIMIT 1",
            (self._tenant, merge.reference_clock, limit),
        ).fetchone():
            detail = "no registered package holds this clock"
            found.append(CatalogFinding("unknown_clock", merge.reference_clock, detail))
        # A record id names one body in every package that holds it (ADR 0002 §6), so the
        # first registration's row is the mapping.
        held = {
            str(record): clock_mapping(str(record), str(source), str(target), str(text))
            for record, source, target, text in conn.execute(
                "SELECT DISTINCT ON (record_id) record_id, source_clock, target_clock, mapping"
                " FROM thread_clock_mapping WHERE tenant_id = %s AND record_id = ANY(%s)"
                "   AND registration_key <= %s ORDER BY record_id, registration_key",
                (self._tenant, list(merge.mappings), limit),
            ).fetchall()
        }
        detail = "no registered package holds a ClockMapping with this id"
        found += [
            CatalogFinding("unknown_mapping", m, detail)
            for m in sorted(merge.mappings, key=lambda m: m.encode("utf-8"))
            if m not in held
        ]
        if found:
            return tuple(found), ()
        return (), tuple(held[m] for m in sorted(held, key=lambda m: m.encode("utf-8")))

    def threads_of(self, record_id: str, *, as_of: int | None = None) -> ThreadsOf:
        """Every thread a record id is a member of, per registering package (ADR 0003 §1.4)."""
        problem = _bad_record_request(record_id, as_of)
        if problem is not None:
            point, _, _ = self._run(lambda conn: self._lookup(conn, "", None))
            return unknown_threads_of(str(record_id) or "record_id", point, problem)

        def body(conn: Conn) -> ThreadsOf:
            point, _, beyond = self._lookup(conn, "", as_of)
            if beyond:
                return unknown_threads_of(record_id, point, _beyond(as_of))
            limit = point.value.tx_seq if isinstance(point, Known) else 0
            return read_threads_of(conn, self._tenant, record_id, limit, point)

        return self._run(body)

    def lineage(self, record_id: str, *, as_of: int | None = None) -> LineageGraph:
        """The transform DAG behind a record id and its lineage siblings (ADR 0003 §4.1)."""
        problem = _bad_record_request(record_id, as_of)
        if problem is not None:
            point, _, _ = self._run(lambda conn: self._lookup(conn, "", None))
            return unknown_record(str(record_id) or "record_id", point, problem)

        def body(conn: Conn) -> LineageGraph:
            point, _, beyond = self._lookup(conn, "", as_of)
            if beyond:
                return unknown_record(record_id, point, _beyond(as_of))
            limit = point.value.tx_seq if isinstance(point, Known) else 0
            return read_lineage(conn, self._tenant, record_id, limit, point)

        return self._run(body)

    def query(self, spec: QuerySpec) -> Any:
        raise NotImplementedError("catalog API query() is not implemented yet (MVL-98)")

    # --- transactions --------------------------------------------------------------------------

    def _connection(self) -> Conn:
        if self._borrowed is not None:  # never replaced: a write must not escape the rebuild
            if self._borrowed.closed:
                raise CatalogUnavailable("the rebuild's connection is closed")
            return self._borrowed
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


def _batches(rows: Sequence[T]) -> Iterator[Sequence[T]]:
    """``rows`` in consecutive slices of ``BATCH_ROWS`` (ADR 0009 §4)."""
    for start in range(0, len(rows), BATCH_ROWS):
        yield rows[start : start + BATCH_ROWS]


_PREFERENCES: Final = (History, LatestTransform, Pinned, AsRegisteredBy)


def _valid(value: object) -> bool:
    """Whether a request record is inside the contract: it encodes under its JSON Schema."""
    try:
        codec.to_json(value)
    except (codec.CodecError, TypeError, ValueError):
        return False
    return True


def _thread_id(key: object) -> str:
    """ADR 0003 §1.3's thread id; for a key outside the contract, the same hash over whatever
    of it can be written as JSON, so a rejected call still names what it was asked."""
    if isinstance(key, ThreadKey) and _valid(key):
        return key.thread_id
    try:
        text = canonical_json.dumps(codec.to_json(key))
    except (codec.CodecError, canonical_json.CanonicalJsonError, TypeError, ValueError):
        text = repr(key).encode("utf-8")
    return "sha256:" + hashlib.sha256(text).hexdigest()


def _bad_record_request(record_id: object, as_of: object) -> CatalogFinding | None:
    if not isinstance(record_id, str) or not record_id:
        return CatalogFinding("invalid_request", "record_id", "a record id is a non-empty string")
    if _bad_as_of(as_of):
        return CatalogFinding("invalid_request", str(as_of), "as_of is a tx_seq, at least 1")
    return None


def _beyond(as_of: int | None) -> CatalogFinding:
    detail = "as_of is beyond the latest committed catalog point"
    return CatalogFinding("as_of_out_of_range", str(as_of), detail)


def _bad_as_of(as_of: object) -> bool:
    """An ``as_of`` outside the contract: not a tx_seq of at least 1. A bool is not a tx_seq."""
    return as_of is not None and (
        isinstance(as_of, bool) or not isinstance(as_of, int) or as_of < 1
    )


def _region(step: Any) -> Region:
    """The innermost locator step and how it addresses the source (``Region``)."""
    kind = step.get("kind") if isinstance(step, Mapping) else None
    addressing = kind if isinstance(kind, str) and kind in _CORE_STEPS else "adapter"
    return Region(addressing, step if isinstance(step, Mapping) else {})  # type: ignore[arg-type]


def _anchor_locator(anchor: EvidenceAnchor) -> str | None:
    """The anchor's locator as the canonical JSON ``record.source_locator`` holds, or None when
    the anchor is outside the contract (not a content id, or not a non-empty list of objects)."""
    steps = anchor.locator
    if not isinstance(anchor.source, str) or not _CONTENT_ID.fullmatch(anchor.source):
        return None
    if not isinstance(steps, tuple) or not steps:
        return None
    if not all(isinstance(step, Mapping) for step in steps):
        return None
    try:
        return canonical_json.dumps(list(steps)).decode("utf-8")
    except canonical_json.CanonicalJsonError:
        return None


def _unresolved(
    anchor: EvidenceAnchor,
    region: Region,
    point: Knowledge[TransactionKey],
    finding: CatalogFinding,
) -> Resolution:
    """A rejected or unresolvable ``resolve``: empty payload, ``size`` NotCovered (ADR 0004 §2)."""
    return Resolution(
        evidence_ref=anchor,
        status="unresolvable",
        size=NotCovered(),
        region=region,
        fetch=(),
        cited_by=(),
        as_of=point,
        findings=(finding,),
    )


def _record_values(tenant: str, package_id: str, seq: int, r: RecordRow) -> tuple[Any, ...]:
    """One ``record`` row's values in ``_RECORD_COLUMNS`` order."""
    return (
        tenant,
        r.kind,
        r.record_id,
        package_id,
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
        r.body,
        list(r.unknown_pointers),
        *r.projected,
    )


def _counts(checked: Checked) -> tuple[KindCount, ...]:
    tables = checked.manifest["tables"]
    return tuple(KindCount(kind, tables[kind]) for kind in sorted(tables) if tables[kind])
