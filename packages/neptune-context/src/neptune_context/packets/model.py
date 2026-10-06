"""The context packet (ADR 0003): a typed, provenance-carrying, content-addressed answer.

A ``ContextPacket`` is the record a query returns. Its header says which question it answers
(``query_id``, an opaque hash of the query's canonical JSON), at which snapshot (``as_of``,
Memory's generation and snapshot, the catalog API version) and within which budget; its items
say what was found, each with its provenance, ``assertion_kind``, confidence and the relevance
that put it there. Nothing here retrieves, ranks or renders: this module only fixes what a packet
may contain and refuses anything else (``PacketError``), so every packet in memory is valid.

Evidence is not interpretation: an item is ``observed``, ``stated`` or ``inferred``, an inferred
item names its model and carries a confidence, and a packet whose header says
``inference_included = false`` cannot hold one. Missingness is explicit: a field the evidence
does not settle is a ``Knowledge`` state, and a part of the question the sources could not answer
is a ``Gap``; a packet never answers with silence.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, Final, TypeAlias

from neptune_memory.schema.claim import (
    Claim,
    ClaimAssertionKind,
    ClaimId,
    ClaimProvenance,
    ModelRef,
    is_inferred,
    parse_claim_id,
)
from neptune_memory.schema.interval import LedgerTx, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.supersede import ResolutionFinding

from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.frames import FrameRef
from neptune.model.ids import (
    ConfigHash,
    ContentId,
    RecordId,
    check_text,
    check_token,
    check_verbatim,
    parse_config_hash,
    parse_content_id,
    parse_record_id,
)
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Inherited,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
    to_json,
)
from neptune.model.provenance import EvidenceRef
from neptune.model.time import INT64_MAX, INT64_MIN, Timestamp
from neptune_context.packets.findings import PacketError
from neptune_context.packets.findings import PacketFindingCode as Code
from neptune_context.packets.trails import (
    MAX_TRAILS,
    TRAIL_KINDS,
    Trail,
    WhyTrail,
    trail_index,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from neptune.model.jsonvalue import JsonObject, JsonValue

# The packet document's wire version. Every packet's JSON carries it as ``packet_version``; a
# reader refuses any other value. Raise it only for a change an older reader would misread.
PACKET_VERSION: Final = 1
PACKET_KIND: Final = "context_packet"
ITEM_ID_SCHEME: Final = "neptune-context.item-id/1"
PACKET_ID_SCHEME: Final = "neptune-context.packet-id/1"
# How ``BudgetUse.tokens`` is counted: ceil(bytes / 4) of the items' canonical JSON. An estimate
# that needs no model and gives the same number everywhere; a renderer that counts real tokens
# for one model does so after the packet, against its own limit.
TOKENIZER: Final = "neptune-context.utf8-bytes-div-4/1"
MAX_ITEMS: Final = 10_000
MAX_PACKET_BYTES: Final = 64 * 1024 * 1024
MAX_DETAIL_CHARS: Final = 2000
_QUERY_ID: Final = re.compile(r"query:sha256:[0-9a-f]{64}")
_SEMVER: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SERIES_PATH_PREFIX: Final = "series/"


def _fail(code: Code, message: str) -> PacketError:
    return PacketError(code, message)


def _sorted_unique(keys: Sequence[Any], what: str) -> None:
    """``keys`` (one per member, in member order) are unique and ascending."""
    if len(set(keys)) != len(keys):
        raise _fail(Code.DUPLICATE, f"{what} repeat")
    if list(keys) != sorted(keys):
        raise _fail(Code.ORDER, f"{what} must be sorted")


def _ids(values: Sequence[str], what: str) -> None:
    _sorted_unique(values, what)


def _tx(value: int, what: str) -> LedgerTx:
    try:
        return ledger_tx(value)
    except (TypeError, ValueError) as exc:
        raise _fail(Code.BAD_VALUE, f"{what}: {exc}") from exc


def _count(value: int, what: str, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= INT64_MAX:
        raise _fail(Code.BAD_VALUE, f"{what} must be an integer in [{low}, 2^63)")
    return value


def _ticks(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not INT64_MIN <= value <= INT64_MAX:
        raise _fail(Code.BAD_VALUE, f"{what} must be an int64 tick count")
    return value


def _text(value: str, what: str) -> str:
    """Non-empty text with a canonical form (valid Unicode)."""
    try:
        if not isinstance(value, str):
            raise TypeError(f"must be a string, got {type(value).__name__}")
        return check_text(what, value)
    except (TypeError, ValueError) as exc:
        raise _fail(Code.BAD_VALUE, f"{what}: {exc}") from exc


def _score(value: float, what: str) -> float:
    # A float, never an int: canonical JSON writes 1 and 1.0 differently, and a score is real.
    if type(value) is not float or not math.isfinite(value) or value < 0.0:
        raise _fail(Code.BAD_VALUE, f"{what} must be a finite float >= 0, got {value!r}")
    return value


def _check_inherited(knowledge: Knowledge[object], what: str) -> None:
    """An item's ``Knowledge`` fields rest on the item's own provenance (ADR 0003 §3)."""
    if isinstance(knowledge, KnownAbsent):
        raise _fail(Code.BAD_VALUE, f"{what}: KnownAbsent needs its own grounding; use Unknown")
    if not isinstance(knowledge, Known | Unknown | NotCovered | NotApplicable | Ambiguous):
        raise _fail(Code.SHAPE, f"{what} must be a Knowledge state, got {knowledge!r}")
    slots = (
        [c.provenance for c in knowledge.candidates]
        if isinstance(knowledge, Ambiguous)
        else []
        if isinstance(knowledge, NotApplicable)
        else [knowledge.provenance]
    )
    if not all(isinstance(slot, Inherited) for slot in slots):
        raise _fail(Code.BAD_VALUE, f"{what} inherits the item's provenance (INHERITED)")


def _knowledge_json(
    knowledge: Knowledge[object], encode: Callable[[object], JsonValue]
) -> JsonValue:
    return to_json(knowledge, encode)


def _plain(value: object) -> JsonValue:
    return value  # type: ignore[return-value]


def _node_key(node: NodeRef) -> tuple[str, str]:
    return (str(node.node_type), node.node_id)


def series_path(stream: RecordId) -> str:
    """The compiler's package path for a stream's rows: ``series/<64 hex>.parquet``."""
    return f"{SERIES_PATH_PREFIX}{parse_record_id(stream).removeprefix('rec:sha256:')}.parquet"


# --- Relevance --------------------------------------------------------------------------------


class Channel(StrEnum):
    """The retrieval channels. Peers: no channel ranks ahead of another by construction."""

    CATALOG = "catalog"  # Ledger catalog: records, threads, lineage, evidence resolution
    GRAPH = "graph"  # Memory claim graph: claims, neighbours, as_of
    LEXICAL = "lexical"  # BM25 over text-bearing records and claims
    SPATIAL = "spatial"  # frames, regions, sites and zones
    VECTOR = "vector"  # embedding similarity: a score, never a claim


@dataclass(frozen=True)
class ChannelHit:
    """One channel's opinion of an item: its 1-based rank in that channel and its raw score."""

    channel: Channel
    rank: int
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.channel, Channel):
            raise _fail(Code.BAD_VALUE, f"channel must be a Channel, got {self.channel!r}")
        _count(self.rank, "rank", low=1)
        _score(self.score, "a channel score")

    def to_json(self) -> JsonObject:
        return {"channel": str(self.channel), "rank": self.rank, "score": self.score}


