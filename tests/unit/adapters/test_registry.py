"""The registry and the selection rule (ADR 0024 §5): conflicts refused, ties never guessed."""

import itertools
from dataclasses import dataclass, replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.adapters.builtin import builtin_adapters, default_registry
from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ContractError,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    Resources,
    SourceReader,
)
from neptune.adapters.registry import (
    AdapterRegistry,
    Candidate,
    RegistryError,
    SelectionStatus,
    rank,
    select,
)


def descriptor(adapter_id: str, version: str = "1.0.0") -> AdapterDescriptor:
    return AdapterDescriptor(
        id=adapter_id,
        version=version,
        abi=ABI_VERSION,
        summary=f"The {adapter_id} stub.",
        formats=(FormatSpec(adapter_id),),
        record_kinds=("document_record",),
        config=(),
        libraries=(),
        finding_codes=(),
        locator_steps=(),
        conventions=(),
        resources=Resources(0, True),
        security=(),
    )


@dataclass
class Stub:
    """An adapter that only probes, with a fixed confidence."""

    descriptor: AdapterDescriptor
    confidence: float

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        return ProbeResult(self.confidence, ())

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        raise NotImplementedError

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        raise NotImplementedError

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        raise NotImplementedError


def stub(adapter_id: str, confidence: float, version: str = "1.0.0") -> Stub:
    return Stub(descriptor(adapter_id, version), confidence)


HEAD = b"some bytes"
HINTS = ProbeHints("file.bin", len(HEAD))


# --- Registration ------------------------------------------------------------------------------


def test_two_adapters_with_one_id_conflict_at_registration() -> None:
    with pytest.raises(RegistryError, match="two adapters have id 'a'"):
        AdapterRegistry([stub("a", 0.5), stub("a", 0.5, version="2.0.0")])


def test_an_adapter_for_another_abi_is_refused() -> None:
    other = Stub(replace(descriptor("a"), abi=ABI_VERSION + 1), 0.5)
    with pytest.raises(RegistryError, match="ABI"):
        AdapterRegistry([other])


def test_an_object_without_the_contract_is_refused() -> None:
    class NoMethods:
        descriptor = descriptor("a")

    with pytest.raises(RegistryError, match="no probe method"):
        AdapterRegistry([NoMethods()])  # type: ignore[list-item]
    with pytest.raises(RegistryError, match="no AdapterDescriptor"):
        AdapterRegistry([object()])  # type: ignore[list-item]


def test_adapters_are_kept_and_listed_in_id_order() -> None:
    registry = AdapterRegistry([stub("b", 0.5), stub("a", 0.5)])
    assert [a.descriptor.id for a in registry.adapters()] == ["a", "b"]
    assert list(registry.descriptors()) == ["a", "b"]
    assert registry.get("b").descriptor.id == "b"
    with pytest.raises(RegistryError):
        registry.get("c")


def test_the_builtin_registry_holds_the_shipped_adapters() -> None:
    assert list(default_registry().descriptors()) == ["config", "mcap", "text"]
    assert builtin_adapters()[0] is not builtin_adapters()[0]


# --- Selection ---------------------------------------------------------------------------------


def test_the_most_confident_adapter_is_selected() -> None:
    registry = AdapterRegistry([stub("generic", 0.4), stub("specific", 0.9), stub("no", 0.0)])
    selection = registry.select(HEAD, HINTS)
    assert selection.status is SelectionStatus.SELECTED
    assert selection.adapter == "specific"
    assert [c.adapter for c in selection.candidates] == ["specific", "generic"]
    assert [c.adapter for c in selection.tied] == ["specific"]


def test_a_tie_at_the_top_is_ambiguous_and_names_every_tied_adapter() -> None:
    registry = AdapterRegistry([stub("b", 0.9), stub("a", 0.9), stub("c", 0.4)])
    selection = registry.select(HEAD, HINTS)
    assert selection.status is SelectionStatus.AMBIGUOUS
    assert selection.adapter is None
    assert [c.adapter for c in selection.tied] == ["a", "b"]
    assert [c.adapter for c in selection.candidates] == ["a", "b", "c"]


def test_a_tie_below_the_top_does_not_matter() -> None:
    selection = AdapterRegistry([stub("a", 0.4), stub("b", 0.4), stub("c", 0.9)]).select(
        HEAD, HINTS
    )
    assert (selection.status, selection.adapter) == (SelectionStatus.SELECTED, "c")


def test_no_claim_is_unsupported() -> None:
    selection = AdapterRegistry([stub("a", 0.0)]).select(HEAD, HINTS)
    assert selection.status is SelectionStatus.UNSUPPORTED
    assert (selection.adapter, selection.candidates, selection.tied) == (None, (), ())
    assert AdapterRegistry().select(HEAD, HINTS).status is SelectionStatus.UNSUPPORTED


def test_candidates_record_the_adapter_version() -> None:
    (candidate,) = AdapterRegistry([stub("a", 0.5, version="3.1.4")]).probe(HEAD, HINTS)
    assert (candidate.adapter, candidate.version, candidate.confidence) == ("a", "3.1.4", 0.5)


def test_probe_is_given_exactly_the_head() -> None:
    registry = AdapterRegistry([stub("a", 0.5)])
    with pytest.raises(ValueError):
        registry.probe(HEAD, ProbeHints("f", len(HEAD) + 1))
    big = ProbeHints("f", PROBE_HEAD_SIZE * 2)
    assert registry.probe(bytes(PROBE_HEAD_SIZE), big)
    with pytest.raises(ValueError):
        registry.probe(bytes(PROBE_HEAD_SIZE + 1), big)


def test_a_probe_must_return_a_probe_result() -> None:
    broken = stub("a", 0.5)
    broken.probe = lambda head, hints: 0.5  # type: ignore[assignment, method-assign, return-value]
    with pytest.raises(ContractError):
        AdapterRegistry([broken]).probe(HEAD, HINTS)


def test_one_adapter_cannot_be_a_candidate_twice() -> None:
    twice = [Candidate("a", "1.0.0", ProbeResult(0.5, ()))] * 2
    with pytest.raises(RegistryError):
        select(twice)


@given(
    st.lists(st.sampled_from([0.0, 0.1, 0.4, 0.7, 0.9, 1.0]), min_size=0, max_size=6).flatmap(
        lambda cs: st.permutations(list(enumerate(cs)))
    )
)
def test_selection_does_not_depend_on_probe_order(order: list[tuple[int, float]]) -> None:
    candidates = [Candidate(f"a{i}", "1.0.0", ProbeResult(c, ())) for i, c in order]
    reference = select(sorted(candidates, key=lambda c: c.adapter))
    assert select(candidates) == reference
    assert rank(candidates) == reference.candidates


def test_the_rule_matches_its_definition_exhaustively() -> None:
    levels = [0.0, 0.4, 0.9]
    for confidences in itertools.product(levels, repeat=3):
        candidates = [
            Candidate(name, "1.0.0", ProbeResult(c, ()))
            for name, c in zip("abc", confidences, strict=True)
        ]
        selection = select(candidates)
        positive = sorted((c for c in confidences if c > 0), reverse=True)
        if not positive:
            assert selection.status is SelectionStatus.UNSUPPORTED
        elif len(positive) > 1 and positive[0] == positive[1]:
            assert selection.status is SelectionStatus.AMBIGUOUS
        else:
            assert selection.status is SelectionStatus.SELECTED
            assert selection.candidates[0].confidence == positive[0]
