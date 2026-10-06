"""Golden packets for the ten worked queries (ADR 0003 §10), over the example graph.

The example graph is Memory's published golden graph (``contracts/graph-schema/v1.0.0/golden/
graph.json``: four robots, five Ledger transactions, supersessions, inferred identity candidates
and resolver findings), read through Memory's ``ReferenceReader``. Records that are not claims
(streams, frame graphs, calibrations, images, site registers, risk assessments) come from the
compiler's worked examples (``tests/fixtures/model/``), and package ids from their golden package
manifests. Every id, evidence ref and provenance in a golden is copied from those sources;
nothing is invented but the relevance scores, which use reciprocal-rank fusion (k = 60) as a
stand-in until fusion lands (MVL-145).

Each worked query is written as its canonical query JSON in the shape of the query language
(ADR 0002, MVL-108); the packet names it only by ``query:sha256:<hex>`` of those bytes. This
package does not import the query model, so the two contracts merge independently; the C1 gate
(MVL-111) adds the test that decodes these documents with the query reader.

``python packages/neptune-context/tests/context_packet_goldens.py`` rewrites the files;
``test_packet_goldens_context.py`` fails when they drift from what this module builds.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from neptune_memory.schema.claim import Claim, ClaimId
from neptune_memory.schema.codec import GraphDocument, graph_from_json
from neptune_memory.schema.interval import OPEN, Interval, LedgerTx, Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.reference import ReferenceReader

from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.frames import FrameRef
from neptune.model.ids import ConfigHash, ContentId, RecordId
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.time import Timestamp, timestamp_from_json
from neptune_context.packets.codec import canonical_bytes
from neptune_context.packets.model import (
    ArrowHandle,
    BudgetUse,
    Channel,
    ChannelHit,
    ClaimItem,
    ConfigurationItem,
    ContextPacket,
    DocumentSpanItem,
    During,
    Engine,
    EvidenceItem,
    EvidenceStatus,
    FrameItem,
    Gap,
    GapCode,
    Item,
    ItemProvenance,
    LedgerSnapshot,
    Limit,
    Limits,
    MemorySnapshot,
    Relevance,
    SceneItem,
    SeriesWindowItem,
    Superseded,
    Transform,
    series_path,
)
from neptune_context.packets.schema import packet_schema
from neptune_context.pins import CATALOG_API_VERSION

if TYPE_CHECKING:
    from neptune_memory.schema.supersede import ResolutionFinding

    from neptune.model.jsonvalue import JsonValue

HERE: Final = Path(__file__).resolve().parent
ROOT: Final = HERE.parents[2]
GRAPH: Final = ROOT / "contracts" / "graph-schema" / "v1.0.0" / "golden" / "graph.json"
EXAMPLES: Final = ROOT / "tests" / "fixtures" / "model"
PACKAGES: Final = ROOT / "tests" / "golden" / "packages"
GOLDEN: Final = HERE / "golden"
PACKETS: Final = GOLDEN / "packets"
QUERIES: Final = GOLDEN / "queries"
SCHEMA: Final = GOLDEN / "context-packet.schema.json"
RRF_K: Final = 60
ENGINE_CONFIG: Final[dict[str, JsonValue]] = {
    "fusion": "rrf",
    "k": RRF_K,
    "note": "golden stand-in until MVL-145",
}
ENGINE: Final = Engine("neptune-context.golden", "1", ConfigHash(content_id(dumps(ENGINE_CONFIG))))


def pretty(value: Any) -> bytes:
    """How goldens are stored: sorted keys, two-space indent, one trailing newline."""
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def query_id(document: Any) -> str:
    return "query:sha256:" + hashlib.sha256(dumps(document)).hexdigest()


def run_node(record: str) -> NodeRef:
    return NodeRef(NodeType.RUN, f"record:{record}")


def machine(tag: str) -> NodeRef:
    return NodeRef(NodeType.MACHINE, f"asset-tag:{tag}")


RUN_DRONE: Final = "rec:sha256:bbe0a997b0284bd7a1f3982de3dbddf3abcd70485034c4d1af9fe1709da3401b"
RUN_QUADRUPED: Final = "rec:sha256:5bf3bccba577623acf6104e6d71ed1cf1dc7c0a383a97791f700564511548557"
DRONE_LOG_CLOCK: Final = (
    "rec:sha256:930f36cc553dce34af08e794001077c634527116bc12138b8ecd91d415b9ee31"
)
QUAD_STREAM_CLOCK: Final = (
    "rec:sha256:ab683ba52eb6d738d33b657e7b3e9704ea0772891ca65acd83b17b9ac62f0445"
)
ARM_FRAME_GRAPH: Final = (
    "rec:sha256:4e12de3b72e12a5547463a4230660633176fb35ff6498a2e7938a1d278fc162a"
)


# --- Query documents (ADR 0002's canonical JSON shape) -------------------------------------------


def _query(**members: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "as_of": "head",
        "clock_bridges": [],
        "explain": [],
        "frame_bridges": [],
        "query_version": 1,
        "regions": [],
        "subjects": [],
    }
    out.update(members)
    return out


def _subject(kind: str, declared_id: str, same_as_depth: int = 0) -> dict[str, Any]:
    return {"declared_id": declared_id, "kind": kind, "same_as_depth": same_as_depth}


def _graph(predicates: list[str], direction: str = "out", hops: int = 1) -> dict[str, Any]:
    return {"direction": direction, "hops": hops, "predicates": sorted(predicates)}


def _domain(domain_id: str) -> dict[str, Any]:
    return {"domain_id": domain_id, "kind": "domain"}


# --- Sources ----------------------------------------------------------------------------------


@dataclass
class Sources:
    """The example graph, the worked examples' records and the golden catalog."""

    document: GraphDocument
    records: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Sources:
        sources = cls(graph_from_json(json.loads(GRAPH.read_text(encoding="utf-8"))))
        for path in sorted(EXAMPLES.glob("*/records/*.jsonl")):
            example = path.parts[-3]
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record["kind"] == "source_artifact":
                    sources.sizes[record["content_id"]] = record["size"]
                elif "id" in record:
                    sources.records[record["id"]] = (example, record)
        return sources

    @cached_property
    def reader(self) -> ReferenceReader:
        return ReferenceReader(self.document)

    @property
    def head(self) -> LedgerTx:
        return self.document.head

    def record(self, record_id: str) -> dict[str, Any]:
        return self.records[record_id][1]

    def package_id(self, record_id: str) -> ContentId:
        example = self.records[record_id][0]
        return content_id((PACKAGES / example / "manifest.json").read_bytes())

    def provenance(
        self, record_id: str, grounding: dict[str, Any] | None = None
    ) -> tuple[AssertionKind, ItemProvenance]:
        """A compiler record's grounding (or one field's), as item provenance."""
        record = self.record(record_id)
        grounding = grounding or record["provenance"]
        transform = self.record(grounding["transform"])
        return AssertionKind(grounding["assertion_kind"]), ItemProvenance(
            evidence=(evidence_ref_from_json(grounding["evidence"]),),
            records=(RecordId(record_id),),
            transform=Transform(
                transform["adapter_id"], transform["adapter_version"], transform["config_hash"]
            ),
        )

    def history(self, claim_id: str) -> Claim:
        (claim,) = (c for c in self.document.resolution.claims if c.id == claim_id)
        return claim

    def superseded_since(self, claims: list[Claim], as_of: int) -> tuple[Superseded, ...]:
        out = []
        for claim in sorted(claims, key=lambda c: c.id):
            at = self.history(claim.id).superseded_at
            if isinstance(at, Open) or not as_of < at <= self.head:
                continue
            by = sorted(c.id for c in self.document.resolution.claims if claim.id in c.supersedes)
            out.append(Superseded(claim.id, at, tuple(ClaimId(i) for i in by)))
        return tuple(out)


