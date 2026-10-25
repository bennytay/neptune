"""Mappings from config, not code (spec §12): a second-shaped site maps from its mapping file
alone, unmapped files stay searchable and show in ``gaps``, and the mappers hold no Plant 7."""

import ast
import json
from pathlib import Path

import pytest

from neptune_memory.site import mappers
from neptune_memory.site.__main__ import main
from neptune_memory.site.build import build
from neptune_memory.site.facts import Fact, Ref, Value
from neptune_memory.site.log import read
from neptune_memory.site.query import gaps, search_evidence
from neptune_memory.site.views import load

LAYOUT = """\
site: {id: site:ks1, name: Kestrel cold store}
zones:
  - {id: zone:dock-1, name: Dock 1, part_of: site:ks1}
robots:
  - {id: robot:fl-7, name: Forklift 7, located_in: zone:dock-1}
sources:
  - {id: telemetry, name: Forklift telemetry, report_interval: 10}
  - {id: eam, name: Asset system}
  - {id: hr, name: Certifications}
"""

MAPPINGS = """\
mappings: 1
tables:
  - file: eam/*.csv
    source: eam
    subject: "robot:{Equip ID|lower}"
    facts:
      - {predicate: name, column: Description}
      - {predicate: serial, column: Serial No}
  - file: hr/certs.jsonl
    source: hr
    subject: "person:{person|lower}"
    time: since
    facts:
      - {predicate: holds_qualification, column: cert, ref: "qualification:{cert}"}
feeds:
  - source: telemetry
    decoder: json
    subject: "robot:{topic[1]}"
    fields:
      st: mode
      soc: {predicate: battery, unit: "%"}
      flt: health
      where: {predicate: in_zone, ref: zone}
"""

EQUIPMENT = "Equip ID,Description,Serial No\nFL-7,Forklift 7 (reach truck),RT-55012\n"
CERTS = (
    '{"person": "Ito", "cert": "forklift-class-2", "since": "2025-01-01"}\n'
    '{"person": "Ito", "cert": "cold-store-ppe", "since": "2025-02-01"}\n'
)
TEMPS = "Probe,Reading C,At\nP-4,-18.5,2026-10-20T08:00:00\nP-5,-17.9,2026-10-20T08:00:00\n"


def _envelope(t: str, payload: dict[str, object]) -> str:
    message = {"t": t, "source": "telemetry", "topic": "ks1/fl-7/telemetry", "payload": payload}
    return json.dumps(message)


FEED = [
    _envelope("2026-10-20T08:00:00", {"st": "DRIVE", "soc": 77, "flt": "", "where": "dock-1"}),
    _envelope("2026-10-20T08:00:10", {"st": "DRIVE", "soc": 76, "flt": "", "where": "dock-1"}),
    _envelope("2026-10-20T08:00:20", {"st": "STOP", "soc": 76, "flt": "E42", "where": "dock-1"}),
]


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("ks1")
    files = {
        "static/layout.yaml": LAYOUT,
        "static/neptune.mappings.yaml": MAPPINGS,
        "static/eam/equipment.csv": EQUIPMENT,
        "static/hr/certs.jsonl": CERTS,
        "static/temps.csv": TEMPS,
        "feed/telemetry.jsonl": "\n".join(FEED) + "\n",
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    build(root)
    return root


@pytest.fixture(scope="module")
def facts(site: Path) -> list[Fact]:
    return [entry.fact for entry in read(site) if entry.fact is not None]


def _one(facts: list[Fact], subject: str, predicate: str, obj: Ref | Value) -> Fact:
    found = [f for f in facts if (f.subject, f.predicate, f.object) == (subject, predicate, obj)]
    assert len(found) == 1, (subject, predicate, obj, [f for f in facts if f.subject == subject])
    return found[0]


def test_tables_map_from_config(facts: list[Fact]) -> None:
    serial = _one(facts, "robot:fl-7", "serial", Value("RT-55012"))
    assert (serial.kind, serial.source) == ("stated", "eam")
    assert serial.citations[0].file == "static/eam/equipment.csv"
    assert serial.citations[0].locator == 'row 2 col "Serial No"'
    cert = _one(facts, "person:ito", "holds_qualification", Ref("qualification:forklift-class-2"))
    assert cert.valid_from == "2025-01-01"
    assert cert.citations[0].locator == 'line 1 col "cert"'


def test_feed_maps_from_config(facts: list[Fact]) -> None:
    battery = _one(facts, "robot:fl-7", "battery", Value(76, "%"))
    assert (battery.kind, battery.valid_from) == ("observed", "2026-10-20T08:00:10")
    assert _one(facts, "robot:fl-7", "in_zone", Ref("zone:dock-1")).citations[0].quote == "dock-1"
    fault = _one(facts, "robot:fl-7", "health", Value("fault:E42"))
    assert fault.citations[0].locator == "line 3"
    assert len([f for f in facts if f.predicate == "mode"]) == 2  # only on change


def test_unmapped_file_is_searchable_and_a_gap(site: Path) -> None:
    model = load(site)
    found = search_evidence(model, "P-4")
    assert [item.subject for item in found.items] == ["document:temps.csv"]
    assert found.sources[found.items[0].cite[0]].file == "static/temps.csv"
    assert "static/temps.csv: not mapped" in gaps(model).gaps


def test_draft_mapping_for_the_unmapped_table_and_the_feed(
    site: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["draft-mapping", str(site), "static/temps.csv"]) == 0
    table = capsys.readouterr().out
    assert '- file: "temps.csv"' in table and 'time: "At"' in table
    assert '"Reading C"' in table
    assert main(["draft-mapping", str(site), "feed/telemetry.jsonl"]) == 0
    feed = capsys.readouterr().out
    assert "decoder: json" in feed and '"soc": battery' in feed


def test_mappers_hold_no_plant_7() -> None:
    """Outside docstrings, ``site/mappers/`` names no Plant 7 id, path, column or topic."""
    plant_7 = (
        "p7", "cv-2", "cv2", "c3", "amr", "fleet", "okafor", "assets.csv", "work_orders",
        "roster", "qualification", "cmms", "docs/", "photos", "uagv", "serialnumber",
        "operatingmode", "orderid", "parts_out", "sop",
    )  # fmt: skip
    root = Path(mappers.__file__).parent
    for module in sorted(root.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            first = body[0] if isinstance(body, list) and body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                body[0] = ast.Pass()  # type: ignore[index]
        code = ast.unparse(tree).lower()
        assert [word for word in plant_7 if word in code] == [], module.name
