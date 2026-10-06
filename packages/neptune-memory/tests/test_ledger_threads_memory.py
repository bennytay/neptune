"""Thread answers in the Ledger reader and the export (ADR 0018 §1): the catalog-api 1.7.0 wire
form, read strictly; snapshots of the export never show a later package's membership."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from memory_catalog_threads import anchored, declared, package_id, subject
from memory_configuration_records import hardware, maintenance
from neptune.model.ids import LogicalId
from neptune_memory.ledger import (
    ExportedPackage,
    LedgerExport,
    ThreadsOf,
    ledger_export_from_json,
    threads_of_from_json,
)
from neptune_memory.pins import CATALOG_API_VERSION

CATALOG_API = Path(__file__).resolve().parents[3] / "contracts" / "catalog-api"
FIRST, SECOND = package_id("compiled"), package_id("lifecycle")
SNAPSHOT = hardware("CFG-ARM3A.yaml")
WORK_ORDER = maintenance("WO-26-0911", [LogicalId("cmms.asset", "ARM-3A")], None, None)
SNAPSHOT_KEY = anchored("configuration", SNAPSHOT)
SERIAL_KEY = declared("asset", LogicalId("serial", "SN-6700-118"))


def answer(*packages: str) -> ThreadsOf:
    """The snapshot's answer: subject of its configuration thread in each package, and of a
    declared asset thread in the first (two kinds of key, as the catalog may report)."""
    memberships = [subject(SNAPSHOT_KEY, p) for p in packages] + [subject(SERIAL_KEY, packages[0])]
    return ThreadsOf(
        str(SNAPSHOT["id"]),
        "found",
        tuple(sorted(memberships, key=lambda m: (m.thread_id, m.package_id))),
        (),
    )


def export() -> LedgerExport:
    return LedgerExport(
        2,
        CATALOG_API_VERSION,
        (
            ExportedPackage(FIRST, 7, 1, (SNAPSHOT,)),
            ExportedPackage(SECOND, 6, 2, (SNAPSHOT, WORK_ORDER)),
        ),
        tuple(
            sorted(
                (answer(FIRST, SECOND), ThreadsOf(str(WORK_ORDER["id"]), "found", (), ())),
                key=lambda t: t.record_id,
            )
        ),
    )


def wire(threads_of: ThreadsOf) -> dict[str, Any]:
    """The catalog's full response: the export's form plus the bookkeeping it drops."""
    return {
        **threads_of.to_json(),
        "api_version": CATALOG_API_VERSION,
        "as_of": {
            "knowledge": "known",
            "value": {"tx_seq": 2, "tx_time": "2026-10-06T00:00:00.000000Z"},
        },
        "findings": [],
    }


def test_an_answer_is_the_published_threads_of_without_its_bookkeeping() -> None:
    schema = json.loads((CATALOG_API / f"v{CATALOG_API_VERSION}" / "schema.json").read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/ThreadsOf"})
    for item in export().threads or ():
        validator.validate(wire(item))
        assert threads_of_from_json(item.to_json()) == item


def test_the_pin_is_a_published_stable_catalog_api_version() -> None:
    version = json.loads((CATALOG_API / f"v{CATALOG_API_VERSION}" / "version.json").read_text())
    assert (version["version"], version["status"]) == (CATALOG_API_VERSION, "stable")


def test_the_export_round_trips_with_and_without_threads() -> None:
    assert ledger_export_from_json(export().to_json()) == export()
    bare = LedgerExport(2, "records-only", export().packages)
    assert "threads" not in bare.to_json()
    assert ledger_export_from_json(bare.to_json()) == bare
    assert bare.at(2).threads_of(str(SNAPSHOT["id"])) is None  # not answered, never "no thread"


def test_a_snapshot_drops_memberships_and_records_of_later_packages() -> None:
    at_1, at_2 = export().at(1), export().at(2)
    snapshot_id, work_order_id = str(SNAPSHOT["id"]), str(WORK_ORDER["id"])
    assert at_2.threads_of(snapshot_id) == answer(FIRST, SECOND)
    assert at_1.threads_of(snapshot_id) == answer(FIRST)
    assert at_1.threads_of(work_order_id) == ThreadsOf(work_order_id, "unknown_record", (), ())
    assert at_2.threads_of(work_order_id) == ThreadsOf(work_order_id, "found", (), ())
    assert at_2.threads_of("rec:sha256:" + "f" * 64).status == "unknown_record"  # type: ignore[union-attr]


def _threads(change: Any) -> dict[str, Any]:
    data = copy.deepcopy(export().to_json())
    change(data["threads"])
    return data


def _first(threads: list[dict[str, Any]]) -> dict[str, Any]:
    return next(t for t in threads if t["memberships"])


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t.reverse(),
        lambda t: t.append(t[0]),
        lambda t: t[0].update(status="maybe"),
        lambda t: t[0].update(record_id=""),
        lambda t: t[0].update(api_version="1.7.0"),
        lambda t: t[0].pop("unresolved"),
        lambda t: t[0].update(memberships={}),
        lambda t: _first(t).update(status="unknown_record"),
        lambda t: _first(t)["memberships"].reverse(),
        lambda t: _first(t)["memberships"].append(_first(t)["memberships"][0]),
        lambda t: _first(t)["memberships"][0].update(roles=[]),
        lambda t: _first(t)["memberships"][0].update(roles=["subject", "subject"]),
        lambda t: _first(t)["memberships"][0].update(roles=["owner"]),
        lambda t: _first(t)["memberships"][0].update(thread_id="not-a-content-id"),
        lambda t: _first(t)["memberships"][0].update(package_id="package-0"),
        lambda t: _first(t)["memberships"][0].update(extra=1),
        lambda t: _first(t)["memberships"][0]["key"].update(kind="robot"),
        lambda t: _first(t)["memberships"][0]["key"].update(key={"namespace": "x"}),
        lambda t: _first(t)["memberships"][0]["key"].update(key={"source": "x", "locator": []}),
        lambda t: _first(t)["memberships"][0]["key"].update(
            key={"namespace": "Not A Token", "value": "x"}
        ),
        lambda t: t[0]["unresolved"].append({"thread_id": t[0]["record_id"]}),
        lambda t: _first(t)["memberships"][0]["key"].update(kind=[]),
        lambda t: _first(t)["memberships"][0].update(roles=[{}]),
        lambda t: t.pop(),  # a held record left unanswered would read as "not in the Ledger"
        lambda t: t.clear(),
    ],
)
def test_a_malformed_thread_answer_is_refused(change: Any) -> None:
    with pytest.raises(ValueError):
        ledger_export_from_json(_threads(change))


def test_a_threads_key_that_is_not_an_array_is_refused() -> None:
    data = copy.deepcopy(export().to_json())
    data["threads"] = {}
    with pytest.raises(ValueError):
        ledger_export_from_json(data)


def test_a_declared_key_names_its_node_in_any_role_an_anchored_one_only_as_subject() -> None:
    from memory_catalog_threads import thread_id
    from neptune_memory.consolidate.threads import catalog_threads
    from neptune_memory.ledger import Membership, StubLedger
    from neptune_memory.schema.nodes import NodeRef, NodeType

    machine = declared("machine", LogicalId("fleet", "ARM-3A"))
    cites = (
        Membership(thread_id(machine), machine, FIRST, ("cites",)),
        Membership(thread_id(SNAPSHOT_KEY), SNAPSHOT_KEY, FIRST, ("part_of",)),
    )
    rid = str(SNAPSHOT["id"])
    found = catalog_threads(
        StubLedger({}, "1.7.0", {rid: ThreadsOf(rid, "found", cites, ())}), [rid]
    )
    assert found.nodes == {NodeRef(NodeType.MACHINE, "fleet:ARM-3A")}
    assert found.opened == {} and found.anchor_only == set() and found.holds(rid) is True
