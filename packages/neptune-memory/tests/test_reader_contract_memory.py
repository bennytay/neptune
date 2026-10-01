"""The graph-schema contract suite: green on the reference reader, red on wrong readers.

This file is listed in ``contracts/graph-schema/contract.toml`` as the owner's contract tests. A
downstream package runs ``neptune_memory.contract.suite.CHECKS`` the same way against its reader.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from memory_golden_fixtures import published
from memory_schema_builders import INFERRED, claim, node
from neptune.model.knowledge import Known
from neptune_memory.contract.suite import (
    CHECKS,
    Check,
    ContractViolation,
    StubReader,
    check_as_of_never_leaks,
    check_claims_match_reference,
    check_during_filters_on_one_clock,
    check_findings_travel_with_claims,
    check_golden_story,
    check_inference_filter,
    check_neighbours,
    check_nodes,
    check_provisional_queries_are_not_covered,
    check_superseded_versions_vanish_exactly,
)
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.nodes import NodeType
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.reader import ClaimsResult, MemoryReader
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import resolve, resolver_config

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.interval import Interval, LedgerTx
    from neptune_memory.schema.nodes import NodeRef

# Checks that pass vacuously on a reader that answers nothing (it does answer NotCovered).
VACUOUS_ON_STUB = {
    "check_identity",
    "check_as_of_never_leaks",
    "check_as_of_beyond_head_is_refused",
}


def _name(check: Check) -> str:
    return check.__name__


@pytest.mark.parametrize("check", CHECKS, ids=_name)
def test_reference_reader_meets_the_contract(check: Check) -> None:
    check(ReferenceReader, published())


@pytest.mark.parametrize(
    "check", [c for c in CHECKS if c.__name__ not in VACUOUS_ON_STUB], ids=_name
)
def test_contract_is_red_against_a_stub(check: Check) -> None:
    with pytest.raises(ContractViolation):
        check(StubReader, published())


def test_the_suite_as_a_whole_is_red_against_a_stub() -> None:
    failures = []
    for check in CHECKS:
        try:
            check(StubReader, published())
        except ContractViolation:
            failures.append(check.__name__)
    assert len(failures) >= len(CHECKS) - len(VACUOUS_ON_STUB)
    assert isinstance(StubReader(published()), MemoryReader)


class _Leaky(ReferenceReader):
    """Shows the later supersession: the bug ADR 0006 §6 closes."""

    def claims(
        self,
        subject: NodeRef,
        predicate: str | None,
        as_of: LedgerTx,
        during: Interval | None = None,
        *,
        include_inferred: bool = True,
    ) -> ClaimsResult:
        result = super().claims(
            subject, predicate, as_of, during, include_inferred=include_inferred
        )
        history = {c.id: c for c in self._history.claims}
        return replace(result, claims=tuple(history[c.id] for c in result.claims))


class _IgnoresInferenceFilter(ReferenceReader):
    def claims(
        self,
        subject: NodeRef,
        predicate: str | None,
        as_of: LedgerTx,
        during: Interval | None = None,
        *,
        include_inferred: bool = True,
    ) -> ClaimsResult:
        return super().claims(subject, predicate, as_of, during)


class _NeverSupersedes(ReferenceReader):
    """Answers every query from the head snapshot: as-of is ignored."""

    def claims(
        self,
        subject: NodeRef,
        predicate: str | None,
        as_of: LedgerTx,
        during: Interval | None = None,
        *,
        include_inferred: bool = True,
    ) -> ClaimsResult:
        result = super().claims(
            subject, predicate, self.head, during, include_inferred=include_inferred
        )
        return replace(result, as_of=as_of)


class _EmptyEpisodes(ReferenceReader):
    def episodes(self, filter: object) -> Known[tuple[()]]:  # type: ignore[override]
        return Known(())


@pytest.mark.parametrize(
    ("reader", "check"),
    [
        (_Leaky, check_as_of_never_leaks),
        (_Leaky, check_claims_match_reference),
        (_IgnoresInferenceFilter, check_inference_filter),
        (_NeverSupersedes, check_superseded_versions_vanish_exactly),
        (_NeverSupersedes, check_golden_story),
        (_EmptyEpisodes, check_provisional_queries_are_not_covered),
    ],
    ids=lambda x: getattr(x, "__name__", ""),
)
def test_each_guarantee_catches_its_bug(reader: type[ReferenceReader], check: Check) -> None:
    golden: GraphDocument = published()
    check(ReferenceReader, golden)
    with pytest.raises(ContractViolation):
        check(reader, golden)


# --- Graphs the golden does not contain (review of MVL-105) -----------------------------------

GENERAL = (
    check_claims_match_reference,
    check_as_of_never_leaks,
    check_during_filters_on_one_clock,
    check_findings_travel_with_claims,
    check_nodes,
    check_neighbours,
)


def _document(*claims: Claim) -> GraphDocument:
    priorities = {"memory.test": 0}
    return GraphDocument(
        resolve(claims, CORE_PREDICATES, priorities), resolver_config(CORE_PREDICATES, priorities)
    )


def test_during_keeps_same_clock_claims_out_of_the_window_out() -> None:
    run = node(NodeType.RUN, "record:run-1")
    first = claim(run, "recorded_by", node(NodeType.MACHINE, "a:1"), 0, 10, tx=1)
    later = claim(run, "recorded_by", node(NodeType.MACHINE, "b:2"), 20, 30, tx=1, ev=1)
    for check in GENERAL[:3]:
        check(ReferenceReader, _document(first, later))


def test_an_override_finding_stays_visible_after_its_winner_is_superseded() -> None:
    run = node(NodeType.RUN, "record:run-2")
    winner = claim(run, "recorded_by", node(NodeType.MACHINE, "w:1"), 0, tx=1)
    guess = claim(run, "recorded_by", node(NodeType.MACHINE, "g:1"), 0, tx=2, kind=INFERRED)
    newer = claim(run, "recorded_by", node(NodeType.MACHINE, "w:2"), 5, tx=3, ev=1)
    document = _document(winner, guess, newer)
    (overridden,) = document.resolution.findings
    assert overridden.code == "overridden_on_arrival" and overridden.others == (winner.id,)
    reader = ReferenceReader(document)
    assert overridden.id in {f.id for f in reader.claims(run, None, ledger_tx(3)).findings}
    filtered = reader.claims(run, None, ledger_tx(3), include_inferred=False)
    assert overridden.id not in {f.id for f in filtered.findings}  # names only inferred + gone
    for check in GENERAL:
        check(ReferenceReader, document)