# --- Assembly ---------------------------------------------------------------------------------


@dataclass
class Candidate:
    """An item before fusion: how to build it, and where each channel ranked it."""

    build: Any  # Callable[[Relevance], Item]
    hits: list[tuple[Channel, int, float]]

    def item(self) -> Item:
        hits = tuple(
            ChannelHit(channel, rank, score)
            for channel, rank, score in sorted(self.hits, key=lambda h: str(h[0]))
        )
        fused = sum(1.0 / (RRF_K + rank) for _, rank, _ in self.hits)
        item: Item = self.build(Relevance(fused, hits))
        return item


def claim_candidate(claim: Claim, channel: Channel, rank: int) -> Candidate:
    return Candidate(lambda r: ClaimItem.of(claim, r), [(channel, rank, 1.0)])


@dataclass
class Worked:
    """One worked query: who asks, what, the query document and the packet that answers it."""

    name: str
    persona: str
    question: str
    query: dict[str, Any]
    packet: ContextPacket


def assemble(
    sources: Sources,
    query: dict[str, Any],
    *,
    as_of: int,
    candidates: list[Candidate],
    limits: Limits,
    include_inferred: bool,
    findings: tuple[ResolutionFinding, ...] = (),
    gaps: tuple[Gap, ...] = (),
    during: During | None = None,
) -> ContextPacket:
    ranked = sorted((c.item() for c in candidates), key=lambda i: (-i.relevance.score, i.id))
    kept, dropped = ranked[: limits.items], ranked[limits.items :]
    claims = [i.claim for i in kept if isinstance(i, ClaimItem)]
    ids = {c.id for c in claims}
    return ContextPacket(
        query_id=query_id(query),
        as_of=LedgerTx(as_of),
        head=sources.head,
        during=during,
        memory=MemorySnapshot(
            # The major of the graph read (a 1.x document stays major 1), as the engine reports it.
            sources.document.graph_schema_version,
            sources.document.generation,
            LedgerTx(as_of),
        ),
        ledger=LedgerSnapshot(CATALOG_API_VERSION),
        produced_by=ENGINE,
        inference_included=include_inferred,
        budget=BudgetUse.measured(
            limits, kept, dropped=len(dropped), exhausted=(Limit.ITEMS,) if dropped else ()
        ),
        items=tuple(kept),
        superseded_since=sources.superseded_since(claims, as_of),
        findings=tuple(f for f in findings if {f.claim, *f.others} & ids),
        gaps=tuple(sorted(gaps, key=lambda g: g.sort_key())),
    )


