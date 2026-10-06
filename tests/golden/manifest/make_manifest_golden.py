"""Ingest three small folders under a manifest and keep their packages as golden files (ADR 0072).

Three embodiments, each a real folder with a ``neptune.yaml`` that declares what no file says:

- ``manipulator_cell``: an arm's recording and the cell's controller configuration two
  directories away, outside the recording's unit, pinned by path; the machine has a serial alias,
  the run a site and a task.
- ``aerial_survey``: a PX4 flight log (the drone worked example's ULog) whose own ``sys_uuid`` the
  manifest gives the drone as an alias, and its parameter file pinned by content id.
- ``amr_fleet``: two mobile bases in one session directory, each run declared with its own
  machine, and the fleet configuration both share (no one's own by nearness, ADR 0064 §4)
  pinned for both.

Run ``make examples`` (or ``uv run python tests/golden/manifest/make_manifest_golden.py``) after
a change to what the manifest or an adapter these folders use writes, and explain the diff in the
PR (ADR 0003). Each package's documents and every non-empty record table are kept;
``tests/integration/test_manifest_golden.py`` checks they are exactly what ingesting gives.
"""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Final

HERE: Final = Path(__file__).parent
TESTS: Final = HERE.parents[1]
MODEL: Final = TESTS / "fixtures" / "model"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


FORMATS: Final = _load("manifest_golden_formats", MODEL / "formats.py")
ULOG: Final = (MODEL / "drone" / "sources" / "flight.ulg").read_bytes()
PARAMS: Final = b"MPC_XY_VEL_MAX: 12.0\nMIS_TAKEOFF_ALT: 2.5\nCOM_RC_LOSS_T: 0.5\n"
SYS_UUID: Final = "000200000000343233345117003a0027"  # what flight.ulg's own header states


def recording(topic: str, start_ns: int) -> bytes:
    """A one-message ROS 2 MCAP on ``topic`` at ``start_ns``."""
    schema = FORMATS.McapSchema(1, "std_msgs/msg/String", "ros2msg", b"string data")
    channel = FORMATS.McapChannel(1, 1, topic, "cdr")
    payload = FORMATS.Cdr().string(topic).bytes()
    message = FORMATS.McapMessage(1, 0, start_ns, start_ns, payload)
    data, _ = FORMATS.mcap("ros2", (schema,), (channel,), (message,))
    return bytes(data)


def content_id(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


FOLDERS: Final[dict[str, dict[str, bytes]]] = {
    "manipulator_cell": {
        "neptune.yaml": b"""\
neptune: 1
machines:
  - {id: ur5e-cell-3, embodiment: manipulator, aliases: {serial: "20235400123"}}
sites:
  - {id: plant-2, name: "Riverside plant 2"}
tasks:
  - {id: bin-pick, description: "Pick parts from the inbound bin"}
runs:
  - name: cell3-pick-0412
    paths: [cell3/runs/pick_0412]
    machine: ur5e-cell-3
    site: plant-2
    task: bin-pick
    snapshots:
      - {path: cell3/config/controller.yaml}
""",
        "cell3/runs/pick_0412/arm.mcap": recording("/arm/joint_states", 2_000_000_000),
        "cell3/config/controller.yaml": b"controller:\n  rate_hz: 500\n  payload_kg: 2.5\n",
    },
    "aerial_survey": {
        "neptune.yaml": b"""\
neptune: 1
machines:
  - id: survey-quad-7
    embodiment: aerial
    aliases: {px4.sys_uuid: "%s"}
sites:
  - {id: north-field}
runs:
  - name: survey-flight-1
    paths: [flights/flight.ulg]
    machine: survey-quad-7
    site: north-field
    snapshots:
      - {content: "%s"}
"""
        % (SYS_UUID.encode(), content_id(PARAMS).encode()),
        "flights/flight.ulg": ULOG,
        "params/survey_quad_7.yaml": PARAMS,
    },
    "amr_fleet": {
        "neptune.yaml": b"""\
neptune: 1
machines:
  - {id: AMR-01, embodiment: mobile_base}
  - {id: AMR-02, embodiment: mobile_base}
sites:
  - {id: DC-7, name: "Northgate distribution centre"}
runs:
  - name: amr01-shift-1
    paths: [fleet/run_001/amr_01]
    machine: AMR-01
    site: DC-7
    snapshots: [{path: fleet/run_001/fleet.yaml}]
  - name: amr02-shift-1
    paths: [fleet/run_001/amr_02]
    machine: AMR-02
    site: DC-7
    snapshots: [{path: fleet/run_001/fleet.yaml}]
""",
        "fleet/run_001/amr_01/drive.mcap": recording("/amr_01/odom", 1_000_000_000),
        "fleet/run_001/amr_02/drive.mcap": recording("/amr_02/odom", 1_000_000_000),
        "fleet/run_001/fleet.yaml": b"fleet:\n  robots: 2\n  max_speed: 1.2\n",
    },
}
NAMES: Final = tuple(FOLDERS)


def materialise(name: str, root: Path) -> Path:
    """Write the folder ``name`` under ``root``; returns its directory."""
    for relative, data in FOLDERS[name].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def ingest(source: Path, destination: Path, workspace: Path) -> None:
    from neptune.sdk import Neptune

    # No installed plugin: a workspace plugin must not change the compiler's goldens (ADR 0058).
    result = Neptune(workspace, plugins=False).ingest(source, destination)
    if not result.committed:
        raise RuntimeError(f"{source} was not ingested: {result}")


def package(name: str) -> dict[str, bytes]:
    """One folder's golden files, by path relative to its package directory."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = materialise(name, work / "source")
        out = work / "package"
        ingest(source, out, work / "workspace")
        files = {"manifest.json": (out / "manifest.json").read_bytes()}
        files["receipt.json"] = (out / "receipt.json").read_bytes()
        for table in sorted((out / "records").glob("*.jsonl")):
            data = table.read_bytes()
            if data:
                files[f"records/{table.name}"] = data
        shutil.rmtree(out)
        return files


def build() -> dict[str, bytes]:
    """Every golden file, by path relative to this directory."""
    return {f"{name}/{path}": content for name in NAMES for path, content in package(name).items()}


if __name__ == "__main__":
    for name in NAMES:
        shutil.rmtree(HERE / name, ignore_errors=True)
    for relative, content in build().items():
        path = HERE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
