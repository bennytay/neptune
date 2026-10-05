"""The graph-schema contract suite: checks any ``MemoryReader`` against the reference (ADR 0006 §8).

A downstream package (Context, Deploy, Learn), or a new reader implementation, runs every check
in ``CHECKS`` with a *factory* that loads a graph document into the reader under test, on the
published golden graph (``contracts/graph-schema/v<version>/golden/graph.json``)::

    import pytest
    from neptune_memory.contract.suite import CHECKS, load_golden

    GOLDEN = load_golden(REPO / "contracts/graph-schema/v1.4.0/golden/graph.json")

    @pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
    def test_graph_schema_contract(check):
        check(my_reader_factory, GOLDEN)

A check raises ``ContractViolation`` (an ``AssertionError``) with the first difference it finds.
Expected answers come from ``schema.reference.ReferenceReader``; the checks that do not compare
with it pin the guarantees directly: as-of never leaks later knowledge, superseded versions
vanish exactly at their transaction, the inference filter keeps only deterministic claims,
findings travel with the claims they name, and ``episodes``/``spatial`` answer ``NotCovered``.
``StubReader`` is a plausible empty reader the suite must reject ("red against a stub").
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Final, TypeAlias

from neptune.identity.ids import record_id
from neptune.model.frames import FrameRef
from neptune.model.knowledge import Known, NotCovered
from neptune.model.time import Timestamp
from neptune_memory.schema import GRAPH_SCHEMA_VERSION
from neptune_memory.schema.claim import Claim, ClaimId, is_inferred
from neptune_memory.schema.codec import GraphDocument, graph_from_json
from neptune_memory.schema.interval import OPEN, Interval, LedgerTx, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.reader import (
    AsOfBeyondHeadError,
    ClaimsResult,
    EpisodeFilter,
    MemoryReader,
    NeighboursResult,
)
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import as_of as snapshot_at

if TYPE_CHECKING:
    from pathlib import Path

    from neptune.model.ids import ConfigHash
    from neptune.model.knowledge import Knowledge
    from neptune_memory.schema.reader import EpisodeView, NodeView, SpatialView

ReaderFactory: TypeAlias = Callable[[GraphDocument], MemoryReader]
Check: TypeAlias = Callable[[ReaderFactory, GraphDocument], None]
MAX_HOPS: Final = 3
ABSENT: Final = NodeRef(NodeType.MACHINE, "contract:absent-node")


class ContractViolation(AssertionError):
    """A reader broke a graph-schema guarantee."""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ContractViolation(message)


def load_golden(path: Path) -> GraphDocument:
    """A published golden graph document, parsed strictly."""
    return graph_from_json(json.loads(path.read_text(encoding="utf-8")))


def _transactions(golden: GraphDocument) -> range:
    return range(golden.head + 1)


def _nodes(golden: GraphDocument) -> list[NodeRef]:
    found: set[NodeRef] = set()
    for claim in golden.resolution.claims:
        found.add(claim.subject)
        if isinstance(claim.object, NodeRef):
            found.add(claim.object)
    return sorted(found, key=lambda n: (str(n.node_type), n.node_id))


def _subjects(golden: GraphDocument) -> list[tuple[NodeRef, str | None]]:
    pairs = {(c.subject, c.predicate) for c in golden.resolution.claims}
    subjects = {c.subject for c in golden.resolution.claims}
    out: list[tuple[NodeRef, str | None]] = [(s, None) for s in subjects]
    out += sorted(pairs, key=lambda p: (p[0].node_id, p[1]))
    return sorted(out, key=lambda p: (str(p[0].node_type), p[0].node_id, p[1] or ""))


def _ids(claims: tuple[Claim, ...]) -> list[str]:
    return [c.id for c in claims]


# --- Checks ------------------------------------------------------------------------------------


def check_identity(factory: ReaderFactory, golden: GraphDocument) -> None:
    """Version, generation and head match the document the reader was loaded with."""
    reader = factory(golden)
    _expect(isinstance(reader, MemoryReader), "the reader does not implement MemoryReader")
    _expect(
        reader.graph_schema_version == GRAPH_SCHEMA_VERSION,
        f"graph_schema_version {reader.graph_schema_version} != {GRAPH_SCHEMA_VERSION}",
    )
    _expect(reader.generation == golden.generation, "generation differs from the document's")
    _expect(reader.head == golden.head, f"head {reader.head} != {golden.head}")


def check_claims_match_reference(factory: ReaderFactory, golden: GraphDocument) -> None:
    """Every (subject, predicate) at every transaction, with and without inference, equals the
    reference: the same claims, presented the same way, with the same findings."""
    reader, reference = factory(golden), ReferenceReader(golden)
    for tx in _transactions(golden):
        for subject, predicate in _subjects(golden):
            for inferred in (True, False):
                got = reader.claims(subject, predicate, ledger_tx(tx), include_inferred=inferred)
                want = reference.claims(
                    subject, predicate, ledger_tx(tx), include_inferred=inferred
                )
                _expect(
                    got == want,
                    f"claims({subject.node_id}, {predicate}, as_of={tx}, inferred={inferred}):"
                    f" got {_ids(got.claims)}, want {_ids(want.claims)}",
                )


def check_during_filters_on_one_clock(factory: ReaderFactory, golden: GraphDocument) -> None:
    """``during`` keeps overlapping claims on its clock and sets other clocks aside, never drops."""
    reader, reference = factory(golden), ReferenceReader(golden)
    tx = golden.head
    for claim in golden.resolution.claims:
        windows = [claim.valid, Interval(claim.valid_from, _after(claim.valid_from))]
        for during in windows:
            got = reader.claims(claim.subject, None, tx, during)
            want = reference.claims(claim.subject, None, tx, during)
            _expect(got == want, f"claims(during={during.to_json()}) differs from the reference")
            kept = {c.id for c in (*got.claims, *got.other_clocks)}
            every = reader.claims(claim.subject, None, tx).claims
            due = {
                c.id
                for c in every
                if c.valid.domain_id != during.domain_id or c.valid.overlaps(during)
            }
            _expect(kept == due, "during dropped a claim on another clock, or kept one outside it")


def _after(start: Timestamp) -> Timestamp:
    return Timestamp(start.ticks + 1, start.domain_id)


def check_as_of_never_leaks(factory: ReaderFactory, golden: GraphDocument) -> None:
    """A snapshot holds only what was active at ``as_of``, each as known then (ADR 0006 §6)."""
    reader = factory(golden)
    for tx in _transactions(golden):
        for subject, _ in _subjects(golden):
            result = reader.claims(subject, None, ledger_tx(tx))
            for claim in (*result.claims, *result.other_clocks):
                _expect(claim.recorded_at <= tx, f"{claim.id} recorded after as_of {tx}")
                _expect(
                    isinstance(claim.superseded_at, Open),
                    f"{claim.id} at as_of {tx} shows a later supersession ({claim.superseded_at})",
                )
            for finding in result.findings:
                _expect(finding.recorded_at <= tx, f"{finding.id} recorded after as_of {tx}")
                _expect(
                    isinstance(finding.superseded_at, Open),
                    f"{finding.id} at as_of {tx} shows a later supersession",
                )


def check_superseded_versions_vanish_exactly(factory: ReaderFactory, golden: GraphDocument) -> None:
    """A version superseded at ``s`` is visible at ``s - 1`` (if recorded by then), not at ``s``."""
    reader = factory(golden)
    superseded = [c for c in golden.resolution.claims if not isinstance(c.superseded_at, Open)]
    _expect(bool(superseded), "the golden graph must contain a superseded version")
    for claim in superseded:
        assert not isinstance(claim.superseded_at, Open)
        end = claim.superseded_at
        after = _ids(reader.claims(claim.subject, claim.predicate, end).claims)
        _expect(claim.id not in after, f"{claim.id} still visible at its supersession {end}")
        if claim.recorded_at < end:
            before = reader.claims(claim.subject, claim.predicate, ledger_tx(end - 1)).claims
            _expect(claim.id in _ids(before), f"{claim.id} not visible before it was superseded")
            (shown,) = [c for c in before if c.id == claim.id]
            _expect(shown.superseded_at == OPEN, f"{claim.id} leaks its supersession before {end}")


def check_superseding_claim_names_what_it_replaced(
    factory: ReaderFactory, golden: GraphDocument
) -> None:
    """Every visible claim's ``supersedes`` is the reference's, so the displacement is traceable."""
    reader, reference = factory(golden), ReferenceReader(golden)
    for claim in golden.resolution.claims:
        if not claim.supersedes:
            continue
        tx = claim.recorded_at
        got = {c.id: c.supersedes for c in reader.claims(claim.subject, claim.predicate, tx).claims}
        want = {
            c.id: c.supersedes for c in reference.claims(claim.subject, claim.predicate, tx).claims
        }
        _expect(got == want, f"supersedes at {tx} differs for {claim.subject.node_id}")


def check_inference_filter(factory: ReaderFactory, golden: GraphDocument) -> None:
    """``include_inferred=False`` returns exactly the observed and stated claims."""
    reader = factory(golden)
    saw_inferred = False
    for tx in _transactions(golden):
        for subject, predicate in _subjects(golden):
            every = reader.claims(subject, predicate, ledger_tx(tx))
            kept = reader.claims(subject, predicate, ledger_tx(tx), include_inferred=False)
            saw_inferred |= any(is_inferred(c.assertion_kind) for c in every.claims)
            _expect(
                not any(is_inferred(c.assertion_kind) for c in kept.claims),
                f"inference filter returned an inferred claim about {subject.node_id} at {tx}",
            )
            deterministic = tuple(c for c in every.claims if not is_inferred(c.assertion_kind))
            _expect(kept.claims == deterministic, "inference filter dropped a deterministic claim")
    _expect(saw_inferred, "no inferred claim was ever visible: the filter is untested")


def check_provenance_always_present(factory: ReaderFactory, golden: GraphDocument) -> None:
    """Every returned claim carries evidence, its transform, and a model exactly if inferred."""
    reader = factory(golden)
    seen = 0
    for subject, _ in _subjects(golden):
        for claim in reader.claims(subject, None, golden.head).claims:
            seen += 1
            p = claim.provenance
            _expect(bool(p.evidence), f"{claim.id} has no evidence")
            _expect(bool(p.consolidator_id and p.consolidator_version), f"{claim.id}: no transform")
            _expect(
                is_inferred(claim.assertion_kind) == (p.model is not None),
                f"{claim.id}: model present iff inferred",
            )
    _expect(seen > 0, "no claim was returned at the head transaction")


def check_findings_travel_with_claims(factory: ReaderFactory, golden: GraphDocument) -> None:
    """Each finding active at ``as_of`` comes back with a query about its own claim's subject,
    even when that claim is no longer (or never was) current; an inactive one never does."""
    reader = factory(golden)
    by_id = {c.id: c for c in golden.resolution.claims}
    _expect(bool(golden.resolution.findings), "the golden graph must contain a finding")
    for finding in golden.resolution.findings:
        end = finding.superseded_at if not isinstance(finding.superseded_at, Open) else None
        for tx in _transactions(golden):
            active = finding.recorded_at <= tx and (end is None or tx < end)
            subject = by_id[finding.claim].subject
            got = [f.id for f in reader.claims(subject, None, ledger_tx(tx)).findings]
            _expect(
                (finding.id in got) == active,
                f"finding {finding.id} {'missing' if active else 'leaked'} at as_of {tx}",
            )


def check_nodes(factory: ReaderFactory, golden: GraphDocument) -> None:
    """``node`` equals the reference for every node at every transaction; absence is NotCovered."""
    reader, reference = factory(golden), ReferenceReader(golden)
    for tx in _transactions(golden):
        for node in [*_nodes(golden), ABSENT]:
            for inferred in (True, False):
                got = reader.node(node, ledger_tx(tx), include_inferred=inferred)
                want = reference.node(node, ledger_tx(tx), include_inferred=inferred)
                _expect(
                    got == want,
                    f"node({node.node_id}, as_of={tx}, inferred={inferred}) differs from the"
                    " reference",
                )
    _expect(
        isinstance(reader.node(ABSENT, golden.head), NotCovered),
        "a node the graph never names must be NotCovered, not an empty fact",
    )


def check_neighbours(factory: ReaderFactory, golden: GraphDocument) -> None:
    """Same nodes at the same shortest depths and the same findings as the reference; each
    ``via`` is a path of claims active at ``as_of``, exactly as the snapshot shows them."""
    reader, reference = factory(golden), ReferenceReader(golden)
    saw_findings = False
    for tx in _transactions(golden):
        active = {c.id: c for c in snapshot_at(golden.resolution, ledger_tx(tx)).claims}
        for node in _nodes(golden):
            for hops in range(MAX_HOPS + 1):
                for inferred in (True, False):
                    got = reader.neighbours(node, hops, ledger_tx(tx), include_inferred=inferred)
                    want = reference.neighbours(
                        node, hops, ledger_tx(tx), include_inferred=inferred
                    )
                    _neighbours_agree(got, want, ledger_tx(tx), inferred, active)
                    saw_findings |= bool(want.findings)
    _expect(saw_findings, "no neighbours result had findings: the findings check is untested")


def _neighbours_agree(
    got: NeighboursResult,
    want: NeighboursResult,
    tx: LedgerTx,
    inferred: bool,
    active: Mapping[ClaimId, Claim],
) -> None:
    where = f"neighbours({got.start.node_id}, {got.hops}, as_of={tx})"
    _expect(
        (got.start, got.hops, got.as_of) == (want.start, want.hops, want.as_of),
        f"{where}: the result does not echo its query",
    )
    _expect(
        [(n.node, n.depth) for n in got.neighbours] == [(n.node, n.depth) for n in want.neighbours],
        f"{where}: nodes or depths differ from the reference",
    )
    _expect(got.findings == want.findings, f"{where}: findings differ from the reference")
    for neighbour in got.neighbours:
        _expect(len(neighbour.via) == neighbour.depth, f"{where}: via length != depth")
        here = got.start
        for claim in neighbour.via:
            _expect(active.get(claim.id) == claim, f"{where}: via {claim.id} is not active")
            _expect(inferred or not is_inferred(claim.assertion_kind), f"{where}: inferred via")
            if claim.subject == here and isinstance(claim.object, NodeRef):
                here = claim.object
            elif claim.object == here:
                here = claim.subject
            else:
                raise ContractViolation(f"{where}: via is not a path")
        _expect(here == neighbour.node, f"{where}: via does not end at {neighbour.node.node_id}")


def check_provisional_queries_are_not_covered(
    factory: ReaderFactory, golden: GraphDocument
) -> None:
    """``episodes`` and ``spatial`` are G3: v1 answers NotCovered, never an empty "fact"."""
    reader = factory(golden)
    episodes = reader.episodes(EpisodeFilter(as_of=golden.head))
    _expect(isinstance(episodes, NotCovered), f"episodes must be NotCovered in v1: {episodes!r}")
    site = NodeRef(NodeType.SITE, "contract:any-site")
    frame = FrameRef("map", record_id("memory.contract.frame_graph", {"site": "any"}))
    spatial = reader.spatial(site, frame, golden.head)
    _expect(isinstance(spatial, NotCovered), f"spatial must be NotCovered in v1: {spatial!r}")


def check_as_of_beyond_head_is_refused(factory: ReaderFactory, golden: GraphDocument) -> None:
    """A transaction later than the head is refused, so one ``as_of`` always has one answer."""
    reader = factory(golden)
    later = ledger_tx(golden.head + 1)
    subject = golden.resolution.claims[0].subject
    calls: list[Callable[[], object]] = [
        lambda: reader.claims(subject, None, later),
        lambda: reader.node(subject, later),
        lambda: reader.neighbours(subject, 1, later),
    ]
    for call in calls:
        try:
            call()
        except AsOfBeyondHeadError:
            continue
        raise ContractViolation("a query beyond the head transaction was answered")


def check_golden_story(factory: ReaderFactory, golden: GraphDocument) -> None:
    """The golden graph's own story, stated without the reference (ADR 0006 §10): an inferred
    recorder is current until an operator's stated claim supersedes it at a later transaction."""
    reader = factory(golden)
    guesses = [
        c
        for c in golden.resolution.claims
        if is_inferred(c.assertion_kind)
        and not isinstance(c.superseded_at, Open)
        and c.recorded_at < c.superseded_at  # it was current once
    ]
    _expect(bool(guesses), "the golden graph must contain a superseded inferred claim")
    for guess in guesses:
        assert not isinstance(guess.superseded_at, Open)
        end = guess.superseded_at
        before = reader.claims(guess.subject, guess.predicate, ledger_tx(end - 1)).claims
        _expect(guess.id in _ids(before), "the inferred claim was not current before correction")
        after = reader.claims(guess.subject, guess.predicate, end).claims
        _expect(guess.id not in _ids(after), "the inferred claim survived its correction")
        _expect(
            any(guess.id in c.supersedes and not is_inferred(c.assertion_kind) for c in after),
            "no deterministic claim names the inferred claim it superseded",
        )


