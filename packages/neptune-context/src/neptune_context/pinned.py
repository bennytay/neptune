"""Upstream vocabularies and definitions at the pinned contract versions (ADR 0006 §9).

What Context publishes depends on upstream vocabularies: the query schema's subject-kind and
predicate enums, the Memory definitions the packet schema embeds, and the planner's prompt. They
come from the registry versions declared in ``pins.py``, never from the owners' live code, so an
upstream release changes nothing Context publishes until Context bumps a pin, and that bump is a
reviewed Context change that regenerates this snapshot and Context's own exports with it.

``pinned.json`` is the snapshot: graph-schema's ``$defs``, predicate names and each predicate's
domain (subject node types) and range (object node or value types), and catalog-api's thread
kinds, copied from ``contracts/<contract>/v<pin>/``. It ships inside the package so a reader never
needs the repository's registry. Regenerate after a pin bump with
``uv run python -m neptune_context.pinned contracts
packages/neptune-context/src/neptune_context/pinned.json``; a test fails when it is stale.
"""

from __future__ import annotations

import copy
import functools
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neptune_context.pins import CATALOG_API_VERSION, GRAPH_SCHEMA_VERSION

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.nodes import NodeRef
    from neptune_memory.schema.supersede import ResolutionFinding

    from neptune.model.jsonvalue import JsonValue


def build(contracts: Path) -> bytes:
    """The snapshot's bytes, read from the registry at ``contracts`` at the pinned versions."""

    def read(contract: str, version: str, name: str) -> Any:
        return json.loads((contracts / contract / f"v{version}" / name).read_bytes())

    graph = read("graph-schema", GRAPH_SCHEMA_VERSION, "schema.json")
    vocabulary = read("graph-schema", GRAPH_SCHEMA_VERSION, "golden/vocabulary.json")
    catalog = read("catalog-api", CATALOG_API_VERSION, "schema.json")
    snapshot = {
        "catalog-api": {
            "thread_kinds": sorted(catalog["$defs"]["ThreadKey"]["properties"]["kind"]["enum"]),
            "version": CATALOG_API_VERSION,
        },
        "graph-schema": {
            "defs": graph["$defs"],
            "predicates": sorted(p["name"] for p in vocabulary["predicates"]),
            "signatures": {
                p["name"]: {"domain": sorted(p["domain"]), "range": sorted(p["range"])}
                for p in vocabulary["predicates"]
            },
            "version": GRAPH_SCHEMA_VERSION,
        },
    }
    text = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


@functools.cache
def _snapshot() -> Any:
    return json.loads(files("neptune_context").joinpath("pinned.json").read_bytes())


@functools.cache
def node_types() -> frozenset[str]:
    """graph-schema's node types at the pin."""
    return frozenset(_snapshot()["graph-schema"]["defs"]["NodeType"]["enum"])


@functools.cache
def predicates() -> frozenset[str]:
    """graph-schema's predicate names at the pin."""
    return frozenset(_snapshot()["graph-schema"]["predicates"])


@functools.cache
def signatures() -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """graph-schema's predicate signatures at the pin: name -> (domain, range)."""
    return {
        name: (frozenset(sig["domain"]), frozenset(sig["range"]))
        for name, sig in _snapshot()["graph-schema"]["signatures"].items()
    }


@functools.cache
def thread_kinds() -> frozenset[str]:
    """catalog-api's thread kinds at the pin."""
    return frozenset(_snapshot()["catalog-api"]["thread_kinds"])


@functools.cache
def value_types() -> frozenset[str]:
    """graph-schema's literal and record value types at the pin."""
    return frozenset(_snapshot()["graph-schema"]["defs"]["ValueType"]["enum"])


@functools.cache
def finding_codes() -> frozenset[str]:
    """graph-schema's resolver finding codes at the pin."""
    return frozenset(_snapshot()["graph-schema"]["defs"]["FindingCode"]["enum"])


def node_beyond_pin(node: NodeRef) -> str | None:
    """Why ``node`` is not describable at the pinned graph-schema, or ``None`` when it is."""
    if str(node.node_type) not in node_types():
        return f"node type {str(node.node_type)!r}"
    return None


def claim_beyond_pin(claim: Claim) -> str | None:
    """Why ``claim`` is not in the pinned graph-schema, or ``None`` when it is.

    Memory may run ahead of Context's pin (a predicate, node type or value type added in a
    minor release), and a document may hold a claim its own vocabulary forbids. The pinned packet
    schema does not describe either, so Context never passes it through: the engine reports it
    as a gap and the packet reader refuses it (ADR 0006 Consequences, ADR 0007 §6, ADR 0012 §9).
    Checks the predicate, the subject and, for an edge, the object node type, and for a literal
    its value type; then the predicate's pinned domain (subject type) and range (object type).
    """
    if claim.predicate not in predicates():
        return f"predicate {claim.predicate!r}"
    reason = node_beyond_pin(claim.subject)
    if reason is not None:
        return reason
    obj = claim.object
    node_type = getattr(obj, "node_type", None)
    if node_type is not None:
        reason = node_beyond_pin(obj)  # type: ignore[arg-type]
        return reason if reason is not None else _off_signature(claim)
    datatype = getattr(obj, "datatype", None)
    if datatype is not None and str(datatype) not in value_types():
        return f"value type {str(datatype)!r}"
    return _off_signature(claim)


def _off_signature(claim: Claim) -> str | None:
    signature = signatures().get(claim.predicate)
    if signature is None:  # a predicate the pin names without a signature: nothing to check
        return None
    domain, range_ = signature
    subject = str(claim.subject.node_type)
    if subject not in domain:
        return f"predicate {claim.predicate!r} on a {subject!r} subject (domain {sorted(domain)})"
    obj = claim.object
    kind = getattr(obj, "node_type", None) or getattr(obj, "datatype", None) or "record"
    if str(kind) not in range_:
        return f"predicate {claim.predicate!r} with a {str(kind)!r} object (range {sorted(range_)})"
    return None


def older_graph_notice(graph_schema_version: int) -> str | None:
    """The one sentence a renderer states when the graph read is an older graph-schema major than
    the pin (ADR 0012 §10), or ``None``. A 1.x document is read as written: its ``succeeds``
    still marks a chain change, which 2.0.0 no longer means."""
    pinned_major = int(GRAPH_SCHEMA_VERSION.split(".")[0])
    if graph_schema_version >= pinned_major:
        return None
    return (
        f"Graph read: graph-schema {graph_schema_version}.x, older than Context's pin"
        f" {GRAPH_SCHEMA_VERSION}; predicates such as succeeds carry their"
        f" {graph_schema_version}.x meaning."
    )


def finding_beyond_pin(finding: ResolutionFinding) -> str | None:
    """Why a resolver finding is newer than the pinned graph-schema, or ``None``."""
    if str(finding.code) not in finding_codes():
        return f"finding code {str(finding.code)!r}"
    return None


def graph_schema_defs() -> dict[str, JsonValue]:
    """A fresh copy of graph-schema's ``$defs`` at the pin (callers may not mutate the snapshot)."""
    defs: dict[str, JsonValue] = copy.deepcopy(_snapshot()["graph-schema"]["defs"])
    return defs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: python -m neptune_context.pinned <contracts-dir> <output.json>\n")
        return 2
    Path(argv[1]).write_bytes(build(Path(argv[0])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