def evidence_candidates(
    sources: Sources, claims: list[Claim], start: int
) -> tuple[list[Candidate], list[Gap]]:
    """An evidence item per distinct source cited by ``claims`` (catalog channel), and a gap for
    every source no package in the golden catalog holds."""
    out: list[Candidate] = []
    unresolvable: list[str] = []
    seen: list[EvidenceRef] = []
    for claim in claims:
        for ref in claim.provenance.evidence:
            if ref in seen:
                continue
            seen.append(ref)
            source = str(ref.source)
            size = sources.sizes.get(source)
            status = EvidenceStatus.RESOLVED if size is not None else EvidenceStatus.UNRESOLVABLE
            if size is None:
                unresolvable.append(source)
            provenance = ItemProvenance(
                (ref,),
                claim.provenance.records,
                Transform(
                    claim.provenance.consolidator_id,
                    claim.provenance.consolidator_version,
                    claim.provenance.config_hash,
                ),
            )
            kind = (
                AssertionKind.OBSERVED
                if claim.assertion_kind == "observed"
                else AssertionKind.STATED
            )

            def build(
                r: Relevance,
                ref: EvidenceRef = ref,
                p: ItemProvenance = provenance,
                s: Any = size,
                st: EvidenceStatus = status,
                k: AssertionKind = kind,
            ) -> Item:
                return EvidenceItem(
                    assertion_kind=k,
                    confidence=NotApplicable(),
                    provenance=p,
                    relevance=r,
                    evidence=ref,
                    status=st,
                    size=Known(s) if s is not None else NotCovered(),
                )

            out.append(Candidate(build, [(Channel.CATALOG, start + len(out), 1.0)]))
    gaps = (
        [
            Gap(
                GapCode.UNRESOLVABLE,
                "",
                Channel.CATALOG,
                tuple(sorted(set(unresolvable))),
                "cited sources no registered package holds at as_of; the citations stand",
            )
        ]
        if unresolvable
        else []
    )
    return out, gaps


