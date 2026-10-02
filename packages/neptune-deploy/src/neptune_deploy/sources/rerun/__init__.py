"""Rerun Hub connector: a dataset's ``.rrd`` objects as Sources, its catalog as stated records.

One ``neptune.sources`` entry point, ``deploy_rerun``, whose value is a factory with the signature
every connector in this package has (ADR 0006 §1):

    rerun_source(url, *, network, ledger=None, options=None, credentials=None, environ=None)
        -> RerunSource

- ``url``: the path of a catalog export file (``export.py``), plain or as ``file:///...``.
- ``network``: the compiler's workspace; asked before the source is built and before every request.
- ``options``: ``storage``, ``timeline_clocks``, ``max_objects``, ``max_export_bytes``
  (``options.RerunOptions``); ``credentials``: the object-store credential names, else
  ``NEPTUNE_*``.

Importing this package touches nothing: no network, file or environment access until the factory is
called.
"""

import os
import urllib.parse
from collections.abc import Mapping

from neptune.identity.revisions import SourceLedger
from neptune.model.jsonvalue import JsonValue
from neptune_deploy.sources.object_store.config import ObjectStoreConfigError
from neptune_deploy.sources.object_store.transport import NetworkGate
from neptune_deploy.sources.rerun.export import RerunExport, parse_export, read_export
from neptune_deploy.sources.rerun.options import CONNECTOR_ID, RerunOptions
from neptune_deploy.sources.rerun.source import RerunSource, parse_storage_url

__all__ = [
    "CONNECTOR_ID",
    "RerunExport",
    "RerunOptions",
    "RerunSource",
    "parse_storage_url",
    "rerun_source",
]


def _path(url: str) -> str:
    if not isinstance(url, str) or not url or "\x00" in url:
        raise ObjectStoreConfigError("a catalog export is named by a file path")
    if url.startswith("file://"):
        parts = urllib.parse.urlsplit(url)
        if parts.netloc not in ("", "localhost"):
            raise ObjectStoreConfigError("a file:// URL names this machine's files only")
        return urllib.parse.unquote(parts.path)
    return url


def rerun_source(
    url: str,
    *,
    network: NetworkGate,
    ledger: SourceLedger | None = None,
    options: Mapping[str, JsonValue] | None = None,
    credentials: Mapping[str, str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> RerunSource:
    """``deploy_rerun``: the ``.rrd`` objects a Rerun Hub dataset's catalog export names.

    A local-only workspace refuses it, as every connector (root ADR 0026 §6): resolving the objects
    needs their stores.
    """
    network.require_network(f"reading {CONNECTOR_ID} sources")
    parsed = RerunOptions.parse(options)
    export = parse_export(read_export(_path(url), limit=parsed.max_export_bytes))
    return RerunSource(
        export,
        parsed,
        network,
        credentials,
        os.environ if environ is None else environ,
        ledger=ledger,
    )
