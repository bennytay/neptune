"""The live site (#202): broker -> ``live`` -> log, equal to ``build`` over the same capture."""

import dataclasses
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

from neptune_memory.site import corpus
from neptune_memory.site.build import Appender, Seam, build
from neptune_memory.site.facts import Value
from neptune_memory.site.live import broker, captured, live, publish
from neptune_memory.site.log import log_dir, read

UNTIL = "2026-10-19T14:05:00"  # the feed's first quarter hour: a few hundred messages


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _log_bytes(site_dir: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(log_dir(site_dir).iterdir())}


def _wait(condition: Callable[[], bool], seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_site_live_log_equals_build_over_the_capture(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    corpus.generate(site_dir)
    url = f"mqtt://127.0.0.1:{_free_port()}"
    stop_broker, stop_live, started = threading.Event(), threading.Event(), threading.Event()
    server = threading.Thread(
        target=broker, args=(url,), kwargs={"stop": stop_broker, "started": started}
    )
    server.start()
    lines: list[str] = []
    runner = threading.Thread(
        target=live, args=(site_dir, url), kwargs={"stop": stop_live, "say": lines.append}
    )
    try:
        assert started.wait(10)
        runner.start()
        _wait(lambda: any("subscribed" in line for line in lines))
        sent = publish(site_dir, url, speed=1e9, until=UNTIL, sleep=lambda _s: None)
        assert sent > 100
        _wait(lambda: _reports(site_dir) == sent)
    finally:
        stop_live.set()
        runner.join(10)
        stop_broker.set()
        server.join(10)

    assert sum(1 for _ in captured(site_dir)) == sent
    assert any(
        e.fact and e.fact.citations[0].file == "feed/live/segment-000001.jsonl"
        for e in read(site_dir)
    )
    live_log = _log_bytes(site_dir)
    build(site_dir, seam=dataclasses.replace(Seam.default(), messages=captured))
    assert _log_bytes(site_dir) == live_log


SOURCES = {"plc.cv2", "fleet", "arm.c3"}


def _reports(site_dir: Path) -> int:
    """``seen`` entries of the three feed sources: one per message logged."""
    return sum(1 for e in read(site_dir) if e.seen is not None and e.seen.source in SOURCES)


def test_site_live_static_change_appends_only_new_facts(tmp_path: Path) -> None:
    site_dir = tmp_path / "site"
    corpus.generate(site_dir)
    assets = site_dir / "static" / "cmms" / "assets.csv"
    with Appender(site_dir) as log:
        log.static()
        before = log.log.seq
        assert log.restatic() == 0  # nothing changed, nothing appended
        row = "MR-1199,Spare AMR robot,AMR robot,SN-99999,Inbound\n"
        assets.write_text(assets.read_text(encoding="utf-8") + row, encoding="utf-8")
        added = log.restatic()
    new = [e.fact for e in read(site_dir) if e.seq > before and e.fact is not None]
    assert added == len(new) > 0
    assert {f.subject for f in new} == {"robot:MR-1199"}
    assert any(f.predicate == "serial" and f.object == Value("SN-99999") for f in new)
