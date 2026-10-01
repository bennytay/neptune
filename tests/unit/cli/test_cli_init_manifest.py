"""``neptune init-manifest`` and the ingest command's manifest flags (ADR 0047 §7)."""

import io
from pathlib import Path

from neptune.cli import exit_codes
from neptune.cli.main import run
from neptune.manifest import parse_bytes


def call(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = run(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def folder(tmp_path: Path) -> Path:
    root = tmp_path / "boat"
    (root / "survey_007").mkdir(parents=True, exist_ok=True)
    (root / "survey_007" / "log.txt").write_text("sonar sweep, harbour entrance\n")
    return root


def init(tmp_path: Path, *extra: str) -> tuple[int, str, str]:
    root = folder(tmp_path)
    return call(
        "init-manifest", str(root), "-w", str(tmp_path / "ws"), "--isolation", "in_process", *extra
    )


def test_it_writes_a_valid_manifest_that_declares_nothing(tmp_path: Path) -> None:
    code, out, _ = init(tmp_path)
    target = tmp_path / "boat" / "neptune.yaml"
    assert code == exit_codes.OK and out == f"wrote {target}\n"
    assert parse_bytes(target.read_bytes(), "neptune.yaml").to_json() == {"neptune": 1}
    assert not list((tmp_path / "boat").glob(".*tmp"))


def test_it_never_overwrites_without_force(tmp_path: Path) -> None:
    root = folder(tmp_path)
    (root / "neptune.yaml").write_text("neptune: 1  # mine\n")
    code, _, err = init(tmp_path)
    assert code == exit_codes.BY_CODE["destination_exists"] and "--force" in err
    assert (root / "neptune.yaml").read_text() == "neptune: 1  # mine\n"
    code, _, _ = init(tmp_path, "--force")
    assert code == exit_codes.OK and "# mine" not in (root / "neptune.yaml").read_text()


def test_stdout_and_determinism(tmp_path: Path) -> None:
    first = init(tmp_path, "-o", "-")
    second = init(tmp_path, "-o", "-")
    assert first[0] == exit_codes.OK and first[1] == second[1] and "neptune: 1" in first[1]
    assert not (tmp_path / "boat" / "neptune.yaml").exists()


def test_a_folder_is_required(tmp_path: Path) -> None:
    code, _, err = call("init-manifest", str(tmp_path / "missing"))
    assert code == exit_codes.BY_CODE["invalid_source"] and "not a folder" in err


def test_ingest_manifest_flags(tmp_path: Path) -> None:
    root = folder(tmp_path)
    code, _, err = call("ingest", str(root), "--dry-run", "--manifest", "x", "--no-manifest")
    assert code == exit_codes.USAGE and "contradict" in err
    (root / "neptune.yaml").write_text("neptune: 1\nfoo: 1\n")
    common = ("--dry-run", "-w", str(tmp_path / "ws"), "--isolation", "in_process")
    code, _, err = call("ingest", str(root), *common)
    assert code == exit_codes.BY_CODE["invalid_configuration"] and "unknown keys" in err
    assert call("ingest", str(root), *common, "--no-manifest")[0] == exit_codes.OK
    (tmp_path / "outside.yaml").write_text("neptune: 1\n")
    code, _, err = call("ingest", str(root), *common, "--manifest", str(tmp_path / "outside.yaml"))
    assert code == exit_codes.BY_CODE["invalid_configuration"] and "outside the folder" in err
