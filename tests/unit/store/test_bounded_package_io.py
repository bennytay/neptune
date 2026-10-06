"""Bounded, crash-safe package I/O (ADR 0070): writes renamed into place whole, stale partial
writes reclaimed, derived runs released early, and a reader that never holds the package."""

import fcntl
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.kinds import RECORD_KINDS
from neptune.model.references import named
from neptune.store import package as package_module
from neptune.store import spill as spill_module
from neptune.store.package import (
    MANIFEST,
    RECEIPT,
    IngestPackage,
    PackageError,
    PackageRecords,
    package_files,
    partial_path,
    read_files,
    read_package,
    table_path,
    write_package,
)
from neptune.store.spill import SpillSpace
from neptune.store.writer import PackageWriter, write_package_stream
from neptune.validate import rules as rules_module
from neptune.validate import validate_package
from neptune.validate.engine import Bounds, Context, Inputs
from neptune.validate.rules import dangling_reference

ROOT: Final = Path(__file__).resolve().parents[3]
ARCHETYPE: Final = (
    ROOT / "packages" / "neptune-deploy" / "tests" / "fixtures" / "archetypes" / "packages"
)
MODEL: Final = ROOT / "tests" / "fixtures" / "model"
TINY: Final = 4096


def load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCALE: Final = load(ROOT / "tests" / "fixtures" / "store" / "make_scale_package.py")