def withheld(sources: Sources, node: NodeRef, as_of: int, at: str) -> list[Gap]:
    """Inferred claims about ``node`` that an evidence-only query leaves out, named by id."""
    view = sources.reader.node(node, LedgerTx(as_of), include_inferred=True)
    if not isinstance(view, Known):
        return []
    ids = sorted(
        {c.id for c in (*view.value.claims, *view.value.incoming) if c.assertion_kind == "inferred"}
    )
    if not ids:
        return []
    return [
        Gap(
            GapCode.INFERRED_WITHHELD,
            at,
            Channel.GRAPH,
            tuple(ids),
            "inferred claims match but the query excludes inference",
        )
    ]


# --- The ten worked queries -------------------------------------------------------------------


def q01(s: Sources) -> Worked:
    query = _query(
        budget={"items": 20},
        graph=_graph(["recorded_by"]),
        include_inferred=False,
        subjects=[_subject("run", f"record:{RUN_DRONE}")],
    )
    result = s.reader.claims(run_node(RUN_DRONE), "recorded_by", s.head, include_inferred=False)
    claims = list(result.claims)
    evidence, gaps = evidence_candidates(
        s, [c for c in claims if c.assertion_kind == "observed"], 1
    )
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=[claim_candidate(c, Channel.GRAPH, i) for i, c in enumerate(claims, 1)]
        + evidence,
        limits=Limits(items=20),
        include_inferred=False,
        findings=result.findings,
        gaps=tuple(gaps),
    )
    return Worked(
        "q01-fleet-engineer-who-recorded-the-drone-run",
        "fleet engineer",
        "Which machine recorded the drone's run, as known now? Evidence only.",
        query,
        packet,
    )


def q02(s: Sources) -> Worked:
    query = _query(
        as_of=3,
        budget={"items": 20},
        graph=_graph(["recorded_by"]),
        include_inferred=False,
        subjects=[_subject("run", f"record:{RUN_DRONE}")],
    )
    result = s.reader.claims(
        run_node(RUN_DRONE), "recorded_by", LedgerTx(3), include_inferred=False
    )
    packet = assemble(
        s,
        query,
        as_of=3,
        candidates=[claim_candidate(c, Channel.GRAPH, i) for i, c in enumerate(result.claims, 1)],
        limits=Limits(items=20),
        include_inferred=False,
        findings=result.findings,
    )
    return Worked(
        "q02-fleet-engineer-same-question-at-a-stale-as-of",
        "fleet engineer",
        "The same question as known at transaction 3: what has changed since?",
        query,
        packet,
    )


def q03(s: Sources) -> Worked:
    query = _query(
        as_of=2,
        budget={"items": 20},
        graph=_graph(["recorded_by"]),
        include_inferred=True,
        subjects=[_subject("run", f"record:{RUN_QUADRUPED}")],
    )
    result = s.reader.claims(
        run_node(RUN_QUADRUPED), "recorded_by", LedgerTx(2), include_inferred=True
    )
    packet = assemble(
        s,
        query,
        as_of=2,
        candidates=[claim_candidate(c, Channel.GRAPH, i) for i, c in enumerate(result.claims, 1)],
        limits=Limits(items=20),
        include_inferred=True,
        findings=result.findings,
    )
    return Worked(
        "q03-safety-lead-quadruped-run-with-inferences",
        "safety lead",
        "Who recorded the quadruped's run, as known at transaction 2, with inferences marked?",
        query,
        packet,
    )


def q04(s: Sources) -> Worked:
    claim_id = "claim:sha256:03ef80551292669e368d326b22bd44b2a3c5a6461298c94469f16ad71110ad4a"
    query = _query(
        budget={"items": 10},
        explain=[{"claim_id": claim_id, "kind": "why"}],
        include_inferred=False,
    )
    result = s.reader.claims(run_node(RUN_DRONE), "recorded_by", s.head, include_inferred=False)
    (claim,) = (c for c in result.claims if c.id == claim_id)
    evidence, gaps = evidence_candidates(s, [claim], 1)
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=[claim_candidate(claim, Channel.GRAPH, 1), *evidence],
        limits=Limits(items=10),
        include_inferred=False,
        findings=result.findings,
        gaps=tuple(gaps),
    )
    return Worked(
        "q04-auditor-why-uav-0043",
        "auditor",
        "Why do we believe UAV-0043 recorded the drone's run? Show the evidence.",
        query,
        packet,
    )


