"""The committed PDF fixtures are exactly what their generator builds, and stay small."""

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Final

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "pdf"


def _generator() -> ModuleType:
    if "make_pdfs" in sys.modules:
        return sys.modules["make_pdfs"]
    spec = importlib.util.spec_from_file_location("make_pdfs", FIXTURES / "make_pdfs.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_pdfs"] = module  # its dataclasses resolve their annotations through it
    spec.loader.exec_module(module)
    return module


MAKE: Final = _generator()


def test_committed_fixtures_match_the_generator_and_stay_small() -> None:
    built = MAKE.build()
    committed = {p.name for p in FIXTURES.iterdir() if p.is_file()}
    assert committed == {*built, "make_pdfs.py", "README.md"}
    for name, data in built.items():
        assert (FIXTURES / name).read_bytes() == data, f"run make_pdfs.py: {name} differs"
        assert len(data) < 512 * 1024, name
    readme = (FIXTURES / "README.md").read_text()
    assert all(f"`{name}`" in readme for name in built)


def test_hand_written_deflate_is_what_zlib_reads() -> None:
    data = b"BT /F1 11 Tf (Inspection) Tj ET\n" * 5000
    assert zlib.decompress(MAKE.stored_deflate(data)) == data
    assert zlib.decompress(MAKE.stored_deflate(b"")) == b""
    bomb = MAKE.bomb_deflate(0x20, 100_003)
    assert zlib.decompress(bomb) == b" " * 100_003
    assert len(bomb) < 100_003 // 100


def test_the_rc4_handler_matches_the_standard_password_check() -> None:
    from pypdf import PdfReader

    owner_only = PdfReader(FIXTURES / "encrypted_owner.pdf")
    assert owner_only.is_encrypted and owner_only.decrypt("supervisor") != 0
    with_user = PdfReader(FIXTURES / "encrypted_user.pdf")
    assert with_user.decrypt("operator") != 0
    assert with_user.pages[0].extract_text().startswith("North plant")
