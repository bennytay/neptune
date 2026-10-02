"""The tabular fixtures are what their generator builds, and the registry sends each to the
tabular adapter by its bytes alone (a renamed copy is read the same).
"""

from pathlib import Path
from types import ModuleType
from typing import Final

import pyarrow.parquet as pq
import pytest

from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.registry import SelectionStatus

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "tabular"
WORKBOOKS: Final = (".xlsx", ".xlsm")
PARQUET: Final = ("humanoid_joints.parquet", "truncated.parquet", "bad_tail.parquet")
SENT_TO_TABULAR: Final = (
    "telemetry_amr.csv",
    "inspection_quadruped.tsv",
    "events_auv.jsonl",
    "joint_states_arm.json",
    "unclosed_quote.csv",
    "damaged.jsonl",
    "truncated.json",
    *PARQUET,
)


def test_text_fixtures_are_exactly_what_the_generator_builds(tabular_fixtures: ModuleType) -> None:
    built = tabular_fixtures.build()
    # The workbooks have a generator of their own (make_xlsx_fixtures.py).
    on_disk = {p.name for p in FIXTURES.iterdir() if p.suffix not in (".py", ".md", *WORKBOOKS)}
    assert set(built) == on_disk - {"__pycache__"}
    for name, data in built.items():
        if name not in PARQUET:
            assert (FIXTURES / name).read_bytes() == data, name


def test_parquet_fixtures_are_what_the_generator_builds_by_content(
    tabular_fixtures: ModuleType,
) -> None:
    # pyarrow writes its own version into the footer, so bytes are compared through the reader
    built = tabular_fixtures.build()
    committed = (FIXTURES / "humanoid_joints.parquet").read_bytes()
    import io

    assert pq.read_table(io.BytesIO(built["humanoid_joints.parquet"])).equals(
        pq.read_table(io.BytesIO(committed))
    )
    assert (FIXTURES / "truncated.parquet").read_bytes() == committed[: len(committed) * 6 // 10]
    assert (FIXTURES / "bad_tail.parquet").read_bytes() == committed[:-4] + b"PAR2"


def test_every_fixture_is_small() -> None:
    assert all(
        p.stat().st_size < 64 * 1024 for p in FIXTURES.iterdir() if p.suffix not in WORKBOOKS
    )


@pytest.mark.parametrize("name", SENT_TO_TABULAR)
def test_the_registry_sends_each_table_to_the_tabular_adapter(name: str) -> None:
    data = (FIXTURES / name).read_bytes()
    for label in (name, "renamed"):  # the name never decides
        selection = default_registry().select(data[:PROBE_HEAD_SIZE], ProbeHints(label, len(data)))
        assert selection.adapter == "tabular", (name, label, selection)
        assert selection.status is SelectionStatus.SELECTED


def test_a_json_object_and_prose_are_left_to_other_adapters() -> None:
    registry = default_registry()
    for data in (b'{"robot":"h1","joints":[1,2]}', b'{"a":1}\n', b"just a note\nwith two lines\n"):
        selection = registry.select(data, ProbeHints("config.json", len(data)))
        assert selection.adapter != "tabular", data


def test_ragged_csv_is_declared_not_sniffed() -> None:
    # its head disagrees on the field count, so no delimiter is sniffed and text reads it
    data = (FIXTURES / "ragged.csv").read_bytes()
    selection = default_registry().select(data, ProbeHints("ragged.csv", len(data)))
    assert selection.adapter == "text"
