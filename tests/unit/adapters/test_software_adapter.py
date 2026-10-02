"""The software adapter's laws over every fixture: probing, citations, determinism, lineage,
limits and hostile bytes (ADR 0040). Format-by-format expectations are in
``test_software_formats.py``; the job end to end in ``tests/integration``.
"""

import importlib.util
import json
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from neptune.adapters.check import check_chunk_output
from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    SIGNATURE,
    STRUCTURE,
    VERIFIED,
    ChunkOutput,
    ProbeHints,
    configure,
    make_chunk,
)
from neptune.adapters.harness import SourceOutput
from neptune.adapters.software import DESCRIPTOR, FORMATS, SoftwareAdapter, detect
from neptune.adapters.text import TextAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.machine import SoftwareConfiguration


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "fixtures" / "software" / "software_oracle.py"
    spec = importlib.util.spec_from_file_location("software_oracle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["software_oracle"] = module
    spec.loader.exec_module(module)
    return module


ORACLE: Final = _load()
FIXTURES: Final[list[str]] = ORACLE.fixtures()
# Fixtures the adapter must leave to others: a relocatable object, a workspace-only manifest.
NOT_MINE: Final = {"firmware/object.o", "manifests/Cargo_workspace.toml"}


def run(data: bytes, **config: object) -> SourceOutput:
    output: SourceOutput = ORACLE.run(data, **config)
    return output


def probe(data: bytes, name: str = "blob") -> tuple[float, list[str]]:
    result = SoftwareAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons]


def as_bytes(output: SourceOutput) -> bytes:
    rows = [record.to_json() for record in output.package_records()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


# --- Every fixture -----------------------------------------------------------------------------


def test_there_is_a_fixture_for_every_format() -> None:
    claimed = {
        detect(ORACLE.fixture(f)[:PROBE_HEAD_SIZE], len(ORACLE.fixture(f))) for f in FIXTURES
    }
    keys = {found[0].key for found in claimed if found is not None}
    assert keys == {spec.key for spec in FORMATS}


@pytest.mark.parametrize("relative", FIXTURES)
def test_every_fixture_is_claimed_by_its_bytes_whatever_its_name(relative: str) -> None:
    data = ORACLE.fixture(relative)
    named, unnamed = probe(data, Path(relative).name), probe(data, "renamed")
    if relative in NOT_MINE:
        assert named[0] == unnamed[0] == 0.0
        return
    if relative == "git/release.sha256":
        assert named[0] == 0.0 and "software.checksum_name" in named[1]
        assert unnamed[0] == STRUCTURE  # the bytes alone are a ref: only the name says otherwise
        return
    assert named == unnamed
    assert named[0] in (STRUCTURE, SIGNATURE, VERIFIED)
    text = TextAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints("renamed", len(data)))
    assert named[0] > text.confidence  # a specific reader always outranks generic text


@pytest.mark.parametrize("relative", FIXTURES)
def test_every_fixture_keeps_the_contract_and_every_citation_resolves(relative: str) -> None:
    data = ORACLE.fixture(relative)
    output = run(data)  # the harness checks every law of the contract
    assert output.records() or output.findings()
    ORACLE.check_citations(data, output)
    for record in output.records():
        assert isinstance(record, SoftwareConfiguration)
        assert record.machine.state == "not_covered"


@pytest.mark.parametrize("relative", FIXTURES)
def test_every_fixture_is_read_identically_twice(relative: str) -> None:
    data = ORACLE.fixture(relative)
    assert as_bytes(run(data)) == as_bytes(run(data))


def test_a_new_config_is_new_lineage_and_leaves_the_old_output_alone() -> None:
    data = ORACLE.fixture("lockfiles/uv.lock")
    first, again = run(data), run(data, max_items=19999)
    assert first.config.transform.id != again.config.transform.id
    assert {r.id for r in first.records()}.isdisjoint(r.id for r in again.records())
    assert as_bytes(first) == as_bytes(run(data))


