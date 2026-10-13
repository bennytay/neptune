"""``python -m harness.demo``'s progress lines and its clean start (Platform ADR 0011): one short
line per stage on stderr while it runs, and a rerun over a half-written ``demo/`` starts clean."""

import io
import re
from pathlib import Path
from typing import Any

from harness import demo
from harness.run import run


def test_each_stage_says_what_it_is_doing_then_how_it_ended(tmp_path: Path) -> None:
    stream = io.StringIO()
    out = tmp_path / "run"
    live = demo.Progress(stream, out / "work")
    live.setup()
    report, code = run(out, owner_tests=False, corpus_name="worked-examples", on_stage=live.stage)
    assert code == 0
    lines = stream.getvalue().splitlines()
    assert lines[0] == "[setup] checking the contracts and building the corpus…"
    starts = [line for line in lines if line.startswith("[")][1:]
    names = [re.match(r"\[(\d)/5\] (\w+): ", line) for line in starts]
    assert [m.groups() for m in names if m] == [
        ("1", "compiler"),
        ("2", "deploy"),
        ("3", "ledger"),
        ("4", "memory"),
        ("5", "context"),
    ]
    assert re.fullmatch(r"\[1/5\] compiler: ingesting \d+ files…", starts[0])
    ends = [line for line in lines if not line.startswith("[")]
    assert len(ends) == 6 and all(re.fullmatch(r" {6}ok \d+\.\d s", end) for end in ends)
    assert [e["status"] for e in report["stages"]] == ["ok"] * 5


def test_a_failed_or_skipped_stage_says_so_with_its_first_problem(tmp_path: Path) -> None:
    stream = io.StringIO()
    live = demo.Progress(stream, tmp_path)
    stage: Any = type("S", (), {"id": "ledger"})()
    live.stage(stage, 3, 5, {"status": "failed", "problems": ["no space left", "more"]})
    live.stage(stage, 4, 5, {"status": "skipped", "problems": ["an upstream stage did not pass"]})
    first, second = stream.getvalue().splitlines()
    assert re.fullmatch(r" {6}failed \d+\.\d s: no space left", first)
    assert second == "      skipped: an upstream stage did not pass"


def test_without_a_stream_it_is_silent(tmp_path: Path, capsys: Any) -> None:
    live = demo.Progress(None, tmp_path)
    live.setup()
    live.begin("[packs] x")
    live.end()
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_a_rerun_removes_the_last_runs_outputs_and_nothing_else(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    (out / "packs" / "p").mkdir(parents=True)
    (out / "packs" / "p" / "pack.json").write_text("{}")
    for name in ("graph.json", "answers.md", "demo.md", "incident-timeline-INC-C3-0011.pdf"):
        (out / name).write_text("stale")
    (out / "work").mkdir()
    (out / "notes.txt").write_text("mine")
    demo._clean(out)
    assert sorted(p.name for p in out.iterdir()) == ["notes.txt", "work"]
    demo._clean(out)  # idempotent, and fine on a directory with nothing in it
