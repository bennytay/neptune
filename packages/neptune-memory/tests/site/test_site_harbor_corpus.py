"""The Harbor DC corpus is deterministic, unlike Plant 7 in shape, and tells its story (#201)."""

import json
import zipfile
from pathlib import Path

import yaml

from neptune_memory.site.corpus import generate
from neptune_memory.site.replay import messages


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_site_harbor_corpus_generates_identical_bytes(tmp_path: Path) -> None:
    generate(tmp_path / "a", site="harbor")
    generate(tmp_path / "b", site="harbor")
    first = _bytes(tmp_path / "a")
    assert first == _bytes(tmp_path / "b")
    assert {
        "static/cmms/work_orders.json",
        "static/changes/change_log.xlsx",
        "static/docs/HSR-7_excerpt.pdf",
        "static/photos/whiteboard_2026-11-04.extraction.json",
        "feed/inspector_diag.jsonl",
    } <= set(first)
    assert not any(name.endswith(".csv") and "work_orders" in name for name in first)


def test_site_harbor_corpus_leaves_plant_7_alone(tmp_path: Path) -> None:
    generate(tmp_path)
    assert (tmp_path / "static/cmms/work_orders.csv").is_file()
    assert not (tmp_path / "feed/sorter_plc.jsonl").exists()


def test_site_harbor_story_in_the_feeds(tmp_path: Path) -> None:
    generate(tmp_path, site="harbor")
    stream = list(messages(tmp_path))
    assert {m.source for m in stream} == {"plc.sort1", "vendor.humanoid", "rmf.fleet", "ros.insp1"}

    def at(source: str, t: str, topic_end: str = "") -> dict[str, object]:
        match = next(
            m for m in stream if m.source == source and m.t == t and m.topic.endswith(topic_end)
        )
        return match.payload

    # The inspector warns about the hot motor well before the sorter trips.
    warns = [
        m
        for m in stream
        if m.source == "ros.insp1"
        for s in m.payload["status"]
        if "thermal_scan" in s["name"] and s["level"] == 1
    ]
    assert warns[0].t == "2026-11-04T08:05:00" and warns[0].t < "2026-11-04T10:12:10"

    # The sorter faults, then the humanoids idle, and a GTP waits at the dock.
    fault = at("plc.sort1", "2026-11-04T10:12:10")
    assert {"name": "Sorter1.FaultCode", "value": "OVERTEMP_M2"} in fault["nodes"]  # type: ignore[operator]
    assert at("vendor.humanoid", "2026-11-04T10:20:00", "hum-01/state")["status"] == "IDLE"
    waiting = at("rmf.fleet", "2026-11-04T10:20:00", "gtp-01")
    assert waiting["robot"]["mode"] == {"mode": "MODE_WAITING"}  # type: ignore[index]

    # The same robot is gtp-03 to the fleet and AMR-17 to the CMMS: one serial, two names.
    serial = at("rmf.fleet", "2026-11-04T09:00:00", "gtp-03")["extra"]["serial_number"]  # type: ignore[index]
    assets = json.loads((tmp_path / "static/cmms/assets.json").read_text(encoding="utf-8"))
    assert {a["tag"] for a in assets["assets"] if a["serial"] == serial} == {"AMR-17"}
    last = [m for m in stream if m.topic.endswith("/gtp-03")][-1]
    assert last.t == "2026-11-04T09:50:10"


def test_site_harbor_exports_are_not_plant_7_shaped(tmp_path: Path) -> None:
    generate(tmp_path, site="harbor")
    with zipfile.ZipFile(tmp_path / "static/changes/change_log.xlsx") as book:
        assert "xl/worksheets/sheet1.xml" in book.namelist()
    roster = (tmp_path / "static/roster/roster.csv").read_text(encoding="utf-8").splitlines()
    assert roster[0].startswith("Employee,Position,Crew,Shift date,Clock in")
    assert "electrical-isolation|sorter-reset" in roster[1]


def test_site_harbor_mapping_file_covers_every_export(tmp_path: Path) -> None:
    generate(tmp_path, site="harbor")
    mapping = yaml.safe_load((tmp_path / "static/neptune.mappings.yaml").read_text("utf-8"))
    layout = yaml.safe_load((tmp_path / "static/layout.yaml").read_text("utf-8"))
    feed_files = {p.name for p in (tmp_path / "feed").glob("*.jsonl")}
    assert mapping["mappings"] == 1
    feeds = {entry["source"] for entry in mapping["feeds"]}
    assert feeds == {"plc.sort1", "vendor.humanoid", "rmf.fleet", "ros.insp1"}
    assert len(feed_files) == len(feeds)
    declared = {source["id"] for source in layout["sources"]}
    assert feeds <= declared
    assert {t["source"] for t in mapping["tables"]} <= declared
    table_files = {t["file"] for t in mapping["tables"]}
    assert table_files == {
        "cmms/assets.json",
        "cmms/work_orders.json",
        "changes/change_log.xlsx",
        "roster/roster.csv",
    }
    for file in table_files:
        assert (tmp_path / "static" / file).is_file(), file