def q05(s: Sources) -> Worked:
    during = During(RecordId(DRONE_LOG_CLOCK), 0, None)
    query = _query(
        budget={"items": 20},
        during={"clock": _domain(DRONE_LOG_CLOCK), "end": "open", "start": 0},
        graph=_graph(["recorded_by"]),
        include_inferred=False,
        subjects=[_subject("run", f"record:{RUN_DRONE}")],
    )
    window = Interval(Timestamp(0, RecordId(DRONE_LOG_CLOCK)), OPEN)
    result = s.reader.claims(
        run_node(RUN_DRONE), "recorded_by", s.head, window, include_inferred=False
    )
    gaps = (
        Gap(
            GapCode.OTHER_CLOCK,
            "/during",
            Channel.GRAPH,
            tuple(sorted(c.id for c in result.other_clocks)),
            "these claims hold on another clock; no clock bridge in the query places them on the"
            " drone log's clock, so they are named, not compared",
        ),
    )
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=[claim_candidate(c, Channel.GRAPH, i) for i, c in enumerate(result.claims, 1)],
        limits=Limits(items=20),
        include_inferred=False,
        findings=result.findings,
        gaps=gaps,
        during=during,
    )
    return Worked(
        "q05-auditor-drone-run-on-its-own-clock",
        "auditor",
        "Who flew the drone during its logged flight, on the log's own clock?",
        query,
        packet,
    )


def _series(s: Sources, stream: str, start: int, end: int, rank: int) -> Candidate:
    kind, provenance = s.provenance(stream)

    def build(r: Relevance) -> Item:
        return SeriesWindowItem(
            assertion_kind=kind,
            confidence=NotApplicable(),
            provenance=provenance,
            relevance=r,
            stream=RecordId(stream),
            clock=RecordId(QUAD_STREAM_CLOCK),
            start=start,
            end=end,
            arrow=ArrowHandle(s.package_id(stream), series_path(RecordId(stream))),
        )

    return Candidate(build, [(Channel.CATALOG, rank, 1.0)])


def _configuration(
    s: Sources, record_id: str, rank: int, subject: Any, channel: Channel = Channel.CATALOG
) -> Candidate:
    kind, provenance = s.provenance(record_id)
    record = s.record(record_id)

    def build(r: Relevance) -> Item:
        return ConfigurationItem(
            assertion_kind=kind,
            confidence=NotApplicable(),
            provenance=provenance,
            relevance=r,
            record=RecordId(record_id),
            record_kind=record["kind"],
            subject=subject,
            claims=(),
        )

    return Candidate(build, [(channel, rank, 1.0)])


QUAD_STREAMS: Final = (
    "rec:sha256:016871b7b1d0143f0c3beca54c2e52a0d3d9f93cd4f77ae33fd1f2436c1ec239",
    "rec:sha256:7c71ce89a8f62ebabbe687bf51780b02d01821436410852e426cf23678db5a91",
)
QUAD_HARDWARE: Final = "rec:sha256:8f6ee2cc76048b1d40463b67b8b409f732645d1708c33db799b79928dc056fe1"


def q06(s: Sources) -> Worked:
    start, end = 0, 100_000_000
    query = _query(
        budget={"items": 2, "latency_ms": 100},
        during={"clock": _domain(QUAD_STREAM_CLOCK), "end": end, "start": start},
        include_inferred=False,
        subjects=[_subject("machine", "asset-tag:QUAD-03")],
    )
    candidates = [
        _configuration(s, QUAD_HARDWARE, 1, NotCovered()),
        *(_series(s, stream, start, end, rank) for rank, stream in enumerate(QUAD_STREAMS, 2)),
    ]
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=candidates,
        limits=Limits(items=2, latency_ms=100),
        include_inferred=False,
        gaps=tuple(withheld(s, machine("QUAD-03"), s.head, "/subjects/0")),
        during=During(RecordId(QUAD_STREAM_CLOCK), start, end),
    )
    return Worked(
        "q06-vla-policy-quadruped-window-under-budget",
        "VLA policy at inference",
        "Configuration and the latest sensor window for QUAD-03, evidence only, two items at most.",
        query,
        packet,
    )


ARM_FRAME_TRANSFORM: Final = (
    "rec:sha256:e2c144c52c3f1c398b3a8f14738901a89d68bd7853dce97e558c112e596044f6"
)
ARM_CALIBRATION: Final = (
    "rec:sha256:db51ab20577a4798da4bc4014bb82c195f9ca6e5d3cd7fe9f455c4dbeeb1ec94"
)


