"""One explain run's shared state (ADR 0010): Memory reads, admission rules, what to carry.

``why`` and ``diff`` read the same snapshot through the same rules as the graph channel
(ADR 0007 §3): a claim is carried only when the pinned graph-schema describes it, inference is
asked for (or it is not inferred) and, with a ``during``, it sits on a clock the query asked for
or bridged. A claim that fails is never dropped silently: it is withheld (inference), named
(beyond the pin, another clock) and reported in a gap at the clause's pointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING

from neptune_memory.schema.claim import is_inferred
from neptune_memory.schema.interval import Open, ledger_tx

from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import Known
from neptune_context import pinned
from neptune_context.answer import allowed_clocks
from neptune_context.packets.model import Channel, Gap, GapCode, Superseded
from neptune_context.packets.trails import MAX_DIFF_CLAIMS, MAX_WHY_DEPTH, MAX_WHY_STEPS
from neptune_context.pins import GRAPH_SCHEMA_VERSION
from neptune_context.retrieve.graph import weight

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune_ledger.api import CatalogApi
    from neptune_memory.schema.claim import Claim, ClaimId
    from neptune_memory.schema.interval import LedgerTx
    from neptune_memory.schema.nodes import NodeRef
    from neptune_memory.schema.reader import ClaimsResult, MemoryReader
    from neptune_memory.schema.supersede import ResolutionFinding

    from neptune_context.explain.history import ClaimHistory
    from neptune_context.retrieve.channel import Retrieval


@dataclass(frozen=True)
class Caps:
    """How far a trail may reach; every cut is a gap, never a silent stop."""

    depth: int = 3  # why: levels below the root
    fan_out: int = 16  # why: related claims followed from one claim
    steps: int = 128  # why: claims one tree names
    diff_claims: int = 512  # diff: claims one diff names

    def __post_init__(self) -> None:
        bounds = {
            "depth": (0, MAX_WHY_DEPTH),
            "fan_out": (1, MAX_WHY_STEPS),
            "steps": (1, MAX_WHY_STEPS),
            "diff_claims": (1, MAX_DIFF_CLAIMS),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"Caps.{name} is an integer in [{low}, {high}]: {value!r}")

    def to_json(self) -> dict[str, int]:
        return {
            "depth": self.depth,
            "diff_claims": self.diff_claims,
            "fan_out": self.fan_out,
            "steps": self.steps,
        }


__all__ = ["Caps", "Run", "object_key", "weight"]


def object_key(claim: Claim) -> bytes:
    """Objects compare as canonical JSON, as Memory's resolver compares them."""
    return dumps(claim.object.to_json())


@dataclass(frozen=True)
class _Mark:
    carried: dict[ClaimId, tuple[float, Claim]]
    cited: dict[ClaimId, tuple[float, Claim]]
    findings: dict[str, ResolutionFinding]
    gaps: int


