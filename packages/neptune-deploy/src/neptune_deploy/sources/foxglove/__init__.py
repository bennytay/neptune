"""Foxglove Data Platform connector: a recording index as a read-only compiler Source (ADR 0007).

One ``neptune.sources`` entry point, ``deploy_foxglove``, whose value is a factory with the same
signature as the object-store connectors' (ADR 0006 §1):

    foxglove_source(url, *, network, ledger=None, options=None, credentials=None, environ=None)
        -> FoxgloveSource

- ``url``: ``foxglove://<project id>``, or ``foxglove://-`` for every project the key can read.
- ``network``: the compiler's workspace (anything with ``require_network(purpose)``). It is asked
  before the source is built and before every request, so a local-only workspace refuses it.
- ``ledger``: the ingest root's ``SourceLedger``; with it, ``walk`` yields only new or changed
  recordings.
- ``options``: declared options (``config.Options``); ``credentials``: ``{"foxglove_api_key":
  ...}``, else ``NEPTUNE_FOXGLOVE_API_KEY`` of ``environ`` (``os.environ`` by default). No ambient
  credential is read.

Importing this package touches nothing: no network, file or environment access until a factory is
called.
"""

import os
from collections.abc import Mapping

from neptune.identity.revisions import SourceLedger
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.foxglove.client import FoxgloveClient, FoxgloveTransport
from neptune_deploy.sources.foxglove.config import (
    CONNECTOR_ID,
    FoxgloveConfigError,
    Options,
    api_key_for,
    endpoint_for,
    parse_url,
)
from neptune_deploy.sources.foxglove.declared import (
    DeclaredEntry,
    DeclaredRecording,
    DeclaredTopic,
)
from neptune_deploy.sources.foxglove.source import (
    FoxgloveDiscovery,
    FoxgloveSource,
    Recording,
    RecordingIndex,
    StreamEntry,
)
from neptune_deploy.sources.object_store.transport import NetworkGate

__all__ = [
    "CONNECTOR_ID",
    "DeclaredEntry",
    "DeclaredRecording",
    "DeclaredTopic",
    "FoxgloveConfigError",
    "FoxgloveDiscovery",
    "FoxgloveSource",
    "Recording",
    "RecordingIndex",
    "StreamEntry",
    "foxglove_source",
]


def foxglove_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> FoxgloveSource:
    """``deploy_foxglove``: the recordings of a Foxglove project (``foxglove://<project id>``)."""
    purpose = f"reading {CONNECTOR_ID} sources"
    network.require_network(purpose)
    project = parse_url(url)
    parsed = Options.parse(options)
    key = api_key_for(credentials, os.environ if environ is None else environ)
    transport = FoxgloveTransport(endpoint_for(parsed), network, purpose, timeout=parsed.timeout)
    client = FoxgloveClient(
        transport,
        key,
        network,
        purpose,
        link_hosts=parsed.link_hosts,
        compression=parsed.compression,
        timeout=parsed.timeout,
    )
    return FoxgloveSource(project, client, parsed, ledger=ledger)
