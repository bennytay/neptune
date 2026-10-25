"""A machine's own export as a table (issue #211): ``changes: true`` turns a row per sample into
state intervals, ``values`` maps cells to vocabulary values, ``strip`` cleans a quoted timestamp,
the clock falls back to the latest observed fact, and ``draft-mapping`` reads a table back from
the built package (as it does for an XLSX workbook)."""

from pathlib import Path

from neptune_memory.site.build import build
from neptune_memory.site.draft import _package_table
from neptune_memory.site.facts import Fact, Value
from neptune_memory.site.views import load

LAYOUT = """\
site: {id: site:lab, name: Lab}
robots:
  - {id: robot:arm-1, name: Arm 1}
"""

MAPPINGS = """\
mappings: 1
tables:
  - file: arm/export.csv
    source: arm
    kind: observed
    changes: true
    subject: robot:arm-1
    time: '{Timestamp|strip:"|until:.}'
    facts:
      - {predicate: health, column: Stop, values: {"False": ok, "True": "fault:stop"}}
      - {predicate: temperature, column: Temp}
"""

EXPORT = """\
Timestamp,Stop,Temp
2022-10-26T08:00:00.100Z,False,30
2022-10-26T08:00:01.100Z,False,30
"2022-10-26T08:00:02.100Z",True,31
2022-10-26T08:00:03.100Z,,31
2022-10-26T08:00:04.100Z,False,31
"""


def _value(fact: Fact) -> object:
    assert isinstance(fact.object, Value)
    return fact.object.value


def test_site_machine_export_maps_changes_only(tmp_path: Path) -> None:
    static = tmp_path / "static"
    (static / "arm").mkdir(parents=True)
    (static / "layout.yaml").write_text(LAYOUT)
    (static / "neptune.mappings.yaml").write_text(MAPPINGS)
    (static / "arm" / "export.csv").write_text(EXPORT)
    build(tmp_path)
    model = load(tmp_path)
    spans = [(i.start, _value(i.fact)) for i in model.history("robot:arm-1", "health")]
    assert spans == [
        ("2022-10-26T08:00:00", "ok"),
        ("2022-10-26T08:00:02", "fault:stop"),  # the blank row 5 keeps the stop
        ("2022-10-26T08:00:04", "ok"),
    ]
    temps = [(i.start, _value(i.fact)) for i in model.history("robot:arm-1", "temperature")]
    assert temps == [("2022-10-26T08:00:00", "30"), ("2022-10-26T08:00:02", "31")]
    assert model.history("robot:arm-1", "health")[1].fact.citations[0].locator == (
        'row 4 col "Stop"'
    )
    assert model.clock() == "2022-10-26T08:00:04"
    header, samples = _package_table(tmp_path, "arm/export.csv")
    assert header == ["Timestamp", "Stop", "Temp"]
    assert samples[0]["Stop"] == "False"