def test_the_descriptor_documents_every_code_the_fixtures_produce() -> None:
    declared = {code.name for code in DESCRIPTOR.finding_codes}
    produced = {f.code for name in FIXTURES for f in run(ORACLE.fixture(name)).findings()}
    assert produced <= declared
    # Every code but the limits and an empty declaration shows up in a real fixture.
    untested = declared - produced
    expected = {"too_large", "too_many_entries", "too_many_items", "no_software_declared"}
    assert untested == {f"software.{name}" for name in expected}


# --- Boundaries --------------------------------------------------------------------------------


@pytest.mark.parametrize("data", [b"", b"\x00", b"\n", b"{}", b"[tool.x]", b"ref: \n"])
def test_empty_and_degenerate_sources_are_not_claimed(data: bytes) -> None:
    assert probe(data)[0] == 0.0


def test_bytes_in_no_format_forced_onto_the_adapter_are_one_finding() -> None:
    output = run(b"just some text\n")
    assert output.plan.chunks[0].context == {"format": "unrecognised"}
    assert ORACLE.codes(output) == ["unrecognised"]
    assert output.records() == ()


def test_inspect_names_the_format_from_the_head() -> None:
    reader = BytesReader(ORACLE.fixture("sbom/robot.cdx.json"))
    summary = SoftwareAdapter().inspect(reader, configure(DESCRIPTOR)).summary
    assert summary == {"format": "cyclonedx", "size": reader.size, "version": "1.5"}
    empty = SoftwareAdapter().inspect(BytesReader(b""), configure(DESCRIPTOR)).summary
    assert empty == {"format": "unrecognised", "size": 0}


def test_a_document_over_max_document_bytes_is_not_parsed() -> None:
    data = ORACLE.fixture("lockfiles/Cargo.lock")
    output = run(data, max_document_bytes=len(data) - 1)
    assert ORACLE.codes(output) == ["too_large"]
    assert run(data, max_document_bytes=len(data)).records()


def test_more_items_than_max_items_make_no_record() -> None:
    data = ORACLE.fixture("lockfiles/Cargo.lock")
    output = run(data, max_items=2)
    assert ORACLE.codes(output) == ["too_many_items"]
    assert output.records() == ()
    assert len(ORACLE.items(run(data, max_items=3))) == 3


def test_a_header_over_max_header_bytes_is_not_read() -> None:
    data = ORACLE.fixture("checkpoints/policy.safetensors")
    (length,) = struct.unpack_from("<Q", data)
    assert ORACLE.codes(run(data, max_header_bytes=length - 1)) == ["too_large"]
    assert ORACLE.codes(run(data, max_header_bytes=length)) == []
    elf = ORACLE.fixture("firmware/app.elf")
    assert "too_large" in ORACLE.codes(run(elf, max_header_bytes=64))


def test_a_file_that_declares_nothing_says_so() -> None:
    data = b"[project]\n"  # a pyproject.toml whose [project] table is empty
    output = run(data)
    (item,) = ORACLE.items(output)
    assert item.name.state == item.release.state == "unknown"
    assert ORACLE.codes(output) == ["software_identity_missing"]
    empty = run(b'{"bomFormat": "CycloneDX", "specVersion": "1.5"}')
    assert ORACLE.codes(empty) == ["no_software_declared"]


def test_a_value_longer_than_a_version_may_be_is_unknown_with_a_finding() -> None:
    data = b'[package]\nname = "x"\nversion = "' + b"1" * 300 + b'"\n'
    output = run(data)
    (item,) = ORACLE.items(output)
    assert item.release.state == "unknown"
    assert ORACLE.codes(output) == ["invalid_value"]


# --- Hostile bytes -----------------------------------------------------------------------------


def test_a_billion_laughs_document_is_refused_unexpanded() -> None:
    entities = "".join(f'<!ENTITY e{i} "{f"&e{i - 1};" * 10}">' for i in range(1, 10))
    data = f'<?xml version="1.0"?><!DOCTYPE package [<!ENTITY e0 "lol">{entities}]>'
    data += "<package><name>&e9;</name><version>1.0.0</version></package>"
    assert ORACLE.codes(run(data.encode())) == ["malformed"]


