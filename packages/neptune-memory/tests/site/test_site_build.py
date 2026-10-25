"""build/replay over a tiny static dir and stub feed messages (spec §5).

The mappers (#184) and the replay (#182) are stubbed here through ``Seam``.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neptune_memory.site.build import Seam, build, replay
from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.log import log_dir, read


@dataclass(frozen=True)
class StubMessage:
    t: str
    source: str
    topic: str = "stub"
    payload: dict[str, Any] = field(default_factory=dict)


MESSAGES = (
    StubMessage("2026-10-20T10:05:00", "plc.cv2", payload={"state": "RUNNING"}),
    StubMessage("2026-10-20T10:05:02", "plc.cv2", payload={"state": "STOPPED"}),
    StubMessage("2026-10-20T10:05:30", "arm.c3", payload={"state": "IDLE_STARVED"}),
)


def _map_layout(path: Path) -> list[Fact]:
    cite = (Citation("static/layout.yaml", "line 1", path.read_text(encoding="utf-8").strip()),)
    return [Fact("machine:cv-2", "feeds", Ref("cell:c3"), None, None, "stated", "layout", cite)]


def _map_record(record: Any, package: Any) -> list[Fact]:
    if record.kind != "structured_record":
        return []
    cite = (Citation("static/cmms/assets.csv", str(record.provenance.evidence.locator[0]), None),)
    return [Fact(f"record:{record.id}", "name", Value("row"), None, None, "stated", "cmms", cite)]


def _map_message(message: StubMessage) -> list[Fact]:
    cite = (Citation(f"feed/{message.source}.jsonl", "line 1", message.payload["state"]),)
    mode = Value(message.payload["state"].lower())
    return [Fact("machine:x", "mode", mode, message.t, None, "observed", message.source, cite)]


SEAM = Seam(_map_layout, _map_record, _map_message, lambda site_dir: iter(MESSAGES))


def _site(root: Path) -> Path:
    (root / "static" / "cmms").mkdir(parents=True)
    (root / "static" / "layout.yaml").write_text("site: p7\n", encoding="utf-8")
    (root / "static" / "cmms" / "assets.csv").write_text(
        "asset,serial\nMR-1141,SN-88411\nMR-1142,SN-88412\n", encoding="utf-8"
    )
    return root


def _log_bytes(site_dir: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(log_dir(site_dir).iterdir())}


def test_site_build_static_then_feed(tmp_path: Path) -> None:
    site_dir = _site(tmp_path / "site")
    assert build(site_dir, seam=SEAM) == log_dir(site_dir)

    entries = list(read(site_dir))
    assert [e.seq for e in entries] == list(range(1, len(entries) + 1))
    kinds = [
        (e.type, e.fact.source if e.fact else e.seen.source if e.seen else None) for e in entries
    ]
    assert kinds == [
        *[("fact", "cmms")] * 3,  # one per CSV row, header included
        ("fact", "layout"),
        ("seen", "plc.cv2"),
        ("fact", "plc.cv2"),
        ("seen", "plc.cv2"),
        ("fact", "plc.cv2"),
        ("seen", "arm.c3"),
        ("fact", "arm.c3"),
    ]


def test_site_build_until_stops_the_feed(tmp_path: Path) -> None:
    site_dir = _site(tmp_path / "site")
    build(site_dir, until="2026-10-20T10:05:02", seam=SEAM)
    seen = [e.seen.t for e in read(site_dir) if e.seen]
    assert seen == ["2026-10-20T10:05:00", "2026-10-20T10:05:02"]


def test_site_build_twice_gives_the_same_log(tmp_path: Path) -> None:
    first, second = _site(tmp_path / "a"), _site(tmp_path / "b")
    build(first, seam=SEAM)
    build(second, seam=SEAM)
    once = _log_bytes(first)
    assert once == _log_bytes(second)
    build(first, seam=SEAM)  # a rebuild starts a fresh log
    assert _log_bytes(first) == once


def test_site_replay_paces_and_matches_build(tmp_path: Path) -> None:
    built, live = _site(tmp_path / "built"), _site(tmp_path / "live")
    build(built, seam=SEAM)
    sleeps: list[float] = []
    replay(live, speed=2.0, seam=SEAM, sleep=sleeps.append)
    assert sleeps == [1.0, 14.0]
    assert _log_bytes(live) == _log_bytes(built)


def test_site_build_fleet_messages_are_seen_per_robot() -> None:
    import yaml

    from neptune_memory.site.corpus.static import MAPPINGS
    from neptune_memory.site.mappers import Mapping, device
    from neptune_memory.site.replay import FeedMessage

    mapping = Mapping(yaml.safe_load(MAPPINGS))
    fleet = FeedMessage("t", "fleet", "uagv/v2/p7/amr-03/state", {}, "feed/fleet.jsonl", 1)
    plc = FeedMessage("t", "plc.cv2", "spBv1.0/P7/DDATA/cv2-edge/cv2", {}, "feed/plc.jsonl", 1)
    assert device(fleet, mapping) == "amr-03"
    assert device(plc, mapping) is None
