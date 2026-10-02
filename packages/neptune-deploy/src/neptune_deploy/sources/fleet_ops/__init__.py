"""Fleet-operations connectors: Formant and Open-RMF as read-only sources (ADR 0010).

Each is one ``neptune.sources`` entry point, named by its connector id, whose value is a factory
with ADR 0006 §1's signature:

    factory(url, *, network, ledger=None, options=None, credentials=None, environ=None)

- ``deploy_formant`` (``formant_source``): ``formant://<organization id>``, over Formant's admin
  API. Devices, events, annotations, interventions and recording file records become documents
  with ``stated`` tables, and interventions also ``Intervention`` lifecycle records. The workspace
  is asked before the source is built and before every request.
- ``deploy_open_rmf`` (``open_rmf_source``): a local directory of Open-RMF logs (JSON, JSON Lines
  or SQLite read-only). Task logs, fleet states and dispatch records become documents with
  ``stated`` tables and ``Run`` declarations, and lane and zone maps spatial records. It uses no
  network, so it asks no workspace.

ROS 2 diagnostics are not a source: they are a mapper over a compiler package
(``neptune_deploy.diagnostics``). Importing this package touches nothing.
"""

from neptune_deploy.sources.fleet_ops.base import (
    Discovery,
    DocumentEntry,
    DocumentReadError,
    FleetOpsSource,
)
from neptune_deploy.sources.fleet_ops.citing import Catalog
from neptune_deploy.sources.fleet_ops.formant import FormantSource, formant_source
from neptune_deploy.sources.fleet_ops.options import FleetOpsConfigError
from neptune_deploy.sources.fleet_ops.rmf import OpenRmfSource, open_rmf_source
from neptune_deploy.sources.stated_records import CatalogDocument

__all__ = [
    "Catalog",
    "CatalogDocument",
    "Discovery",
    "DocumentEntry",
    "DocumentReadError",
    "FleetOpsConfigError",
    "FleetOpsSource",
    "FormantSource",
    "OpenRmfSource",
    "formant_source",
    "open_rmf_source",
]