@dataclass(frozen=True)
class Relevance:
    """Why the item is in the packet: the fused ``score`` and every channel that found it.

    Relevance is about the question, not the world: it never enters an item's id, and a high
    score never makes an inferred item less inferred.
    """

    score: float
    hits: tuple[ChannelHit, ...]

    def __post_init__(self) -> None:
        _score(self.score, "the fused score")
        if not isinstance(self.hits, tuple) or not self.hits:
            raise _fail(Code.BAD_VALUE, "relevance names at least one channel hit")
        for hit in self.hits:
            if not isinstance(hit, ChannelHit):
                raise _fail(Code.SHAPE, f"hits must be ChannelHits, got {hit!r}")
        _sorted_unique([str(h.channel) for h in self.hits], "relevance channels")

    def to_json(self) -> JsonObject:
        return {"hits": [h.to_json() for h in self.hits], "score": self.score}


# --- Provenance -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Transform:
    """What produced the item's content: a compiler adapter, a Memory consolidator, or a Ledger
    component, by id, version and resolved-config hash. Never the Context engine itself: the
    engine selects evidence, it does not author it (that is ``ContextPacket.produced_by``)."""

    producer_id: str
    producer_version: str
    config_hash: ConfigHash

    def __post_init__(self) -> None:
        try:
            check_token("producer_id", self.producer_id)
            check_text("producer_version", self.producer_version)
            parse_config_hash(self.config_hash)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"transform: {exc}") from exc

    def to_json(self) -> JsonObject:
        return {
            "config_hash": self.config_hash,
            "producer_id": self.producer_id,
            "producer_version": self.producer_version,
        }


@dataclass(frozen=True)
class ItemProvenance:
    """What an item rests on: at least one evidence ref (in the producer's order, unique), the
    Ledger records read (unique, sorted), the transform, and the model exactly when inferred."""

    evidence: tuple[EvidenceRef, ...]
    records: tuple[RecordId, ...]
    transform: Transform
    model: ModelRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise _fail(Code.BAD_VALUE, "an item cites at least one EvidenceRef")
        for ref in self.evidence:
            if not isinstance(ref, EvidenceRef):
                raise _fail(Code.SHAPE, f"evidence must be EvidenceRefs, got {ref!r}")
        if len(set(self.evidence)) != len(self.evidence):
            raise _fail(Code.DUPLICATE, "evidence refs repeat")
        if not isinstance(self.records, tuple):
            raise _fail(Code.SHAPE, "records must be a tuple of record ids")
        for record in self.records:
            try:
                parse_record_id(record)
            except (TypeError, ValueError) as exc:
                raise _fail(Code.BAD_VALUE, str(exc)) from exc
        _ids(self.records, "provenance records")
        if not isinstance(self.transform, Transform):
            raise _fail(Code.SHAPE, f"transform must be a Transform, got {self.transform!r}")
        if self.model is not None and not isinstance(self.model, ModelRef):
            raise _fail(Code.SHAPE, f"model must be a ModelRef or None, got {self.model!r}")

    @classmethod
    def of_claim(cls, provenance: ClaimProvenance) -> ItemProvenance:
        """A Memory claim's provenance, verbatim: its consolidator is the item's transform."""
        return cls(
            evidence=provenance.evidence,
            records=provenance.records,
            transform=Transform(
                provenance.consolidator_id,
                provenance.consolidator_version,
                provenance.config_hash,
            ),
            model=provenance.model,
        )

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "evidence": [ref.to_json() for ref in self.evidence],
            "records": list(self.records),
            "transform": self.transform.to_json(),
        }
        if self.model is not None:
            out["model"] = self.model.to_json()
        return out


def _check_assertion_kind(kind: object) -> None:
    if isinstance(kind, AssertionKind) or (type(kind) is str and kind == "inferred"):
        return
    raise _fail(Code.BAD_VALUE, f"assertion_kind is observed, stated or 'inferred': {kind!r}")


