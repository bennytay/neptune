"""``neptune ingest`` in-process (ADR 0043): usage, exit codes, output, hostile input, resume,
single-file sources and determinism. Every ingest is a real job over real files."""

import io
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.cli import exit_codes, run
from neptune.sdk import ERRORS, Neptune, PublishIncompleteError

FIXTURES: Final = Path(__file__).parents[2] / "fixtures"
RESULT_KEYS: Final = {
    "cache",
    "destination",
    "error",
    "exit_code",
    "findings",
    "format",
    "package",
    "receipt",
    "receipt_path",
    "records",
    "source",
    "sources",
    "state",
    "type",
}


def cli(*argv: str, cancel: threading.Event | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run(list(argv), stdout=out, stderr=err, cancel=cancel)
    return code, out.getvalue(), err.getvalue()


def result_of(stdout: str) -> dict[str, Any]:
    lines = [json.loads(line) for line in stdout.splitlines()]
    assert lines and lines[-1]["type"] == "result"
    assert all(line["type"] == "event" for line in lines[:-1])
    result: dict[str, Any] = lines[-1]
    assert set(result) == RESULT_KEYS  # the same keys, whatever happened
    return result


@pytest.fixture
def run_folder(tmp_path: Path) -> Path:
    """A small run folder: a recording, two notes, and the debris a laptop copy leaves."""
    root = tmp_path / "run"
    (root / "arm").mkdir(parents=True)
    shutil.copy(FIXTURES / "mcap" / "robot.mcap", root / "arm" / "episode.mcap")
    shutil.copy(FIXTURES / "text" / "notes.txt", root / "notes.txt")
    shutil.copy(FIXTURES / "text" / "corrupted.txt", root / "arm" / "operator.txt")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    return root


@pytest.fixture
def at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from ``tmp_path``, so paths on the command line are relative, as a user types them."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- the exit-code contract --------------------------------------------------------------------


def test_every_sdk_error_code_has_its_fixed_exit_code() -> None:
    # A contract: scripts branch on these. Never renumber; a new code takes the next number.
    assert exit_codes.BY_CODE == {
        "error": 1,
        "invalid_request": 2,
        "invalid_source": 3,
        "invalid_destination": 4,
        "destination_exists": 5,
        "invalid_configuration": 6,
        "nothing_to_resume": 7,
        "unsupported": 8,
        "network_refused": 9,
        "sandbox_unavailable": 10,
        "workspace_unusable": 11,
        "package_invalid": 12,
        "job_failed": 13,
        "publish_incomplete": 14,
    }
    assert {kind.code for kind in ERRORS} == set(exit_codes.BY_CODE)
    assert (exit_codes.OK, exit_codes.CANCELLED) == (0, 130)
    assert exit_codes.for_code("a_code_from_the_future") == exit_codes.INTERNAL


def test_help_documents_every_exit_code_and_needs_no_setup() -> None:
    code, out, _ = cli("ingest", "--help")
    assert code == 0
    for number in (*exit_codes.BY_CODE.values(), exit_codes.OK, exit_codes.CANCELLED):
        assert f"  {number:>3}  " in out
    for flag in ("--out", "--dry-run", "--resume", "--json", "--ignore", "--workspace"):
        assert flag in out
    assert cli("--version")[0] == 0


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("ingest",),
        ("ingest", "run"),  # no --out and not a dry run
        ("ingest", "run", "--dry-run", "--out", "p"),
        ("ingest", "run", "--out", "p", "--attempts", "0"),
        ("ingest", "run", "--out", "p", "--attempts", "two"),
        ("ingest", "run", "--out", "p", "--isolation", "none"),
        ("ingest", "run", "--out", "p", "--no-such-flag"),
        ("frobnicate",),
    ],
)
def test_a_wrong_command_line_is_a_usage_error(argv: tuple[str, ...], at: Path) -> None:
    code, out, err = cli(*argv)
    assert code == exit_codes.USAGE
    assert out == "" and err
    assert not (at / "p").exists()


# --- success, quiet by default -----------------------------------------------------------------


def test_a_committed_ingest_is_quiet_and_says_where_the_receipt_is(
    run_folder: Path, at: Path
) -> None:
    code, out, err = cli("ingest", "run", "--out", "pkg", "-w", "ws")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == "committed pkg"
    assert lines[2] == f"  receipt  {Path('pkg', 'receipt.json')}"
    assert "3 ingested" in lines[3]
    assert (at / "pkg" / "receipt.json").is_file()


def test_verbose_adds_one_progress_line_per_event_on_stderr(run_folder: Path, at: Path) -> None:
    events: list[Any] = []
    Neptune(at / "ws0").dry_run(run_folder, on_event=events.append)
    code, out, err = cli("ingest", "run", "--dry-run", "-w", "ws", "-v")
    assert code == 0
    assert len(err.splitlines()) == len(events)
    assert all(line.startswith("neptune: ") for line in err.splitlines())
    assert out.startswith("planned run: 3 sources to ingest; nothing written")


