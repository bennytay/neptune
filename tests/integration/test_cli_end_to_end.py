"""MVL-11 acceptance, end to end: the installed ``neptune`` command over a messy fleet folder.

The folder spans three embodiments (a manipulator cell's MCAP recording and operator log, a
quadruped's notes and a truncated recording, a surface vessel's damaged site notes) plus the debris
a copy through a laptop leaves (``.git/``, ``.DS_Store``, ``._`` files), a ``.neptune-ignore``
and a symlink. No manifest, no setup: one command, the default sandbox, a fresh workspace.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.sdk import read_package

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures"
NEPTUNE: Final = Path(sys.executable).parent / "neptune"  # the console script (pyproject.toml)


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    root = tmp_path / "fleet-2026-09-30"
    arm, legs, boat = root / "arm-cell-3", root / "quadruped-b2", root / "usv-harbour"
    for directory in (arm, legs, boat, root / ".git", root / "scratch"):
        directory.mkdir(parents=True)
    shutil.copy(FIXTURES / "mcap" / "robot.mcap", arm / "episode-0007.mcap")
    shutil.copy(FIXTURES / "text" / "operator_log", arm / "operator_log")
    shutil.copy(FIXTURES / "text" / "notes.txt", legs / "notes.txt")
    shutil.copy(FIXTURES / "mcap" / "truncated.mcap", legs / "gait-trial.mcap")
    shutil.copy(FIXTURES / "text" / "corrupted.txt", boat / "site-notes.txt")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (boat / "._site-notes.txt").write_bytes(b"\x00\x05\x16\x07")
    (root / "scratch" / "half-copied.mcap").write_bytes(b"\x89MCAP0\r\n")
    (legs / "export.tmp").write_bytes(b"partial")
    (root / ".neptune-ignore").write_bytes(b"# the copy tool's leftovers\nscratch/\n*.tmp\n")
    (root / "latest").symlink_to(arm)
    return root


def neptune(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(NEPTUNE), *argv], cwd=cwd, capture_output=True, text=True, timeout=300, check=False
    )


def result_of(stdout: str) -> dict[str, Any]:
    *events, last = (json.loads(line) for line in stdout.splitlines())
    assert last["type"] == "result" and all(e["type"] == "event" for e in events)
    result: dict[str, Any] = last
    return result


def test_one_command_turns_a_messy_fleet_folder_into_a_package(fleet: Path, tmp_path: Path) -> None:
    env_free = tmp_path  # nothing configured: the workspace is a flag, the rest are defaults
    done = neptune("ingest", fleet.name, "--out", "pkg", "-w", "ws", "--json", cwd=env_free)
    assert done.returncode == 0, done.stderr
    result = result_of(done.stdout)
    assert result["state"] == "committed"
    # Five files of evidence plus the .neptune-ignore itself (evidence like any other file).
    assert result["sources"] == 6
    codes = result["findings"]["by_code"]
    # .git/, .DS_Store, ._site-notes.txt, scratch/, export.tmp: each left out, each declared.
    assert codes["neptune.discovery.ignored"] == 5
    assert any(code.startswith("mcap.") for code in codes)  # the truncated recording
    assert any(code.startswith("text.") for code in codes)  # the damaged notes
    receipt = tmp_path / result["receipt_path"]
    assert receipt.is_file()
    package = read_package(tmp_path / "pkg")  # whole and verified
    assert str(package.receipt.id) == result["receipt"]

    human = neptune("ingest", fleet.name, "--out", "pkg2", "-w", "ws", cwd=tmp_path)
    assert human.returncode == 0 and human.stderr == ""
    assert human.stdout.splitlines()[0] == "committed pkg2"
    assert human.stdout.splitlines()[2] == f"  receipt  {Path('pkg2', 'receipt.json')}"


def test_a_dry_run_says_what_would_happen_and_writes_nothing(fleet: Path, tmp_path: Path) -> None:
    done = neptune("ingest", fleet.name, "--dry-run", "-w", "ws", cwd=tmp_path)
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.startswith(f"planned {fleet.name}: 6 sources to ingest; nothing written")
    assert sorted(p.name for p in tmp_path.iterdir()) == [fleet.name, "ws"]


def test_the_module_runs_as_the_command_does(fleet: Path, tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "neptune.cli", "ingest", "missing", "--out", "p"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 3
    assert done.stderr.startswith("neptune: invalid_source: ")


def test_ctrl_c_stops_at_a_checkpoint_and_resume_finishes_the_same_package(
    fleet: Path, tmp_path: Path
) -> None:
    for n in range(60):  # long enough that the interrupt lands mid-job
        text = "".join(f"leg {n % 4}, trial {k}: slip nominal\n\n" for k in range(3))
        (fleet / "quadruped-b2" / f"trial-{n:02d}.txt").write_text(text, encoding="utf-8")
    job = subprocess.Popen(
        [str(NEPTUNE), "ingest", fleet.name, "--out", "pkg", "-w", "ws", "-v"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert job.stderr is not None
    for line in job.stderr:  # interrupt once parsing has begun
        if line.startswith("neptune: parse: "):
            job.send_signal(signal.SIGINT)
            break
    _, rest = job.communicate(timeout=300)
    assert job.returncode in (0, 130), rest
    if job.returncode == 130:
        assert "--resume" in rest and not (tmp_path / "pkg").exists()
        resumed = neptune(
            "ingest", fleet.name, "--out", "pkg", "-w", "ws", "--resume", "--json", cwd=tmp_path
        )
        assert resumed.returncode == 0, resumed.stderr
        package = result_of(resumed.stdout)["package"]
    else:  # the job beat the signal past its last checkpoint: it published, as it must
        package = read_package(tmp_path / "pkg").id
    fresh = neptune("ingest", fleet.name, "--out", "fresh", "-w", "ws2", "--json", cwd=tmp_path)
    assert str(package) == result_of(fresh.stdout)["package"]


def test_the_console_script_is_installed() -> None:
    assert NEPTUNE.is_file() and os.access(NEPTUNE, os.X_OK)
