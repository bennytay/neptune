"""Compiler records of a small hand-made ``static/`` folder become cited facts (spec §5, §7, §9)."""

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from neptune.runtime.job import JobOptions
from neptune.runtime.sandbox import Isolation
from neptune.sdk import ingest
from neptune.store.package import read_package
from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.mappers import map_record

ROOT = Path(__file__).resolve().parents[4]

CSV = {
    "cmms/assets.csv": "asset,type,name,serial\nMR-1142,robot,AMR 2,SN-88412\n",
    "cmms/work_orders.csv": (
        "wo,asset_type,asset,opened,closed,action,cause,by,loto\n"
        "WO-5530,machine,cv-2,2026-10-20T10:08:00,2026-10-20T10:41:00,"
        "cleared jam at PE3,belt tracking off,lindqvist,yes\n"
    ),
    "changes/changes.csv": (
        "change,asset_type,asset,at,description\n"
        "CHG-311,machine,cv-2,2026-10-20T05:40:00,speed setpoint 0.80 -> 0.95 m/s\n"
    ),
    "roster/shift_roster.csv": (
        "person,name,role,shift,start,end,break_start,break_end\n"
        "okafor,Okafor,maintenance-tech,A-2026-10-20,2026-10-20T06:00:00,2026-10-20T14:00:00,"
        "2026-10-20T10:30:00,2026-10-20T11:00:00\n"
    ),
    "roster/qualifications.csv": (
        "person,qualification,granted\n"
        "okafor,arm-reset-c3,\n"
        "lindqvist,conveyor-lockout,2025-03-01T00:00:00\n"
    ),
}
TENSION = "Belt tension: 45\N{EN DASH}55 N."
PDFS = {
    "docs/SOP-CV-04_rev-B.pdf": [
        "SOP-CV-04 rev B: CV-2 belt",
        TENSION,
        "Clearing a jam requires the conveyor-lockout qualification.",
    ],
    "docs/PSR-12_excerpt.pdf": [
        "PSR-12 plant safety regulation (excerpt)",
        "§4.2 Jams on powered conveyors are cleared only under lockout by a person "
        "qualified for conveyor lockout.",
    ],
}
EXTRACTION = {
    "image": "whiteboard_2026-10-20.png",
    "model": "claude-test",
    "prompt_sha256": "0123456789abcdef" * 4,
    "taken": "2026-10-20T06:15:00",
    "regions": [
        {
            "box": [120, 40, 380, 90],
            "subject": "machine:cv-2",
            "text": "CV-2: PE3 dirty — clean every shift",
        }
    ],
}


def _pdf_writer() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "neptune_pdf_writer", ROOT / "tests" / "fixtures" / "pdf" / "make_pdfs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _pdf(lines: list[str]) -> bytes:
    w = _pdf_writer()
    pdf = w.Pdf()
    tree = pdf.reserve()
    font = pdf.add(w.HELVETICA)
    content = b"".join(
        w.text("F1", 11, 54, 700 - 40 * i, line.encode("cp1252")) for i, line in enumerate(lines)
    )
    page = w.page(pdf, tree, content, {"Font": {"F1": font}})
    w.page_tree(pdf, [page], tree)
    data: bytes = pdf.build(w.catalog(pdf, tree))
    return data


@pytest.fixture(scope="module")
def facts(tmp_path_factory: pytest.TempPathFactory) -> list[Fact]:
    site = tmp_path_factory.mktemp("site")
    static = site / "static"
    for name, text in CSV.items():
        (static / name).parent.mkdir(parents=True, exist_ok=True)
        (static / name).write_text(text, encoding="utf-8")
    for name, lines in PDFS.items():
        (static / name).parent.mkdir(parents=True, exist_ok=True)
        (static / name).write_bytes(_pdf(lines))
    photos = static / "photos"
    photos.mkdir()
    shutil.copy(ROOT / "tests/fixtures/image/amr_dock.png", photos / "whiteboard_2026-10-20.png")
    extraction = json.dumps(EXTRACTION, ensure_ascii=False)
    (photos / "whiteboard_2026-10-20.extraction.json").write_text(extraction, encoding="utf-8")
    package_dir = site / ".neptune" / "package"
    ingest(static, package_dir, options=JobOptions(isolation=Isolation.IN_PROCESS))
    package = read_package(package_dir)
    return [fact for record in package.records for fact in map_record(record, package)]


