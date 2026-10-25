"""The Plant 7 corpus is deterministic and replays in arrival order (spec §9)."""

import json
from pathlib import Path

from neptune_memory.site.corpus import generate
from neptune_memory.site.replay import FeedMessage, messages, pace


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_site_corpus_generates_identical_bytes(tmp_path: Path) -> None:
    generate(tmp_path / "a")
    generate(tmp_path / "b")
    first = _bytes(tmp_path / "a")
    assert first == _bytes(tmp_path / "b")
    assert "static/docs/PSR-12_excerpt.pdf" in first
    assert "feed/plc_cv2.jsonl" in first


def test_site_replay_is_in_arrival_order(tmp_path: Path) -> None:
    generate(tmp_path)
    stream = list(messages(tmp_path))
    keys = [(m.t, m.source, m.line) for m in stream]
    assert keys == sorted(keys)
    assert {m.source for m in stream} == {"plc.cv2", "fleet", "arm.c3"}

    jam = next(m for m in stream if m.source == "plc.cv2" and m.t == "2026-10-20T10:05:02")
    assert jam.file == "feed/plc_cv2.jsonl"
    assert {"name": "alarm", "value": "JAM_PE3"} in jam.payload["metrics"]
    last_amr3 = [m for m in stream if m.topic.endswith("/amr-03/state")][-1]
    assert last_amr3.t == "2026-10-20T09:40:05"
    assert last_amr3.payload["batteryState"]["batteryCharge"] == 22.0

    line = (tmp_path / jam.file).read_text(encoding="utf-8").splitlines()[jam.line - 1]
    assert json.loads(line)["t"] == jam.t


def test_site_pace_sleeps_the_scaled_gap() -> None:
    def msg(t: str) -> FeedMessage:
        return FeedMessage(t, "plc.cv2", "x", {}, "feed/x.jsonl", 1)

    slept: list[float] = []
    out = list(
        pace(
            [msg("2026-10-20T10:00:00"), msg("2026-10-20T10:00:05"), msg("2026-10-20T10:00:05")],
            speed=5.0,
            sleep=slept.append,
        )
    )
    assert len(out) == 3
    assert slept == [1.0]