def _check_epistemics(
    kind: ClaimAssertionKind, confidence: Knowledge[float], provenance: ItemProvenance
) -> None:
    """Memory's rule, for every item: deterministic content has no confidence and no model;
    inferred content names its model and has ``Known(p)``, 0 <= p <= 1, or ``Unknown``."""
    _check_assertion_kind(kind)
    _check_inherited(confidence, "confidence")  # type: ignore[arg-type]
    if not is_inferred(kind):
        if not isinstance(confidence, NotApplicable):
            raise _fail(Code.ASSERTION_MISMATCH, f"a {kind} item's confidence is NotApplicable")
        if provenance.model is not None:
            raise _fail(Code.ASSERTION_MISMATCH, f"a {kind} item names no model")
        return
    if provenance.model is None:
        raise _fail(Code.ASSERTION_MISMATCH, "an inferred item names its model in provenance")
    if isinstance(confidence, Unknown):
        return
    if not isinstance(confidence, Known):
        raise _fail(Code.ASSERTION_MISMATCH, "an inferred item's confidence is Known or Unknown")
    value = confidence.value
    if type(value) is not float or not 0.0 <= value <= 1.0:
        raise _fail(Code.ASSERTION_MISMATCH, f"confidence is a float in [0, 1]: {value!r}")


# --- Items --------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class _Item:
    """The envelope every item kind shares (ADR 0003 §3)."""

    kind: ClassVar[str]
    assertion_kind: ClaimAssertionKind
    confidence: Knowledge[float]
    provenance: ItemProvenance
    relevance: Relevance

    def __post_init__(self) -> None:
        if not isinstance(self.provenance, ItemProvenance):
            raise _fail(Code.SHAPE, f"provenance must be an ItemProvenance: {self.provenance!r}")
        if not isinstance(self.relevance, Relevance):
            raise _fail(Code.SHAPE, f"relevance must be a Relevance: {self.relevance!r}")
        _check_epistemics(self.assertion_kind, self.confidence, self.provenance)
        self._check_body()
        try:  # every value must have a canonical form (a lone surrogate has none)
            dumps(self.content_json())
        except ValueError as exc:
            raise _fail(Code.BAD_VALUE, f"{self.kind} item: {exc}") from exc

    def _check_body(self) -> None:
        """Each kind's own rules, run after the envelope's."""

    def body_json(self) -> dict[str, JsonValue]:
        raise NotImplementedError

    def content_json(self) -> JsonObject:
        """Everything but ``id`` and ``relevance``: what the item asserts and rests on."""
        return {
            **self.body_json(),
            "assertion_kind": str(self.assertion_kind),
            "confidence": to_json(self.confidence),
            "kind": self.kind,
            "provenance": self.provenance.to_json(),
        }

    @cached_property
    def id(self) -> str:
        """``item:sha256:<hex>``: equal content, equal id, whichever query found it."""
        payload: JsonObject = {"item": self.content_json(), "scheme": ITEM_ID_SCHEME}
        return "item:" + content_id(dumps(payload))

    @property
    def is_inferred(self) -> bool:
        return is_inferred(self.assertion_kind)

    def claim_refs(self) -> tuple[ClaimId, ...]:
        """Claims this item names that must be carried as ``ClaimItem``s in the same packet."""
        return ()

    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        """Every evidence ref the item mentions, provenance first, in a fixed order."""
        return self.provenance.evidence

    def to_json(self) -> JsonObject:
        return {**self.content_json(), "id": self.id, "relevance": self.relevance.to_json()}


def _cites(item: _Item, ref: EvidenceRef, what: str) -> None:
    if not isinstance(ref, EvidenceRef):
        raise _fail(Code.SHAPE, f"{what} must be an EvidenceRef, got {ref!r}")
    if ref not in item.provenance.evidence:
        raise _fail(Code.BAD_VALUE, f"{what} must be one of the item's provenance evidence refs")


def _record(value: str, what: str) -> RecordId:
    try:
        return parse_record_id(value)
    except (TypeError, ValueError) as exc:
        raise _fail(Code.BAD_VALUE, f"{what}: {exc}") from exc


def _claim_ids(values: tuple[ClaimId, ...], what: str) -> None:
    if not isinstance(values, tuple):
        raise _fail(Code.SHAPE, f"{what} must be a tuple of claim ids")
    for value in values:
        try:
            parse_claim_id(value)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"{what}: {exc}") from exc
    _ids(values, what)


def _record_ids(values: tuple[RecordId, ...], what: str) -> None:
    if not isinstance(values, tuple):
        raise _fail(Code.SHAPE, f"{what} must be a tuple of record ids")
    for value in values:
        _record(value, what)
    _ids(values, what)


@dataclass(frozen=True, kw_only=True)
class ClaimItem(_Item):
    """A Memory claim as known at the packet's snapshot. The envelope repeats the claim's own
    ``assertion_kind``, confidence and provenance, so every item kind reads alike; they must be
    equal. Use ``ClaimItem.of``."""

    kind: ClassVar[str] = "claim"
    claim: Claim

    def _check_body(self) -> None:
        if not isinstance(self.claim, Claim):
            raise _fail(Code.SHAPE, f"claim must be a Claim, got {self.claim!r}")
        if (
            self.assertion_kind != self.claim.assertion_kind
            or self.confidence != self.claim.confidence
            or self.provenance != ItemProvenance.of_claim(self.claim.provenance)
        ):
            raise _fail(Code.ASSERTION_MISMATCH, "a claim item's envelope must equal its claim's")
        if not isinstance(self.claim.superseded_at, Open):
            raise _fail(
                Code.NOT_AS_OF,
                "a packet presents claims as known at as_of (superseded_at open); later "
                "supersessions go in superseded_since",
            )

    @classmethod
    def of(cls, claim: Claim, relevance: Relevance) -> ClaimItem:
        return cls(
            assertion_kind=claim.assertion_kind,
            confidence=claim.confidence,
            provenance=ItemProvenance.of_claim(claim.provenance),
            relevance=relevance,
            claim=claim,
        )

    def body_json(self) -> dict[str, JsonValue]:
        return {"claim": self.claim.to_json()}


class EvidenceStatus(StrEnum):
    RESOLVED = "resolved"  # a registered package holds the source at the snapshot
    UNRESOLVABLE = "unresolvable"  # none does: the citation stands, the bytes are not reachable


