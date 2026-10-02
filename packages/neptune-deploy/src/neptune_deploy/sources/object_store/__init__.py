"""Object-store connectors: S3-compatible stores, Google Cloud Storage and Azure Blob (ADR 0006).

Each provider is one ``neptune.sources`` entry point, named by its connector id, whose value is a
factory with one signature (ADR 0006 §1):

    factory(url, *, network, ledger=None, options=None, credentials=None, environ=None)
        -> ObjectStoreSource

- ``url``: ``s3://<bucket>/<prefix>``, ``gs://<bucket>/<prefix>`` or
  ``az://<account>/<container>/<prefix>``.
- ``network``: the compiler's workspace (anything with ``require_network(purpose)``). It is asked
  before the source is built and before every request, so a local-only workspace refuses it.
- ``ledger``: the ingest root's ``SourceLedger``; with it, ``walk`` yields only new or changed
  objects.
- ``options``: declared options (``config.Options``); ``credentials``: declared read-only
  credentials, else the ``NEPTUNE_*`` variables of ``environ`` (``os.environ`` by default).

Importing this package touches nothing: no network, file or environment access until a factory is
called.
"""

import os
from collections.abc import Mapping

from neptune.identity.revisions import SourceLedger
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.clients import (
    AzureBlobClient,
    GcsClient,
    Provider,
    S3Client,
    StoreClient,
)
from neptune_deploy.sources.object_store.config import (
    CONNECTOR_IDS,
    ObjectStoreConfigError,
    Options,
    credentials_for,
    endpoint_for,
    parse_url,
)
from neptune_deploy.sources.object_store.source import (
    Discovery,
    Listing,
    ObjectEntry,
    ObjectReader,
    ObjectReadError,
    ObjectStoreSource,
    SkippedObject,
)
from neptune_deploy.sources.object_store.transport import NetworkGate, Transport

__all__ = [
    "CONNECTOR_IDS",
    "Discovery",
    "Listing",
    "NetworkGate",
    "ObjectEntry",
    "ObjectReadError",
    "ObjectReader",
    "ObjectStoreConfigError",
    "ObjectStoreSource",
    "Provider",
    "SkippedObject",
    "azure_source",
    "gcs_source",
    "object_store_source",
    "s3_source",
]


def object_store_source(
    provider: Provider,
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ObjectStoreSource:
    """The source ``url`` names on ``provider``'s store; a local-only workspace refuses it."""
    connector = CONNECTOR_IDS[provider]
    network.require_network(f"reading {connector} sources")
    location = parse_url(url, provider)
    parsed = Options.parse(options, provider)
    found = credentials_for(
        provider,
        credentials,
        os.environ if environ is None else environ,
        anonymous=parsed.anonymous,
    )
    endpoint, addressing = endpoint_for(location, parsed)
    transport = Transport(endpoint, network, f"reading {connector} sources", timeout=parsed.timeout)
    client: StoreClient
    if provider is Provider.S3:
        client = S3Client(
            transport,
            location.bucket,
            addressing=addressing,
            region=parsed.region,
            credentials=found.aws,
            versions=parsed.versions,
        )
    elif provider is Provider.GCS:
        client = GcsClient(transport, location.bucket, access_token=found.gcs_token)
    else:
        client = AzureBlobClient(transport, location.bucket, sas=found.azure_sas or ())
    return ObjectStoreSource(location, client, network, parsed, ledger=ledger)


def s3_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ObjectStoreSource:
    """``deploy_s3``: an S3 or S3-compatible bucket prefix (``s3://<bucket>/<prefix>``)."""
    return object_store_source(
        Provider.S3,
        url,
        network=network,
        ledger=ledger,
        options=options,
        credentials=credentials,
        environ=environ,
    )


def gcs_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ObjectStoreSource:
    """``deploy_gcs``: a Google Cloud Storage bucket prefix (``gs://<bucket>/<prefix>``)."""
    return object_store_source(
        Provider.GCS,
        url,
        network=network,
        ledger=ledger,
        options=options,
        credentials=credentials,
        environ=environ,
    )


def azure_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ObjectStoreSource:
    """``deploy_azure_blob``: an Azure Blob container prefix (``az://<account>/<container>/<p>``)."""
    return object_store_source(
        Provider.AZURE,
        url,
        network=network,
        ledger=ledger,
        options=options,
        credentials=credentials,
        environ=environ,
    )
