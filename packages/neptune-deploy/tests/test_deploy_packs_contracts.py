"""The pack compiler against the published contracts: fixtures are graph-schema documents, the
appendix's requests are catalog-api documents, and Deploy pins the versions it reads."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from deploy_pack_graphs import GENERATORS, fixture_bytes, fixture_path
from deploy_pack_support import (
    CONTRACTS,
    SITE,
    configuration_pack,
    events_pack,
    plain,
    schema_path,
)
from neptune_deploy.packs.appendix import CATALOG_API_VERSION, resolution

GRAPH_SCHEMA_PIN = "1.2.0"
EVENTS_SCHEMA = "1.6.0"  # published by MVL-134 (PR #125)
DOCS = Path(__file__).resolve().parents[1] / "docs"


def _validator(path: Path, pointer: str, patch: Any = None) -> Draft202012Validator:
    schema = json.loads(path.read_text(encoding="utf-8"))
    if patch is not None:
        patch(schema)
    return Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": f"#/$defs/{pointer.rsplit('/', 1)[-1]}"}
    )


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_fixture_files_are_what_the_generator_writes(name: str) -> None:
    assert fixture_path(name).read_bytes() == fixture_bytes(name)


def test_the_configuration_snapshot_is_a_graph_schema_document() -> None:
    document = json.loads(fixture_path("arm_cell_configuration").read_bytes())
    _validator(schema_path("graph-schema", GRAPH_SCHEMA_PIN), "Graph").validate(document)


def _with_event_node_type(schema: dict[str, Any]) -> None:
    """graph-schema 1.6.0 differs from 1.2.0, in what the event fixture uses, by the node type
    ``event`` (and the ``EventKind`` enum, which no schema reference requires)."""
    node_types = schema["$defs"]["NodeType"]["enum"]
    if "event" not in node_types:
        node_types.append("event")


def test_the_events_snapshot_is_a_graph_schema_document_of_the_event_minor() -> None:
    document = json.loads(fixture_path("arm_cell_events").read_bytes())
    published = schema_path("graph-schema", EVENTS_SCHEMA)
    if published.exists():
        _validator(published, "Graph").validate(document)
    else:  # until #125 merges: 1.2.0 plus the one node type 1.6.0 adds
        _validator(
            schema_path("graph-schema", GRAPH_SCHEMA_PIN), "Graph", _with_event_node_type
        ).validate(document)
        with pytest.raises(Exception, match="event"):
            _validator(schema_path("graph-schema", GRAPH_SCHEMA_PIN), "Graph").validate(document)


@pytest.mark.skipif(
    not schema_path("graph-schema", EVENTS_SCHEMA).exists(),
    reason="graph-schema 1.6.0 lands with MVL-134 (PR #125); then this runs unpatched",
)
def test_event_kinds_are_the_published_enum() -> None:
    schema = json.loads(schema_path("graph-schema", EVENTS_SCHEMA).read_text("utf-8"))
    kinds = set(schema["$defs"]["EventKind"]["enum"])
    document = json.loads(fixture_path("arm_cell_events").read_bytes())
    used = {c["object"]["value"] for c in document["claims"] if c["predicate"] == "event_kind"}
    assert used <= kinds


def test_appendix_requests_are_catalog_api_documents() -> None:
    schema = schema_path("catalog-api", CATALOG_API_VERSION)
    validators = {
        "resolve": _validator(schema, "ResolveRequest"),
        "threads_of": _validator(schema, "ThreadsOfRequest"),
        "lineage": _validator(schema, "LineageRequest"),
    }
    seen = set()
    for pack in (configuration_pack(subject=SITE, inference="include"), events_pack()):
        for item in (*pack.appendix.evidence, *pack.appendix.records):
            resolve = item["resolve"]
            assert isinstance(resolve, dict)
            if resolve["via"] != "ledger":
                continue
            assert resolve["api_version"] == CATALOG_API_VERSION
            for call in resolve["calls"]:
                validators[call["call"]].validate(call["request"])
                seen.add(call["call"])
    assert seen == set(validators)


def test_a_graph_at_head_zero_pins_no_transaction() -> None:
    evidence = plain(configuration_pack().appendix.to_json())["evidence"]
    ref = next(e["ref"] for e in evidence if e["resolve"]["via"] == "ledger")
    assert "as_of" in plain(resolution(ref, 1))["calls"][0]["request"]
    assert "as_of" not in plain(resolution(ref, 0))["calls"][0]["request"]


def test_deploy_pins_the_contracts_it_reads() -> None:
    lock = tomllib.loads((CONTRACTS / "lock.toml").read_text(encoding="utf-8"))["neptune-deploy"]
    assert lock["graph-schema"] == GRAPH_SCHEMA_PIN
    assert lock["catalog-api"] == CATALOG_API_VERSION
    table = (DOCS / "contracts.md").read_text(encoding="utf-8")
    assert re.search(
        rf"`graph-schema` \| `neptune-memory` \| \*\*{re.escape(GRAPH_SCHEMA_PIN)}\*\*", table
    )
    assert re.search(
        rf"`catalog-api` \| `neptune-ledger` \| \*\*{re.escape(CATALOG_API_VERSION)}\*\*", table
    )