@dataclass(frozen=True, kw_only=True)
class EvidenceItem(_Item):
    """Source bytes, cited exactly, with what the Ledger's ``resolve`` said at the snapshot.

    The resolution handle is ``(evidence, status, size)``: hydrating the item is
    ``CatalogApi.resolve(evidence, as_of=packet.as_of)``, and ``status`` says beforehand whether
    that will reach bytes. Fetch locations are host-specific and never enter a packet.
    """

    kind: ClassVar[str] = "evidence"
    evidence: EvidenceRef
    status: EvidenceStatus
    size: Knowledge[int]

    def _check_body(self) -> None:
        _cites(self, self.evidence, "evidence")
        if self.is_inferred:
            raise _fail(Code.ASSERTION_MISMATCH, "source bytes are never inferred")
        if not isinstance(self.status, EvidenceStatus):
            raise _fail(Code.BAD_VALUE, f"status must be an EvidenceStatus: {self.status!r}")
        _check_inherited(self.size, "size")  # type: ignore[arg-type]
        if isinstance(self.size, Known):
            _count(self.size.value, "size")
        if self.status is EvidenceStatus.UNRESOLVABLE and not isinstance(self.size, NotCovered):
            raise _fail(Code.BAD_VALUE, "an unresolvable source's size is NotCovered")

    def body_json(self) -> dict[str, JsonValue]:
        return {
            "evidence": self.evidence.to_json(),
            "size": _knowledge_json(self.size, _plain),  # type: ignore[arg-type]
            "status": str(self.status),
        }


@dataclass(frozen=True)
class ArrowHandle:
    """Where a series window's rows are: one package's ``series/<stream>.parquet``, read as
    Arrow and filtered to the item's interval on its clock column. No row offsets: they depend
    on the package's layout, the interval does not."""

    package_id: ContentId
    path: str

    def __post_init__(self) -> None:
        try:
            parse_content_id(self.package_id)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"package_id: {exc}") from exc
        if not isinstance(self.path, str):
            raise _fail(Code.SHAPE, "path must be a string")

    def to_json(self) -> JsonObject:
        return {"package_id": self.package_id, "path": self.path}


@dataclass(frozen=True, kw_only=True)
class SeriesWindowItem(_Item):
    """A window ``[start, end)`` of one stream, in ticks of one of its clocks, never converted."""

    kind: ClassVar[str] = "series_window"
    stream: RecordId
    clock: RecordId
    start: int
    end: int
    arrow: ArrowHandle

    def _check_body(self) -> None:
        _record(self.stream, "stream")
        _record(self.clock, "clock")
        _ticks(self.start, "start")
        _ticks(self.end, "end")
        if self.start >= self.end:
            raise _fail(Code.BAD_VALUE, "a series window is non-empty: start < end")
        if not isinstance(self.arrow, ArrowHandle):
            raise _fail(Code.SHAPE, f"arrow must be an ArrowHandle: {self.arrow!r}")
        if self.arrow.path != series_path(self.stream):
            raise _fail(Code.BAD_VALUE, f"arrow path must be {series_path(self.stream)!r}")

    def body_json(self) -> dict[str, JsonValue]:
        return {
            "arrow": self.arrow.to_json(),
            "clock": self.clock,
            "end": self.end,
            "start": self.start,
            "stream": self.stream,
        }


@dataclass(frozen=True, kw_only=True)
class FrameItem(_Item):
    """One sensor sample (an image, a scan, a video frame): its stream (``NotApplicable`` for a
    standalone image), its instant on its own clock, its encoding as the record declares it
    (``png``, ``cdr``, ``jpeg``) and the coordinate frame it was taken in."""

    kind: ClassVar[str] = "frame"
    stream: Knowledge[RecordId]
    at: Knowledge[Timestamp]
    evidence: EvidenceRef
    encoding: Knowledge[str]
    frame: Knowledge[FrameRef]

    def _check_body(self) -> None:
        _cites(self, self.evidence, "evidence")
        for name in ("stream", "at", "encoding", "frame"):
            _check_inherited(getattr(self, name), name)
        if isinstance(self.stream, Known):
            _record(self.stream.value, "stream")
        if isinstance(self.encoding, Known):
            _text(self.encoding.value, "encoding")
        if isinstance(self.at, Known) and not isinstance(self.at.value, Timestamp):
            raise _fail(Code.SHAPE, "at must be a Timestamp")
        if isinstance(self.frame, Known) and not isinstance(self.frame.value, FrameRef):
            raise _fail(Code.SHAPE, "frame must be a FrameRef")

    def body_json(self) -> dict[str, JsonValue]:
        return {
            "encoding": _knowledge_json(self.encoding, _plain),  # type: ignore[arg-type]
            "at": _knowledge_json(self.at, lambda t: t.to_json()),  # type: ignore[arg-type, attr-defined]
            "evidence": self.evidence.to_json(),
            "frame": _knowledge_json(self.frame, lambda f: f.to_json()),  # type: ignore[arg-type, attr-defined]
            "stream": _knowledge_json(self.stream, _plain),  # type: ignore[arg-type]
        }


@dataclass(frozen=True, kw_only=True)
class DocumentSpanItem(_Item):
    """A span of a document or table record: where it is, and its text exactly as extracted."""

    kind: ClassVar[str] = "document_span"
    document: RecordId
    evidence: EvidenceRef
    text: Knowledge[str]

    def _check_body(self) -> None:
        _record(self.document, "document")
        _cites(self, self.evidence, "evidence")
        _check_inherited(self.text, "text")  # type: ignore[arg-type]
        if isinstance(self.text, Known):
            try:
                check_verbatim("text", self.text.value)
            except (TypeError, ValueError) as exc:
                raise _fail(Code.BAD_VALUE, str(exc)) from exc

    def body_json(self) -> dict[str, JsonValue]:
        return {
            "document": self.document,
            "evidence": self.evidence.to_json(),
            "text": _knowledge_json(self.text, _plain),  # type: ignore[arg-type]
        }


