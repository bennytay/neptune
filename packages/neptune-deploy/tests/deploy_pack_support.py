"""Shared helpers for the pack compiler's tests: the fixture snapshots and specs over them."""

import json
from functools import cache
from pathlib import Path
from typing import Any, Final

from deploy_pack_graphs import CIVIL, DAY, REPO, T0, fixture_path
from neptune_deploy.packs import EvidencePack, PackSpec, Snapshot, compile_pack, load_snapshot
from neptune_deploy.packs.snapshot import Interval, Node, Stamp

CONTRACTS: Final = REPO / "contracts"
ARM: Final = Node("machine", "asset-tag:ARM-06")
AMR: Final = Node("machine", "asset-tag:AMR-12")
SITE: Final = Node("site", "site-code:CELL-3")
DEPLOYMENT: Final = Node("deployment", "deployment:cell-3-pilot")
FROM_T0: Final = Interval(Stamp(CIVIL, T0), "open")
FIRST_DAY: Final = Interval(Stamp(CIVIL, T0), Stamp(CIVIL, T0 + DAY))


@cache
def snapshot(name: str) -> Snapshot:
    return load_snapshot(fixture_path(name).read_bytes())


def configuration() -> Snapshot:
    return snapshot("arm_cell_configuration")


def events() -> Snapshot:
    return snapshot("arm_cell_events")


def spec(
    snap: Snapshot,
    template: str = "configuration-lineage",
    subject: Node = ARM,
    interval: Interval = FROM_T0,
    inference: str = "exclude",
    version: int = 1,
) -> PackSpec:
    return PackSpec(template, version, subject, interval, snap.id, inference)


def configuration_pack(**kwargs: Any) -> EvidencePack:
    snap = configuration()
    return compile_pack(spec(snap, **kwargs), snap)


def events_pack(**kwargs: Any) -> EvidencePack:
    snap = events()
    kwargs.setdefault("template", "event-timeline")
    kwargs.setdefault("interval", FIRST_DAY)
    return compile_pack(spec(snap, **kwargs), snap)


def plain(value: Any) -> Any:
    """A JSON value as plain, untyped Python data (for asserting on documents)."""
    return json.loads(json.dumps(value))


def contract(path: str) -> Any:
    return json.loads((CONTRACTS / path).read_text(encoding="utf-8"))


def schema_path(contract_id: str, version: str) -> Path:
    return CONTRACTS / contract_id / f"v{version}" / "schema.json"
