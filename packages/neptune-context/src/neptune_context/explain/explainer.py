"""The explainer (ADR 0010 §5): answers a query's ``explain`` clauses for the local engine.

``Explainer.explain(request)`` runs every ``Why`` and ``Diff`` clause against Memory's snapshot
and returns what the engine fuses into the packet: the claims each trail carries (graph hits,
scored like the graph channel's: weight, halved per level below a why root), the evidence items
for every cited source the Ledger resolves (catalog hits at half their best citing claim's
score), the findings and supersessions of carried claims, the gaps, and one trail per clause it
could answer. A clause it cannot answer is a gap at its pointer. Deterministic and read-only;
an exception inside one clause loses that clause (a gap), never the others.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune_context.explain.diff import explain_diff
from neptune_context.explain.history import ClaimHistory
from neptune_context.explain.run import Caps, Run
from neptune_context.explain.why import explain_why
from neptune_context.packets.model import (
    Channel,
    ChannelHit,
    ClaimItem,
    EvidenceItem,
    EvidenceStatus,
    Gap,
    GapCode,
    ItemProvenance,
    Relevance,
    Transform,
)
from neptune_context.query.model import Why
from neptune_context.retrieve.channel import ChannelAnswer, answer

if TYPE_CHECKING:
    from neptune_ledger.api import CatalogApi
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.reader import MemoryReader

    from neptune.model.jsonvalue import JsonObject
    from neptune.model.provenance import EvidenceRef
    from neptune_context.packets.model import Item
    from neptune_context.packets.trails import Trail
    from neptune_context.retrieve.channel import Retrieval

EVIDENCE_DECAY: Final = 0.5  # an evidence item scores half its best citing claim


@dataclass(frozen=True)
class Explained:
    """What the explain clauses add to a packet: two peer answers and the trails."""

    answers: tuple[ChannelAnswer, ...]
    trails: tuple[Trail, ...]


class Explainer:
    """``why`` and ``diff`` over a Memory reader, a claim history and, optionally, a Ledger."""

    def __init__(
        self,
        memory: MemoryReader,
        catalog: CatalogApi | None = None,
        *,
        history: ClaimHistory | None = None,
        caps: Caps | None = None,
    ) -> None:
        self._memory = memory
        self._catalog = catalog
        self._history = history if history is not None else _history_of(memory)
        self._caps = caps or Caps()

    @property
    def config(self) -> JsonObject:
        """Every setting that decides an explain answer (part of the engine's config hash)."""
        return {
            "caps": self._caps.to_json(),
            "claim_history": self._history is not None,
            "evidence_decay": EVIDENCE_DECAY,
            "ledger": self._catalog is not None,
        }

    def explain(self, request: Retrieval) -> Explained:
        run = Run(self._memory, self._catalog, self._history, request, self._caps)
        trails: list[Trail] = []
        for index, clause in enumerate(request.query.explain):
            try:
                trail = (
                    explain_why(run, index, clause.claim_id)
                    if isinstance(clause, Why)
                    else explain_diff(run, index, clause)
                )
            except Exception as exc:  # partial success: one failing clause is a gap
                run.gap(
                    GapCode.NOT_COVERED,
                    f"/explain/{index}",
                    [],
                    f"this explain clause failed: {type(exc).__name__}: {exc}",
                )
                continue
            if trail is not None:
                trails.append(trail)
        claims = sorted(run.carried.values(), key=lambda pair: pair[1].id)
        held = {c.id for _, c in claims}
        superseded = [s for _, c in claims if (s := run.superseded_since(c)) is not None]
        graph = answer(
            Channel.GRAPH,
            [(score, ClaimItem.of(claim, _PROVISIONAL)) for score, claim in claims],
            gaps=run.gaps,
            findings=[f for f in run.findings.values() if {f.claim, *f.others} & held],
            superseded=superseded,
        )
        return Explained((graph, self._evidence(run)), tuple(trails))

    def _evidence(self, run: Run) -> ChannelAnswer:
        """An evidence item per distinct source ref the trails cite, resolved at the packet's
        ``as_of``; without a Ledger, one gap per clause that cites evidence."""
        if not run.cited:
            return ChannelAnswer(Channel.CATALOG)
        if self._catalog is None:
            gap = Gap(
                GapCode.NOT_COVERED,
                "/explain",
                Channel.CATALOG,
                (),
                "no Ledger catalog is attached: evidence refs are cited, not resolved to bytes",
            )
            return ChannelAnswer(Channel.CATALOG, gaps=(gap,))
        best: dict[EvidenceRef, tuple[float, Claim]] = {}
        for value, claim in sorted(run.cited.values(), key=lambda pair: pair[1].id):
            for ref in claim.provenance.evidence:
                held = best.get(ref)
                if held is None or value > held[0]:
                    best[ref] = (value, claim)
        scored: list[tuple[float, Item]] = []
        gaps: list[Gap] = []
        unresolvable: set[str] = set()
        for ref, (value, claim) in sorted(best.items(), key=lambda p: _ref_key(p[0])):
            item = self._resolve(ref, claim, run, gaps)
            if item is None:
                continue
            if item.status is EvidenceStatus.UNRESOLVABLE:
                unresolvable.add(str(ref.source))
            scored.append((float(value * EVIDENCE_DECAY), item))
        if unresolvable:
            gaps.append(
                Gap(
                    GapCode.UNRESOLVABLE,
                    "/explain",
                    Channel.CATALOG,
                    tuple(sorted(unresolvable)),
                    "cited sources no registered package holds at as_of; the citations stand",
                )
            )
        return answer(Channel.CATALOG, scored, gaps=gaps)

    def _resolve(
        self, ref: EvidenceRef, claim: Claim, run: Run, gaps: list[Gap]
    ) -> EvidenceItem | None:
        from neptune_ledger.api import CodecError, EvidenceAnchor, from_json

        assert self._catalog is not None
        try:
            anchor = from_json(EvidenceAnchor, ref.to_json())
            resolution = self._catalog.resolve(anchor, as_of=int(run.request.snapshot.as_of))
        except (CodecError, ValueError, TypeError) as exc:
            gaps.append(_evidence_gap(ref, f"not a Ledger evidence anchor: {exc}"))
            return None
        except Exception as exc:  # the Ledger failing loses this item, never the trail
            gaps.append(_evidence_gap(ref, f"the Ledger could not resolve it: {exc}"))
            return None
        status = EvidenceStatus(resolution.status)
        size: Any = NotCovered()
        if status is EvidenceStatus.RESOLVED:
            size = Known(resolution.size.value) if isinstance(resolution.size, Known) else Unknown()
        provenance = claim.provenance
        kind = (
            AssertionKind.OBSERVED
            if claim.assertion_kind == AssertionKind.OBSERVED
            else AssertionKind.STATED
        )
        return EvidenceItem(
            assertion_kind=kind,
            confidence=NotApplicable(),
            provenance=ItemProvenance(
                (ref,),
                provenance.records,
                Transform(
                    provenance.consolidator_id,
                    provenance.consolidator_version,
                    provenance.config_hash,
                ),
            ),
            relevance=_PROVISIONAL_CATALOG,
            evidence=ref,
            status=status,
            size=size,
        )


def _history_of(memory: object) -> ClaimHistory | None:
    return memory if isinstance(memory, ClaimHistory) else None


def _ref_key(ref: EvidenceRef) -> str:
    from neptune.identity.canonical_json import dumps

    return dumps(ref.to_json()).decode("utf-8")


def _evidence_gap(ref: EvidenceRef, detail: str) -> Gap:
    return Gap(GapCode.NOT_COVERED, "/explain", Channel.CATALOG, (str(ref.source),), detail[:2000])


# Provisional relevance before ``answer`` ranks a hit (it rewrites every one).
_PROVISIONAL: Final = Relevance(1.0, (ChannelHit(Channel.GRAPH, 1, 1.0),))
_PROVISIONAL_CATALOG: Final = Relevance(1.0, (ChannelHit(Channel.CATALOG, 1, 1.0),))
