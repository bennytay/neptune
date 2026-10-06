"""The calibration adapter's golden package: hand-eye results and a timed OpenCV calibration.

``uv run python tests/golden/calibration/make_calibration_golden.py`` (or ``make examples``)
rewrites ``handeye/`` from four fixtures: an arm's wrist camera from easy_handeye and from MoveIt
Calibration, a mobile manipulator's base camera from easy_handeye2, and an arm's OpenCV export that
states its camera and time (ADR 0073). ``tests/integration/test_calibration_golden.py`` checks the
committed files are exactly what the adapter gives today. A changed file here is a changed output of
the adapter: explain it in the PR and bump the adapter's version (ADR 0003).
"""

import io
from pathlib import Path
from typing import Any, Final

from neptune.adapters.calibration import CalibrationAdapter
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import LocalPath
from neptune.store.package import package_files

HERE: Final = Path(__file__).parent
FIXTURES: Final = HERE.parents[1] / "fixtures" / "calibration"
NAME: Final = "handeye"
# Where each fixture sits in the site's tree, as a deployment would keep them.
SOURCES: Final = (
    ("arm_wrist_easy_handeye.yaml", "cell2/calibration/arm_wrist_easy_handeye.yaml"),
    ("arm_wrist_moveit_camera_pose.launch", "cell2/calibration/arm_wrist_camera_pose.launch"),
    ("arm_wrist_opencv_handeye.yml", "cell2/vision/wrist_camera_handeye.yml"),
    ("mobile_manipulator_easy_handeye2.calib", "amr7/calibration/amr_arm_base_camera_eob.calib"),
)


def build() -> dict[str, bytes]:
    """The package's files, by path relative to this directory: documents and non-empty tables."""
    ledger = SourceLedger()
    transforms: dict[str, Any] = {}
    records: list[Any] = []
    for fixture, path in SOURCES:
        data = (FIXTURES / fixture).read_bytes()
        artifact = digest_stream(io.BytesIO(data))
        ledger.observe(LocalPath(path), artifact)
        output = ingest_source(CalibrationAdapter(), BytesReader(data, artifact.content_id))
        transforms[output.config.transform.id] = output.config.transform
        records.extend((*output.records(), *output.findings()))
    package = [*ledger.artifacts(), *ledger.revisions(), *transforms.values(), *records]
    return {
        f"{NAME}/{path}": content for path, content in package_files(package).items() if content
    }


if __name__ == "__main__":
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
