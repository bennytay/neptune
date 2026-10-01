"""Derived tables in an ingest package: written apart from evidence, checked, read (ADR 0036 §7)."""

import io
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from neptune.derived.grouping import Grouping, LayoutGrouper
from neptune.derived.sessions import read_derived
from neptune.discovery.layout import LayoutFile, layout_of
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id, digest_stream
from neptune.identity.revisions import SourceLedger
from neptune.model.source import local_location
from neptune.store.package import (
    MANIFEST,
    PackageError,
    derived_path,
    package_files,
    read_files,
    read_package,
    write_package,
)

PATHS = (b"runs/run_1/a.mcap", b"runs/run_1/notes.txt", b"x_0.bag", b"x_1.bag", b"README")


def ledger_and_grouping() -> tuple[list[Any], Grouping]:
    ledger = SourceLedger()
    files = []
    for path in PATHS:
        observation = ledger.observe(local_location(path), digest_stream(io.BytesIO(path)))
        revision = observation.revision
        files.append(LayoutFile(revision.id, revision.location, revision.content_id))  # type: ignore[arg-type]
    grouping = LayoutGrouper().propose(layout_of(files))
    records = [*ledger.artifacts(), *ledger.revisions(), grouping.transform, *grouping.findings]
    return records, grouping


def test_derived_tables_land_apart_from_the_evidence_and_read_back(tmp_path: Path) -> None:
    records, grouping = ledger_and_grouping()
    files = package_files(records, derived=grouping.tables())
    assert derived_path("session_proposal") in files
    assert not any(path.startswith("records/session") for path in files)
    root = tmp_path / "package"
    write_package(root, files)
    package = read_package(root)
    assert package.derived == {kind: tuple(lines) for kind, lines in grouping.tables().items()}
    assert package.files() == files
    parsed = read_derived(package.derived)
    assert {r.id for r in parsed} == {
        *(p.id for p in grouping.proposals),
        *(u.id for u in grouping.unassigned),
    }
    # The tables are in the manifest, so in the package id.
    listed = {file.path for file in package.manifest.files}
    assert {derived_path("session_proposal"), derived_path("session_unassigned")} <= listed


def test_the_order_lines_are_given_in_never_shows() -> None:
    records, grouping = ledger_and_grouping()
    tables = grouping.tables()
    reversed_tables = {kind: list(reversed(lines)) for kind, lines in tables.items()}
    assert package_files(records, derived=tables) == package_files(records, derived=reversed_tables)


def test_absent_and_empty_are_different_packages() -> None:
    records, _ = ledger_and_grouping()
    absent = package_files(records)
    empty = package_files(records, derived={"session_proposal": []})
    assert read_files(absent).derived == {}
    assert read_files(empty).derived == {"session_proposal": ()}
    assert absent[MANIFEST] != empty[MANIFEST]


def first_line(grouping: Grouping) -> dict[str, Any]:
    return dict(grouping.tables()["session_proposal"][0])


@pytest.mark.parametrize(
    ("edit", "error"),
    [
        (lambda line, _: line | {"kind": "session_unassigned"}, "not a session_proposal"),
        (lambda line, _: line | {"schema_version": 0}, "schema_version"),
        (lambda line, _: line | {"schema_version": True}, "schema_version"),
        (lambda line, _: line | {"id": "nope"}, "record id"),
        (lambda line, _: {k: v for k, v in line.items() if k != "id"}, "record id"),
        (lambda line, _: line | {"transform": "rec:sha256:" + "1" * 64}, "transform"),
    ],
)
def test_the_writer_refuses_a_line_the_reader_would(edit: Any, error: str) -> None:
    records, grouping = ledger_and_grouping()
    bad = edit(first_line(grouping), grouping)
    with pytest.raises(PackageError, match=error):
        package_files(records, derived={"session_proposal": [bad]})


def test_two_lines_with_one_id_are_refused() -> None:
    records, grouping = ledger_and_grouping()
    line = first_line(grouping)
    with pytest.raises(PackageError, match="each id once"):
        package_files(records, derived={"session_proposal": [line, line]})
    with pytest.raises(PackageError, match="not a derived table kind"):
        package_files(records, derived={"Bad Kind": []})


def with_manifest_for(files: dict[str, bytes], changed: dict[str, bytes]) -> dict[str, bytes]:
    """``files`` with some changed and a manifest rehashed to match: only deeper checks fail."""
    package = read_files(files)
    edited = {**files, **changed}
    listed = tuple(
        replace(file, size=len(edited[file.path]), sha256=content_id(edited[file.path]))
        for file in package.manifest.files
    )
    manifest = replace(package.manifest, files=listed)
    return {**edited, MANIFEST: canonical_json.dumps(manifest.to_json())}


def test_the_reader_refuses_a_table_out_of_order_or_not_canonical() -> None:
    records, grouping = ledger_and_grouping()
    files = package_files(records, derived=grouping.tables())
    path = derived_path("session_proposal")
    lines = files[path].splitlines(keepends=True)
    assert len(lines) > 1
    with pytest.raises(PackageError, match="sorted by id"):
        read_files(with_manifest_for(files, {path: b"".join(reversed(lines))}))
    spaced = files[path].replace(b'{"assertion_kind"', b'{ "assertion_kind"', 1)
    with pytest.raises(PackageError, match="canonical"):
        read_files(with_manifest_for(files, {path: spaced}))
    with pytest.raises(PackageError, match="canonical"):
        read_files(with_manifest_for(files, {path: files[path].rstrip(b"\n")}))


def test_a_derived_file_outside_the_manifest_has_no_place(tmp_path: Path) -> None:
    records, grouping = ledger_and_grouping()
    root = tmp_path / "package"
    write_package(root, package_files(records, derived=grouping.tables()))
    (root / "derived" / "caption.jsonl").write_bytes(b"")
    with pytest.raises(PackageError, match="do not match the manifest"):
        read_package(root)
    (root / "derived" / "caption.jsonl").unlink()
    (root / "derived" / "notes.txt").write_bytes(b"")
    with pytest.raises(PackageError, match="do not match the manifest"):
        read_package(root)


def test_derived_lines_must_be_objects() -> None:
    records, _ = ledger_and_grouping()
    with pytest.raises(PackageError):
        package_files(records, derived={"session_proposal": [{"id": "x"}]})
