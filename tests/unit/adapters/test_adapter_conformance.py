"""The reusable conformance check: well-behaved adapters pass, each broken law is caught."""

import itertools
import platform
import sys
import zlib
from dataclasses import replace
from typing import Any
from xml.parsers import expat

import pytest

from neptune.adapters.builtin import builtin_adapters
from neptune.adapters.conformance import check_conformance, check_libraries, conformance_inputs
from neptune.adapters.contract import (
    AdapterConfig,
    Chunk,
    ChunkOutput,
    ContractError,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    SourceReader,
)
from neptune.adapters.text import TextAdapter

SAMPLES = (b"first paragraph\n\nsecond\n", b"one line, no ending")


class _Wrapped:
    """``TextAdapter`` with one method overridden by a test."""

    def __init__(self, **overrides: Any) -> None:
        self._text = TextAdapter(chunk_bytes=8)
        self.descriptor = self._text.descriptor
        self.probe = overrides.get("probe", self._text.probe)
        self.inspect = overrides.get("inspect", self._text.inspect)
        self.plan = overrides.get("plan", self._text.plan)
        self.ingest = overrides.get("ingest", self._text.ingest)


def test_the_reference_adapter_conforms() -> None:
    check_conformance(TextAdapter(), SAMPLES)
    check_conformance(TextAdapter(chunk_bytes=4), SAMPLES, {"block_rule": "line"})


def test_inputs_start_empty_and_add_each_sample_cut_in_half() -> None:
    assert conformance_inputs([b"abcd", b"xy"]) == (b"", b"abcd", b"xy", b"ab", b"x")
    assert conformance_inputs([]) == (b"",)
    assert conformance_inputs([b"a", b"a"]) == (b"", b"a")


def test_a_sample_must_be_bytes() -> None:
    with pytest.raises(TypeError, match="bytes"):
        conformance_inputs(["text"])  # type: ignore[list-item]


def test_an_object_that_is_not_an_adapter_is_refused() -> None:
    with pytest.raises(ContractError, match="AdapterDescriptor"):
        check_conformance(object())  # type: ignore[arg-type]


def test_another_abi_is_refused() -> None:
    adapter = _Wrapped()
    adapter.descriptor = replace(adapter.descriptor, abi=adapter.descriptor.abi + 1)
    with pytest.raises(ContractError, match="ABI"):
        check_conformance(adapter, SAMPLES)


def test_an_exception_on_a_hostile_input_is_a_broken_law() -> None:
    text = TextAdapter()

    def plan(source: SourceReader, config: AdapterConfig) -> Any:
        if source.size == 0:
            raise IndexError("empty")
        return text.plan(source, config)

    with pytest.raises(ContractError, match=r"raised IndexError.*0 bytes.*not exceptions"):
        check_conformance(_Wrapped(plan=plan), SAMPLES)


def test_a_probe_that_answers_two_ways_is_refused() -> None:
    answers = itertools.cycle([0.1, 0.2])

    def probe(head: bytes, hints: ProbeHints) -> ProbeResult:
        return ProbeResult(next(answers), ())

    with pytest.raises(ContractError, match="probe answered two ways"):
        check_conformance(_Wrapped(probe=probe))


def test_a_probe_reason_of_another_adapter_is_refused() -> None:
    def probe(head: bytes, hints: ProbeHints) -> ProbeResult:
        return ProbeResult(0.0, (ProbeReason("mcap.magic", "borrowed"),))

    with pytest.raises(ContractError, match=r"probe reason 'mcap\.magic'"):
        check_conformance(_Wrapped(probe=probe))


def test_an_inspect_that_answers_two_ways_is_refused() -> None:
    text = TextAdapter()
    calls = itertools.count()

    def inspect(source: SourceReader, config: AdapterConfig) -> Any:
        result = text.inspect(source, config)
        return replace(result, summary={**result.summary, "call": next(calls)})

    with pytest.raises(ContractError, match="inspect answered two ways"):
        check_conformance(_Wrapped(inspect=inspect))


def test_two_runs_that_differ_are_refused() -> None:
    text = TextAdapter()
    seen: set[str] = set()

    def ingest(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        output = text.ingest(source, chunk, config)
        # The first run over a chunk drops its findings: output that depends on state.
        if chunk.id in seen:
            return output
        seen.add(chunk.id)
        return replace(output, findings=())

    with pytest.raises(ContractError, match="two runs over one source differ"):
        check_conformance(_Wrapped(ingest=ingest), [b"bad \xff byte\n"])


def test_a_law_broken_in_ingest_names_the_input() -> None:
    def ingest(source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        return ChunkOutput()

    with pytest.raises(ContractError, match=r"said nothing about its source.*\(input of 0 bytes\)"):
        check_conformance(_Wrapped(ingest=ingest), SAMPLES)


def _with_libraries(*libraries: tuple[str, str]) -> _Wrapped:
    adapter = _Wrapped()
    adapter.descriptor = replace(adapter.descriptor, libraries=tuple(sorted(libraries)))
    return adapter


def test_every_shipped_adapter_declares_no_interpreter_build() -> None:
    for adapter in builtin_adapters():
        check_libraries(adapter)


@pytest.mark.parametrize(
    "library",
    [
        ("expat", expat.EXPAT_VERSION),
        ("expat", expat.EXPAT_VERSION.removeprefix("expat_")),
        ("python", sys.version),
        ("python", sys.version.split()[0]),
        ("python", platform.python_version()),
        ("python", f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"),
        ("cpython", platform.python_compiler()),
        ("bz2", f"cpython-{platform.python_version()}"),
        ("zlib", zlib.ZLIB_RUNTIME_VERSION),
        ("host", platform.platform()),
        ("host", f"linux {platform.release()}"),
    ],
    ids=lambda library: f"{library[0]}-{str(library[1])[:20]}",
)
def test_a_library_taken_from_the_interpreter_build_is_refused(library: tuple[str, str]) -> None:
    with pytest.raises(ContractError, match="interpreter build"):
        check_conformance(_with_libraries(library), SAMPLES)


def test_a_library_the_interpreter_ships_is_declared_by_minor_version() -> None:
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    check_conformance(
        _with_libraries(("python", minor), ("bz2", f"cpython-{minor}"), ("pyyaml", "6.0.2")),
        SAMPLES,
    )
