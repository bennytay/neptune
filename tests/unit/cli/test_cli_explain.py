"""``neptune ingest --explain``: a dry run that prints its explanation (ADR 0044, ADR 0043)."""

import io
import json
import shutil
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.cli import exit_codes, run
from neptune.sdk import Neptune

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"


def cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A run folder from two embodiments, a stray blob and a link, in the working directory."""
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "run"
    (root / "arm" / "episode_001").mkdir(parents=True)
    (root / "amr").mkdir()
    shutil.copy(FIXTURES / "mcap" / "robot.mcap", root / "arm" / "episode_001" / "episode.mcap")
    shutil.copy(FIXTURES / "tabular" / "telemetry_amr.csv", root / "amr" / "telemetry.csv")
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    (root / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    (root / "latest").symlink_to("arm/episode_001")
    return tmp_path


def lines_of(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def test_explain_prints_the_plan_after_the_planned_lines(at: Path) -> None:
    code, out, err = cli("ingest", "run", "--explain", "-w", "ws")
    assert code == exit_codes.OK and err == ""
    assert out.startswith("planned run: 3 sources to ingest; nothing written\n")
    for needle in ("Inventory: 4 files", "episode.mcap", "Sessions (inferred", "Left out:"):
        assert needle in out
    assert "blob.bin  [unsupported]" in out and "latest  [link]" in out
    assert sorted(p.name for p in at.iterdir()) == ["run", "ws"]  # no package anywhere
    assert list((at / "ws" / "chunks").iterdir()) == []


def test_explain_json_is_events_then_the_explanation_then_the_result(at: Path) -> None:
    code, out, _ = cli("ingest", "run", "--explain", "-w", "ws", "--json")
    assert code == exit_codes.OK
    lines = lines_of(out)
    assert [line["type"] for line in lines[-2:]] == ["explanation", "result"]
    assert all(line["type"] == "event" for line in lines[:-2])
    result, explanation = lines[-1], lines[-2]["explanation"]
    assert result["state"] == "planned" and result["exit_code"] == 0
    assert explanation["schema"] == "neptune.explanation/1"
    assert explanation["work"]["sources"] == result["sources"] == 3
    # The line is the explanation's canonical bytes, as the SDK gives them from a fresh workspace.
    expected = Neptune(at / "sdk-ws").dry_run("run").explanation
    assert expected is not None
    raw = out.splitlines()[-2]
    assert raw == '{"explanation":' + expected.dumps().decode() + ',"type":"explanation"}'


def test_explain_json_is_byte_identical_from_fresh_workspaces(at: Path) -> None:
    first = cli("ingest", "run", "--explain", "-w", "a", "--json")[1]
    second = cli("ingest", "run", "--explain", "-w", "b", "--json")[1]
    assert first == second and str(at) not in first


def test_dry_run_alone_prints_no_explanation(at: Path) -> None:
    code, out, _ = cli("ingest", "run", "--dry-run", "-w", "ws", "--json")
    assert code == 0 and all(line["type"] != "explanation" for line in lines_of(out))
    assert "Inventory:" not in cli("ingest", "run", "--dry-run", "-w", "ws2")[1]


def test_explain_with_dry_run_is_the_same_command(at: Path) -> None:
    alone = cli("ingest", "run", "--explain", "-w", "a", "--json")[1]
    both = cli("ingest", "run", "-n", "--explain", "-w", "b", "--json")[1]
    assert alone == both


def test_explain_writes_no_package_so_out_is_a_usage_error(at: Path) -> None:
    code, out, err = cli("ingest", "run", "--explain", "--out", "pkg")
    assert code == exit_codes.USAGE and out == "" and "--explain" in err
    assert not (at / "pkg").exists()


def test_the_ingest_after_explain_resumes_its_work_and_plans_nothing(at: Path) -> None:
    assert cli("ingest", "run", "--explain", "-w", "ws")[0] == 0
    code, out, _ = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--resume", "--json")
    assert code == 0
    result = lines_of(out)[-1]
    assert result["state"] == "committed" and result["cache"]["plans"] == {"hit": 3, "miss": 0}


def test_a_failed_explain_has_its_exit_code_and_no_explanation(at: Path) -> None:
    code, out, _ = cli("ingest", "missing", "--explain", "-w", "ws", "--json")
    assert code == exit_codes.for_code("invalid_source")
    assert [line["type"] for line in lines_of(out)] == ["result"]
