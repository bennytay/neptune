"""The harness end to end: today's stages, determinism, partial success and the failure exits."""

import json
import shutil
from pathlib import Path
from typing import Any, Final

import pytest
from harness import contracts, corpus
from harness.run import main, run

REPO: Final = Path(__file__).resolve().parents[3]
FIXTURES: Final = REPO / "tests" / "fixtures" / "model"


def test_today_the_compiler_is_real_and_the_rest_are_stubs(tmp_path: Path) -> None:
    report, code = run(tmp_path / "run", owner_tests=False)
    assert code == 0 and report["ok"] is True
    modes = {stage["stage"]: (stage["mode"], stage["status"]) for stage in report["stages"]}
    assert modes == {
        "compiler": ("real", "ok"),
        "ledger": ("stub", "ok"),
        "memory": ("stub", "ok"),
        "context": ("stub", "ok"),
    }
    assert [stage["stage"] for stage in report["stages"]] == [
        "compiler",
        "ledger",
        "memory",
        "context",
    ]
    ledger = report["stages"][1]
    assert ledger["output"]["contract"] == "catalog-api"
    assert ledger["output"]["contract_version"] == "1.6.0"
    served = contracts.registry().latest("catalog-api")
    assert served is not None
    assert ledger["output"]["served"] == "goldens"
    assert len(ledger["output"]["goldens"]) == len(served.goldens)
    assert report["smoke"]["ok"] is True
    assert report["smoke"]["packet_source"].startswith("golden query-packet packet.q01-")
    assert report["corpus"] == {"name": "worked-examples", "cases": list(corpus.EXAMPLE_NAMES)}


def test_the_compiler_stage_ingests_validates_and_verifies_every_case(tmp_path: Path) -> None:
    report, _ = run(tmp_path / "run", owner_tests=False)
    cases = report["stages"][0]["output"]["cases"]
    assert [case["case"] for case in cases] == list(corpus.EXAMPLE_NAMES)
    for case in cases:
        assert case["state"] == "committed"
        assert case["manifest_valid"] and case["receipt_valid"] and case["package_verified"]
        assert case["package"].startswith("sha256:")
    # Real ingest, partial success: the drone's ULog is claimed by the flightlog (PX4) adapter,
    # which reports the one sample dropout the log declares. Stream introspection (MVL-21) has no
    # layout reader for its two streams' encoding, so each is not covered. Clock alignment (MVL-36)
    # maps the boot clock onto the GPS time its fix messages carry, and says the latency between
    # each fix and its publication is stated nowhere, so the bound is unknown. Its folder holds no
    # configuration, software, hardware or calibration file, so snapshot binding (MVL-38) says the
    # run has no software identity and leaves the other three kinds unresolved. Ingest is
    # deterministic.
    drone = cases[0]
    assert drone["findings"] == {
        "flightlog.dropout": 1,
        "neptune.bindings.no_software_identity": 1,
        "neptune.bindings.snapshot_unresolved": 3,
        "neptune.clocks.latency_unbounded": 1,
        "neptune.introspection.encoding_not_covered": 2,
    }
    ids = {case["package"] for case in cases}
    assert len(ids) == 4  # four robots, four packages
    # query-packet 1.0.0 is published (MVL-111): the smoke query reads its first golden packet.
    packet = report["smoke"]["packet"]
    assert (packet["kind"], packet["packet_version"]) == ("context_packet", 1)


def test_two_runs_give_byte_identical_reports_with_no_path_in_them(tmp_path: Path) -> None:
    run(tmp_path / "a", owner_tests=False)
    run(tmp_path / "elsewhere" / "b", owner_tests=False)
    first = (tmp_path / "a" / "report.json").read_bytes()
    assert first == (tmp_path / "elsewhere" / "b" / "report.json").read_bytes()
    assert (tmp_path / "a" / "report.md").read_bytes() == (
        tmp_path / "elsewhere" / "b" / "report.md"
    ).read_bytes()
    text = first.decode()
    assert str(tmp_path) not in text and str(REPO) not in text
    assert text.endswith("}\n") and list(json.loads(text)) == sorted(json.loads(text))


def test_a_rerun_clears_its_own_scratch_directory_only(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run(run_dir, owner_tests=False)
    (run_dir / "keep.txt").write_text("not scratch", encoding="utf-8")
    run(run_dir, owner_tests=False)  # ingesting into work/packages again would fail if not cleared
    assert (run_dir / "keep.txt").exists()


def test_a_corrupt_source_is_a_finding_not_a_failed_run(tmp_path: Path) -> None:
    case = tmp_path / "corpus" / "truncated"
    case.mkdir(parents=True)
    whole = (FIXTURES / "manipulator" / "sources" / "session.mcap").read_bytes()
    (case / "session.mcap").write_bytes(whole[:300])
    report, code = run(tmp_path / "run", owner_tests=False, corpus_root=tmp_path / "corpus")
    row = report["stages"][0]["output"]["cases"][0]
    assert code == 0 and row["state"] == "committed"
    # The adapter's finding, validation's roll-up of the source it cut short (ADR 0054), and the
    # run's unbound snapshots (ADR 0064): no software identity, three kinds unresolved.
    assert row["findings"] == {
        "mcap.truncated": 1,
        "neptune.bindings.no_software_identity": 1,
        "neptune.bindings.snapshot_unresolved": 3,
        "neptune.validate.source_incomplete": 1,
    }
    assert report["corpus"]["name"] == "custom"


def test_a_registry_that_breaks_its_own_rule_fails_the_run(tmp_path: Path) -> None:
    broken = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", broken)
    golden = broken / "catalog-api" / "v0.0.0" / "golden" / "manipulator.lineage.json"
    golden.write_text("{}\n", encoding="utf-8")
    report, code = run(tmp_path / "run", contracts_root=broken, owner_tests=False)
    assert code == 1 and report["ok"] is False
    assert report["contracts"]["ok"] is False and report["contracts"]["problems"]
    # Contract failure is reported, and the stages still ran (the report says all of it).
    assert report["stages"][0]["status"] == "ok"
    assert "RED" in (tmp_path / "run" / "report.md").read_text(encoding="utf-8")


def test_the_cli_writes_the_report_and_exits_zero_when_green(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--run-dir", str(tmp_path / "run"), "--no-owner-tests"]) == 0
    out = capsys.readouterr().out
    assert "harness green" in out and "compiler: real ok" in out
    assert (tmp_path / "run" / "report.json").is_file()


def test_the_real_contracts_check_all_passes_with_owner_tests_skipped() -> None:
    code, notes, problems, tail = contracts.run_check_all(None, owner_tests=False)
    assert (code, problems, tail) == (0, [], "")
    assert any(line.startswith("package-schema 1.0.0") for line in notes)
    assert all(not line.startswith((".", "tests/")) for line in notes)


def test_report_json_is_canonical(tmp_path: Path) -> None:
    run(tmp_path / "run", owner_tests=False)
    text = (tmp_path / "run" / "report.json").read_text(encoding="utf-8")
    parsed: Any = json.loads(text)
    assert text == json.dumps(parsed, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