def q07(s: Sources) -> Worked:
    frame = FrameRef("tool0", RecordId(ARM_FRAME_GRAPH))
    query = _query(
        budget={"items": 10},
        include_inferred=False,
        regions=[
            {
                "frame": {"frame_id": "tool0", "graph_id": ARM_FRAME_GRAPH},
                "shape": {"center": [0.0, 0.0, 0.0], "kind": "sphere", "radius": 0.5},
                "unit": "m",
            }
        ],
    )
    kind, provenance = s.provenance(ARM_FRAME_TRANSFORM)
    _, graph_provenance = s.provenance(ARM_FRAME_GRAPH)
    scene_provenance = ItemProvenance(
        graph_provenance.evidence + provenance.evidence,
        tuple(sorted((RecordId(ARM_FRAME_GRAPH), RecordId(ARM_FRAME_TRANSFORM)))),
        provenance.transform,
    )

    def scene(r: Relevance) -> Item:
        return SceneItem(
            assertion_kind=kind,
            confidence=NotApplicable(),
            provenance=scene_provenance,
            relevance=r,
            frame=frame,
            site=NotCovered(),
            nodes=(),
            claims=(),
            records=scene_provenance.records,
        )

    candidates = [
        Candidate(scene, [(Channel.CATALOG, 1, 1.0), (Channel.SPATIAL, 1, 1.0)]),
        _configuration(s, ARM_CALIBRATION, 2, Unknown()),
    ]
    gaps = (
        Gap(
            GapCode.NOT_COVERED,
            "/regions/0",
            Channel.SPATIAL,
            (),
            "Memory's spatial view answers not_covered until its spatial structure lands (G3):"
            " the scene rests on frame-graph records only, with no placed nodes",
        ),
    )
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=candidates,
        limits=Limits(items=10),
        include_inferred=False,
        gaps=gaps,
    )
    return Worked(
        "q07-simulator-setup-manipulator-scene",
        "simulator setup",
        "The scene around the arm's tool frame and the calibration a simulator needs.",
        query,
        packet,
    )


SITE_ROW: Final = "rec:sha256:175f3687f0b09d6724fd61e5ba07697637688a1e4e76ef8a7627451c0c5ce2a9"
IMAGE: Final = "rec:sha256:deec240cd3f458123c480eb7794a21c41facef5588f32c45e3dcd8888fe26006"


def q08(s: Sources) -> Worked:
    query = _query(
        budget={"items": 10, "tokens": 4000},
        include_inferred=False,
        text={"channels": ["lexical"], "fields": ["document", "record"], "text": "Berth 4"},
    )
    row = s.record(SITE_ROW)
    row_kind, row_provenance = s.provenance(SITE_ROW)
    image = s.record(IMAGE)
    image_kind, image_provenance = s.provenance(IMAGE)
    time = image["capture"]["time"]

    def span(r: Relevance) -> Item:
        return DocumentSpanItem(
            assertion_kind=row_kind,
            confidence=NotApplicable(),
            provenance=row_provenance,
            relevance=r,
            document=RecordId(row["table"]),
            evidence=row_provenance.evidence[0],
            text=NotCovered(),
        )

    def sample(r: Relevance) -> Item:
        return FrameItem(
            assertion_kind=image_kind,
            confidence=NotApplicable(),
            provenance=image_provenance,
            relevance=r,
            stream=NotApplicable(),
            at=Known(timestamp_from_json(time["value"])),
            evidence=image_provenance.evidence[0],
            encoding=Known(image["encoding"]),
            frame=NotCovered(),
        )

    candidates = [
        Candidate(span, [(Channel.LEXICAL, 1, 2.5)]),
        Candidate(sample, [(Channel.CATALOG, 1, 1.0)]),
    ]
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=candidates,
        limits=Limits(items=10, tokens=4000),
        include_inferred=False,
    )
    return Worked(
        "q08-fleet-engineer-site-register-and-image",
        "fleet engineer",
        "What does the site register say about Berth 4, and which camera image do we hold?",
        query,
        packet,
    )


