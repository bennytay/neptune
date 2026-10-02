"""``RobotoSource``: one Roboto dataset as a read-only compiler ``Source`` (ADR 0009).

It is an ``ObjectStoreSource`` whose store is Roboto's REST API (``RobotoApi``): the listing, its
determinism and limits, ledger discovery, ranged reads, chunk-checked readers and findings are the
object-store connector's own (ADR 0006), so a Roboto file and an S3 object cannot differ in policy.
What differs is what a Roboto object is:

- Identity: ``ExternalObjectRef("deploy_roboto", "<org>/<dataset>/<relative path>",
  "version:<file id>:<version>")``. Roboto numbers a file's versions; a re-upload is a new version
  of the same path, so the ledger chains it as a new revision, and a file deleted and uploaded again
  has a new file id, so it is never read as unchanged.
- Catalog: ``catalog()`` is the dataset record, the file records, the events and the comments of the
  dataset as ``stated`` structured records (``neptune_deploy.sources.stated_records``), each cited
  to a catalog document, with the event times on declared clocks. Roboto annotations (events) say
  what happened over a range of time on a file, topic or dataset; the connector records what they
  state.

``ObjectStoreSource.__init__`` is not called: it builds an object-store location and client, and
this source has its own. Every attribute the inherited methods read is set below; a test runs each
inherited method to check that none is missing.
"""

from functools import cached_property
from typing import Final

from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.identity.revisions import SourceLedger
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import TransformRecord
from neptune_deploy.sources.object_store.config import Options
from neptune_deploy.sources.object_store.source import CONNECTOR_VERSION, ObjectStoreSource
from neptune_deploy.sources.object_store.transport import NetworkGate, TransportError
from neptune_deploy.sources.roboto.client import Records, RobotoApi
from neptune_deploy.sources.roboto.config import CONNECTOR_ID, RobotoLocation, RobotoOptions
from neptune_deploy.sources.stated_records import (
    StatedCatalog,
    build_document,
    clock_domain,
    stated_table,
)

CATALOG_CODES: Final[dict[str, tuple[FindingCategory, Severity, str]]] = {
    "catalog_failed": (
        FindingCategory.FAILED,
        Severity.ERROR,
        "a catalog request (dataset record, events or comments) failed; that part of the catalog is"
        " incomplete or absent",
    ),
    "catalog_limit": (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "a catalog read stopped at its record, byte or page limit; later records are not covered",
    ),
    "catalog_invalid": (
        FindingCategory.CORRUPT,
        Severity.ERROR,
        "a catalog response is not the strict JSON the API documents; that part is not recorded",
    ),
    "value_unrepresentable": (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "catalog values that cannot be stored as cell text (a lone surrogate, or too large) are"
        " Unknown",
    ),
}
_LIMIT_CAUSES: Final = {"record_limit", "byte_limit", "page_limit"}