def files_of(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def archetype() -> IngestPackage:
    """A committed package with series and derived tables (Deploy's warehouse fleet)."""
    committed = ARCHETYPE / "warehouse_amr_fleet"
    if not committed.is_dir():
        pytest.skip("no committed archetype package")
    return read_package(committed)


def scratch_of(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return scratch


def stream_write(package: IngestPackage, root: Path, scratch: Path) -> Any:
    return write_package_stream(
        root,
        package.records,
        scratch=scratch,
        series=package.series,
        blobs=package.blobs,
        store=package.manifest.store,
        derived=package.derived,
        budget=TINY,
    )


# --- Atomic writes -----------------------------------------------------------------------------


def failing_copy(after: int) -> Any:
    """``copy_file`` that copies ``after`` files whole, then half of the next one and fails."""
    copied: list[Path] = []
    real = package_module.copy_file

    def copy(path: Path, target: Path) -> None:
        if len(copied) == after:
            target.write_bytes(path.read_bytes()[:100])
            raise OSError("disk unplugged mid-copy")
        real(path, target)
        copied.append(path)

    return copy


@pytest.mark.parametrize("writer", ["write_package", "write_package_stream"])
@pytest.mark.parametrize("after", [0, 3])
def test_a_write_that_fails_mid_copy_leaves_no_package(
    writer: str, after: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = archetype()
    assert len(package.series) > after
    monkeypatch.setattr(package_module, "copy_file", failing_copy(after))
    root = tmp_path / "out" / "package"
    with pytest.raises(OSError, match="mid-copy"):
        if writer == "write_package":
            write_package(root, package.files())
        else:
            stream_write(package, root, scratch_of(tmp_path))
    assert not root.exists()
    assert list((tmp_path / "out").iterdir()) == []  # the partial directory went with it


@pytest.mark.parametrize("writer", ["write_package", "write_package_stream"])
def test_a_root_that_exists_empty_is_replaced_whole(writer: str, tmp_path: Path) -> None:
    package = archetype()
    root = tmp_path / "package"
    root.mkdir()
    if writer == "write_package":
        identity = write_package(root, package.files())
    else:
        identity = stream_write(package, root, scratch_of(tmp_path))
    assert identity == package.id
    assert files_of(root) == files_of(ARCHETYPE / "warehouse_amr_fleet")
    assert not partial_path(root).exists()


def test_a_root_that_is_a_symlink_or_occupied_is_refused(tmp_path: Path) -> None:
    files = package_files(SCALE.scale_records(3))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere)
    with pytest.raises(PackageError, match="not an empty directory"):
        write_package(link, files)
    assert list(elsewhere.iterdir()) == []
    (tmp_path / "file").write_bytes(b"x")
    with pytest.raises(PackageError, match="not an empty directory"):
        write_package(tmp_path / "file", files)


_KILLED: Final = """
import os, signal, sys
from pathlib import Path
from neptune.store import package as package_module
from neptune.store.package import read_package
from neptune.store.writer import write_package_stream

real = package_module.copy_file
copied = []

def copy(path, target):
    if len(copied) == 2:  # two series whole, the third half-copied: then the process dies
        target.write_bytes(path.read_bytes()[:64])
        os.kill(os.getpid(), signal.SIGKILL)
    real(path, target)
    copied.append(path)

package_module.copy_file = copy
base = read_package(Path(sys.argv[1]))
write_package_stream(
    Path(sys.argv[2]), base.records, scratch=Path(sys.argv[3]), series=base.series,
    blobs=base.blobs, store=base.manifest.store, derived=base.derived,
)
"""


def test_a_killed_write_leaves_no_package_and_the_next_write_reclaims_its_partial(
    tmp_path: Path,
) -> None:
    package = archetype()
    root = tmp_path / "package"
    arguments = [str(ARCHETYPE / "warehouse_amr_fleet"), str(root), str(scratch_of(tmp_path))]
    done = subprocess.run(
        [sys.executable, "-c", _KILLED, *arguments],
        capture_output=True,
        check=False,
    )
    assert done.returncode == -9, done.stderr
    assert not root.exists()  # never a manifest without its series
    partial = partial_path(root)
    left = files_of(partial)  # the hazard, kept out of ``root``: a manifest, half its series
    assert MANIFEST in left and len([n for n in left if n.startswith("series/")]) == 3
    assert stream_write(package, root, scratch_of(tmp_path)) == package.id
    assert not partial.exists()
    assert files_of(root) == files_of(ARCHETYPE / "warehouse_amr_fleet")


def test_a_stale_partial_of_any_shape_is_emptied_never_followed(tmp_path: Path) -> None:
    records = list(SCALE.scale_records(5))
    files = package_files(records)
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "precious").write_bytes(b"not ours")
    root = tmp_path / "package"
    partial = partial_path(root)
    (partial / "records" / "deep").mkdir(parents=True)
    (partial / "records" / "deep" / "x.jsonl").write_bytes(b"junk")
    (partial / MANIFEST).write_bytes(b"{}")
    (partial / "link").symlink_to(keep)
    write_package(root, files)
    assert files_of(root) == files
    assert (keep / "precious").read_bytes() == b"not ours"
    # A file or a link where the partial directory goes is removed, and the link not followed.
    other = tmp_path / "other"
    partial_path(other).symlink_to(keep)
    write_package(other, files)
    assert files_of(other) == files and (keep / "precious").exists()
    third = tmp_path / "third"
    partial_path(third).write_bytes(b"stale")
    write_package(third, files)
    assert files_of(third) == files


def test_a_write_of_a_root_in_progress_elsewhere_is_refused(tmp_path: Path) -> None:
    files = package_files(SCALE.scale_records(3))
    root = tmp_path / "package"
    partial = partial_path(root)
    partial.mkdir()
    (partial / "half").write_bytes(b"someone else's")
    held = os.open(partial, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(PackageError, match="being written by another process"):
            write_package(root, files)
        assert (partial / "half").read_bytes() == b"someone else's"  # untouched
        assert not root.exists()
    finally:
        os.close(held)
    write_package(root, files)  # released: the next write reclaims it
    assert files_of(root) == files


def test_atomic_writes_are_deterministic(tmp_path: Path) -> None:
    records = list(SCALE.scale_records(40))
    ids = {
        write_package(tmp_path / "a", package_files(records)),
        write_package_stream(tmp_path / "b", records, scratch=scratch_of(tmp_path), budget=TINY),
        write_package_stream(tmp_path / "c", records, scratch=scratch_of(tmp_path), budget=1),
    }
    assert len(ids) == 1
    assert files_of(tmp_path / "a") == files_of(tmp_path / "b") == files_of(tmp_path / "c")


# --- Derived runs released as soon as they are merged ------------------------------------------


def test_derived_runs_are_removed_as_soon_as_their_table_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = list(SCALE.scale_records(10))
    transform = next(r for r in records if r.kind == "transform_record").id
    lines = [
        {
            "id": "rec:sha256:" + content_id(b"%d" % n)[7:],
            "kind": "note",
            "schema_version": 1,
            "text": "x" * 120,
            "transform": transform,
        }
        for n in range(200)
    ]
    spilled: list[str] = []
    real = spill_module.Sorter.spill

    def spill(self: spill_module.Sorter) -> None:
        if self.held:
            spilled.append(self.name)
        real(self)

    monkeypatch.setattr(spill_module.Sorter, "spill", spill)
    scratch = scratch_of(tmp_path)
    with PackageWriter(scratch, budget=TINY) as writer:
        writer.extend(records)
        contents = writer.finish(tmp_path / "out", derived={"note": lines})
        assert "derived-note" in spilled  # it did spill runs
        assert [p.name for p in scratch.glob("spill-*/*")] == []  # all released before close
    expected = package_files(records, derived={"note": lines})
    assert contents[MANIFEST] == expected[MANIFEST]  # the same package, byte for byte
    assert files_of(tmp_path / "out")["derived/note.jsonl"] == expected["derived/note.jsonl"]


# --- Streaming read ----------------------------------------------------------------------------


def test_read_records_are_lazy_re_readable_and_the_written_ones(tmp_path: Path) -> None:
    records = list(SCALE.scale_records(60))
    root = tmp_path / "package"
    write_package(root, package_files(records))
    package = read_package(root)
    assert isinstance(package.records, PackageRecords)
    first, second = list(package.records), list(package.records)
    assert first == second and len(package.records) == len(first)
    assert {canonical_json.dumps(r.to_json()) for r in first} == {
        canonical_json.dumps(r.to_json()) for r in records
    }
    rows = list(package.records.of("structured_record"))
    assert len(rows) == 60 and rows == sorted(rows, key=lambda r: r.id)
    assert list(package.records.of("calibration")) == []
    assert rows[0] in package.records and object() not in package.records
    assert package.records == read_files(package_files(records)).records == tuple(first)
    assert package.files() == package_files(records)


@pytest.mark.parametrize("budget", [1, TINY, 10**9])
def test_reading_with_scratch_spills_and_leaves_it_empty(budget: int, tmp_path: Path) -> None:
    records = list(SCALE.scale_records(300))
    root = tmp_path / "package"
    write_package(root, package_files(records))
    scratch = scratch_of(tmp_path)
    spilled = read_package(root, scratch=scratch, budget=budget)
    plain = read_package(root)
    assert spilled.id == plain.id and spilled.records == plain.records
    assert spilled.receipt == plain.receipt
    assert list(scratch.iterdir()) == []


def test_a_package_changed_after_it_was_read_is_refused_when_read_again(tmp_path: Path) -> None:
    root = tmp_path / "package"
    write_package(root, package_files(SCALE.scale_records(20)))
    package = read_package(root)
    table = root / table_path("structured_record")
    table.write_bytes(table.read_bytes().replace(b"Berth 1", b"Berth 2", 1))
    with pytest.raises(PackageError, match="changed since the package was read"):
        list(package.records)
    (root / RECEIPT).write_bytes((root / RECEIPT).read_bytes() + b" ")
    with pytest.raises(PackageError, match="changed since the package was read"):
        _ = package.receipt


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (lambda data: data.rstrip(b"\n"), "canonical"),
        (lambda data: data + b"\n", "canonical JSON"),
        (lambda data: b"not json\n" + data, "canonical JSON"),
        (lambda data: data.replace(b'{"', b'{ "', 1), "canonical"),
    ],
    ids=["no final newline", "blank line", "not json", "spaced"],
)
def test_malformed_table_lines_are_refused_as_they_stream(edit: Any, error: str) -> None:
    files = package_files(SCALE.scale_records(5))
    path = table_path("structured_record")
    edited = {**files, path: edit(files[path])}
    manifest = canonical_json.loads(files[MANIFEST])
    assert isinstance(manifest, dict)
    for listed in manifest["files"]:
        assert isinstance(listed, dict)
        if listed["path"] == path:
            listed["size"], listed["sha256"] = len(edited[path]), content_id(edited[path])
    edited[MANIFEST] = canonical_json.dumps(manifest)
    with pytest.raises(PackageError, match=error):
        read_files(edited)