def q09(s: Sources) -> Worked:
    query = _query(
        budget={"items": 20},
        graph=_graph(["recorded_by"], direction="in"),
        include_inferred=False,
        subjects=[_subject("machine", "asset-tag:QUAD-03", same_as_depth=1)],
    )
    view = s.reader.node(machine("QUAD-03"), s.head, include_inferred=False)
    assert isinstance(view, Known)
    claims = [c for c in view.value.incoming if c.predicate == "recorded_by"]
    candidates = [claim_candidate(c, Channel.GRAPH, i) for i, c in enumerate(claims, 1)]
    candidates += [
        _series(s, stream, 0, 100_000_000, rank) for rank, stream in enumerate(QUAD_STREAMS, 1)
    ]
    gaps = [
        *withheld(s, machine("QUAD-03"), s.head, "/subjects/0"),
        Gap(
            GapCode.NOT_COVERED,
            "/graph",
            Channel.GRAPH,
            (),
            "Memory answers episodes not_covered until episode structure lands (G3): runs are"
            " returned whole, not segmented into episodes",
        ),
    ]
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=candidates,
        limits=Limits(items=20),
        include_inferred=False,
        findings=view.value.findings,
        gaps=tuple(gaps),
    )
    return Worked(
        "q09-curator-runs-recorded-by-quad-03",
        "training-data curator",
        "Which runs did QUAD-03 record (following declared same_as), with their sensor windows?",
        query,
        packet,
    )


WAREHOUSE_RISK: Final = (
    "rec:sha256:6d03f4d6bf1da68121844b55d449611ad3de7cfbb6122b2d13228a681c47853c"
)
COMMISSIONING: Final = "rec:sha256:72691e355bb00480b91e2f5167b22628334850a49e4aefc9ab4ac03dafd2d68b"


def q10(s: Sources) -> Worked:
    query = _query(
        budget={"items": 10},
        include_inferred=False,
        text={
            "channels": ["lexical", "vector"],
            "fields": ["record"],
            "text": "AMR-07 risk assessment",
        },
    )
    risk_id = WAREHOUSE_RISK
    hazard = s.record(risk_id)["hazards"][0]["hazard"]
    kind, provenance = s.provenance(risk_id, hazard["provenance"])

    def span(r: Relevance) -> Item:
        return DocumentSpanItem(
            assertion_kind=kind,
            confidence=NotApplicable(),
            provenance=provenance,
            relevance=r,
            document=RecordId(risk_id),
            evidence=provenance.evidence[0],
            text=Known(hazard["value"]),
        )

    candidates = [
        Candidate(span, [(Channel.LEXICAL, 1, 3.75), (Channel.VECTOR, 2, 0.82)]),
        _configuration(s, COMMISSIONING, 1, NotCovered(), channel=Channel.LEXICAL),
    ]
    candidates[1].hits.append((Channel.VECTOR, 1, 0.86))
    gaps = (
        Gap(
            GapCode.NOT_COVERED,
            "/text",
            Channel.GRAPH,
            (),
            "the example graph holds no claims about this machine; Memory has not consolidated"
            " its deployment records, so no claim is returned",
        ),
    )
    packet = assemble(
        s,
        query,
        as_of=s.head,
        candidates=candidates,
        limits=Limits(items=10),
        include_inferred=False,
        gaps=gaps,
    )
    return Worked(
        "q10-safety-lead-risk-and-commissioning",
        "safety lead",
        "What risk assessment and commissioning baseline do we hold for warehouse AMR-07?",
        query,
        packet,
    )


WORKED: Final = (q01, q02, q03, q04, q05, q06, q07, q08, q09, q10)


def build() -> tuple[Worked, ...]:
    sources = Sources.load()
    return tuple(make(sources) for make in WORKED)


def files() -> dict[Path, bytes]:
    """Every golden file this module owns, by path: packets, queries and the schema."""
    out: dict[Path, bytes] = {SCHEMA: pretty(packet_schema())}
    for worked in build():
        out[QUERIES / f"{worked.name}.json"] = pretty(worked.query)
        out[PACKETS / f"{worked.name}.json"] = pretty(json.loads(canonical_bytes(worked.packet)))
    return out


if __name__ == "__main__":
    for path, data in files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        sys.stdout.write(f"{path.relative_to(ROOT)}\n")
