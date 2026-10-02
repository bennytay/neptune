"""``RerunSource``: a Rerun Hub dataset's ``.rrd`` objects as read-only Sources (ADR 0009 §5).

The catalog (a segment table export, ``export.py``) says which ``.rrd`` objects a dataset has: one
per segment layer, at the ``rerun_storage_urls`` Rerun documents. The connector resolves each URL on
its store with the object-store connector's own client (ADR 0006): one exact-key listing for the
object's size and revision token (version id, generation or etag). So an object is the same
``ExternalObjectRef`` whether it is read through this connector or through ``deploy_s3``, and one
re-uploaded under the same path is a new revision of the same location, as ADR 0006 §3 has it.
Reads, ranges, chunk checks, limits and findings are the object-store source's; this class routes
them to the source of the object's own bucket.

The catalog itself is ``stated`` metadata: ``catalog()`` gives the segment rows, the schema's entity
paths and components, and the dataset's indexes as structured records cited to catalog documents,
and each index (timeline) as a ``TimestampDomain`` whose epoch, timescale, resolution and role are
``Unknown`` unless the operator declared them. An RRD adapter in the compiler does not exist yet
(compiler gap, ADR 0009): the objects are listed and read, and the entity paths are the catalog's
word until an adapter reads the file's own.

``ObjectStoreSource.__init__`` is not called (see ``roboto.source``); a test runs each inherited
method to check that every attribute it reads is set.
"""

from collections.abc import Mapping
from functools import cached_property
from typing import Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, TransformRecord
from neptune.model.reference import TimestampDomain
from neptune_deploy.sources.object_store import clients
from neptune_deploy.sources.object_store.clients import Provider, S3Client, StoreClient
from neptune_deploy.sources.object_store.config import (
    CONNECTOR_IDS,
    SCHEMES,
    Credentials,
    Options,
    StoreLocation,
    credentials_for,
    endpoint_for,
)
from neptune_deploy.sources.object_store.source import (
    CONNECTOR_VERSION,
    Listing,
    ObjectEntry,
    ObjectReadError,
    ObjectStoreSource,
)
from neptune_deploy.sources.object_store.transport import NetworkGate, Transport
from neptune_deploy.sources.rerun.export import RerunExport
from neptune_deploy.sources.rerun.options import CONNECTOR_ID, RerunOptions
from neptune_deploy.sources.stated_records import (
    CatalogDocument,
    DeclaredClock,
    StatedCatalog,
    build_document,
    pointer,
    stated_table,
)

CATALOG_CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "segment_invalid": (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "a segment row has no usable id, or layer names and storage URLs that do not pair up; its"
        " objects are not resolved",
    ),
    "storage_url_unsupported": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "a storage URL is not an s3://, gs:// or az:// object URL; the object is not read",
    ),
    "object_not_found": (
        FindingCategory.MISSING,
        Severity.WARNING,
        "the store does not list the object a segment layer names (or the listing failed)",
    ),
    "catalog_size_differs": (
        FindingCategory.INCONSISTENT,
        Severity.WARNING,
        "the catalog states a segment's size and its one object has another; the store's is used",
    ),
    "value_unrepresentable": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "catalog values that cannot be stored as cell text are Unknown",
    ),
}


def _hex(text: str) -> str:
    return text.encode("utf-8", "surrogatepass")[:256].hex()


def parse_storage_url(url: object) -> tuple[Provider, str, str | None, str] | None:
    """``(provider, bucket or container, account, key)`` of an object URL (s3, gs or az).

    The key is verbatim: no percent-decoding, no normalisation. Anything else is ``None``.
    """
    if not isinstance(url, str):
        return None
    scheme, sep, rest = url.partition("://")
    provider = SCHEMES.get(scheme.lower()) if sep else None
    if provider is None:
        return None
    account = None
    if provider is Provider.AZURE:
        account, _, rest = rest.partition("/")
    bucket, slash, key = rest.partition("/")
    if not slash or not key or not bucket:
        return None
    return provider, bucket, account, key