def test_json_nested_past_the_parser_guard_is_malformed_not_a_crash() -> None:
    data = b'{"lockfileVersion": 3, "packages": ' + b"[" * 200_000 + b"]" * 200_000 + b"}"
    assert ORACLE.codes(run(data)) == ["malformed"]


def test_a_safetensors_header_length_past_any_file_reads_nothing() -> None:
    data = struct.pack("<Q", 2**63) + b'{"a":1}'
    forced = _forced("safetensors", data)
    assert [f.code for f in forced.findings] == ["software.too_large"]


def test_an_elf_whose_tables_point_past_the_end_is_a_finding() -> None:
    data = bytearray(ORACLE.fixture("firmware/app.elf"))
    struct.pack_into("<Q", data, 40, 2**40)  # e_shoff far past the end
    output = run(bytes(data))
    assert "truncated" in ORACLE.codes(output)
    (item,) = ORACLE.items(output)
    assert item.build.state == "unknown"


def test_an_elf_note_whose_sizes_overrun_its_section_is_skipped() -> None:
    data = bytearray(ORACLE.fixture("firmware/app.elf"))
    struct.pack_into("<I", data, 64 + 4, 0xFFFFFF)  # the build-id note's descsz
    output = run(bytes(data))
    assert "malformed_entry" in ORACLE.codes(output)


def test_a_zip_whose_directory_claims_to_be_huge_is_not_read() -> None:
    data = bytearray(ORACLE.fixture("checkpoints/policy.pt"))
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<I", data, end + 12, 2**31)
    assert "too_large" in ORACLE.codes(run(bytes(data)))


def test_a_packed_refs_file_of_many_refs_is_read_line_by_line() -> None:
    lines = [f"{index:040x} refs/tags/t{index}\n^{index + 1:040x}\n" for index in range(5000)]
    data = ("# pack-refs with: peeled \n" + "".join(lines)).encode()
    assert len(ORACLE.items(run(data))) == 5000


def _forced(key: str, data: bytes) -> ChunkOutput:
    """Ingest ``data`` as format ``key`` whatever it holds, checking the chunk's output."""
    adapter, source, config = SoftwareAdapter(), BytesReader(data), configure(DESCRIPTOR)
    chunk = make_chunk(source, config, {"format": key}, len(data))
    output = adapter.ingest(source, chunk, config)
    check_chunk_output(DESCRIPTOR, source, config, chunk, output)
    assert output.records or output.findings  # the output always cites its source
    return output


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    relative=st.sampled_from(FIXTURES),
    edits=st.lists(st.tuples(st.integers(0, 2**20), st.integers(0, 255)), max_size=6),
    cut=st.integers(0, 2**20),
    key=st.sampled_from([spec.key for spec in FORMATS]),
)
@example(relative="git/packed-refs", edits=[(99, 0xC3)], cut=268, key="git_packed_refs")
@example(
    relative="git/packed-refs", edits=[(99, 0xFF), (100, 0x80)], cut=268, key="git_packed_refs"
)
def test_damaged_fixtures_read_as_any_format_are_findings_never_exceptions(
    relative: str, edits: list[tuple[int, int]], cut: int, key: str
) -> None:
    raw = bytearray(ORACLE.fixture(relative))
    for at, value in edits:
        if raw:
            raw[at % len(raw)] = value
    _forced(key, bytes(raw[: cut % (len(raw) + 1)]))


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=4096))
def test_arbitrary_bytes_probe_and_read_without_raising(data: bytes) -> None:
    probe(data)
    for spec in FORMATS:
        _forced(spec.key, data)


def test_json_numbers_and_constants_json_forbids_are_malformed() -> None:
    lock = json.dumps({"lockfileVersion": 3, "packages": {"": {"version": "1"}}})
    assert ORACLE.codes(run(lock.replace('"1"', "NaN").encode())) == ["malformed"]
