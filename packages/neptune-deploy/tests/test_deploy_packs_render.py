"""Renderers: byte-identical JSON and PDF, a well-formed PDF, and the WinAnsi rule."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from deploy_pack_support import SITE, configuration_pack, events_pack
from neptune.identity import canonical_json
from neptune_deploy.lifecycle.cli import main
from neptune_deploy.packs import EvidencePack, render_json, render_pdf
from neptune_deploy.packs import cli as pack_cli
from neptune_deploy.packs.pdf import PlacedLine, literal, write_pdf
from neptune_deploy.packs.text import ascii_only, winansi

TESTS = Path(__file__).resolve().parent

# Golden digests of the fixture packs. A change here changes what every pack of these inputs
# says: explain it in the PR, and raise COMPILER_VERSION when a pack's content changes.
GOLDEN = {
    "configuration.json": "sha256:31b62af1828e986eb4d6ac577d487dbd8580b798333f57893dcd4c649a7b9461",
    "configuration.pdf": "sha256:159f72ea3d0bcef965342fd5ba9f258ab5df8f9336b6eac7b88db9388e0def3a",
    "events.json": "sha256:32a6e162df8954b56831385f8cbc8eb5cda6f4a84a3c75cdf6e7fb3da78f1fec",
    "events.pdf": "sha256:1cd6d05222a5f5b3f2037892642c155729da5f4923fa97bd1403f6b543e574d7",
}


def _shown(text: str) -> bytes:
    """How ``text`` appears inside a PDF string literal."""
    return literal(winansi(text))[1:-1]


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _packs() -> dict[str, EvidencePack]:
    return {"configuration": configuration_pack(), "events": events_pack()}


@pytest.mark.parametrize("name", ["configuration", "events"])
def test_the_same_spec_renders_byte_identical_json_and_pdf(name: str) -> None:
    first, second = _packs()[name], _packs()[name]
    assert render_json(first) == render_json(second)
    assert render_pdf(first) == render_pdf(second)


def test_renders_are_identical_across_processes_and_hash_seeds() -> None:
    script = (
        "import hashlib, sys; sys.path.insert(0, sys.argv[1]);"
        "from deploy_pack_support import configuration_pack, events_pack;"
        "from neptune_deploy.packs import render_json, render_pdf;"
        "print(*[hashlib.sha256(r(p())).hexdigest() for p in (configuration_pack, events_pack)"
        " for r in (render_json, render_pdf)])"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", script, str(TESTS)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "1", "4242")
    }
    assert len(runs) == 1
    here = " ".join(
        hashlib.sha256(r(p)).hexdigest()
        for p in (configuration_pack(), events_pack())
        for r in (render_json, render_pdf)
    )
    assert runs == {here + "\n"}


def test_golden_digests() -> None:
    packs = _packs()
    found = {
        f"{name}.{kind}": _digest(render(pack))
        for name, pack in packs.items()
        for kind, render in (("json", render_json), ("pdf", render_pdf))
    }
    assert found == GOLDEN


def test_json_is_canonical_and_reads_back() -> None:
    data = render_json(configuration_pack())
    document = canonical_json.loads(data)
    assert isinstance(document, dict)
    assert document["schema"] == "neptune-deploy.evidence-pack/1"
    assert document["id"] == configuration_pack().id


def _objects(pdf: bytes) -> dict[int, int]:
    return {int(m.group(1)): m.start() for m in re.finditer(rb"(?m)^(\d+) 0 obj\n", pdf)}


@pytest.mark.parametrize("name", ["configuration", "events"])
def test_the_pdf_is_well_formed(name: str) -> None:
    pack = _packs()[name]
    pdf = render_pdf(pack)
    assert pdf.startswith(b"%PDF-1.4\n")
    assert pdf.endswith(b"%%EOF\n")
    startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf).group(1))  # type: ignore[union-attr]
    assert pdf[startxref:].startswith(b"xref\n0 ")
    table = pdf[startxref:].split(b"trailer")[0].splitlines()[2:]
    assert all(len(row) == 19 for row in table)  # 20 bytes with the newline
    offsets = [int(row[:10]) for row in table[1:]]
    assert offsets == [_objects(pdf)[n] for n in range(1, len(offsets) + 1)]
    for match in re.finditer(rb"<< /Length (\d+) >>\nstream\n", pdf):
        end = match.end() + int(match.group(1))
        assert pdf[end : end + len(b"\nendstream")] == b"\nendstream"
    file_id = pack.id.removeprefix("pack:sha256:")[:32].upper().encode()
    assert b"/ID [<" + file_id + b"> <" + file_id + b">]" in pdf
    assert b"/CreationDate (D:19700101000000Z)" in pdf
    assert all(byte < 0x80 for byte in pdf[15:])  # 7-bit after the binary marker
    pages = int(re.search(rb"/Count (\d+)", pdf).group(1))  # type: ignore[union-attr]
    assert f"page {pages} of {pages}".encode() in pdf


def test_the_pdf_shows_every_cited_claim_and_the_ambiguity() -> None:
    pack = configuration_pack()
    pdf = render_pdf(pack)
    for claim in pack.claims:
        assert claim.id.encode() in pdf
    assert (
        _shown("[AMBIGUOUS - every reading below; none is chosen] machine asset-tag:ARM-06") in pdf
    )
    assert _shown("cmms.config:ARM06-C·Ω") == b"cmms.config:ARM06-C\\267<U+03A9>"
    assert _shown("cmms.config:ARM06-C·Ω") in pdf
    assert b"UNKNOWN - stated as not known" in pdf
    assert b"Appendix A - evidence references" in pdf


def test_included_inference_is_marked_in_the_pdf() -> None:
    excluded = render_pdf(configuration_pack(subject=SITE))
    included = render_pdf(configuration_pack(subject=SITE, inference="include"))
    assert b"INFERRED" not in excluded
    assert _shown("Inference: excluded (1 inferred claims left out)") in excluded
    assert b"[INFERRED]" in included
    assert b"INCLUDED - 1 inferred claims, each marked [INFERRED]" in included


def test_event_text_outside_winansi_is_escaped_not_dropped() -> None:
    pdf = render_pdf(events_pack())
    assert b"Operator pressed the E-stop <U+2192> arm halted mid-pick" in pdf
    assert b"CONFLICT - this event is placed at different times on the pack clock" in pdf
    assert _shown("placed through (cited by this placement only)") in pdf


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("plain ASCII ~", "plain ASCII ~"),
        ("café €5 \u2013 ok", "café €5 \u2013 ok"),
        ("a → b", "a <U+2192> b"),
        ("tab\there\nnewline", "tab<U+0009>here<U+000A>newline"),
        ("\x7f\x85\x9d", "<U+007F><U+0085><U+009D>"),
        ("Ω 😀", "<U+03A9> <U+1F600>"),
        ("", ""),
    ],
)
def test_winansi_rule(text: str, shown: str) -> None:
    assert winansi(text) == shown
    winansi(text).encode("cp1252")  # always encodable


def test_ascii_only_for_metadata() -> None:
    assert ascii_only("Zelle 3 \u2013 Süd") == "Zelle 3 <U+2013> S<U+00FC>d"


def test_pdf_literals_escape_delimiters_and_high_bytes() -> None:
    assert literal("a(b)c\\d") == b"(a\\(b\\)c\\\\d)"
    assert literal("é€") == b"(\\351\\200)"


def test_pdf_writer_refuses_bad_input() -> None:
    line = PlacedLine("F2", 8, 50, 700, "x")
    with pytest.raises(ValueError, match="16 bytes"):
        write_pdf([[line]], {}, b"short")
    with pytest.raises(ValueError, match="at least one page"):
        write_pdf([], {}, bytes(16))
    with pytest.raises(ValueError, match="printable ASCII"):
        write_pdf([[line]], {"Title": "é"}, bytes(16))
    with pytest.raises(ValueError, match="unknown font"):
        write_pdf([[PlacedLine("F9", 8, 50, 700, "x")]], {}, bytes(16))


def test_cli_writes_the_pack_and_never_changes_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pack = configuration_pack()
    spec_file, snapshot_file = tmp_path / "spec.json", tmp_path / "graph.json"
    spec_file.write_text(json.dumps(pack.spec.to_json()), encoding="utf-8")
    snapshot_file.write_bytes(
        (TESTS / "fixtures/packs/arm_cell_configuration.graph.json").read_bytes()
    )
    out = tmp_path / "out"
    argv = ["pack", "--spec", str(spec_file), "--snapshot", str(snapshot_file), "--out", str(out)]
    assert main(argv) == 0
    assert pack.id in capsys.readouterr().out
    assert (out / "pack.json").read_bytes() == render_json(pack)
    assert (out / "pack.pdf").read_bytes() == render_pdf(pack)
    assert main(argv) == 0  # same bytes again: nothing to do
    (out / "pack.pdf").write_bytes(b"tampered")
    assert main(argv) == 2
    assert "exists with other bytes" in capsys.readouterr().err
    (out / "pack.pdf").unlink()
    (out / "pack.pdf").symlink_to(tmp_path / "elsewhere.pdf")
    assert main(argv) == 2
    assert "symlink" in capsys.readouterr().err
    assert not (tmp_path / "elsewhere.pdf").exists()


def test_cli_refuses_before_writing_anything(tmp_path: Path) -> None:
    """Review finding: a refused pack.pdf leaves no new pack.json behind."""
    pack = configuration_pack()
    spec_file, snapshot_file = tmp_path / "spec.json", tmp_path / "graph.json"
    spec_file.write_text(json.dumps(pack.spec.to_json()), encoding="utf-8")
    snapshot_file.write_bytes(
        (TESTS / "fixtures/packs/arm_cell_configuration.graph.json").read_bytes()
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "pack.pdf").write_bytes(b"someone else's pdf")
    argv = ["pack", "--spec", str(spec_file), "--snapshot", str(snapshot_file), "--out", str(out)]
    assert main(argv) == 2
    assert sorted(p.name for p in out.iterdir()) == ["pack.pdf"]


def test_cli_reads_at_most_one_byte_past_the_limit(tmp_path: Path) -> None:
    big = tmp_path / "big"
    big.write_bytes(b"x" * 100)
    assert len(pack_cli._read(big, 10)) == 11


def test_cli_reports_refused_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pack = configuration_pack()
    spec_file, snapshot_file = tmp_path / "spec.json", tmp_path / "graph.json"
    spec_file.write_text(json.dumps(pack.spec.to_json()), encoding="utf-8")
    snapshot_file.write_bytes(b'{"kind": "memory.graph"}')
    argv = [
        "pack",
        "--spec",
        str(spec_file),
        "--snapshot",
        str(snapshot_file),
        "--out",
        str(tmp_path / "o"),
    ]
    assert main(argv) == 2
    assert "snapshot_malformed" in capsys.readouterr().err
    assert main([*argv[:4], str(tmp_path / "missing.json"), *argv[5:]]) == 1
    assert not (tmp_path / "o").exists()