class RerunSource(ObjectStoreSource):
    """A Rerun Hub dataset's ``.rrd`` objects, from its catalog export, read only."""

    def __init__(
        self,
        export: RerunExport,
        options: RerunOptions,
        network: NetworkGate,
        credentials: Mapping[str, str] | None,
        environ: Mapping[str, str],
        *,
        ledger: SourceLedger | None = None,
    ) -> None:
        # Not super().__init__: see the module docstring.
        self.export = export
        self.rerun = options
        self.options = Options(
            max_objects=options.max_objects, max_listing_bytes=options.max_listing_bytes
        )
        self.connector_id = CONNECTOR_ID
        self._network = network
        self._ledger = ledger
        self._findings: dict[str, IngestFinding] = {}
        self._credentials = credentials
        self._environ = environ
        self._clients: dict[tuple[Provider, str, str | None], StoreClient] = {}
        self._inner: dict[ExternalObjectRef, ObjectStoreSource] = {}
        self._entries: dict[ExternalObjectRef, ObjectEntry] = {}
        self._objects: dict[tuple[str, int], list[ExternalObjectRef]] = {}
        self._inner_sources: list[ObjectStoreSource] = []

    @cached_property
    def transform(self) -> TransformRecord:
        """What decided which objects and records were seen. No endpoint, path or credential."""
        opt = self.rerun
        config: dict[str, JsonValue] = {
            "catalog": self.export.catalog,
            "dataset": self.export.dataset_id,
            "max_listing_bytes": opt.max_listing_bytes,
            "max_objects": opt.max_objects,
            "timeline_clocks": {
                name: clock.config() for name, clock in sorted(opt.timeline_clocks.items())
            },
        }
        return transform_record(
            adapter_id=CONNECTOR_ID, adapter_version=CONNECTOR_VERSION, config=config
        )

    @property
    def listing_ref(self) -> ExternalObjectRef:
        return ExternalObjectRef(self.connector_id, self._document_id("segments"), "listing")

    def report(self, code: str, subject: ExternalObjectRef, details: dict[str, JsonValue]) -> None:
        if code not in CATALOG_CODES:
            super().report(code, subject, details)
            return
        category, severity, message = CATALOG_CODES[code]
        finding = ingest_finding(
            code=f"{self.connector_id}.{code}",
            category=category,
            severity=severity,
            subject=subject,
            transform=self.transform,
            message=message,
            details=details,
        )
        self._findings[finding.id] = finding

    def findings(self) -> tuple[IngestFinding, ...]:
        """Its own findings and those of the sources it read objects through, sorted by id."""
        merged = dict(self._findings)
        for inner in self._inner_sources:
            for finding in inner.findings():
                merged[finding.id] = finding
        return tuple(merged[key] for key in sorted(merged))

    # --- Listing ---------------------------------------------------------------------------------

    def _client(self, provider: Provider, bucket: str, account: str | None) -> StoreClient:
        """The client of one bucket, built once and shared by every object in it."""
        key = (provider, bucket, account)
        if key not in self._clients:
            options = self.rerun.storage_options(provider)
            location = StoreLocation(provider, bucket, "", account, options.store)
            names = {
                Provider.S3: ("s3_access_key_id", "s3_secret_access_key", "s3_session_token"),
                Provider.GCS: ("gcs_access_token",),
                Provider.AZURE: ("azure_sas_token",),
            }[provider]
            declared = (
                None
                if self._credentials is None
                else {k: v for k, v in self._credentials.items() if k in names}
            )
            found: Credentials = credentials_for(
                provider,
                declared or None,
                self._environ,
                anonymous=options.anonymous,
            )
            endpoint, addressing = endpoint_for(location, options)
            transport = Transport(
                endpoint, self._network, f"reading {CONNECTOR_ID} sources", timeout=options.timeout
            )
            client: StoreClient
            if provider is Provider.S3:
                client = S3Client(
                    transport,
                    bucket,
                    addressing=addressing,
                    region=options.region,
                    credentials=found.aws,
                    versions=options.versions,
                )
            elif provider is Provider.GCS:
                client = clients.GcsClient(transport, bucket, access_token=found.gcs_token)
            else:
                client = clients.AzureBlobClient(transport, bucket, sas=found.azure_sas or ())
            self._clients[key] = client
        return self._clients[key]

    def _resolve(
        self, provider: Provider, bucket: str, account: str | None, key: str
    ) -> tuple[ObjectStoreSource, ObjectEntry] | None:
        """The one object at exactly ``key``: a listing of that prefix, keeping its entry."""
        options = self.rerun.storage_options(provider)
        location = StoreLocation(provider, bucket, key, account, options.store)
        probe = Options(
            endpoint=options.endpoint,
            store=options.store,
            region=options.region,
            addressing=options.addressing,
            versions=options.versions,
            anonymous=options.anonymous,
            max_objects=64,
            page_size=64,
            timeout=options.timeout,
        )
        inner = ObjectStoreSource(
            location, self._client(provider, bucket, account), self._network, probe
        )
        self._inner_sources.append(inner)
        for entry in inner.listing().entries:
            if entry.key == key:
                return inner, entry
        return None

    @cached_property
    def _listing(self) -> Listing:
        wanted: dict[tuple[Provider, str, str | None, str], list[int]] = {}
        for index, row in enumerate(self.export.segments):
            assert isinstance(row, Mapping)
            self._segment(index, row, wanted)
        entries: dict[ExternalObjectRef, ObjectEntry] = {}
        complete = True
        count = 0
        for target in sorted(wanted, key=lambda t: (t[0].value, t[1], t[2] or "", t[3])):
            if count >= self.rerun.max_objects:
                self.report("listing_limit", self.listing_ref, {"max_objects": count})
                complete = False
                break
            count += 1
            provider, bucket, account, key = target
            found = self._resolve(provider, bucket, account, key)
            if found is None:
                self.report(
                    "object_not_found",
                    self.listing_ref,
                    {"provider": provider.value, "key_hex": _hex(key)},
                )
                complete = False
                continue
            inner, entry = found
            entries[entry.location] = entry
            self._inner[entry.location] = inner
            for segment_index in wanted[target]:
                self._objects.setdefault((self.export.dataset_id, segment_index), []).append(
                    entry.location
                )
            self._size_check(wanted[target], entry)
        self._entries = entries
        ordered = tuple(sorted(entries.values(), key=lambda e: e.location.object_id))
        return Listing(ordered, (), complete)

    def _segment(
        self,
        index: int,
        row: Mapping[str, JsonValue],
        wanted: dict[tuple[Provider, str, str | None, str], list[int]],
    ) -> None:
        segment, layers, urls = (
            row.get("rerun_segment_id"),
            row.get("rerun_layer_names"),
            row.get("rerun_storage_urls"),
        )
        subject = self.listing_ref
        if (
            not isinstance(segment, str)
            or not segment
            or not isinstance(layers, list)
            or not isinstance(urls, list)
            or len(layers) != len(urls)
            or not all(isinstance(name, str) for name in layers)
        ):
            self.report("segment_invalid", subject, {"row": index})
            return
        for url in urls:
            parsed = parse_storage_url(url)
            if parsed is None:
                scheme = url.partition("://")[0][:16] if isinstance(url, str) else ""
                self.report(
                    "storage_url_unsupported",
                    subject,
                    {"row": index, "scheme": scheme if scheme.isalnum() else ""},
                )
                continue
            wanted.setdefault(parsed, []).append(index)

    def _size_check(self, users: list[int], entry: ObjectEntry) -> None:
        """A segment with one layer whose catalog row states a size: the store's must agree."""
        for index in users:
            row = self.export.segments[index]
            assert isinstance(row, Mapping)
            stated = row.get("rerun_size_bytes")
            urls = row.get("rerun_storage_urls")
            if (
                isinstance(stated, int)
                and not isinstance(stated, bool)
                and isinstance(urls, list)
                and len(urls) == 1
                and stated != entry.size
            ):
                self.report(
                    "catalog_size_differs",
                    entry.location,
                    {"catalog": stated, "row": index, "store": entry.size},
                )

    # --- The Source protocol ---------------------------------------------------------------------

    def entry(self, location: object) -> ObjectEntry:
        """The listed object at ``location``, at the revision the listing holds."""
        self.listing()
        if not isinstance(location, ExternalObjectRef):
            raise TypeError(f"{self.connector_id} cannot open {location!r}")
        found = self._entries.get(location)
        if found is None:
            if location.connector_id not in CONNECTOR_IDS.values():
                raise TypeError(f"{self.connector_id} cannot open {location!r}")
            raise ObjectReadError("not_listed", location)
        return found

    def fetch(self, entry: ObjectEntry, start: int, length: int) -> bytes:
        """Bytes of ``entry``, through the source of its own bucket (its failures too)."""
        return self._inner[entry.location].fetch(entry, start, length)

    def _gone(self, listing: Listing, ledger: SourceLedger) -> tuple[()]:
        """Nothing is asserted gone. A catalog that stops naming an object, or a layer replaced, is
        not the object leaving its store; the object-store connector asserts that of its own
        prefix."""
        return ()

    # --- Catalog ---------------------------------------------------------------------------------

    def _document_id(self, part: str) -> str:
        return f"{self.export.catalog}:{self.export.dataset_id}/{part}"

    def objects_of(self, segment_row: int) -> tuple[ExternalObjectRef, ...]:
        """The objects a segment row's layers resolved to, as the listing found them."""
        self.listing()
        return tuple(self._objects.get((self.export.dataset_id, segment_row), ()))

    @cached_property
    def _catalog(self) -> StatedCatalog:
        export = self.export
        catalog = StatedCatalog()
        parts = (
            ("dataset", [export.dataset]),
            ("segments", list(export.segments)),
            ("schema", list(export.schema)),
            ("indexes", list(export.indexes)),
        )
        for part, items in parts:
            if not items:
                continue
            document = build_document(self.connector_id, self._document_id(part), items)
            table = stated_table(document, f"rerun {part}", self.transform)
            clocks = self._timelines(document) if part == "indexes" else []
            catalog = StatedCatalog(
                (*catalog.documents, document),
                (*catalog.tables, *table.tables),
                (*catalog.rows, *table.rows),
                (*catalog.domains, *clocks),
                (*catalog.skipped, *table.skipped),
            )
        counts: dict[tuple[str, str], int] = {}
        for name, _, reason in catalog.skipped:
            counts[(name, reason)] = counts.get((name, reason), 0) + 1
        for (name, reason), count in sorted(counts.items()):
            subject = ExternalObjectRef(
                self.connector_id, self._document_id(name.removeprefix("rerun ")), "catalog"
            )
            self.report("value_unrepresentable", subject, {"count": count, "reason": reason})
        return catalog

    def _timelines(self, document: CatalogDocument) -> list[TimestampDomain]:
        """One clock per index the catalog names: ``field`` is the index's name verbatim, ``scope``
        the dataset. Its parts are ``Unknown`` unless the operator declared that index's clock."""
        found: list[TimestampDomain] = []
        seen: set[str] = set()
        for index, item in enumerate(document.items):
            name = item.get("name")
            if not isinstance(name, str) or not name or name in seen:
                continue
            seen.add(name)
            where = EvidenceRef(
                document.content_id, (JsonPointer(pointer("items", index, "name")),)
            )
            declared = self.rerun.timeline_clocks.get(name, DeclaredClock())
            found.append(
                TimestampDomain(
                    id=evidence_record_id(TimestampDomain.kind, where, self.transform),
                    provenance=Provenance(where, self.transform.id, AssertionKind.STATED),
                    field=name,
                    scope=(self.export.dataset_id,),
                    role=Unknown() if declared.role is None else Known(declared.role),
                    resolution=(
                        Unknown() if declared.resolution is None else Known(declared.resolution)
                    ),
                    epoch=Unknown() if declared.epoch is None else Known(declared.epoch),
                    timescale=(
                        Unknown() if declared.timescale is None else Known(declared.timescale)
                    ),
                    declared_monotonic=Unknown(),
                )
            )
        return found

    def catalog(self) -> StatedCatalog:
        """The dataset, segment rows, schema and indexes as ``stated`` records.

        Entity paths and timelines are what the catalog says; nothing here reads the ``.rrd`` files.
        """
        return self._catalog


__all__ = ["RerunSource", "parse_storage_url"]
