"""Record-system connectors: CMMS and ticketing APIs and document stores (ADR 0008).

Each system is one ``neptune.sources`` entry point, named by its connector id, whose value is a
factory with the one signature of ADR 0006 §1:

    factory(url, *, network, ledger=None, options=None, credentials=None, environ=None)
        -> RecordSource

=====================  ==========================================  ================================
entry point            URL                                         reads
=====================  ==========================================  ================================
``deploy_jira``        ``jira://<site host>/<PROJECT>``            issues, attachments
``deploy_servicenow``  ``servicenow://<instance host>/<table>``    table records, attachments
``deploy_gdrive``      ``gdrive://<shared drive id or my-drive>``  files with bytes; change feed
``deploy_confluence``  ``confluence://<site host>/<space id>``     current pages (storage format)
``deploy_rest``        ``rest://<host>`` + a declared profile      a CMMS or EAM REST API
=====================  ==========================================  ================================

- ``network``: the compiler's workspace; it is asked before the source is built and before every
  request, so a local-only workspace refuses it.
- ``ledger``: the ingest root's ``SourceLedger``; with it, ``walk`` yields only new or changed
  items.
- ``options``: declared and closed (``config.Options`` and each system's own). ``since`` is the
  cursor the previous run's ``RecordSource.cursor`` returned.
- ``credentials``: declared read-only credentials, else the system's ``NEPTUNE_*`` variables of
  ``environ`` (``os.environ`` by default). Ambient credentials are never read.

Importing this package touches nothing: no network, file or environment access until a factory is
called.
"""

import os
from collections.abc import Mapping
from typing import Protocol

from neptune.identity.revisions import SourceLedger
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.source import ObjectReadError as ObjectReadError
from neptune_deploy.sources.object_store.transport import NetworkGate
from neptune_deploy.sources.records import config
from neptune_deploy.sources.records.config import Location, Options, RecordConfigError
from neptune_deploy.sources.records.http import Api, RecordTransport
from neptune_deploy.sources.records.model import Listing as Listing
from neptune_deploy.sources.records.model import (
    RecordEntry,
    Relation,
    SkippedRecord,
)
from neptune_deploy.sources.records.source import Discovery, RecordReader, RecordSource
from neptune_deploy.sources.records.systems import confluence, gdrive, jira, rest, servicenow
from neptune_deploy.sources.records.systems.spec import Spec

__all__ = [
    "CONNECTOR_IDS",
    "Discovery",
    "Listing",
    "ObjectReadError",
    "RecordConfigError",
    "RecordEntry",
    "RecordReader",
    "RecordSource",
    "Relation",
    "SkippedRecord",
    "SourceFactory",
    "confluence_source",
    "gdrive_source",
    "jira_source",
    "record_source",
    "rest_source",
    "servicenow_source",
]

SPECS: dict[str, Spec] = {
    spec.connector_id: spec
    for spec in (jira.SPEC, servicenow.SPEC, gdrive.SPEC, confluence.SPEC, rest.SPEC)
}
CONNECTOR_IDS = tuple(sorted(SPECS))


def record_source(
    connector_id: str,
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RecordSource:
    """The source ``url`` names on ``connector_id``'s system; a local-only workspace refuses it."""
    spec = SPECS[connector_id]
    network.require_network(f"reading {connector_id} sources")
    authority, path = config.split_url(url, spec.scheme)
    parsed = Options.parse(options, spec.extras, max_page_size=spec.max_page_size)
    config.cursor_payload(connector_id, parsed.since, spec.since_shape)
    plan = spec.plan(authority, path, parsed)
    found = config.credentials(spec.env, credentials, os.environ if environ is None else environ)
    auth = spec.auth(found, parsed)
    instance = config.instance_name(plan.endpoint, parsed, declared_endpoint=plan.declared_endpoint)
    transport = RecordTransport(
        plan.endpoint, network, f"reading {connector_id} sources", timeout=parsed.timeout
    )
    system, settings = spec.build(Api(transport, auth), plan.what, parsed)
    return RecordSource(
        Location(connector_id, instance, plan.what),
        system,
        network,
        parsed,
        settings,
        ledger=ledger,
    )


class SourceFactory(Protocol):
    """The signature every ``neptune.sources`` entry point of this package has (ADR 0008 §1)."""

    def __call__(
        self,
        url: str,
        *,
        network: NetworkGate,
        ledger: SourceLedger | None = None,
        options: Mapping[str, JsonValue] | None = None,
        credentials: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> RecordSource: ...


def _factory(connector_id: str, name: str, doc: str) -> SourceFactory:
    def factory(
        url: str,
        *,
        network: NetworkGate,
        ledger: SourceLedger | None = None,
        options: Mapping[str, JsonValue] | None = None,
        credentials: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> RecordSource:
        return record_source(
            connector_id,
            url,
            network=network,
            ledger=ledger,
            options=options,
            credentials=credentials,
            environ=environ,
        )

    factory.__name__ = factory.__qualname__ = name
    factory.__doc__ = doc
    return factory


jira_source = _factory(
    "deploy_jira", "jira_source", "``deploy_jira``: a Jira Cloud project (``jira://<site>/<KEY>``)."
)
servicenow_source = _factory(
    "deploy_servicenow",
    "servicenow_source",
    "``deploy_servicenow``: a ServiceNow table (``servicenow://<instance>/<table>``).",
)
gdrive_source = _factory(
    "deploy_gdrive",
    "gdrive_source",
    "``deploy_gdrive``: a Google Drive (``gdrive://<shared drive id>`` or ``gdrive://my-drive``).",
)
confluence_source = _factory(
    "deploy_confluence",
    "confluence_source",
    "``deploy_confluence``: a Confluence Cloud space (``confluence://<site>/<space id>``).",
)
rest_source = _factory(
    "deploy_rest",
    "rest_source",
    "``deploy_rest``: a declared REST CMMS or EAM API (``rest://<host>`` plus a ``profile``).",
)
