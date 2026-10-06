"""MVL-41 end to end: the job's ``validate`` phase adds the integrity rules' findings to the package
(ADR 0054). A truncated recording beside a whole one gets the roll-up in its receipt, ranked and
cited; the package verifies; a second job writes it byte for byte; a clean recording's package
is exactly what it was without the engine.
"""

import shutil
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.model.finding import Severity
from neptune.model.package import SEVERITY_ORDER
from neptune.runtime import IngestJob, Isolation, JobEvent, JobOptions, JobState
from neptune.store.package import package_contents, read_package, write_package
from neptune.store.workspace import Workspace
from neptune.validate import VALIDATOR_ID, validate_package

pytestmark = pytest.mark.integration

FIXTURES: Final = Path(__file__).parents[1] / "fixtures" / "mcap"
TABULAR: Final = Path(__file__).parents[1] / "fixtures" / "tabular"
CONFIG: Final = Path(__file__).parents[1] / "fixtures" / "config"
OPTIONS: Final = JobOptions(isolation=Isolation.IN_PROCESS)


def ingest(work: Path, *names: str) -> tuple[Path, list[JobEvent]]:
    root = work / "root"
    root.mkdir(parents=True)
    for name in names:
        shutil.copyfile(FIXTURES / name, root / name)
    events: list[JobEvent] = []
    job = IngestJob(
        root,
        work / "package",
        Workspace(work / "home"),
        default_registry(),
        OPTIONS,
        on_event=events.append,
    )
    assert job.run().state is JobState.COMMITTED
    return work / "package", events


def files(package: Path) -> dict[str, bytes]:
    return {
        path.relative_to(package).as_posix(): path.read_bytes()
        for path in sorted(package.rglob("*"))
        if path.is_file() and "volatile" not in path.parts
    }


def test_a_truncated_recording_is_rolled_up_in_the_receipt(tmp_path: Path) -> None:
    package_dir, events = ingest(tmp_path / "a", "robot.mcap", "truncated.mcap")
    package = read_package(package_dir)
    ours = [f for f in package.records if f.kind == "ingest_finding" and "validate" in f.code]
    assert ours, "the engine found nothing in a truncated recording"
    codes = {f.code for f in ours}
    assert f"{VALIDATOR_ID}.source_incomplete" in codes
    (validator,) = [
        t for t in package.records if t.kind == "transform_record" and t.adapter_id == VALIDATOR_ID
    ]
    assert all(f.transform == validator.id for f in ours)
    receipt_codes = [f.code for f in package.receipt.findings]
    assert set(codes) <= set(receipt_codes)
    ranks = [SEVERITY_ORDER.index(f.severity) for f in package.receipt.findings]
    assert ranks == sorted(ranks)
    # robot.mcap writes /imu out of log_time order on purpose; its clock declares no order.
    assert {(f.code, f.severity) for f in ours} >= {
        (f"{VALIDATOR_ID}.source_incomplete", Severity.WARNING),
        (f"{VALIDATOR_ID}.time_out_of_order", Severity.INFO),
    }
    (verified,) = [e for e in events if e.kind == "package_verified"]
    validation = verified.details["validation"]
    assert isinstance(validation, dict) and validation["findings"] == len(ours)
    assert "neptune.validate.declared_limit_exceeded" in validation["not_covered"]
    # Validating the published package again finds exactly what is already in it.
    assert {f.id for f in validate_package(package).findings} == {f.id for f in ours}
    again, _ = ingest(tmp_path / "b", "robot.mcap", "truncated.mcap")
    assert files(again) == files(package_dir)


def test_a_clean_table_gains_nothing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copyfile(TABULAR / "telemetry_amr.csv", root / "telemetry_amr.csv")
    events: list[JobEvent] = []
    job = IngestJob(
        root,
        tmp_path / "package",
        Workspace(tmp_path / "home"),
        default_registry(),
        OPTIONS,
        on_event=events.append,
    )
    assert job.run().state is JobState.COMMITTED
    package = read_package(tmp_path / "package")
    assert not any(
        t.adapter_id == VALIDATOR_ID for t in package.records if t.kind == "transform_record"
    )
    (verified,) = [e for e in events if e.kind == "package_verified"]
    assert verified.details["package"] == package.id
    validation = verified.details["validation"]
    assert isinstance(validation, dict) and validation["findings"] == 0


def test_a_configuration_snapshot_that_lost_values(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    shutil.copyfile(CONFIG / "nav2_params.yaml", root / "nav2_params.yaml")
    job = IngestJob(
        root, tmp_path / "package", Workspace(tmp_path / "home"), default_registry(), OPTIONS
    )
    assert job.run().state is JobState.COMMITTED
    package = read_package(tmp_path / "package")
    assert validate_package(package).findings == ()  # whole: nothing to say
    values = sorted(
        (r for r in package.records if r.kind == "configuration_value"), key=lambda r: r.id
    )
    kept = [r for r in package.records if r != values[-1]]  # records are read afresh: not "is"
    store = dict(package.manifest.store)
    contents = package_contents(kept, series=package.series, store=store, derived=package.derived)
    write_package(tmp_path / "cut", contents)
    (finding,) = validate_package(read_package(tmp_path / "cut")).findings
    assert finding.code == f"{VALIDATOR_ID}.snapshot_incomplete"
    assert (finding.details["missing"], finding.details["stored"]) == (1, len(values) - 1)