CHECKS: Final[tuple[Check, ...]] = (
    check_identity,
    check_claims_match_reference,
    check_during_filters_on_one_clock,
    check_as_of_never_leaks,
    check_superseded_versions_vanish_exactly,
    check_superseding_claim_names_what_it_replaced,
    check_inference_filter,
    check_provenance_always_present,
    check_findings_travel_with_claims,
    check_nodes,
    check_neighbours,
    check_provisional_queries_are_not_covered,
    check_as_of_beyond_head_is_refused,
    check_golden_story,
)


class StubReader:
    """A reader that type-checks and answers nothing: every check that sees data must fail."""

    def __init__(self, document: GraphDocument) -> None:
        self._document = document

    @property
    def graph_schema_version(self) -> int:
        return GRAPH_SCHEMA_VERSION

    @property
    def generation(self) -> ConfigHash:
        return self._document.generation

    @property
    def head(self) -> LedgerTx:
        return self._document.head

    def node(
        self, node: NodeRef, as_of: LedgerTx, *, include_inferred: bool = True
    ) -> Knowledge[NodeView]:
        return NotCovered()

    def claims(
        self,
        subject: NodeRef,
        predicate: str | None,
        as_of: LedgerTx,
        during: Interval | None = None,
        *,
        include_inferred: bool = True,
    ) -> ClaimsResult:
        return ClaimsResult(as_of, (), (), ())

    def neighbours(
        self, node: NodeRef, hops: int, as_of: LedgerTx, *, include_inferred: bool = True
    ) -> NeighboursResult:
        return NeighboursResult(node, hops, as_of, (), ())

    def episodes(self, filter: EpisodeFilter) -> Knowledge[tuple[EpisodeView, ...]]:
        return Known(())  # the empty "fact" the contract forbids

    def spatial(self, site: NodeRef, frame: FrameRef, as_of: LedgerTx) -> Knowledge[SpatialView]:
        return NotCovered()
