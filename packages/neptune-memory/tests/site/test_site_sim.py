"""The simulated plant (spec §16): its feed builds into the site model, commands reach it, and a
seed plus the same commands give the same bytes."""

import json
import socket
import threading
import time
from pathlib import Path

from neptune_memory.site.actions import is_command
from neptune_memory.site.build import build
from neptune_memory.site.live import broker, live
from neptune_memory.site.log import read
from neptune_memory.site.sim import run
from neptune_memory.site.sim.preview import preview

UNTIL = "2026-10-20T08:30:00"  # seed 7 jams CV-2 at 07:13:55


def _feeds(site_dir: Path) -> dict[str, bytes]:
    return {rel: (site_dir / rel).read_bytes() for rel in run.FEEDS.values()}


def test_site_sim_offline_run_is_deterministic_and_builds(tmp_path: Path) -> None:
    site_dir = tmp_path / "plant"
    run.prepare(site_dir)
    summary = run.run(site_dir, until=UNTIL)
    first = _feeds(site_dir)
    work_orders = (site_dir / run.WORK_ORDERS).read_text(encoding="utf-8")

    run.prepare(site_dir)
    run.run(site_dir, until=UNTIL)
    assert _feeds(site_dir) == first
    assert summary["kpis"]["2026-10-20"]["jams"] >= 1
    assert "WO-5532,MR-1141,CV-2" in work_orders
    assert (site_dir / "sim" / "scene.json").exists()
    frame = json.loads((site_dir / "sim" / "timeline.jsonl").read_text().splitlines()[-1])
    assert {a["id"] for a in frame["amrs"]} == {"robot:amr-01", "robot:amr-02", "robot:amr-03"}
    page = preview(site_dir, "2026-10-20T07:10:00", "2026-10-20T07:20:00").read_text()
    assert '"alarm"' not in page and "JAM_PE3" in page and "/*DATA*/null" not in page

    build(site_dir)
    facts = [e.fact for e in read(site_dir) if e.fact is not None]
    assert any(
        f.subject == "machine:cv-2" and f.predicate == "health" and "JAM_PE3" in str(f.object)
        for f in facts
    )
    assert any(f.subject == "robot:arm-c3" and "idle_starved" in str(f.object) for f in facts)
    assert any(f.subject == "work_order:WO-5532" for f in facts)


def test_site_sim_commands_change_the_plant_and_replay(tmp_path: Path) -> None:
    site_dir = tmp_path / "plant"
    run.prepare(site_dir)
    commands = tmp_path / "commands.jsonl"
    order = {"orderId": "neptune-1", "nodes": [{"nodeId": "zone:aisle-b"}], "edges": []}
    dcmd = {"metrics": [{"name": "speed_setpoint", "value": 0.85}]}
    lines = [
        {"t": "2026-10-20T06:30:00", "topic": "spBv1.0/P7/DCMD/cv2-edge/cv2", "payload": dcmd},
        {"t": "2026-10-20T06:31:00", "topic": "uagv/v2/p7/amr-02/order", "payload": order},
    ]
    commands.write_text("".join(json.dumps(line) + "\n" for line in lines))
    run.run(site_dir, commands=commands, until="2026-10-20T06:40:00", frames=0)
    first = _feeds(site_dir)

    plc = [json.loads(x) for x in first["feed/plc_cv2.jsonl"].decode().splitlines()]
    assert plc[-1]["payload"]["metrics"][2]["value"] == 0.85
    fleet = [json.loads(x) for x in first["feed/fleet.jsonl"].decode().splitlines()]
    last = [m for m in fleet if m["topic"] == "uagv/v2/p7/amr-02/state"][-1]["payload"]
    assert last["zone"] == "zone:aisle-b"

    run.prepare(site_dir)
    run.run(site_dir, commands=commands, until="2026-10-20T06:40:00", frames=0)
    assert _feeds(site_dir) == first
    assert is_command("spBv1.0/P7/DCMD/cv2-edge/cv2")
    assert not is_command("spBv1.0/P7/DDATA/cv2-edge/cv2")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def test_site_sim_live_feeds_the_site_model(tmp_path: Path) -> None:
    site_dir = tmp_path / "plant"
    run.prepare(site_dir)
    url = f"mqtt://127.0.0.1:{_free_port()}"
    stop_broker, stop_live, started = threading.Event(), threading.Event(), threading.Event()
    server = threading.Thread(
        target=broker, args=(url,), kwargs={"stop": stop_broker, "started": started}
    )
    server.start()
    said: list[str] = []
    runner = threading.Thread(
        target=live, args=(site_dir, url), kwargs={"stop": stop_live, "say": said.append}
    )
    try:
        assert started.wait(10)
        runner.start()
        deadline = time.monotonic() + 60
        while not any("subscribed" in line for line in said):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        run.run_live(
            site_dir, url, speed=1e9, until="2026-10-20T06:05:00", say=said.append,
            sleep=lambda _s: None,
        )  # fmt: skip
        fleet = "uagv/v2/p7/amr-01/state"
        deadline = time.monotonic() + 60
        while not any(
            e.fact is not None and e.fact.subject == "robot:amr-01" and e.fact.predicate == "mode"
            for e in read(site_dir)
        ):
            assert time.monotonic() < deadline, f"no facts from {fleet}"
            time.sleep(0.1)
    finally:
        stop_live.set()
        runner.join(10)
        stop_broker.set()
        server.join(10)
    assert (site_dir / "feed" / "live" / "segment-000001.jsonl").exists()