def _one(facts: list[Fact], subject: str, predicate: str) -> Fact:
    found = [f for f in facts if f.subject == subject and f.predicate == predicate]
    assert len(found) == 1, (subject, predicate, found)
    return found[0]


def test_cmms_rows_are_stated_and_cite_their_cell(facts: list[Fact]) -> None:
    serial = _one(facts, "robot:MR-1142", "serial")
    assert (serial.object, serial.kind, serial.source) == (Value("SN-88412"), "stated", "cmms")
    assert serial.citations == (
        Citation("static/cmms/assets.csv", 'row 2 col "serial"', "SN-88412"),
    )

    link = _one(facts, "machine:cv-2", "work_order")
    assert link.object == Ref("work_order:WO-5530")
    assert link.valid_from == "2026-10-20T10:08:00"
    assert _one(facts, "work_order:WO-5530", "wo_cause").object == Value("belt tracking off")
    assert _one(facts, "work_order:WO-5530", "wo_by").object == Ref("person:lindqvist")
    loto = _one(facts, "work_order:WO-5530", "wo_loto")
    assert loto.object == Value("yes")
    assert loto.citations[0].locator == 'row 2 col "loto"'

    change = _one(facts, "machine:cv-2", "change")
    assert (change.object, change.valid_from) == (Ref("change:CHG-311"), "2026-10-20T05:40:00")
    assert change.citations[0].file == "static/changes/changes.csv"


def test_roster_gives_roles_shifts_breaks_and_qualifications(facts: list[Fact]) -> None:
    assert _one(facts, "person:okafor", "has_role").object == Ref("role:maintenance-tech")
    shift = _one(facts, "person:okafor", "on_shift")
    assert (shift.valid_from, shift.valid_to) == ("2026-10-20T06:00:00", "2026-10-20T14:00:00")
    brk = _one(facts, "person:okafor", "on_break")
    assert (brk.object, brk.valid_from, brk.valid_to) == (
        Value("break"),
        "2026-10-20T10:30:00",
        "2026-10-20T11:00:00",
    )
    held = _one(facts, "person:lindqvist", "holds_qualification")
    assert held.object == Ref("qualification:conveyor-lockout")
    assert held.valid_from == "2025-03-01T00:00:00"
    assert _one(facts, "person:okafor", "holds_qualification").valid_from is None
    assert held.citations[0].file == "static/roster/qualifications.csv"


def test_pdf_values_are_stated_only_verbatim_citing_page_and_block(facts: list[Fact]) -> None:
    limit = _one(facts, "document:SOP-CV-04", "states_limit")
    assert (limit.object, limit.kind, limit.source) == (
        Value("45\N{EN DASH}55 N"),
        "stated",
        "docs",
    )
    assert limit.citations == (
        Citation("static/docs/SOP-CV-04_rev-B.pdf", "page 1 block 2", TENSION),
    )
    needs = _one(facts, "document:SOP-CV-04", "requires_qualification")
    assert needs.object == Ref("qualification:conveyor-lockout")
    assert needs.citations[0].locator == "page 1 block 3"

    rule = _one(facts, "rule:psr-12-4.2", "rule_text")
    assert str(rule.object.value).startswith("Jams on powered conveyors")  # type: ignore[union-attr]
    assert rule.citations[0].file == "static/docs/PSR-12_excerpt.pdf"
    ruled = _one(facts, "rule:psr-12-4.2", "requires_qualification")
    assert ruled.object == Ref("qualification:conveyor-lockout")
    assert _one(facts, "document:PSR-12", "name").object == Value(
        "PSR-12 plant safety regulation (excerpt)"
    )
    assert not [f for f in facts if f.predicate == "states_limit" and "PSR" in f.subject]


def test_whiteboard_region_is_an_inferred_note_with_its_model(facts: list[Fact]) -> None:
    note = _one(facts, "machine:cv-2", "note")
    assert note.object == Value("CV-2: PE3 dirty — clean every shift")
    assert (note.kind, note.source, note.valid_from) == (
        "inferred",
        "photos",
        "2026-10-20T06:15:00",
    )
    assert note.model == "claude-test@0123456789ab"
    assert note.citations == (
        Citation("static/photos/whiteboard_2026-10-20.png", "region 120,40,380,90", None),
    )


def test_every_fact_is_cited_and_labelled(facts: list[Fact]) -> None:
    assert facts
    assert all(f.citations and f.kind in ("stated", "inferred") for f in facts)