def test_json_is_events_then_one_result_line(run_folder: Path, at: Path) -> None:
    code, out, err = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json")
    assert code == 0 and err == ""
    result = result_of(out)
    assert result["state"] == "committed" and result["exit_code"] == 0
    assert result["error"] is None and result["format"] == 1
    assert result["destination"] == "pkg" and result["source"] == "run"
    assert result["receipt_path"] == str(Path("pkg", "receipt.json"))
    assert result["package"].startswith("sha256:") and result["receipt"].startswith("rec:")
    assert result["sources"] == 3 and result["records"]
    ignored = result["findings"]["by_code"]["neptune.discovery.ignored"]
    assert ignored == 2  # .git/ and .DS_Store: declared, never silent
    kinds = [json.loads(line)["event"]["kind"] for line in out.splitlines()[:-1]]
    assert "job_committed" in kinds


def test_a_dry_run_writes_no_package_and_reports_the_plan(run_folder: Path, at: Path) -> None:
    code, out, _ = cli("ingest", "run", "--dry-run", "-w", "ws", "--json")
    result = result_of(out)
    assert code == 0 and result["state"] == "planned"
    assert result["package"] is None and result["destination"] is None
    assert result["sources"] == 3 and result["cache"]["plans"] == {"hit": 0, "miss": 3}
    assert sorted(p.name for p in at.iterdir()) == ["run", "ws"]


# --- failures: one exit code each, the same result keys ----------------------------------------


def failed(argv: tuple[str, ...], expected: str) -> dict[str, Any]:
    code, out, _ = cli(*argv, "--json")
    result = result_of(out)
    assert result["state"] == "failed" and result["error"]["code"] == expected
    assert code == result["exit_code"] == exit_codes.BY_CODE[expected]
    human_code, human_out, human_err = cli(*argv)
    assert human_code == code and human_out == ""
    assert human_err.startswith(f"neptune: {expected}: ")
    return result


def test_a_missing_source_is_invalid_source(at: Path) -> None:
    failed(("ingest", "missing", "--out", "pkg", "-w", "ws"), "invalid_source")


def test_a_symlink_loop_as_the_source_is_invalid_source(at: Path) -> None:
    (at / "a").symlink_to(at / "b")
    (at / "b").symlink_to(at / "a")
    failed(("ingest", "a", "--out", "pkg", "-w", "ws"), "invalid_source")


def test_a_fifo_as_the_source_is_invalid_source(at: Path) -> None:
    os.mkfifo(at / "pipe")
    failed(("ingest", "pipe", "--out", "pkg", "-w", "ws"), "invalid_source")


def test_a_destination_that_exists_is_never_overwritten(run_folder: Path, at: Path) -> None:
    (at / "pkg").mkdir()
    (at / "pkg" / "keep").write_text("mine")
    failed(("ingest", "run", "--out", "pkg", "-w", "ws"), "destination_exists")
    assert (at / "pkg" / "keep").read_text() == "mine"


def test_a_destination_inside_the_source_is_refused(run_folder: Path, at: Path) -> None:
    failed(("ingest", "run", "--out", "run/pkg", "-w", "ws"), "invalid_destination")


def test_a_refused_ignore_pattern_is_invalid_configuration(run_folder: Path, at: Path) -> None:
    result = failed(
        ("ingest", "run", "--out", "pkg", "--ignore", "!x", "-w", "ws"), "invalid_configuration"
    )
    assert "negation" in result["error"]["message"]


@pytest.mark.parametrize(
    "options",
    [("--isolation", "in_process", "--allow-degraded-sandbox"), ("--job", "")],
    ids=["degraded-without-sandbox", "empty-job"],
)
def test_contradictory_options_are_invalid_configuration(
    options: tuple[str, ...], run_folder: Path, at: Path
) -> None:
    failed(("ingest", "run", "--out", "pkg", "-w", "ws", *options), "invalid_configuration")


@pytest.mark.parametrize(
    "hostile",
    [
        lambda root: (root / ".neptune-ignore").write_bytes(b"*.tmp\n!*.mcap\n"),
        lambda root: (root / ".neptune-ignore").write_bytes(b"../../../etc/*\n"),
        lambda root: (root / ".neptune-ignore").write_bytes(b"#" * (64 * 1024 + 1)),
        lambda root: (root / ".neptune-ignore").write_bytes(b"ok\n\x00\n"),
        lambda root: (root / ".neptune-ignore").symlink_to("/etc/passwd"),
        lambda root: (root / ".neptune-ignore").symlink_to(root / ".neptune-ignore"),
        lambda root: (root / ".neptune-ignore").mkdir(),
    ],
    ids=["negation", "escape", "oversized", "nul", "symlink-out", "symlink-loop", "directory"],
)
def test_a_hostile_neptune_ignore_fails_whole_before_any_work(
    hostile: Any, run_folder: Path, at: Path
) -> None:
    hostile(run_folder)
    failed(("ingest", "run", "--out", "pkg", "-w", "ws"), "invalid_configuration")
    assert not (at / "pkg").exists()


def test_no_ignore_file_skips_a_hostile_one(run_folder: Path, at: Path) -> None:
    (run_folder / ".neptune-ignore").write_bytes(b"!everything\n")
    code, _, _ = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--no-ignore-file")
    assert code == 0