@dataclass(frozen=True, kw_only=True)
class SceneItem(_Item):
    """A spatial subgraph in one named frame: the nodes placed, the placement claims (carried as
    ``ClaimItem``s in the same packet) and the frame-graph and geometry records it rests on."""

    kind: ClassVar[str] = "scene"
    frame: FrameRef
    site: Knowledge[NodeRef]
    nodes: tuple[NodeRef, ...]
    claims: tuple[ClaimId, ...]
    records: tuple[RecordId, ...]

    def _check_body(self) -> None:
        if not isinstance(self.frame, FrameRef):
            raise _fail(Code.SHAPE, f"frame must be a FrameRef: {self.frame!r}")
        _check_inherited(self.site, "site")  # type: ignore[arg-type]
        if isinstance(self.site, Known) and not isinstance(self.site.value, NodeRef):
            raise _fail(Code.SHAPE, "site must be a NodeRef")
        if not isinstance(self.nodes, tuple) or not all(isinstance(n, NodeRef) for n in self.nodes):
            raise _fail(Code.SHAPE, "nodes must be a tuple of NodeRefs")
        _sorted_unique([_node_key(n) for n in self.nodes], "scene nodes")
        _claim_ids(self.claims, "scene claims")
        _record_ids(self.records, "scene records")
        if not self.claims and not self.records:
            raise _fail(Code.BAD_VALUE, "a scene rests on at least one claim or record")

    def claim_refs(self) -> tuple[ClaimId, ...]:
        return self.claims

    def body_json(self) -> dict[str, JsonValue]:
        return {
            "claims": list(self.claims),
            "frame": self.frame.to_json(),
            "nodes": [n.to_json() for n in self.nodes],
            "records": list(self.records),
            "site": _knowledge_json(self.site, lambda n: n.to_json()),  # type: ignore[arg-type, attr-defined]
        }


@dataclass(frozen=True, kw_only=True)
class ConfigurationItem(_Item):
    """A configuration record (calibration, parameter set, URDF revision, software or hardware
    configuration) by id and compiler record kind, what it configures when a claim says so, and
    those claims (carried as ``ClaimItem``s in the same packet)."""

    kind: ClassVar[str] = "configuration"
    record: RecordId
    record_kind: str
    subject: Knowledge[NodeRef]
    claims: tuple[ClaimId, ...]

    def _check_body(self) -> None:
        _record(self.record, "record")
        try:
            check_token("record_kind", self.record_kind)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, str(exc)) from exc
        _check_inherited(self.subject, "subject")  # type: ignore[arg-type]
        if isinstance(self.subject, Known) and not isinstance(self.subject.value, NodeRef):
            raise _fail(Code.SHAPE, "subject must be a NodeRef")
        _claim_ids(self.claims, "configuration claims")

    def claim_refs(self) -> tuple[ClaimId, ...]:
        return self.claims

    def body_json(self) -> dict[str, JsonValue]:
        return {
            "claims": list(self.claims),
            "record": self.record,
            "record_kind": self.record_kind,
            "subject": _knowledge_json(self.subject, lambda n: n.to_json()),  # type: ignore[arg-type, attr-defined]
        }


Item: TypeAlias = (
    ClaimItem
    | EvidenceItem
    | SeriesWindowItem
    | FrameItem
    | DocumentSpanItem
    | SceneItem
    | ConfigurationItem
)
ITEM_KINDS: Final[tuple[type[_Item], ...]] = (
    ClaimItem,
    ConfigurationItem,
    DocumentSpanItem,
    EvidenceItem,
    FrameItem,
    SceneItem,
    SeriesWindowItem,
)


# --- Header -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class During:
    """The world-time window the packet answers, resolved to one clock (a ``TimestampDomain``
    record id; a civil time resolves to its ``CivilClock.domain_id``): ``[start, end)``, open
    when ``end`` is ``None``."""

    domain_id: RecordId
    start: int
    end: int | None

    def __post_init__(self) -> None:
        _record(self.domain_id, "during domain_id")
        _ticks(self.start, "during start")
        if self.end is not None:
            _ticks(self.end, "during end")
            if self.start >= self.end:
                raise _fail(Code.BAD_VALUE, "during is non-empty: start < end")

    def to_json(self) -> JsonObject:
        return {
            "domain_id": self.domain_id,
            "end": "open" if self.end is None else self.end,
            "start": self.start,
        }


@dataclass(frozen=True)
class MemorySnapshot:
    """Which claim graph was read: its graph-schema major, its generation (the resolver
    configuration hash) and the Ledger transaction it was read as of."""

    graph_schema_version: int
    generation: ConfigHash
    as_of: LedgerTx

    def __post_init__(self) -> None:
        _count(self.graph_schema_version, "graph_schema_version", low=1)
        try:
            parse_config_hash(self.generation)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"generation: {exc}") from exc
        _tx(self.as_of, "memory as_of")

    def to_json(self) -> JsonObject:
        return {
            "as_of": self.as_of,
            "generation": self.generation,
            "graph_schema_version": self.graph_schema_version,
        }


@dataclass(frozen=True)
class LedgerSnapshot:
    """Which catalog was read: the catalog API version, at the packet's ``as_of``."""

    catalog_api_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.catalog_api_version, str) or not _SEMVER.fullmatch(
            self.catalog_api_version
        ):
            raise _fail(Code.BAD_VALUE, "catalog_api_version is MAJOR.MINOR.PATCH")

    def to_json(self) -> JsonObject:
        return {"catalog_api_version": self.catalog_api_version}


@dataclass(frozen=True)
class Engine:
    """What assembled the packet: engine id, version and resolved-config hash (planner, channel
    and fusion settings). A new engine version is a new lineage, never an edit to old packets."""

    engine_id: str
    engine_version: str
    config_hash: ConfigHash

    def __post_init__(self) -> None:
        try:
            check_token("engine_id", self.engine_id)
            check_text("engine_version", self.engine_version)
            parse_config_hash(self.config_hash)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"produced_by: {exc}") from exc

    def to_json(self) -> JsonObject:
        return {
            "config_hash": self.config_hash,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
        }


