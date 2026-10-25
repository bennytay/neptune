"""The live view of ``neptune ingest`` (ADR 0074): its text, when it shows, and that it changes
nothing in the package. Real jobs over real fixtures; the renderer alone over hand-made events."""

import io
import json
import re
import shutil
from pathlib import Path
from typing import Final

import pytest

from neptune.cli import exit_codes, run
from neptune.cli.progress import _UNICODE, LiveView, _fit, _glyphs_for, source_label
from neptune.runtime.events import Phase
from neptune.sdk import JobEvent

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
ELAPSED: Final = re.compile(r"\d+\.\d s")


class Tty(io.StringIO):
    """A stream that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def cli(*argv: str, stderr: io.StringIO | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), stderr if stderr is not None else io.StringIO()
    code = run(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def run_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "run"
    (root / "arm").mkdir(parents=True)
    shutil.copy(FIXTURES / "mcap" / "robot.mcap", root / "arm" / "episode.mcap")
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "text" / "corrupted.txt", root / "arm" / "operator.txt")
    monkeypatch.chdir(tmp_path)
    return root


GOLDEN: Final = """\
Neptune · ingesting run/{pad}3 files
✓ arm/episode.mcap      mcap               16 records  · 3 findings
✓ arm/operator.txt      text                4 records  · 1 finding
✓ notes.txt             text                4 records
⚠ arm/operator.txt      text          block 1 is not valid UTF-8 from byte 38; its text is unknown
── 3 ingested · 51 records · 3 warnings · X s ──
unknown: 3 run bindings with no calibration or configuration record (left unresolved)
unknown: 1 run with no software identity (not guessed)
unknown: 2 clock pairs with no shared reading (left unrelated)
unknown: +2 more kinds: see receipt.md
flagged: 1 text block not valid UTF-8 (text unknown)
flagged: 2 MCAP channels with payloads kept raw (encoding not decoded)
flagged: 1 MCAP attachment cited, not extracted (no record kind holds them yet)
flagged: +1 more kinds: see receipt.md
"""


def test_the_live_view_is_golden_text_for_a_small_run(run_folder: Path) -> None:
    # --no-plugins: the count must not depend on which plugin distributions are installed.
    code, _, err = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--progress", "--no-plugins")
    assert code == 0
    header = "Neptune · ingesting run/"
    pad = " " * (120 - len(header) - len("3 files"))
    assert ELAPSED.sub("X s", err) == GOLDEN.format(pad=pad)


def test_it_is_off_unless_stderr_is_a_terminal_or_asked_for(run_folder: Path) -> None:
    assert cli("ingest", "run", "--out", "a", "-w", "ws")[2] == ""
    assert cli("ingest", "run", "--out", "b", "-w", "ws", "--no-progress")[2] == ""
    on = cli("ingest", "run", "--out", "c", "-w", "ws", stderr=Tty())[2]
    assert on.startswith("\x1b[1mNeptune")  # a terminal gets colour and the view
    assert "── 3 ingested" in on
    assert cli("ingest", "run", "--out", "d", "-w", "ws", "--json", stderr=Tty())[2] == ""
    verbose = cli("ingest", "run", "--out", "e", "-w", "ws", "-v", stderr=Tty())[2]
    assert verbose.startswith("neptune: ") and "Neptune ·" not in verbose


def test_no_colour_when_not_a_terminal_or_no_color_is_set(
    run_folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "\x1b" not in cli("ingest", "run", "--out", "a", "-w", "ws", "--progress")[2]
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in cli("ingest", "run", "--out", "b", "-w", "ws", stderr=Tty())[2]


def test_progress_and_verbose_together_are_a_usage_error(run_folder: Path) -> None:
    code, out, err = cli("ingest", "run", "--out", "pkg", "--progress", "-v")
    assert code == exit_codes.USAGE and out == "" and "--progress and -v" in err
    assert not Path("pkg").exists()


def test_the_view_never_changes_the_package_or_stdout(run_folder: Path) -> None:
    quiet = cli("ingest", "run", "--out", "q", "-w", "ws1", "--json")
    viewed = cli("ingest", "run", "--out", "v", "-w", "ws2", "--json", "--progress")
    assert viewed[2] and viewed[0] == quiet[0] == 0

    def result(text: str) -> dict[str, object]:
        last: dict[str, object] = json.loads(text.splitlines()[-1])
        return {k: v for k, v in last.items() if k not in ("destination", "receipt_path")}

    assert result(viewed[1]) == result(quiet[1])
    assert (Path("q") / "manifest.json").read_bytes() == (Path("v") / "manifest.json").read_bytes()
    assert (Path("q") / "receipt.json").read_bytes() == (Path("v") / "receipt.json").read_bytes()
    again = cli("ingest", "run", "--out", "w", "-w", "ws3", "--progress")[2]
    twice = cli("ingest", "run", "--out", "x", "-w", "ws4", "--progress")[2]
    assert ELAPSED.sub("X s", again) == ELAPSED.sub("X s", twice)  # same bytes, same view


def test_a_rerun_in_the_same_workspace_says_reused(run_folder: Path) -> None:
    cli("ingest", "run", "--out", "first", "-w", "ws")
    err = cli("ingest", "run", "--out", "second", "-w", "ws", "--progress")[2]
    assert "reused" in err and "records" not in err.split("──")[0].split("\n", 1)[1]


def test_a_dry_run_shows_what_it_would_read_and_writes_nothing(run_folder: Path) -> None:
    code, out, err = cli("ingest", "run", "--dry-run", "-w", "ws", "--progress")
    assert code == 0 and out.startswith("planned run:")
    assert "would read 2 chunks" in err
    assert re.search(r"── 3 planned · nothing written · \d+\.\d s ──", err)


def test_a_single_file_is_named_without_a_slash(run_folder: Path) -> None:
    err = cli("ingest", "run/notes.txt", "--out", "pkg", "-w", "ws", "--progress")[2]
    assert err.startswith("Neptune · ingesting notes.txt ")
    assert " 1 file\n" in err


# --- the renderer alone ------------------------------------------------------------------------


def view(**options: object) -> tuple[LiveView, io.StringIO]:
    out = io.StringIO()
    return LiveView(out, name="x/", width=100, **options), out  # type: ignore[arg-type]


def ev(kind: str, phase: str = "parse", **details: object) -> JobEvent:
    return JobEvent(kind, Phase(phase), details)  # type: ignore[arg-type]


def hashed(live: LiveView, source: str, *paths: str) -> None:
    for path in paths:
        live.event(ev("source_hashed", "fingerprint", source=source, location={"path": path}))


def test_a_source_with_no_adapter_is_a_line_at_once_and_counted_unread() -> None:
    live, out = view()
    hashed(live, "s1", "a/b.bin")
    live.event(ev("source_unsupported", "inspect", source="s1", location={"path": "a/b.bin"}))
    assert out.getvalue().startswith("· a/b.bin")
    assert out.getvalue().endswith("no adapter claims it: kept as evidence only\n")
    live.finish(ingested=0, records=0, findings=[], by_source={}, elapsed=1.25)
    assert "── 0 ingested · 1 unread · 1.2 s ──" in out.getvalue()


def test_a_failed_chunk_waits_for_the_jobs_verdict() -> None:
    live, out = view()
    hashed(live, "s1", "bag/a.mcap")
    live.event(ev("source_selected", "inspect", source="s1", adapter="mcap"))
    live.event(ev("source_planned", "plan", source="s1", adapter="mcap", chunks=2))
    live.event(ev("chunk_committed", "normalize", source="s1", records=5, findings=0))
    live.event(ev("chunk_failed", "parse", source="s1"))
    assert out.getvalue() == ""
    live.event(ev("source_salvaged", "assemble", source="s1", committed=1, chunks=2, lost=1))
    assert "⚠ bag/a.mcap" in out.getvalue()
    assert "salvaged: 1 of 2 chunks kept, 1 lost and flagged" in out.getvalue()


def test_a_quarantined_source_names_why() -> None:
    live, out = view()
    hashed(live, "s1", "bag/a.mcap")
    live.event(ev("source_selected", "inspect", source="s1", adapter="mcap"))
    live.event(ev("source_quarantined", "assemble", source="s1", codes=["limit_exceeded"]))
    assert "✗ bag/a.mcap" in out.getvalue() and "quarantined: limit_exceeded" in out.getvalue()


def test_duplicate_paths_and_the_ascii_fallback() -> None:
    class Latin1(io.StringIO):
        encoding = "latin-1"

    out = Latin1()
    live = LiveView(out, name="x/", width=100)
    hashed(live, "s1", "a.csv", "copy/a.csv")
    live.event(ev("source_selected", "inspect", source="s1", adapter="tabular"))
    live.event(ev("source_planned", "plan", source="s1", adapter="tabular", chunks=1))
    live.event(ev("chunk_committed", "normalize", source="s1", records=1, findings=0))
    text = out.getvalue()
    assert text.startswith("+ a.csv") and "1 record" in text and "(+1 identical copy)" in text
    assert text.isascii()
    assert _glyphs_for(io.StringIO()) is _UNICODE


def test_paths_are_fitted_by_eliding_the_middle_directories() -> None:
    glyphs = _UNICODE
    path = "sites/PLANT-2/legged/runs/2026-09-14/patrol_0.mcap"
    assert _fit(path, 80, glyphs) == path
    fitted = _fit(path, 34, glyphs)
    assert len(fitted) <= 34 and fitted.startswith("sites/") and fitted.endswith("/patrol_0.mcap")
    assert "…" in fitted
    assert len(_fit("a" * 50 + ".mcap", 20, glyphs)) == 20  # one name too long: cut from the left


def test_source_labels() -> None:
    assert source_label("runs/2026-09-30/", True) == "2026-09-30/"
    assert source_label("episode-7.mcap", False) == "episode-7.mcap"
