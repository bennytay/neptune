"""Graph tiers and typed nodes (ADR 0002 §1).

The Episode tier is the Ledger's records and evidence refs, referenced by id and never copied, so
it has no node types here; claims point into it with ``LedgerRecordRef`` and ``EvidenceRef``. The
Entity and Context tiers are typed nodes. A node is only its type and id: everything said about
it is a ``Claim``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from neptune.model.ids import check_text

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonObject


class Tier(StrEnum):
    EPISODE = "episode"  # Ledger records and evidence refs, by id; never nodes, never copied
    ENTITY = "entity"  # the things a robot programme is made of
    CONTEXT = "context"  # groupings of entities, each with a summary


class NodeType(StrEnum):
    # Entity tier
    MACHINE = "machine"  # any robot: arm, AMR, quadruped, humanoid, UAV, vessel, vehicle
    SENSOR = "sensor"
    SITE = "site"
    ZONE = "zone"
    ASSET = "asset"  # a non-robot physical thing: a fixture, a charger, a pallet, a hull
    TASK = "task"
    PERSON = "person"  # declared only: never created or linked by inference
    SOFTWARE_VERSION = "software_version"
    MODEL_VERSION = "model_version"
    CONFIGURATION = "configuration"  # calibrations, parameter sets, URDF revisions
    POLICY = "policy"  # an operating rule or a control policy
    RUN = "run"
    STREAM = "stream"  # one recorded stream of a run: a topic, a channel, a log message type
    DOCUMENT = "document"  # a declared document: a manual, an SOP, a datasheet, a register
    EPISODE = "episode"  # an entity: a bounded segment of a run, not the Episode tier
    CLOCK = "clock"  # one declared clock, keyed by its compiler TimestampDomain record id
    EVENT = "event"  # something one record states happened: an e-stop, a fault, an incident
    # Context tier
    DEPLOYMENT = "deployment"
    FLEET = "fleet"
    PROGRAMME = "programme"


TIER_OF: Final[Mapping[NodeType, Tier]] = MappingProxyType(
    {
        node_type: (
            Tier.CONTEXT
            if node_type in {NodeType.DEPLOYMENT, NodeType.FLEET, NodeType.PROGRAMME}
            else Tier.ENTITY
        )
        for node_type in NodeType
    }
)
ENTITY_TYPES: Final = frozenset(t for t, tier in TIER_OF.items() if tier is Tier.ENTITY)
CONTEXT_TYPES: Final = frozenset(t for t, tier in TIER_OF.items() if tier is Tier.CONTEXT)
# Node types no inferred claim may name, as subject or object (ADR 0002 §1).
DECLARED_ONLY: Final = frozenset({NodeType.PERSON})


@dataclass(frozen=True)
class NodeRef:
    """A node by type and id. ``node_id`` is opaque here; ``consolidate/`` derives it (MVL-103)."""

    node_type: NodeType
    node_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.node_type, NodeType):
            raise TypeError(f"node_type must be a NodeType, got {self.node_type!r}")
        if not isinstance(self.node_id, str):
            raise TypeError(f"node_id must be a str, got {type(self.node_id).__name__}")
        check_text("node_id", self.node_id)

    @property
    def tier(self) -> Tier:
        return TIER_OF[self.node_type]

    def to_json(self) -> JsonObject:
        return {"kind": "node", "node_id": self.node_id, "node_type": str(self.node_type)}
