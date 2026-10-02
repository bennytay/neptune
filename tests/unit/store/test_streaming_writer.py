"""The streaming package writer: the in-memory writer's bytes, in bounded memory (ADR 0065)."""

import importlib.util
import random
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.store.package import (
    MANIFEST,
    PackageError,
    package_contents,
    package_files,
    read_package,
)
from neptune.store.spill import FAN_IN, Sorter, SpillBudget
from neptune.store.writer import PackageWriter, write_package_stream

ROOT: Final = Path(__file__).resolve().parents[3]


def load_generator(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOLDEN: Final = ROOT / "tests" / "golden" / "packages"
ARCHETYPES: Final = ROOT / "packages" / "neptune-deploy" / "tests" / "fixtures" / "archetypes"
SCALE: Final = load_generator(ROOT / "tests" / "fixtures" / "store" / "make_scale_package.py")
EXAMPLES: Final = load_generator(GOLDEN / "make_packages.py")
TINY: Final = 4096  # a budget that spills every few records, so every path through the merge runs


def files_of(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def spill_dir(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return scratch


@pytest.mark.parametrize("name", EXAMPLES.NAMES)
def test_each_worked_example_streams_to_its_golden_documents(name: str, tmp_path: Path) -> None:
    records = EXAMPLES.example_records(name)
    root = tmp_path / "package"
    scratch = spill_dir(tmp_path)
    write_package_stream(root, iter(records), scratch=scratch, budget=TINY)
    written = files_of(root)
    for document in EXAMPLES.DOCUMENTS:  # the manifest lists every file's hash: all bytes match
        assert written[document] == (GOLDEN / name / document).read_bytes()
    assert written == package_files(records)
    assert list(scratch.iterdir()) == []  # spilled runs are removed


@pytest.mark.parametrize("name", ["manipulator_cell", "warehouse_amr_fleet"])
def test_a_whole_package_with_series_and_derived_tables_streams_byte_for_byte(
    name: str, tmp_path: Path
) -> None:
    """Deploy's committed archetype packages, made by the compiler before ADR 0065: every file,
    series and derived tables included, is written again byte for byte."""
    committed = ARCHETYPES / "packages" / name
    if not committed.is_dir():
        pytest.skip(f"no committed archetype package {name}")
    package = read_package(committed)
    root = tmp_path / "package"
    identity = write_package_stream(
        root,
        reversed(package.records),
        scratch=spill_dir(tmp_path),
        series=package.series,
        blobs=package.blobs,
        store=package.manifest.store,
        derived=package.derived,
        budget=TINY,
    )
    assert files_of(root) == files_of(committed)
    assert identity == package.id
    assert read_package(root).id == package.id


def test_a_spilled_package_is_the_in_memory_package(tmp_path: Path) -> None:
    """Records with findings and ambiguous cells, in no id order, spill hundreds of runs (more than
    one merge can take at once) and still give the in-memory writer's bytes."""
    records = list(SCALE.scale_records(1500))
    random.Random(48).shuffle(records)
    expected = package_files(records)
    root = tmp_path / "package"
    scratch = spill_dir(tmp_path)
    write_package_stream(root, records, scratch=scratch, budget=TINY)
    assert files_of(root) == expected
    package = read_package(root)
    assert len(package.receipt.findings) == 750 and len(package.receipt.ambiguous) == 150
    assert list(scratch.iterdir()) == []


def test_the_writer_is_deterministic_whatever_the_budget(tmp_path: Path) -> None:
    records = list(SCALE.scale_records(300))
    ids = set()
    for index, budget in enumerate((1, TINY, 10**9)):
        root = tmp_path / f"package-{index}"
        ids.add(write_package_stream(root, records, scratch=spill_dir(tmp_path), budget=budget))
    assert len(ids) == 1


def test_a_record_given_again_replaces_only_when_both_say_so() -> None:
    records = EXAMPLES.example_records("drone")
    expected = package_files(records)
    with PackageWriter() as writer:
        writer.extend(records, last_wins=True)
        writer.extend(records, last_wins=True)
        assert writer.finish() == expected
    with PackageWriter() as writer:
        writer.extend(records)
        writer.extend(records[:1], last_wins=True)
        with pytest.raises(PackageError, match="share an id"):
            writer.finish()
    with pytest.raises(PackageError, match="share an id"):
        package_contents([*records, records[0]])


def test_spill_is_removed_when_the_write_fails(tmp_path: Path) -> None:
    records = [*SCALE.scale_records(200), object()]
    scratch = spill_dir(tmp_path)
    with pytest.raises(PackageError, match="not a record"):
        write_package_stream(tmp_path / "package", records, scratch=scratch, budget=TINY)
    assert list(scratch.iterdir()) == []


def test_a_finding_whose_transform_is_absent_is_refused(tmp_path: Path) -> None:
    records = [r for r in SCALE.scale_records(10) if r.kind != "transform_record"]
    with pytest.raises(PackageError, match="transform"):
        write_package_stream(tmp_path / "p", records, scratch=spill_dir(tmp_path), budget=TINY)


def test_the_package_root_must_be_empty(tmp_path: Path) -> None:
    root = tmp_path / "package"
    root.mkdir()
    (root / MANIFEST).write_bytes(b"{}")
    with pytest.raises(PackageError, match="not an empty directory"):
        write_package_stream(root, [], scratch=spill_dir(tmp_path))


def test_a_writer_writes_once() -> None:
    with PackageWriter() as writer:
        writer.finish()
        with pytest.raises(PackageError, match="already written"):
            writer.add(EXAMPLES.example_records("drone")[0])


# --- The sorter --------------------------------------------------------------------------------


def entries(count: int) -> list[tuple[tuple[str | int, ...], bytes]]:
    rng = random.Random(count)
    return [((rng.randrange(50), f"k{rng.randrange(20)}"), b"%d" % n) for n in range(count)]


@pytest.mark.parametrize("budget", [1, 2000, 10**9])
def test_the_sorter_is_stable_and_bounded(budget: int, tmp_path: Path) -> None:
    given = entries(3 * FAN_IN * 3)
    sorter = Sorter("t", tmp_path, SpillBudget(budget))
    for key, payload in given:
        sorter.add(key, payload)
    expected = sorted(given, key=lambda entry: entry[0])  # stable: equal keys in order given
    assert list(sorter) == expected
    assert list(sorter) == expected  # read again
    assert len(sorter) == len(given)
    assert len(list(tmp_path.iterdir())) <= FAN_IN
    sorter.close()
    assert list(tmp_path.iterdir()) == []


def test_spilled_runs_are_deterministic(tmp_path: Path) -> None:
    runs: list[list[bytes]] = []
    for name in ("a", "b"):
        directory = tmp_path / name
        directory.mkdir()
        sorter = Sorter("t", directory, SpillBudget(500))
        for key, payload in entries(200):
            sorter.add(key, payload)
        runs.append([path.read_bytes() for path in sorted(directory.iterdir())])
        sorter.close()
    assert runs[0] == runs[1] and len(runs[0]) > 1


def test_a_sorter_being_read_takes_no_more(tmp_path: Path) -> None:
    sorter = Sorter("t", tmp_path, SpillBudget())
    sorter.add(("a",), b"1")
    list(sorter)
    with pytest.raises(RuntimeError):
        sorter.add(("b",), b"2")