def test_resume_with_no_earlier_work_is_nothing_to_resume(run_folder: Path, at: Path) -> None:
    failed(("ingest", "run", "--out", "pkg", "-w", "ws", "--resume"), "nothing_to_resume")


def test_a_remote_source_no_connector_reads_is_a_configuration_error(at: Path) -> None:
    argv = ("ingest", "s3://bucket/runs/7", "--out", "pkg", "-w", "ws", "--no-plugins")
    failed(argv, "invalid_configuration")


def test_an_unreadable_root_fails_the_job(run_folder: Path, at: Path) -> None:
    run_folder.chmod(0)
    try:
        if os.access(run_folder, os.R_OK):
            pytest.skip("running with privileges that read any directory")
        failed(("ingest", "run", "--out", "pkg", "-w", "ws"), "job_failed")
    finally:
        run_folder.chmod(0o755)


def test_an_unreadable_file_is_a_finding_not_a_failure(run_folder: Path, at: Path) -> None:
    secret = run_folder / "arm" / "locked.txt"
    secret.write_text("nobody reads this")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("running with privileges that read any file")
        code, out, _ = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json")
        result = result_of(out)
        assert code == 0 and result["state"] == "committed"
        assert result["findings"]["by_severity"].get("error", 0) >= 1
    finally:
        secret.chmod(0o600)


def test_a_publish_that_was_not_flushed_names_the_package(
    run_folder: Path, at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable without a failing disk: the SDK's error is stood in for, the mapping is real."""

    def unflushed(*_: object, **__: object) -> None:
        raise PublishIncompleteError("renamed, not flushed", Path("pkg"))

    monkeypatch.setattr(Neptune, "ingest", unflushed)
    result = failed(("ingest", "run", "--out", "pkg", "-w", "ws"), "publish_incomplete")
    assert result["destination"] == "pkg"


def test_a_bug_is_internal_with_a_traceback(
    run_folder: Path, at: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(Neptune, "ingest", broken)
    code, out, err = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json")
    result = result_of(out)
    assert code == exit_codes.INTERNAL and result["error"]["code"] == "internal"
    assert "Traceback" in err and "boom" in err


# --- resume, single files, determinism ----------------------------------------------------------


def test_a_cancelled_ingest_resumes_into_the_package_a_fresh_one_writes(
    run_folder: Path, at: Path
) -> None:
    cancel = threading.Event()
    cancel.set()  # Ctrl-C before the first checkpoint
    code, out, err = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json", cancel=cancel)
    assert code == exit_codes.CANCELLED and result_of(out)["state"] == "cancelled"
    assert not (at / "pkg").exists()
    code, out, _ = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json", "--resume")
    resumed = result_of(out)
    assert code == 0 and resumed["state"] == "committed"
    _, out, _ = cli("ingest", "run", "--out", "fresh", "-w", "ws2", "--json")
    assert resumed["package"] == result_of(out)["package"]
    _, _, err = cli("ingest", "run", "--out", "pkg3", "-w", "ws3", cancel=cancel)
    assert "--resume" in err


def test_resume_continues_a_dry_run_and_plans_nothing_again(run_folder: Path, at: Path) -> None:
    assert cli("ingest", "run", "--dry-run", "-w", "ws")[0] == 0
    code, out, _ = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json", "--resume")
    result = result_of(out)
    assert code == 0 and result["cache"]["plans"] == {"hit": 3, "miss": 0}


def test_a_single_file_ingests_as_a_folder_holding_only_it(run_folder: Path, at: Path) -> None:
    (at / "alone").mkdir()
    shutil.copy(run_folder / "arm" / "episode.mcap", at / "alone" / "episode.mcap")
    _, out, _ = cli("ingest", "run/arm/episode.mcap", "--out", "one", "-w", "ws", "--json")
    single = result_of(out)
    _, out, _ = cli("ingest", "alone", "--out", "folder", "-w", "ws2", "--json")
    assert single["state"] == "committed" and single["sources"] == 1
    assert single["package"] == result_of(out)["package"]


def test_a_file_uri_names_a_local_source(run_folder: Path, at: Path) -> None:
    code, out, _ = cli("ingest", run_folder.as_uri(), "--dry-run", "-w", "ws", "--json")
    assert code == 0 and result_of(out)["sources"] == 3


def package_bytes(package: Path) -> dict[str, bytes]:
    """Every file of a package but ``volatile/`` (its envelope: clock, host, job; ADR 0022)."""
    return {
        str(path.relative_to(package)): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.relative_to(package).parts[0] != "volatile"
    }


def test_the_same_input_gives_byte_identical_packages_and_json(
    run_folder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs, packages = [], []
    for attempt in ("first", "second"):
        here = tmp_path / attempt
        shutil.copytree(run_folder, here / "run", symlinks=True)
        monkeypatch.chdir(here)
        code, out, err = cli("ingest", "run", "--out", "pkg", "-w", "ws", "--json")
        assert code == 0 and err == ""
        outputs.append(out)
        packages.append(package_bytes(here / "pkg"))
    assert outputs[0] == outputs[1]
    assert packages[0] == packages[1]
