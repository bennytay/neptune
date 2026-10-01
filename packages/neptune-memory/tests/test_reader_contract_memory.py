"""The graph-schema contract suite: green on the reference reader, red on wrong readers.

This file is listed in ``contracts/graph-schema/contract.toml`` as the owner's contract tests. A
downstream package runs ``neptune_memory.contract.suite.CHECKS`` the same way against its reader.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from memory_golden_fixtures import published
from neptune.model.knowledge import Known
from neptune_memory.contract.suite import (
    CHECKS,
    Check,
    ContractViolation,
    StubReader,
    check_as_of_never_leaks,
    check_claims_match_reference,
    check_golden_story,
    check_inference_filter,
    check_provisional_queries_are_not_covered,
    check_superseded_versions_vanish_exactly,
)
from neptune_memory.schema.reader import ClaimsResult, MemoryReader
from neptune_memory.schema.reference import ReferenceReader

if TYPE_CHECKING:
    from neptune_memory.schema.codec import GraphDocument
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
