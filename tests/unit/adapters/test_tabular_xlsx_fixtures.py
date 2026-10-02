"""The XLSX fixtures are what their generator builds (by their parts: zlib's deflate bytes may
differ between versions), each is small, and the registry sends each to the tabular adapter."""

import io
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import PROBE_HEAD_SIZE, ProbeHints
from neptune.adapters.registry import SelectionStatus

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "tabular"
WORKBOOKS: Final = sorted(p.name for p in FIXTURES.iterdir() if p.suffix in (".xlsx", ".xlsm"))


def parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def test_every_workbook_fixture_is_listed_by_the_generator(xlsx_fixtures: ModuleType) -> None:
    assert set(WORKBOOKS) == set(xlsx_fixtures.WORKBOOKS) | {"truncated_workorders.xlsx"}


def test_workbook_fixtures_are_what_the_generator_builds_by_content(
    xlsx_fixtures: ModuleType,
) -> None:
    built = xlsx_fixtures.build()
    for name, data in built.items():
        committed = (FIXTURES / name).read_bytes()
        if name == "truncated_workorders.xlsx":
            assert committed == data[: len(data)] and len(data) < 4096
            continue
        assert parts(committed) == parts(data), name


def test_the_truncated_workbook_is_the_first_sixty_percent_of_the_work_order_export(
    xlsx_fixtures: ModuleType,
) -> None:
    whole = xlsx_fixtures.zipped(xlsx_fixtures.workorders_amr_fleet())
    committed = (FIXTURES / "truncated_workorders.xlsx").read_bytes()
    assert len(committed) == len(whole) * 6 // 10
    assert (FIXTURES / "workorders_amr_fleet.xlsx").read_bytes().startswith(committed[:30])


def test_every_workbook_fixture_is_under_512_kib() -> None:
    assert all((FIXTURES / name).stat().st_size < 512 * 1024 for name in WORKBOOKS)


@pytest.mark.parametrize("name", WORKBOOKS)
def test_the_registry_sends_each_workbook_to_the_tabular_adapter_by_its_bytes(name: str) -> None:
    data = (FIXTURES / name).read_bytes()
    for label in (name, "renamed"):  # the name never decides
        selection = default_registry().select(data[:PROBE_HEAD_SIZE], ProbeHints(label, len(data)))
        assert selection.adapter == "tabular", (name, label, selection)
        assert selection.status is SelectionStatus.SELECTED
