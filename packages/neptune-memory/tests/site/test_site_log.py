"""The fact log: append, read back in seq order, roll segments."""

from pathlib import Path

import pytest

from neptune_memory.site import log as site_log
from neptune_memory.site.facts import Citation, Confirm, Fact, Seen, Value
from neptune_memory.site.log import FactLog, last_seq, log_dir, read

FACT = Fact(
    subject="machine:cv-2",
    predicate="mode",
    object=Value("stopped"),
    valid_from="2026-10-20T10:05:02",
    valid_to=None,
    kind="observed",
    source="plc.cv2",
    citations=(Citation("feed/plc_cv2.jsonl", "line 812", '"state": "STOPPED"'),),
)


def test_site_log_append_and_read_back(tmp_path: Path) -> None:
    with FactLog(tmp_path) as log:
        log.seen("plc.cv2", "2026-10-20T10:05:02")
        log.fact(FACT)
        log.confirm("fact:0123456789abcdef", "yes", "operator")

    entries = list(read(tmp_path))
    assert [e.seq for e in entries] == [1, 2, 3]
    assert [e.type for e in entries] == ["seen", "fact", "confirm"]
    assert entries[0].seen == Seen("plc.cv2", "2026-10-20T10:05:02")
    assert entries[1].fact == FACT
    assert entries[1].fact.id == FACT.id
    assert entries[2].confirm == Confirm("fact:0123456789abcdef", "yes", "operator")
    assert (log_dir(tmp_path) / "segment-000001.jsonl").is_file()


def test_site_log_reopen_continues_seq(tmp_path: Path) -> None:
    with FactLog(tmp_path) as log:
        log.fact(FACT)
    with FactLog(tmp_path) as log:
        assert log.fact(FACT).seq == 2
    assert last_seq(tmp_path) == 2
    assert [e.seq for e in read(tmp_path, as_of_seq=1)] == [1]


def test_site_log_rolls_segments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(site_log, "SEGMENT_SIZE", 2)
    with FactLog(tmp_path) as log:
        for minute in range(5):
            log.seen("fleet", f"2026-10-20T10:0{minute}:00")

    names = sorted(p.name for p in log_dir(tmp_path).iterdir())
    assert names == ["segment-000001.jsonl", "segment-000002.jsonl", "segment-000003.jsonl"]
    assert [e.seq for e in read(tmp_path)] == [1, 2, 3, 4, 5]


def test_site_log_two_writers_never_reuse_a_seq(tmp_path: Path) -> None:
    # `live` holds a writer open while the web view appends a confirm through another.
    with FactLog(tmp_path) as live, FactLog(tmp_path) as web:
        live.seen("fleet", "2026-10-20T10:00:00")
        web.seen("web", "2026-10-20T10:00:01")
        live.seen("fleet", "2026-10-20T10:00:05")
    assert [e.seq for e in read(tmp_path)] == [1, 2, 3]
