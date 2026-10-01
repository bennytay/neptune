"""Hostile and degenerate bytes the software adapter must bound, not trust (ADR 0040 §11).

Each case is a finding from the adapter's code review: a probe that backtracks, tables that make
one note section read many times, unbounded findings, a blank that became a build, a BOM that made
a valid SBOM malformed, ``setup`` calls that are not setuptools', a tag read as a commit.
"""

import importlib.util
import struct
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.software._common import describe
from neptune.adapters.software._manifests import ROS_PACKAGE_XML
from neptune.model.knowledge import Ambiguous, Known, Unknown


def _load(name: str) -> ModuleType:
    path = Path(__file__).parents[2] / "fixtures" / "software" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ORACLE: Final = sys.modules.get("software_oracle") or _load("software_oracle")
MAKE: Final = sys.modules.get("make_software_fixtures") or _load("make_software_fixtures")

HEAD: Final = 64 * 1024


@pytest.mark.parametrize(
    "head",
    [
        b" " * HEAD + b"x",
        b"<?a?>" * (HEAD // 5) + b"x",
        b"<?" * (HEAD // 2),
        b"<!---->" * (HEAD // 7) + b"x",
        b"<!--" * (HEAD // 4),
        b" <!DOCTYPE" + b" " * HEAD,
        b"<!DOCTYPE x [" + b"] " * (HEAD // 2),
    ],
    ids=[
        "spaces",
        "pis",
        "open_pi",
        "comments",
        "open_comment",
        "doctype_spaces",
        "doctype_brackets",
    ],
)
def test_the_package_xml_probe_does_not_backtrack_on_a_hostile_head(head: bytes) -> None:
    started = time.perf_counter()
    assert ROS_PACKAGE_XML.detect(head, len(head) + 1) is None
    assert time.perf_counter() - started < 1.0


def test_the_package_xml_probe_still_skips_prologue_nodes() -> None:
    head = b'\xef\xbb\xbf <?xml version="1.0"?>\n<!-- c --><!DOCTYPE package [<!ENTITY x "y">]>\n'
    assert ROS_PACKAGE_XML.detect(head + b'<package format="3"></package>', 10**6) is not None
    assert ROS_PACKAGE_XML.detect(head + b"<other/>", 10**6) is None


def _elf_with_notes(notes: bytes) -> bytes:
    elf: bytes = MAKE.elf64([(b".note.test", notes)])
    return elf


def test_an_elf_with_more_notes_than_max_items_is_cut_with_one_finding() -> None:
    note = MAKE._note(b"", 0, b"")  # twelve bytes, a note of nothing
    output = ORACLE.run(_elf_with_notes(note * 500), max_items=50)
    assert ORACLE.codes(output).count("too_many_entries") == 1


def test_many_distinct_build_ids_are_bounded_by_max_items() -> None:
    notes = b"".join(MAKE._note(b"GNU\x00", 3, i.to_bytes(4, "little") * 5) for i in range(300))
    output = ORACLE.run(_elf_with_notes(notes), max_items=40)
    assert "too_many_entries" in ORACLE.codes(output)


def test_section_headers_that_repeat_one_region_read_it_once() -> None:
    note = MAKE._note(b"GNU\x00", 3, bytes(range(20)))
    once = ORACLE.run(MAKE.elf64([(b".a", note)]))
    twice_data = bytearray(MAKE.elf64([(b".a", note)]))
    # Point the string table section at the note too: two headers, one region.
    shoff = struct.unpack_from("<Q", twice_data, 40)[0]
    section = shoff + 64 * 2  # the shstrtab header (after the null and the note sections)
    struct.pack_into("<I", twice_data, section + 4, 7)  # SHT_NOTE
    struct.pack_into("<QQ", twice_data, section + 24, 64, len(note))
    twice = ORACLE.run(bytes(twice_data), max_items=1)  # a second read would be a second note
    assert ORACLE.known(ORACLE.items(twice)[0].build) == ORACLE.known(ORACLE.items(once)[0].build)
    assert ORACLE.codes(twice) == ORACLE.codes(once) == []


def test_a_flood_of_malformed_ref_lines_is_a_bounded_set_of_findings() -> None:
    sha = "a" * 40
    data = (f"{sha} refs/heads/main\n" + "junk\n" * 5000).encode()
    output = ORACLE.run(data, max_items=20)
    found = ORACLE.codes(output)
    assert found.count("malformed_entry") == 20 and found.count("too_many_entries") == 1
    assert ORACLE.run(data, max_items=20).findings() == output.findings()


@pytest.mark.parametrize("digest", [b"\x00" * 32, b"\xff" * 32])
def test_an_unset_esp_app_hash_is_unknown_not_a_build(digest: bytes) -> None:
    image = bytearray(MAKE.esp_image())
    image[176:208] = digest
    output = ORACLE.run(bytes(image))
    (item,) = ORACLE.items(output)
    assert isinstance(item.build, Unknown)
    assert "invalid_value" in ORACLE.codes(output)
    assert isinstance(ORACLE.items(ORACLE.run(MAKE.esp_image()))[0].build, Known)


def test_a_byte_order_mark_does_not_make_a_valid_sbom_malformed() -> None:
    plain = ORACLE.fixture("sbom/robot.cdx.json")
    marked = ORACLE.run(b"\xef\xbb\xbf" + plain)
    assert ORACLE.codes(marked) == ORACLE.codes(ORACLE.run(plain))
    for field in ("name", "release", "digest"):
        assert [ORACLE.known(getattr(i, field)) for i in ORACLE.items(marked)] == [
            ORACLE.known(getattr(i, field)) for i in ORACLE.items(ORACLE.run(plain))
        ]


SETUP: Final = b"from setuptools import setup\nsetup(name='real', version='1.0')\n"


def test_setup_calls_are_setuptools_only() -> None:
    (item,) = ORACLE.items(ORACLE.run(SETUP))
    assert ORACLE.known(item.name) == "real"
    aliased = b"import setuptools as st\nst.setup(name='aliased', version='2')\n"
    (item,) = ORACLE.items(ORACLE.run(aliased))
    assert ORACLE.known(item.name) == "aliased"
    lookalike = SETUP + b"class A:\n    def go(self):\n        self.setup(name='fake')\n"
    lookalike += b"app = object()\napp.setup(name='other')\n"
    assert len(ORACLE.items(ORACLE.run(lookalike))) == 1
    local = b"import setuptools\ndef setup(**kw): pass\nsetup(name='local')\n"
    assert ORACLE.items(ORACLE.run(local)) == []


@pytest.mark.parametrize("text", ["2024", "1000", "cafe", "beef", "v1.2", "abc123"])
def test_a_tag_made_of_hex_digits_is_not_a_commit(text: str) -> None:
    assert describe(text) == (True, None)


def test_describe_still_reads_a_real_abbreviation_and_a_tag_with_one() -> None:
    assert describe("abc1234") == (False, (0, 7))
    assert describe("v1.2-3-gabc1234-dirty")[0] is True


# --- The bounds themselves ---------------------------------------------------------------------


def _code_list(data: bytes, **config: int) -> list[str]:
    codes: list[str] = ORACLE.codes(ORACLE.run(data, **config))
    return codes


def test_max_script_bytes_bounds_python_and_cmake_exactly() -> None:
    cmake = ORACLE.fixture("manifests/CMakeLists.txt")
    for data in (SETUP, cmake):
        assert "too_large" not in _code_list(data, max_script_bytes=len(data))
        assert "too_large" in _code_list(data, max_script_bytes=len(data) - 1)
    # A data document is bounded by its own option, not the script one.
    toml = ORACLE.fixture("manifests/pyproject.toml")
    assert _code_list(toml, max_script_bytes=1) == _code_list(toml)


def test_a_lockfile_with_more_items_than_max_items_stops_drafting_and_makes_no_record() -> None:
    head = b'version = 1\nrequires-python = ">=3.11"\n\n'
    data = head + b'[[package]]\nname = "a"\nversion = "1"\n' * 50
    output = ORACLE.run(data, max_items=5)
    assert output.records() == () and ORACLE.codes(output) == ["too_many_items"]
    assert len(ORACLE.items(ORACLE.run(data, max_items=50))) == 50


def test_many_nameless_cmake_projects_are_refused_not_truncated() -> None:
    data = b"cmake_minimum_required(VERSION 3.16)\n" + b"project()\n" * 30
    output = ORACLE.run(data, max_items=5)
    assert output.records() == () and ORACLE.codes(output) == ["too_many_items"]


def _package_xml(names: list[str]) -> bytes:
    body = "".join(f"<name>{name}</name>" for name in names)
    return f'<package format="3">{body}<version>1.0.0</version></package>'.encode()


def test_conflicting_values_keep_a_bounded_candidate_list_and_the_finding_counts_all() -> None:
    output = ORACLE.run(_package_xml([f"n{i}" for i in range(40)]))
    (item,) = ORACLE.items(output)
    assert isinstance(item.name, Ambiguous) and len(item.name.candidates) == 32
    (finding,) = [f for f in output.findings() if f.code == "software.conflicting_identity"]
    assert finding.details["distinct"] == 40 and finding.details["kept"] == 32


def test_xml_elements_past_max_items_leave_the_field_unknown_not_a_prefix_of_it() -> None:
    data = _package_xml(["a", "a", "a", "b"])
    assert isinstance(ORACLE.items(ORACLE.run(data))[0].name, Ambiguous)
    capped = ORACLE.run(data, max_items=3)  # the 4th, differing name is never read
    (item,) = ORACLE.items(capped)
    assert isinstance(item.name, Unknown) and "too_many_entries" in ORACLE.codes(capped)
    assert ORACLE.known(item.release) == "1.0.0"  # a field with nothing dropped is unaffected


def test_elf_notes_past_max_items_leave_the_identity_unknown() -> None:
    notes = b"".join(MAKE._note(b"GNU\x00", 3, bytes([i]) * 20) for i in range(5))
    capped = ORACLE.run(_elf_with_notes(notes), max_items=3)
    (item,) = ORACLE.items(capped)
    assert isinstance(item.build, Unknown) and "too_many_entries" in ORACLE.codes(capped)


def test_packed_refs_split_lines_on_line_feed_only_as_git_does() -> None:
    sha = "a" * 40
    data = f"{sha} refs/heads/a\n{sha} refs/heads/b\r{sha} refs/heads/c\n".encode()
    output = ORACLE.run(data)
    assert ORACLE.codes(output) == ["malformed_entry"]
    assert [ORACLE.known(item.name) for item in ORACLE.items(output)] == ["refs/heads/a"]


def test_a_parser_stack_overflow_in_setup_py_is_a_finding_not_a_crash() -> None:
    data = b'from setuptools import setup\nsetup(name="a", version=' + b"-" * 100_000 + b"1)\n"
    output = ORACLE.run(data)
    assert ORACLE.codes(output) == ["malformed"] and output.records() == ()


def test_a_field_forced_unknown_by_a_cap_raises_no_conflict() -> None:
    data = _package_xml(["a", "b", "c", "d"])  # distinct names, one past a cap of 3
    output = ORACLE.run(data, max_items=3)
    assert "conflicting_identity" not in ORACLE.codes(output)
    assert "too_many_entries" in ORACLE.codes(output)


def test_a_lockfile_array_of_tables_past_max_items_stops_collecting() -> None:
    head = b"# This file is automatically @generated by Cargo.\nversion = 4\n\npackage = ["
    data = head + b'{name="a"},' * 50 + b"]\n"
    output = ORACLE.run(data, max_items=5)
    assert output.records() == () and ORACLE.codes(output) == ["too_many_items"]
