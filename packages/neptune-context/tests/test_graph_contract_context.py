"""Memory's graph-schema contract suite, run from Context against the pinned golden (ADR 0001 §4).

``READERS`` is the list of ``MemoryReader`` implementations Context can read through. Today it
holds Memory's reference reader (every check must pass) and Memory's stub reader (every check that
sees data must fail). A real Memory client adds one entry to the first list when it exists.
"""

from pathlib import Path

import pytest
from neptune_memory.contract.suite import (
    CHECKS,
    Check,
    ContractViolation,
    StubReader,
    load_golden,
)
from neptune_memory.schema.reader import MemoryReader
from neptune_memory.schema.reference import ReferenceReader

from neptune_context import pins

GOLDEN = load_golden(
    Path(__file__).resolve().parents[3]
    / "contracts"
    / "graph-schema"
    / f"v{pins.GRAPH_SCHEMA_VERSION}"
    / "golden"
    / "graph.json"
)

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
    check(ReferenceReader, GOLDEN)


@pytest.mark.parametrize(
    "check", [c for c in CHECKS if c.__name__ not in VACUOUS_ON_STUB], ids=_name
)
def test_stub_reader_is_red_against_the_contract(check: Check) -> None:
    with pytest.raises(ContractViolation):
        check(StubReader, GOLDEN)


def test_both_readers_satisfy_the_reader_protocol() -> None:
    assert isinstance(ReferenceReader(GOLDEN), MemoryReader)
    assert isinstance(StubReader(GOLDEN), MemoryReader)
