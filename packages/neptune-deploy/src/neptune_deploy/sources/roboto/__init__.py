"""Roboto connector: a dataset's files as Sources, its annotations as stated records (ADR 0009).

One ``neptune.sources`` entry point, ``deploy_roboto``, whose value is a factory with the signature
every connector in this package has (ADR 0006 §1):

    roboto_source(url, *, network, ledger=None, options=None, credentials=None, environ=None)
        -> RobotoSource

- ``url``: ``roboto://<org id>/<dataset id>/<path prefix>``.
- ``network``: the compiler's workspace (anything with ``require_network(purpose)``). It is asked
  before the source is built and before every request, so a local-only workspace refuses it.
- ``options``: declared options (``config.RobotoOptions``); ``credentials``:
  ``{"roboto_api_token": ...}``, else ``NEPTUNE_ROBOTO_API_TOKEN`` of ``environ``.

Importing this package touches nothing: no network, file or environment access until the factory is
called.
"""

import os
from collections.abc import Mapping

from neptune.identity.revisions import SourceLedger
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.transport import NetworkGate
from neptune_deploy.sources.roboto.client import RobotoApi
from neptune_deploy.sources.roboto.config import (
    CONNECTOR_ID,
    RobotoLocation,
    RobotoOptions,
    api_token,
    parse_url,
)
from neptune_deploy.sources.roboto.source import RobotoSource

__all__ = [
    "CONNECTOR_ID",
    "RobotoApi",
    "RobotoLocation",
    "RobotoOptions",
    "RobotoSource",
    "roboto_source",
]


def roboto_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RobotoSource:
    """``deploy_roboto``: the files and annotations of a Roboto dataset.

    A local-only workspace refuses it, as every connector (root ADR 0026 §6).
    """
    network.require_network(f"reading {CONNECTOR_ID} sources")
    location = parse_url(url)
    parsed = RobotoOptions.parse(options)
    token = api_token(credentials, os.environ if environ is None else environ)
    client = RobotoApi(location, parsed, network, token=token)
    return RobotoSource(location, client, network, parsed, ledger=ledger)