class Limit(StrEnum):
    """The budget limits a packet can exhaust. Latency is a limit on the call, not the packet:
    a packet carries nothing measured by a wall clock (ADR 0003 §5)."""

    BYTES = "bytes"
    ITEMS = "items"
    TOKENS = "tokens"


@dataclass(frozen=True)
class Limits:
    """The query's budget, echoed: ``items`` always, any other limit ``None`` when unbounded."""

    items: int
    tokens: int | None = None
    bytes: int | None = None
    latency_ms: int | None = None

    def __post_init__(self) -> None:
        _count(self.items, "items limit", low=1)
        for name in ("tokens", "bytes", "latency_ms"):
            value = getattr(self, name)
            if value is not None:
                _count(value, f"{name} limit", low=1)

    def of(self, limit: Limit) -> int | None:
        value: int | None = getattr(self, str(limit))
        return value

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"items": self.items}
        for name in ("bytes", "latency_ms", "tokens"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def measure(items: Iterable[Item]) -> tuple[int, int]:
    """``(bytes, tokens)`` of the items: their canonical JSON array's length, and ``TOKENIZER``."""
    size = len(dumps([item.to_json() for item in items]))
    return size, -(-size // 4)


@dataclass(frozen=True)
class BudgetUse:
    """What the packet spent against ``limits``: counts that match its items exactly, the number
    of candidates the budget cut (``dropped``) and the limits that cut them (``exhausted``).
    A truncated packet says so; it never reads as a complete answer."""

    limits: Limits
    items: int
    bytes: int
    tokens: int
    dropped: int
    exhausted: tuple[Limit, ...]
    tokenizer: str = TOKENIZER

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Limits):
            raise _fail(Code.SHAPE, f"limits must be Limits: {self.limits!r}")
        for name in ("items", "bytes", "tokens", "dropped"):
            _count(getattr(self, name), f"budget {name}")
        if self.tokenizer != TOKENIZER:
            raise _fail(Code.BUDGET, f"tokenizer must be {TOKENIZER!r}")
        if not isinstance(self.exhausted, tuple) or not all(
            isinstance(e, Limit) for e in self.exhausted
        ):
            raise _fail(Code.SHAPE, "exhausted must be a tuple of Limits")
        _sorted_unique([str(e) for e in self.exhausted], "exhausted limits")
        for limit in Limit:
            bound = self.limits.of(limit)
            used: int = getattr(self, str(limit))
            if bound is not None and used > bound:
                raise _fail(Code.BUDGET, f"{limit} used {used} exceeds the limit {bound}")
            if limit in self.exhausted and bound is None:
                raise _fail(Code.BUDGET, f"{limit} is exhausted but unbounded")
        if (self.dropped > 0) != bool(self.exhausted):
            raise _fail(Code.BUDGET, "dropped > 0 exactly when some limit is exhausted")

    @classmethod
    def measured(
        cls,
        limits: Limits,
        items: Sequence[Item],
        *,
        dropped: int = 0,
        exhausted: tuple[Limit, ...] = (),
    ) -> BudgetUse:
        size, tokens = measure(items)
        return cls(limits, len(items), size, tokens, dropped, exhausted)

    def to_json(self) -> JsonObject:
        return {
            "dropped": self.dropped,
            "exhausted": [str(e) for e in self.exhausted],
            "limits": self.limits.to_json(),
            "tokenizer": self.tokenizer,
            "used": {"bytes": self.bytes, "items": self.items, "tokens": self.tokens},
        }


# --- Body sections ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Superseded:
    """A claim in the packet that has changed since the snapshot it was read at: the transaction
    that superseded it (``memory.as_of < superseded_at <= head``) and the versions that did."""

    claim: ClaimId
    superseded_at: LedgerTx
    by: tuple[ClaimId, ...]

    def __post_init__(self) -> None:
        _claim_ids((self.claim,), "superseded claim")
        _tx(self.superseded_at, "superseded_at")
        _claim_ids(self.by, "superseded_by")
        if not self.by:
            raise _fail(Code.BAD_VALUE, "a superseded claim names the versions that superseded it")
        if self.claim in self.by:
            raise _fail(Code.BAD_VALUE, "a claim does not supersede itself")

    def to_json(self) -> JsonObject:
        return {"by": list(self.by), "claim": self.claim, "superseded_at": self.superseded_at}


class GapCode(StrEnum):
    """Why part of the question has no item: each code is one explicit kind of missingness."""

    NOT_COVERED = "not_covered"  # the source cannot say (e.g. Memory has no spatial view yet)
    UNKNOWN = "unknown"  # the source could have said and did not
    AMBIGUOUS = "ambiguous"  # the sources support several answers; none is picked
    OTHER_CLOCK = "other_clock"  # matching claims on a clock the query did not bridge
    INFERRED_WITHHELD = "inferred_withheld"  # inferred matches left out: inference excluded
    UNRESOLVABLE = "unresolvable"  # cited evidence that no registered package holds


@dataclass(frozen=True)
class Gap:
    """A part of the query the packet does not answer, and why.

    ``at`` is a JSON pointer into the query's canonical JSON (``""``: the whole query);
    ``channel`` is the channel that reported it, if one did; ``refs`` names what it concerns
    (claim, record, node or item ids), sorted. Withheld or unplaced claims are named by id only,
    never carried: their content is exactly what the query did not ask for.
    """

    code: GapCode
    at: str
    channel: Channel | None
    refs: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, GapCode):
            raise _fail(Code.BAD_VALUE, f"code must be a GapCode: {self.code!r}")
        if not isinstance(self.at, str) or (self.at and not self.at.startswith("/")):
            raise _fail(Code.BAD_VALUE, "at is a JSON pointer: empty or starting with '/'")
        if self.channel is not None and not isinstance(self.channel, Channel):
            raise _fail(Code.BAD_VALUE, f"channel must be a Channel or None: {self.channel!r}")
        _text(self.at or "/", "at")
        if not isinstance(self.refs, tuple) or not all(isinstance(r, str) for r in self.refs):
            raise _fail(Code.SHAPE, "refs must be a tuple of strings")
        for ref in self.refs:
            _text(ref, "a gap ref")
        _ids(self.refs, "gap refs")
        try:
            check_text("detail", self.detail)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, str(exc)) from exc
        if len(self.detail) > MAX_DETAIL_CHARS:
            raise _fail(Code.BAD_VALUE, f"detail is at most {MAX_DETAIL_CHARS} characters")

    def sort_key(self) -> tuple[str, str, str, tuple[str, ...]]:
        return (str(self.code), self.at, str(self.channel or ""), self.refs)

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "at": self.at,
            "code": str(self.code),
            "detail": self.detail,
            "refs": list(self.refs),
        }
        if self.channel is not None:
            out["channel"] = str(self.channel)
        return out


