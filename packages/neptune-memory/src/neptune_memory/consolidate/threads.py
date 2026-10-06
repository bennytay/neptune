"""Nodes from the Ledger catalog's thread membership (ADR 0018).

The catalog API's ``threads_of(record_id)`` says which threads a record is a member of, and in
which role (Ledger ADR 0003 §2). A record that is the ``subject`` of a thread names that thread's
node:

- a **declared** key ``(kind, LogicalId)`` is ``node_ref(type, id)``, the node a ``ledger_thread``
  stand-in declaring the same id keys (ADR 0003 §1.1), so the two sources never key one id twice;
- an **anchored** key (record-level evidence) is the node of what the evidence declares: a run is
  ``record:<run record id>``, the runs consolidator's node (ADR 0009 §2); any other kind is
  ``thread:<thread id>``, a key the Ledger derives from the anchor alone, so a parser upgrade
  citing the same evidence keeps the node (ADR 0010, alternatives).

``cites`` and ``part_of`` memberships open no node here: a record that cites a thread names it
through its own declared field, and part-of is one hop the Ledger already resolved.

Readers that answer no thread queries (stand-in Ledgers, exports made before catalog-api 1.7.0)
contribute nothing, and the stand-in path decides alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune.model.ids import LogicalId
from neptune_memory.consolidate.identity import node_ref
from neptune_memory.consolidate.run_records import RECORD_NAMESPACE
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune.model.provenance import EvidenceRef
    from neptune_memory.ledger import LedgerReader, Membership

# The namespace of an anchored Ledger thread's node id; a declared id in it would forge one.
THREAD_NAMESPACE: Final = "thread"
RESERVED_NAMESPACES: Final = frozenset({RECORD_NAMESPACE, THREAD_NAMESPACE})

# Lifecycle kinds (root ADR 0051) no row of the Ledger's thread table reads (Ledger ADR 0003 §2,
# ADR 0010 Consequences): the catalog holds them in no thread. The ids they declare name nodes
# of their own once the Ledger holds the record (ADR 0018 §2).
UNTHREADED_KINDS: Final = frozenset(
    {
        "authorisation_envelope",
        "change_record",
        "commissioning_baseline",
        "maintenance_event",
        "requalification_record",
    }
)


def ref_key(ref: EvidenceRef) -> bytes:
    return canonical_json.dumps(ref.to_json())


def membership_node(membership: Membership, record: str) -> NodeRef | None:
    """The node ``record``'s subject membership names; ``None`` for one in a reserved namespace."""
    node_type = NodeType(membership.key.kind)
    declared = membership.key.declared
    if declared is not None:
        if declared.namespace in RESERVED_NAMESPACES:
            return None
        return node_ref(node_type, declared)
    if node_type is NodeType.RUN:
        return node_ref(NodeType.RUN, LogicalId(RECORD_NAMESPACE, record))
    return node_ref(node_type, LogicalId(THREAD_NAMESPACE, membership.thread_id))


@dataclass
class CatalogThreads:
    """What the catalog says about the records a consolidator read.

    ``nodes``: every node a declared subject membership names. ``anchors``: for an anchored one,
    ``(node type, anchor key) -> nodes``, the lookup the stand-in's ``evidence`` fed. ``held``:
    record id -> ``True`` if the Ledger holds it, ``False`` if not; absent when the reader
    answers no thread queries (then nothing here decides anything).
    """

    nodes: set[NodeRef] = field(default_factory=set)
    anchors: dict[tuple[NodeType, bytes], set[NodeRef]] = field(default_factory=dict)
    held: dict[str, bool] = field(default_factory=dict)

    def holds(self, record: str) -> bool | None:
        """``True`` / ``False``: the Ledger holds the record or not; ``None``: not answered."""
        return self.held.get(record)


def catalog_threads(ledger: LedgerReader, records: Iterable[str]) -> CatalogThreads:
    """The catalog's answers for ``records``, in record-id order (deterministic)."""
    found = CatalogThreads()
    for record in sorted(set(records)):
        answer = ledger.threads_of(record)
        if answer is None:
            continue
        found.held[record] = answer.status == "found"
        for membership in answer.memberships:
            if "subject" not in membership.roles:
                continue
            node = membership_node(membership, record)
            if node is None:
                continue
            anchor = membership.key.anchor
            if anchor is None:
                found.nodes.add(node)
            else:
                found.anchors.setdefault((node.node_type, ref_key(anchor)), set()).add(node)
    return found