@dataclass
class Run:
    """Reads and accumulations for every explain clause of one query."""

    memory: MemoryReader
    catalog: CatalogApi | None
    history: ClaimHistory | None
    request: Retrieval
    caps: Caps
    carried: dict[ClaimId, tuple[float, Claim]] = field(default_factory=dict)
    cited: dict[ClaimId, tuple[float, Claim]] = field(default_factory=dict)  # evidence to resolve
    findings: dict[str, ResolutionFinding] = field(default_factory=dict)
    gaps: list[Gap] = field(default_factory=list)
    _subjects: dict[tuple[NodeRef, int], ClaimsResult] = field(default_factory=dict)
    _views: dict[tuple[NodeRef, int], tuple[tuple[Claim, ...], tuple[ResolutionFinding, ...]]] = (
        field(default_factory=dict)
    )

    @property
    def as_of(self) -> LedgerTx:
        """Memory's snapshot: the packet's claims are as Memory knew them then."""
        return self.request.snapshot.memory_as_of

    @property
    def include_inferred(self) -> bool:
        return self.request.query.include_inferred

    @cached_property
    def clocks(self) -> frozenset[str] | None:
        """The clocks a carried claim may be on (``None``: any), computed once per query."""
        return allowed_clocks(self.request.query)

    def mark(self) -> _Mark:
        """A point to roll back to when a clause fails partway (see ``rollback``)."""
        return _Mark(dict(self.carried), dict(self.cited), dict(self.findings), len(self.gaps))

    def rollback(self, mark: _Mark) -> None:
        """Drop what a failed clause added: its claims, citations, findings and gaps."""
        self.carried, self.cited, self.findings = mark.carried, mark.cited, mark.findings
        del self.gaps[mark.gaps :]

    def gap(self, code: GapCode, at: str, refs: Iterable[str], detail: str) -> None:
        self.gaps.append(Gap(code, at, Channel.GRAPH, tuple(sorted(set(refs))), detail[:2000]))

    # --- Memory --------------------------------------------------------------------------------

    def about_subject(self, subject: NodeRef, tx: int) -> ClaimsResult:
        """Every claim about ``subject`` current at ``tx`` (any predicate, inferred included)."""
        key = (subject, tx)
        if key not in self._subjects:
            self._subjects[key] = self.memory.claims(
                subject, None, ledger_tx(tx), include_inferred=True
            )
        return self._subjects[key]

    def touching(self, node: NodeRef, tx: int) -> tuple[Claim, ...]:
        """Claims current at ``tx`` whose subject or object is ``node``, sorted by id; the
        findings that qualify them are kept."""
        key = (node, tx)
        if key not in self._views:
            view = self.memory.node(node, ledger_tx(tx), include_inferred=True)
            claims: tuple[Claim, ...] = ()
            findings: tuple[ResolutionFinding, ...] = ()
            if isinstance(view, Known):
                claims = tuple(
                    sorted(
                        {c.id: c for c in (*view.value.claims, *view.value.incoming)}.values(),
                        key=lambda c: c.id,
                    )
                )
                findings = view.value.findings
            self._views[key] = (claims, findings)
        claims, findings = self._views[key]
        if tx == self.as_of:  # every read, so a rolled-back clause never hides them from the next
            for finding in findings:
                self.findings.setdefault(finding.id, finding)
        return claims

    # --- Admission -----------------------------------------------------------------------------

    def withheld(self, claim: Claim) -> bool:
        """An inferred claim the query excludes: named only in an ``inferred_withheld`` gap."""
        return is_inferred(claim.assertion_kind) and not self.include_inferred

    def unplaceable(self, claim: Claim) -> str | None:
        """Why ``claim`` may be named but not carried (beyond the pin, another clock), or None."""
        reason = pinned.claim_beyond_pin(claim)
        if reason is not None:
            return (
                f"{reason} is newer than Context's pinned graph-schema {GRAPH_SCHEMA_VERSION}:"
                " the claim is named, not carried"
            )
        allowed = self.clocks
        if allowed is not None and str(claim.valid.domain_id) not in allowed:
            return (
                f"on clock {claim.valid.domain_id}, which the query neither asked for nor bridged:"
                " named, not compared"
            )
        return None

    def cite(self, claim: Claim, score: float) -> None:
        """Resolve ``claim``'s evidence refs (carried or only named), best score kept."""
        held = self.cited.get(claim.id)
        if held is None or score > held[0]:
            self.cited[claim.id] = (score, claim)

    def carry(self, claim: Claim, score: float) -> None:
        """Carry ``claim`` (current at Memory's snapshot) as a claim item, best score kept."""
        held = self.carried.get(claim.id)
        if held is None or score > held[0]:
            self.carried[claim.id] = (score, claim)

    # --- Supersessions -------------------------------------------------------------------------

    def superseded_since(self, claim: Claim) -> Superseded | None:
        """When, in ``(memory_as_of, head]``, Memory stopped holding ``claim``, and what by; a
        gap (never an invented successor) when no version current then names it."""
        low = int(self.as_of)
        high = min(int(self.memory.head), int(self.request.snapshot.head))
        if low >= high:
            return None
        at: int | None = None
        by: list[str] = []
        if self.history is not None:
            stored = self.history.version(claim.id)
            ended = None if stored is None else stored.superseded_at
            if ended is not None and not isinstance(ended, Open) and low < int(ended) <= high:
                at = int(ended)
                by = sorted(
                    c.id for c in self.history.superseded_by(claim.id) if c.recorded_at == at
                )
        else:

            def current(tx: int) -> tuple[Claim, ...]:
                result = self.memory.claims(claim.subject, claim.predicate, ledger_tx(tx))
                return (*result.claims, *result.other_clocks)

            if all(c.id != claim.id for c in current(high)):
                while high - low > 1:  # current at ``low``, gone at ``high``
                    middle = (low + high) // 2
                    if any(c.id == claim.id for c in current(middle)):
                        low = middle
                    else:
                        high = middle
                at = high
                by = sorted(c.id for c in current(high) if claim.id in c.supersedes)
                if not by:  # as the graph channel looks: every edge of the subject then
                    view = self.memory.node(claim.subject, ledger_tx(high), include_inferred=True)
                    if isinstance(view, Known):
                        edges = (*view.value.claims, *view.value.incoming)
                        by = sorted({c.id for c in edges if claim.id in c.supersedes})
        if at is None:
            return None
        if not by:
            self.gap(
                GapCode.UNKNOWN,
                "/as_of",
                [claim.id],
                f"Memory stopped holding this claim at transaction {at}; no version recorded"
                " then names it as superseded",
            )
            return None
        return Superseded(claim.id, ledger_tx(at), tuple(by))  # type: ignore[arg-type]