def _finding_key(f: ResolutionFinding) -> tuple[int, str, str, tuple[str, ...]]:
    return (f.recorded_at, f.claim, str(f.code), f.others)


# --- The packet ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPacket:
    """One answer to one query at one snapshot (ADR 0003 §2).

    - ``query_id``: ``query:sha256:<hex>`` of the query's canonical JSON (ADR 0002, MVL-108).
    - ``as_of``: the Ledger transaction the packet answers at, never ``head`` unresolved;
      ``head``: the latest transaction known when it was assembled (``as_of <= head``).
    - ``during``: the resolved world-time window, or ``None`` when the query set none.
    - ``memory`` / ``ledger``: the snapshots read; Memory's ``as_of`` may trail the packet's.
    - ``produced_by``: the engine; ``inference_included``: whether inferred items may appear.
    - ``budget``: limits, use and truncation; ``items``: by fused score, then id.
    - ``superseded_since``: claim items changed in ``(memory.as_of, head]``, by claim id.
    - ``findings``: Memory resolver findings active at the snapshot (conflicts, clock
      mismatches) that name a claim item, in resolver order.
    - ``gaps``: what the packet does not answer, and why.
    - ``trails``: the structure of each ``explain`` clause answered (ADR 0010), by clause; a
      member of the JSON only when there is one, so a packet without trails keeps its bytes.
    """

    query_id: str
    as_of: LedgerTx
    head: LedgerTx
    during: During | None
    memory: MemorySnapshot
    ledger: LedgerSnapshot
    produced_by: Engine
    inference_included: bool
    budget: BudgetUse
    items: tuple[Item, ...]
    superseded_since: tuple[Superseded, ...] = ()
    findings: tuple[ResolutionFinding, ...] = ()
    gaps: tuple[Gap, ...] = ()
    trails: tuple[Trail, ...] = ()

    def __post_init__(self) -> None:
        self._check_header()
        self._check_items()
        self._check_sections()
        self._check_trails()

    def _check_header(self) -> None:
        if not isinstance(self.query_id, str) or not _QUERY_ID.fullmatch(self.query_id):
            raise _fail(Code.BAD_VALUE, "query_id is query:sha256:<64 lowercase hex>")
        _tx(self.as_of, "as_of")
        _tx(self.head, "head")
        if self.as_of > self.head:
            raise _fail(Code.NOT_AS_OF, f"as_of {self.as_of} is after head {self.head}")
        if self.during is not None and not isinstance(self.during, During):
            raise _fail(Code.SHAPE, f"during must be a During or None: {self.during!r}")
        for name, cls in (
            ("memory", MemorySnapshot),
            ("ledger", LedgerSnapshot),
            ("produced_by", Engine),
            ("budget", BudgetUse),
        ):
            if not isinstance(getattr(self, name), cls):
                raise _fail(Code.SHAPE, f"{name} must be a {cls.__name__}")
        if self.memory.as_of > self.as_of:
            raise _fail(Code.NOT_AS_OF, "Memory's snapshot is later than the packet's as_of")
        if not isinstance(self.inference_included, bool):
            raise _fail(Code.SHAPE, "inference_included must be a bool")

    def _check_items(self) -> None:
        if not isinstance(self.items, tuple) or not all(
            isinstance(i, ITEM_KINDS) for i in self.items
        ):
            raise _fail(Code.SHAPE, "items must be a tuple of packet items")
        if len(self.items) > MAX_ITEMS:
            raise _fail(Code.BAD_VALUE, f"at most {MAX_ITEMS} items")
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            raise _fail(Code.DUPLICATE, "an item appears twice")
        order = [(-item.relevance.score, item.id) for item in self.items]
        if order != sorted(order):
            raise _fail(Code.ORDER, "items are ordered by fused score (descending), then id")
        for item in self.items:
            if item.is_inferred and not self.inference_included:
                raise _fail(
                    Code.INFERENCE_EXCLUDED,
                    f"{item.id} is inferred but the packet excludes inference",
                )
            if isinstance(item, ClaimItem) and item.claim.recorded_at > self.memory.as_of:
                raise _fail(Code.NOT_AS_OF, f"{item.claim.id} was recorded after the snapshot")
        claims = self.claim_ids
        inferred = frozenset(
            i.claim.id for i in self.items if isinstance(i, ClaimItem) and i.is_inferred
        )
        for item in self.items:
            missing = sorted(set(item.claim_refs()) - claims)
            if missing:
                raise _fail(
                    Code.DANGLING_REFERENCE, f"{item.id} names claims not carried: {missing}"
                )
            resting = sorted(set(item.claim_refs()) & inferred)
            if resting and not item.is_inferred:
                # A scene or configuration is no stronger than the claims it rests on: one that
                # names an inferred claim is inferred itself, so it renders INFERRED.
                raise _fail(
                    Code.ASSERTION_MISMATCH,
                    f"{item.id} is {item.assertion_kind} but rests on inferred claims: {resting}",
                )
        size, tokens = measure(self.items)
        if (self.budget.items, self.budget.bytes, self.budget.tokens) != (
            len(self.items),
            size,
            tokens,
        ):
            raise _fail(
                Code.BUDGET,
                f"budget use must match the items: items {len(self.items)}, bytes {size},"
                f" tokens {tokens}",
            )

    def _check_sections(self) -> None:
        claims = self.claim_ids
        if not isinstance(self.superseded_since, tuple) or not all(
            isinstance(s, Superseded) for s in self.superseded_since
        ):
            raise _fail(Code.SHAPE, "superseded_since must be a tuple of Superseded")
        _sorted_unique([s.claim for s in self.superseded_since], "superseded claims")
        for entry in self.superseded_since:
            if entry.claim not in claims:
                raise _fail(Code.DANGLING_REFERENCE, f"{entry.claim} is not a claim item")
            # The claims are as Memory knew them at its snapshot, which may trail as_of: every
            # supersession Memory has made since then, up to head, is listed (C1 gate, ADR 0006).
            if not self.memory.as_of < entry.superseded_at <= self.head:
                raise _fail(Code.NOT_AS_OF, "superseded_at is in (memory_snapshot.as_of, head]")
        if not isinstance(self.findings, tuple) or not all(
            isinstance(f, ResolutionFinding) for f in self.findings
        ):
            raise _fail(Code.SHAPE, "findings must be a tuple of Memory ResolutionFindings")
        if len({f.id for f in self.findings}) != len(self.findings):
            raise _fail(Code.DUPLICATE, "a finding appears twice")
        if [_finding_key(f) for f in self.findings] != sorted(map(_finding_key, self.findings)):
            raise _fail(Code.ORDER, "findings are in resolver order")
        for finding in self.findings:
            if finding.recorded_at > self.memory.as_of or not isinstance(
                finding.superseded_at, Open
            ):
                raise _fail(Code.NOT_AS_OF, f"{finding.id} was not active at the snapshot")
            if not ({finding.claim, *finding.others} & claims):
                raise _fail(Code.DANGLING_REFERENCE, f"{finding.id} names no claim item")
        if not isinstance(self.gaps, tuple) or not all(isinstance(g, Gap) for g in self.gaps):
            raise _fail(Code.SHAPE, "gaps must be a tuple of Gaps")
        _sorted_unique([g.sort_key() for g in self.gaps], "gaps")
        if self.inference_included and any(g.code is GapCode.INFERRED_WITHHELD for g in self.gaps):
            raise _fail(
                Code.INFERENCE_EXCLUDED,
                "an inferred_withheld gap says inference was excluded; this packet includes it",
            )

    def _check_trails(self) -> None:
        trails = self.trails
        if not isinstance(trails, tuple) or not all(isinstance(t, TRAIL_KINDS) for t in trails):
            raise _fail(Code.SHAPE, "trails must be a tuple of WhyTrails and DiffTrails")
        if len(trails) > MAX_TRAILS:
            raise _fail(Code.BAD_VALUE, f"at most {MAX_TRAILS} trails")
        _sorted_unique([trail_index(t.at) for t in trails], "trails (by explain clause)")
        carried = {i.claim.id: i.claim for i in self.items if isinstance(i, ClaimItem)}
        for trail in trails:
            if isinstance(trail, WhyTrail):
                for step in trail.steps:
                    if step.is_inferred and not self.inference_included:
                        raise _fail(
                            Code.INFERENCE_EXCLUDED,
                            f"{trail.at} names inferred {step.claim}; the packet excludes"
                            " inference",
                        )
                    claim = carried.get(step.claim)
                    if claim is not None and (
                        step.assertion_kind != claim.assertion_kind
                        or step.evidence != claim.provenance.evidence
                    ):
                        raise _fail(
                            Code.ASSERTION_MISMATCH,
                            f"{trail.at}: a step must repeat its claim's assertion kind and"
                            f" evidence ({step.claim})",
                        )
                continue
            nodes = set(trail.nodes)
            for change in trail.changes:
                for claim_id in (*change.before, *change.after):
                    claim = carried.get(claim_id)
                    if claim is None:
                        continue
                    if claim.predicate != change.predicate or not (
                        {claim.subject, claim.object} & nodes
                    ):
                        raise _fail(
                            Code.BAD_VALUE,
                            f"{trail.at}: {claim_id} is not a {change.predicate} claim about"
                            " the diff's nodes",
                        )

    @property
    def claim_ids(self) -> frozenset[ClaimId]:
        return frozenset(i.claim.id for i in self.items if isinstance(i, ClaimItem))

    def header_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "as_of": self.as_of,
            "budget": self.budget.to_json(),
            "head": self.head,
            "inference_included": self.inference_included,
            "ledger_snapshot": self.ledger.to_json(),
            "memory_snapshot": self.memory.to_json(),
            "produced_by": self.produced_by.to_json(),
            "query_id": self.query_id,
        }
        if self.during is not None:
            out["during"] = self.during.to_json()
        return out

    def content_json(self) -> JsonObject:
        """Everything but ``id``: the input the packet id is derived from. ``trails`` is written
        only when there is one (ADR 0010), so every packet without trails keeps its bytes."""
        out: dict[str, JsonValue] = {
            "findings": [f.to_json() for f in self.findings],
            "gaps": [g.to_json() for g in self.gaps],
            "header": self.header_json(),
            "items": [item.to_json() for item in self.items],
            "kind": PACKET_KIND,
            "packet_version": PACKET_VERSION,
            "superseded_since": [s.to_json() for s in self.superseded_since],
        }
        if self.trails:
            out["trails"] = [t.to_json() for t in self.trails]
        return out

    @cached_property
    def id(self) -> str:
        """``packet:sha256:<hex>``: the same answer at the same snapshot has the same id."""
        payload: JsonObject = {"packet": self.content_json(), "scheme": PACKET_ID_SCHEME}
        return "packet:" + content_id(dumps(payload))

    def to_json(self) -> JsonObject:
        return {**self.content_json(), "id": self.id}

    def evidence_refs(self) -> tuple[EvidenceRef, ...]:
        """Every evidence ref the packet mentions, unique, in order of first mention."""
        seen: dict[EvidenceRef, None] = {}
        for item in self.items:
            for ref in item.evidence_refs():
                seen.setdefault(ref, None)
        return tuple(seen)
