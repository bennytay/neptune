"""``layout.yaml`` becomes stated facts citing its lines (spec §5, §9)."""

from pathlib import Path

from neptune_memory.site.facts import Citation, Ref, Value
from neptune_memory.site.mappers import map_layout

LAYOUT = """\
site: {id: site:p7, name: Plant 7 assembly hall}
clock: {name: P7 local, zone: Europe/Berlin}
entities:
  - id: area:assembly
    name: Assembly hall
    part_of: site:p7
  - id: cell:c3
    part_of: area:assembly
    feeds: [zone:aisle-a]
  - id: machine:cv-2
    name: Conveyor CV-2
    feeds: [cell:c3]
  - id: robot:arm-c3
    located_in: cell:c3
sources:
  - {id: plc.cv2, file: feed/plc_cv2.jsonl, report_interval: 5}
  - {id: cmms}
"""


def test_layout_is_stated_topology_names_and_intervals(tmp_path: Path) -> None:
    path = tmp_path / "layout.yaml"
    path.write_text(LAYOUT, encoding="utf-8")
    facts = map_layout(path)
    triples = {(f.subject, f.predicate, f.object) for f in facts}
    assert triples == {
        ("site:p7", "name", Value("Plant 7 assembly hall")),
        ("area:assembly", "name", Value("Assembly hall")),
        ("area:assembly", "part_of", Ref("site:p7")),
        ("cell:c3", "part_of", Ref("area:assembly")),
        ("cell:c3", "feeds", Ref("zone:aisle-a")),
        ("machine:cv-2", "name", Value("Conveyor CV-2")),
        ("machine:cv-2", "feeds", Ref("cell:c3")),
        ("robot:arm-c3", "located_in", Ref("cell:c3")),
        ("source:plc.cv2", "report_interval", Value(5, "s")),
    }
    assert {(f.kind, f.source, f.valid_from) for f in facts} == {("stated", "layout", None)}
    feeds = next(f for f in facts if f.subject == "machine:cv-2" and f.predicate == "feeds")
    assert feeds.citations == (Citation("static/layout.yaml", "line 12", "feeds: [cell:c3]"),)
    assert map_layout(path) == facts  # the same file maps to the same facts
