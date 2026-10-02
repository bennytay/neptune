"""Keep what the tabular adapter emits for the fixtures as golden files (compatibility-sensitive).

Run ``uv run python tests/golden/tabular/make_tabular_golden.py`` after a deliberate change to the
adapter's output, and explain the changed files in the PR. ``test_tabular_golden.py`` checks the
committed files are exactly what ingesting the committed fixtures gives: one canonical JSON
Lines file per fixture (``.golden``), holding the transform, every record and every finding,
in package order.
"""

from pathlib import Path
from typing import Any, Final

from neptune.adapters.harness import ingest_source
from neptune.adapters.tabular import TabularAdapter
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json

HERE: Final = Path(__file__).parent
FIXTURES: Final = HERE.parents[1] / "fixtures" / "tabular"

# fixture -> the config its golden file is made with
CASES: Final[dict[str, dict[str, Any]]] = {
    "telemetry_amr.csv": {"csv_header": "first_row"},
    "inspection_quadruped.tsv": {"csv_header": "first_row"},
    "events_auv.jsonl": {},
    "joint_states_arm.json": {},
    "humanoid_joints.parquet": {},
    "ragged.csv": {"csv_delimiter": ",", "csv_header": "first_row"},
    "damaged.jsonl": {},
    "truncated.parquet": {},
    "workorders_amr_fleet.xlsx": {"csv_header": "first_row"},
    "changelog_manipulator_cell.xlsx": {"csv_header": "first_row"},
    "epoch1904_quadruped.xlsx": {"csv_header": "first_row"},
    "formulas_humanoid_energy.xlsx": {"csv_header": "first_row"},
    "damaged_sheet_xml.xlsx": {"csv_header": "first_row"},
    "truncated_workorders.xlsx": {},
}


def build() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name, config in CASES.items():
        reader = BytesReader((FIXTURES / name).read_bytes())
        output = ingest_source(TabularAdapter(), reader, config)
        lines = [canonical_json.dumps(r.to_json()) for r in output.package_records()]
        files[f"{name}.golden"] = b"\n".join(lines) + b"\n"
    return files


if __name__ == "__main__":
    for relative, data in build().items():
        (HERE / relative).write_bytes(data)
