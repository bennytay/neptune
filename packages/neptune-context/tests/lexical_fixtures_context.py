"""A small multi-embodiment text corpus for the lexical channel's tests (ADR 0008).

Claims go through Memory's real resolver (``supersede.resolve`` under ``CORE_PREDICATES``) and its
``ReferenceReader``, so supersession, findings and snapshots behave as they do in production.
Passages carry real compiler evidence refs and transform records. Robots: a UR10e arm cell, an AMR
fleet, a humanoid, a quadruped, a UAV and an ROV.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from neptune_memory.schema.claim import (
    Claim,
    ClaimAssertionKind,
    ClaimProvenance,
    ModelRef,
    TypedLiteral,
    ValueType,
)
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES, PredicateRegistry
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import resolve, resolver_config

from neptune.model.ids import ConfigHash, ContentId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Page, Span
from neptune.model.time import Timestamp
from neptune_context.packets.model import ItemProvenance, Transform
from neptune_context.query.decode import accept
from neptune_context.query.model import (
    Budget,
    Query,
    TextChannel,
    TextClause,
    TextField,
)
from neptune_context.retrieve.channel import Retrieval, Snapshot
from neptune_context.retrieve.lexical import LexicalChannel, LexicalCorpus, Passage

CLOCK = RecordId("rec:sha256:" + "11" * 32)
CONFIG = ConfigHash("sha256:" + "22" * 32)
MODEL = ModelRef("summariser", "3")
STATED = AssertionKind.STATED
OBSERVED = AssertionKind.OBSERVED
INFERRED: ClaimAssertionKind = "inferred"
PRIORITIES = {"memory.test": 0}


def digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def evidence(label: str, *steps: Page | Span) -> EvidenceRef:
    return EvidenceRef(ContentId(f"sha256:{digest(label)}"), (ByteRange(0, 4096), *steps))


def record(label: str) -> RecordId:
    return RecordId(f"rec:sha256:{digest('record:' + label)}")


def node(kind: NodeType, node_id: str) -> NodeRef:
    return NodeRef(kind, node_id)


def text(value: str) -> TypedLiteral:
    return TypedLiteral(ValueType.TEXT, value)


def claim(
    subject: NodeRef,
    predicate: str,
    value: str,
    *,
    tx: int,
    kind: ClaimAssertionKind = STATED,
    label: str = "",
    start: int = 0,
) -> Claim:
    inferred = kind == INFERRED
    return Claim(
        subject=subject,
        predicate=predicate,
        object=text(value),
        valid_from=Timestamp(start, CLOCK),
        valid_to=OPEN,
        recorded_at=ledger_tx(tx),
        assertion_kind=kind,
        confidence=Known(0.8) if inferred else NotApplicable(),
        provenance=ClaimProvenance(
            evidence=(evidence(label or f"{subject.node_id}:{predicate}:{tx}"),),
            records=(),
            consolidator_id="memory.test",
            consolidator_version="1",
            config_hash=CONFIG,
            model=MODEL if inferred else None,
        ),
    )


# --- Claims: one per embodiment -----------------------------------------------------------------

ARM = node(NodeType.ASSET, "asset_tag:ur10e-04")
AGV_STOP = node(NodeType.EVENT, "event_log:estop-0031")
ROV_STALL = node(NodeType.EVENT, "event_log:rov07-stall")
UAV_BREACH = node(NodeType.EVENT, "event_log:uav21-geofence")
HUMANOID = node(NodeType.MACHINE, "asset_tag:hx-02")
QUADRUPED = node(NodeType.ASSET, "asset_tag:go2-lab-01")
FLEET = node(NodeType.FLEET, "fleet_registry:amr-north")


def claims() -> tuple[Claim, ...]:
    return (
        claim(ARM, "maintenance_state", "gripper pad worn; replace before next shift", tx=1),
        claim(ARM, "maintenance_state", "gripper pad replaced and calibrated", tx=4),
        claim(
            AGV_STOP,
            "has_description",
            "E-stop on AGV 114 while crossing aisle 3: lidar dropout, serial SN-A4471-9",
            tx=2,
        ),
        claim(
            ROV_STALL,
            "has_description",
            "Thrusters stalled during descent at 42 m",
            tx=2,
            kind=OBSERVED,
        ),
        claim(
            UAV_BREACH,
            "has_description",
            "Geofence breach; imu drift seen on /uav21/imu/data before the breach",
            tx=3,
        ),
        claim(HUMANOID, "maintenance_state", "pending torque recalibration of left knee", tx=2),
        claim(QUADRUPED, "maintenance_state", "left rear foot sensor replaced", tx=3),
        claim(
            FLEET,
            "has_summary",
            "North fleet ran 2.4.1-rc3 overnight; a retrofit batch shows brownout correlation",
            tx=3,
            kind=INFERRED,
        ),
    )


def document(
    history: tuple[Claim, ...] | None = None,
    head: int = 4,
    registry: PredicateRegistry = CORE_PREDICATES,
) -> GraphDocument:
    resolution = resolve(history or claims(), registry, PRIORITIES)
    return GraphDocument(resolution, resolver_config(registry, PRIORITIES), ledger_tx(head))


# --- Passages: Ledger records, with provenance -----------------------------------------------


def transform(adapter: str = "pdf-text") -> Transform:
    return Transform(adapter, "1.0.0", CONFIG)


def passage(
    field: TextField,
    label: str,
    body: str,
    *,
    registered_at: int = 1,
    kind: ClaimAssertionKind = STATED,
) -> Passage:
    anchor = evidence(label, Page(2), Span(0, len(body)))
    inferred = kind == INFERRED
    provenance = ItemProvenance(
        (anchor,),
        (record(label),),
        transform(),
        MODEL if inferred else None,
    )
    return Passage(
        field=field,
        document=record(label),
        text=body,
        evidence=anchor,
        provenance=provenance,
        registered_at=registered_at,
        assertion_kind=kind,
        confidence=Unknown() if inferred else NotApplicable(),
    )


SOP = passage(
    TextField.DOCUMENT,
    "sop-cell-entry",
    "2. Lock out the breaker and tag out the pendant before entering the cell. "
    "3. Verify zero energy at the controller.",
)
SOP_OTHER = passage(
    TextField.DOCUMENT,
    "sop-charging",
    "Tag the charger and lock the dock door out of service during battery swaps.",
)
REGISTER = passage(
    TextField.RECORD,
    "amr-register",
    "AGV 114 asset register: serial SN-A4471-9, firmware 2.4.1-rc3, north fleet",
)
REGISTER_OTHER = passage(
    TextField.RECORD,
    "amr-register-2",
    "AGV 115 asset register: serial SN-A4471-7, firmware 2.4.1-rc3, north fleet",
)
TOPICS = passage(
    TextField.RECORD,
    "uav-channels",
    "channels: /uav21/imu/data /uav21/camera/front/image_raw /tf",
)
CHUNK_FINDING = passage(
    TextField.FINDING,
    "go2-chunk-finding",
    "truncated_chunk: MCAP chunk 12 of /joint_states ends mid-record",
)
CAPTION = passage(
    TextField.RECORD,
    "cell-caption",
    "pallet jack blocking the cobalt-blue fixture beside the arm",
    kind=INFERRED,
)
LATE = passage(
    TextField.DOCUMENT, "sop-late", "Lanyard inspection after every dive", registered_at=9
)


def passages() -> tuple[Passage, ...]:
    return (SOP, SOP_OTHER, REGISTER, REGISTER_OTHER, TOPICS, CHUNK_FINDING, CAPTION, LATE)


# --- Assembly -----------------------------------------------------------------------------------

ALL_FIELDS = frozenset(TextField)


@dataclass(frozen=True)
class World:
    corpus: LexicalCorpus
    channel: LexicalChannel
    reader: ReferenceReader
    head: int
    history: tuple[Claim, ...]


def world(*, head: int = 4, doc: GraphDocument | None = None, with_passages: bool = True) -> World:
    doc = doc or document(head=head)
    corpus = LexicalCorpus()
    corpus.add_claims(doc.resolution.claims, through=doc.head)
    if with_passages:
        corpus.add_passages(passages())
    reader = ReferenceReader(doc)
    return World(corpus, LexicalChannel(corpus, reader), reader, head, doc.resolution.claims)


def retrieval(
    body: str,
    *,
    fields: frozenset[TextField] = ALL_FIELDS,
    inferred: bool = False,
    as_of: int | None = None,
    head: int = 4,
    items: int = 50,
    channels: frozenset[TextChannel] = frozenset({TextChannel.LEXICAL}),
) -> Retrieval:
    query = accept(
        Query(
            include_inferred=inferred,
            budget=Budget(items=items),
            text=TextClause(body, fields, channels),
        )
    )
    assert isinstance(query, Query), query
    at = ledger_tx(head if as_of is None else as_of)
    return Retrieval(query, Snapshot(at, ledger_tx(head), at))
