"""The calibration adapter's golden package: hand-eye results and a timed OpenCV calibration.

Its records are compatibility-sensitive output (ADR 0003): any change is a new adapter version and
an explained golden diff. On failure run ``make examples`` (or
``uv run python tests/golden/calibration/make_calibration_golden.py``).
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.model.frames import Pose
from neptune.model.knowledge import Known
from neptune.model.machine import Calibration
from neptune.model.reference import FrameTransform, TimestampDomain
from neptune.model.schema import canonical_schema
from neptune.store.package import MANIFEST, read_files

pytestmark = pytest.mark.integration

GOLDEN: Final = Path(__file__).parents[1] / "golden" / "calibration"


def _load() -> ModuleType:
    path = GOLDEN / "make_calibration_golden.py"
    spec = importlib.util.spec_from_file_location("make_calibration_golden", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_calibration_golden"] = module
    spec.loader.exec_module(module)
    return module


MAKER: Final = _load()


def committed() -> dict[str, bytes]:
    return {
        path.relative_to(GOLDEN).as_posix(): path.read_bytes()
        for path in sorted(GOLDEN.rglob("*"))
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def test_the_golden_package_is_what_ingesting_the_fixtures_gives() -> None:
    # On failure: run `make examples`, bump the calibration adapter's version if its output
    # changed, and explain the diff in the PR.
    assert committed() == MAKER.build()


def test_the_golden_package_reads_back_and_every_record_validates() -> None:
    prefix = f"{MAKER.NAME}/"
    files = {path.removeprefix(prefix): data for path, data in committed().items()}
    manifest = canonical_json.loads(files[MANIFEST])
    assert isinstance(manifest, dict)
    for kind in manifest["tables"]:
        files.setdefault(f"records/{kind}.jsonl", b"")  # empty tables are not kept as files
    package = read_files(files)
    calibrations = [r for r in package.records if isinstance(r, Calibration)]
    transforms = [r for r in package.records if isinstance(r, FrameTransform)]
    domains = [r for r in package.records if isinstance(r, TimestampDomain)]
    assert len(calibrations) == 4 and len(transforms) == 3 and len(domains) == 1
    assert all(isinstance(t.value, Pose) for t in transforms)
    subjects = sorted(c.subject.value for c in calibrations if isinstance(c.subject, Known))
    assert subjects == [
        "WCAM-2B",
        "base_camera_link",
        "camera_color_optical_frame",
        "wrist_camera_color_optical_frame",
    ]
    validator = Draft202012Validator(canonical_schema())
    checked: list[Calibration | FrameTransform | TimestampDomain] = [
        *calibrations,
        *transforms,
        *domains,
    ]
    for record in checked:
        line = canonical_json.loads(canonical_json.dumps(record.to_json()))
        assert list(validator.iter_errors(line)) == []
    receipt = files["receipt.md"].decode()
    assert "calibration 0.2.0" in receipt and "cell2/vision/wrist_camera_handeye.yml" in receipt
