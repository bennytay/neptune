"""Sniffing: signatures match bytes and never names; text is classified, not decoded."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import PROBE_HEAD_SIZE, FormatSpec, Magic
from neptune.discovery.sniff import (
    SIGNATURES,
    ContainerKind,
    Signature,
    TextClass,
    classify_text,
    declared_signatures,
    sniff,
)

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "probe"


def _generator() -> ModuleType:
    path = FIXTURES / "make_probe_fixtures.py"
    spec = importlib.util.spec_from_file_location("make_probe_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_probe_fixtures"] = module
    spec.loader.exec_module(module)
    return module


GENERATOR: Final = _generator()


def names(data: bytes) -> list[str]:
    return [s.name for s in sniff(data[:PROBE_HEAD_SIZE], len(data)).signatures]


# --- Signatures ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GENERATOR.SIGNATURE_FILES))
def test_every_signature_fixture_sniffs_as_its_format(name: str) -> None:
    expected, _ = GENERATOR.SIGNATURE_FILES[name]
    data = (FIXTURES / "signatures" / name).read_bytes()
    assert names(data)[0] == expected


def test_a_renamed_or_extensionless_copy_sniffs_the_same() -> None:
    for index, (name, (expected, _)) in enumerate(GENERATOR.SIGNATURE_FILES.items()):
        renamed = FIXTURES / "renamed" / f"{index:02d}_{Path(name).stem}"
        assert names(renamed.read_bytes())[0] == expected, renamed


def test_containers_are_named_by_the_most_specific_match() -> None:
    zip_head = (FIXTURES / "containers" / "members.zip").read_bytes()
    found = sniff(zip_head[:PROBE_HEAD_SIZE], len(zip_head))
    assert found.container is ContainerKind.ZIP
    tar = (FIXTURES / "containers" / "members.tar").read_bytes()
    assert sniff(tar[:PROBE_HEAD_SIZE], len(tar)).container is ContainerKind.TAR
    assert sniff(b"plain words", 11).container is None


def test_bytes_that_match_nothing_have_no_signature() -> None:
    assert names(b"") == []
    assert names(b"just some notes\n") == []
    assert names(b"\x00\x01\x02\x03") == []


def test_a_signature_needs_every_part() -> None:
    webp = Signature("WebP", (Magic(0, b"RIFF"), Magic(8, b"WEBP")))
    assert webp.matches(b"RIFF\x00\x00\x00\x00WEBP")
    assert not webp.matches(b"RIFF\x00\x00\x00\x00WAVE")
    assert not webp.matches(b"RIFF")  # cut before the second part
    with pytest.raises(ValueError):
        Signature("nothing", ())


def test_matches_are_ordered_by_specificity_then_name_whatever_the_table_order() -> None:
    short = Signature("Short", (Magic(0, b"AB"),))
    long = Signature("Long", (Magic(0, b"ABCD"),))
    other = Signature("Also", (Magic(0, b"ABCD"),))
    head = b"ABCDEFGH"
    forward = sniff(head, len(head), (short, long, other)).signatures
    backward = sniff(head, len(head), (other, long, short)).signatures
    assert forward == backward
    assert [s.name for s in forward] == ["Also", "Long", "Short"]


def test_the_table_is_well_formed_and_every_magic_fits_in_a_head() -> None:
    for signature in SIGNATURES:
        for part in signature.magic:
            assert part.offset + len(part.data) <= PROBE_HEAD_SIZE
    containers = {s.container for s in SIGNATURES if s.container is not None}
    assert containers == set(ContainerKind)


def test_adapters_declared_magic_becomes_signatures_naming_the_adapter() -> None:
    shipped = declared_signatures(default_registry().descriptors().values())
    assert [(s.name, s.adapter, s.magic) for s in shipped] == [
        ("MCAP", "mcap", (Magic(0, b"\x89MCAP0\r\n"),)),  # text declares none
        ("rosbag2 sqlite3 storage", "rosbag2", (Magic(0, b"SQLite format 3\x00"),)),
        ("Parquet", "tabular", (Magic(0, b"PAR1"),)),
    ]
    spec = FormatSpec("Tally", magic=(Magic(0, b"TALLY1\n"),))
    descriptor = default_registry().descriptors()["text"]
    from dataclasses import replace

    declared = declared_signatures([replace(descriptor, formats=(spec,))])
    assert [(s.name, s.adapter) for s in declared] == [("Tally", "text")]
    found = sniff(b"TALLY1\n1 2\n", 11, declared)
    assert found.signatures == declared
    assert found.to_json() == {
        "signatures": [{"adapter": "text", "name": "Tally"}],
        "text": "utf8",
    }


# --- Text class ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "size", "expected"),
    [
        (b"", 0, TextClass.EMPTY),
        (b"hello\n", 6, TextClass.UTF8),
        (b"\xef\xbb\xbfhello", 8, TextClass.UTF8_BOM),
        (b"\xff\xfeh\x00i\x00", 6, TextClass.UTF16_BOM),
        (b"\xfe\xff\x00h", 4, TextClass.UTF16_BOM),
        (b"PK\x03\x04\x00", 5, TextClass.BINARY),
        (b"\x89MCAP0\r\n\x01", 9, TextClass.BINARY),
        (
            b"\x89MCAP0\r\n",
            8,
            TextClass.DAMAGED_UTF8,
        ),  # no control byte: damaged text, as the adapter says
        (b"text \x7f ok", 9, TextClass.UTF8),
        (b"abc\xff", 4, TextClass.DAMAGED_UTF8),
        ("é".encode()[:1], 2, TextClass.UTF8),  # a head cut inside a character is unfinished
        ("é".encode()[:1], 1, TextClass.DAMAGED_UTF8),  # the same bytes as the whole source
    ],
)
def test_text_classification(head: bytes, size: int, expected: TextClass) -> None:
    assert classify_text(head, size) is expected


def test_describe_is_one_deterministic_line() -> None:
    data = (FIXTURES / "signatures" / "recording.mcap").read_bytes()
    assert sniff(data, len(data)).describe() == "MCAP signature; binary"
    assert sniff(b"notes", 5).describe() == "no known signature; utf8"
    assert sniff(b"", 0).describe() == "no known signature; empty"


@given(st.binary(max_size=600), st.integers(0, 3))
def test_sniffing_never_raises_and_is_deterministic(data: bytes, extra: int) -> None:
    size = len(data) + extra
    first, second = sniff(data, size), sniff(data, size)
    assert first == second
    assert first.to_json() == second.to_json()
    assert all(s.matches(data) for s in first.signatures)