def test_an_empty_package_reads_as_one(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    write_package(root, package_files([]))
    package = read_package(root, scratch=scratch_of(tmp_path))
    assert list(package.records) == [] and len(package.records) == 0
    assert package.receipt.findings == () and package.derived == {}


def test_a_large_receipt_that_does_not_recompute_is_refused_without_being_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = package_files(SCALE.scale_records(5))
    other = package_files(SCALE.scale_records(6))[RECEIPT]
    edited = {**files, RECEIPT: other}
    manifest = canonical_json.loads(files[MANIFEST])
    assert isinstance(manifest, dict)
    for listed in manifest["files"]:
        assert isinstance(listed, dict)
        if listed["path"] == RECEIPT:
            listed["size"], listed["sha256"] = len(other), content_id(other)
    edited[MANIFEST] = canonical_json.dumps(manifest)
    with pytest.raises(PackageError, match="is not the receipt of these records"):
        read_files(edited)
    from neptune.store import reader

    monkeypatch.setattr(reader, "_EXPLAIN_LIMIT", 10)
    monkeypatch.setattr(reader, "ingest_receipt_from_json", None)  # never called
    with pytest.raises(PackageError, match=r"receipt\.json is not the receipt of these records"):
        read_files(edited)


# --- Validation over a package it never holds --------------------------------------------------


def example_records(example: str) -> list[Any]:
    found = []
    for path in sorted((MODEL / example / "records").glob("*.jsonl")):
        read = RECORD_KINDS[path.stem][1]
        found += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return found


@pytest.mark.parametrize("example", ["mobile_robot", "warehouse_amr"])
def test_the_spilled_reference_join_finds_what_the_in_memory_one_does(
    example: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = example_records(example)
    ids = {getattr(r, "id", None) for r in records}
    targets = sorted({target for r in records for _, target in named(r)} & ids)
    assert targets
    rest = [r for r in records if getattr(r, "id", None) != targets[0]]

    def context(spill: Path | None) -> Context:
        package = IngestPackage("sha256:" + "0" * 64, None, rest, {}, {})  # type: ignore[arg-type]
        return Context(package, Bounds(), Inputs(), spill)

    held = [d.details for d in dangling_reference(context(None))]
    assert held and any(d["target"] == targets[0] for d in held)
    scratch = scratch_of(tmp_path)
    monkeypatch.setattr(rules_module, "SpillSpace", lambda spill: SpillSpace(spill, 64))
    assert [d.details for d in dangling_reference(context(scratch))] == held
    assert list(scratch.iterdir()) == []


def test_validation_reads_a_package_without_holding_it_and_is_deterministic(
    tmp_path: Path,
) -> None:
    records = list(SCALE.scale_records(200))
    root = tmp_path / "package"
    write_package(root, package_files(records))
    scratch = scratch_of(tmp_path)
    package = read_package(root, scratch=scratch)
    first = validate_package(package, spill=scratch)
    again = validate_package(read_files(package_files(records)))
    assert first.findings == again.findings and first.rules == again.rules
    assert list(scratch.iterdir()) == []
    table = root / table_path("structured_record")
    table.write_bytes(table.read_bytes().replace(b"Berth 1", b"Berth 2", 1))
    with pytest.raises(PackageError, match="changed since"):  # never a rule's failure
        validate_package(package)
    shutil.rmtree(root)
    with pytest.raises(PackageError, match="cannot be read"):  # never held: read as needed
        validate_package(package)


def test_what_is_parsed_is_what_was_hashed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A table swapped between the hash pass and the parse pass is refused, not trusted."""
    root = tmp_path / "package"
    files = package_files(SCALE.scale_records(10))
    write_package(root, files)
    table = root / table_path("structured_record")
    table.write_bytes(table.read_bytes().replace(b"Berth 1", b"Berth 2", 1))
    from neptune.store import reader

    real = package_module._digest

    def stale(content: Any) -> Any:  # the first pass saw the file before it was swapped
        if content == table:
            return len(files[table_path("structured_record")]), content_id(
                files[table_path("structured_record")]
            )
        return real(content)

    monkeypatch.setattr(reader, "_digest", stale)
    with pytest.raises(PackageError, match="does not match its size and hash"):
        read_package(root)