class RobotoSource(ObjectStoreSource):
    """A Roboto dataset (and a path prefix in it), read only."""

    def __init__(
        self,
        location: RobotoLocation,
        client: RobotoApi,
        network: NetworkGate,
        options: RobotoOptions,
        *,
        ledger: SourceLedger | None = None,
    ) -> None:
        # Not super().__init__: see the module docstring.
        self.place = location
        self.api = client
        self.location = location  # type: ignore[assignment]
        self.client = client  # type: ignore[assignment]
        self.options = Options(
            max_objects=options.max_objects,
            max_listing_bytes=options.max_listing_bytes,
            page_size=options.page_size,
            timeout=options.timeout,
        )
        self.roboto = options
        self.connector_id = CONNECTOR_ID
        self._network = network
        self._ledger = ledger
        self._findings = {}

    @cached_property
    def transform(self) -> TransformRecord:
        """The connector as a producer: what decided which objects and records were seen. It holds
        no endpoint, host, token or credential."""
        loc, opt = self.place, self.roboto
        config: dict[str, JsonValue] = {
            "api_version": opt.api_version,
            "comments": opt.comments,
            "dataset": loc.dataset,
            "event_clock": opt.event_clock.config(),
            "events": opt.events,
            "max_listing_bytes": opt.max_listing_bytes,
            "max_objects": opt.max_objects,
            "max_records": opt.max_records,
            "org": loc.org,
            "prefix": loc.prefix,
        }
        return transform_record(
            adapter_id=CONNECTOR_ID, adapter_version=CONNECTOR_VERSION, config=config
        )

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

    # --- Catalog ---------------------------------------------------------------------------------

    def _document_id(self, part: str) -> str:
        """``<org>/<dataset>:<part>``. A file's id is ``<org>/<dataset>/<path>``, so the ``:`` keeps
        a catalog document from ever sharing an id with a file, whatever the file's path."""
        return f"{self.place.org}/{self.place.dataset}:{part}"

    def _stopped(self, part: str, found: Records) -> None:
        subject = ExternalObjectRef(self.connector_id, self._document_id(part), "catalog")
        cause = found.stopped or "unknown"
        details: dict[str, JsonValue] = {"cause": cause, "part": part, "records": len(found.items)}
        if found.status is not None:
            details["status"] = found.status
        if cause in _LIMIT_CAUSES:
            self.report("catalog_limit", subject, details)
        elif cause == "response_invalid":
            self.report("catalog_invalid", subject, details)
        else:
            self.report("catalog_failed", subject, details)

    @cached_property
    def _catalog(self) -> StatedCatalog:
        entries = self.listing().entries  # the listing first: it holds the file records
        wanted = {(entry.key, entry.location.revision_token) for entry in entries}
        sources: list[tuple[str, str, list[JsonValue]]] = [
            (
                "files",
                "files",
                [record for key, token, record in self.api.file_records if (key, token) in wanted],
            )
        ]
        dataset: list[JsonValue] = []
        try:
            dataset.append(self.api.dataset())
        except (TransportError, ValueError) as exc:  # a failed request is a finding
            self._request_failed("dataset", exc)
        sources.insert(0, ("dataset", "dataset", dataset))
        for part, enabled, route in (
            ("events", self.roboto.events, ("datasets", self.place.dataset, "events")),
            ("comments", self.roboto.comments, ("comments", "dataset", self.place.dataset)),
        ):
            if enabled:
                found = self.api.records(
                    route, limit=self.roboto.max_records, budget=self.roboto.max_listing_bytes
                )
                if not found.complete:
                    self._stopped(part, found)
                sources.append((part, part, found.items))
        catalog = StatedCatalog()
        for part, name, items in sources:
            if not items and part != "files":
                continue
            document = build_document(self.connector_id, self._document_id(part), items)
            clocks = {}
            if part == "events":
                for field_name in ("start_time", "end_time"):
                    domain = clock_domain(
                        document,
                        field_name,
                        (self.place.dataset,),
                        self.transform,
                        self.roboto.event_clock,
                    )
                    if domain is not None:
                        clocks[field_name] = domain
            table = stated_table(document, f"roboto {name}", self.transform, clocks=clocks)
            catalog = StatedCatalog(
                (*catalog.documents, document),
                (*catalog.tables, *table.tables),
                (*catalog.rows, *table.rows),
                (*catalog.domains, *clocks.values()),
                (*catalog.skipped, *table.skipped),
            )
        self._report_unrepresentable(catalog)
        return catalog

    def _request_failed(self, part: str, exc: Exception) -> None:
        found = Records()
        found.stopped = exc.code if isinstance(exc, TransportError) else "response_invalid"
        found.status = exc.status if isinstance(exc, TransportError) else None
        self._stopped(part, found)

    def _report_unrepresentable(self, catalog: StatedCatalog) -> None:
        counts: dict[tuple[str, str], int] = {}
        for name, _, reason in catalog.skipped:
            counts[(name, reason)] = counts.get((name, reason), 0) + 1
        for (name, reason), count in sorted(counts.items()):
            subject = ExternalObjectRef(self.connector_id, self._document_id(name), "catalog")
            self.report(
                "value_unrepresentable", subject, {"count": count, "reason": reason, "table": name}
            )

    def catalog(self) -> StatedCatalog:
        """The dataset, files, events and comments as ``stated`` records over catalog documents.

        Read once per source and kept. A part that could not be read is a finding and is absent (or
        partial, for events and comments, with the finding saying so): nothing is invented.
        """
        return self._catalog
