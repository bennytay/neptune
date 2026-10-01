"""Re-hashing referenced sources on request, and the ``ledger`` CLI (MVL-90; ADR 0007).

The compiler's worked examples keep each package's ingest root at
``tests/fixtures/model/<name>/sources/``: the bytes its ``source_revision`` locations name.
"""

import hashlib
import io
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

import psycopg
import pytest

from neptune_ledger.catalog.migrate import apply_migrations
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.catalog.sources import LocalSourceStore, SourceStore
from neptune_ledger.cli import main
from neptune_ledger.contract_tests.examples import (
    EXAMPLES,
    WorkedPackage,
    examples_dir,
    materialise,
    with_moved_source,
    write,
)


class FakeObjectStore:
    """An S3-compatible bucket in memory: objects by key, read through a prefix (ADR 0007)."""

    def __init__(self, objects: dict[str, bytes], prefix: str) -> None:
        self.objects, self.prefix, self.gets = objects, prefix, 0

    def describe(self) -> str:
        return f"s3://bucket/{self.prefix}"

    def open(self, path: bytes) -> BinaryIO | None:
        self.gets += 1
        data = self.objects.get(self.prefix + path.decode("utf-8", "surrogateescape"))
        return None if data is None else io.BytesIO(data)


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with psycopg.connect(pg_uri, autocommit=True) as conn:
        apply_migrations(conn, "acme")
    with PostgresCatalog(pg_uri, "acme", package_roots=None) as made:
        yield made


@pytest.fixture
def sources(tmp_path: Path) -> Path:
    """A copy of the drone's ingest root, free to change."""
    root = tmp_path / "ingest"
    shutil.copytree(examples_dir() / "drone" / "sources", root)
    return root


def _states(report: object) -> list[tuple[str, str]]:
    return [(c.state, json.loads(c.location)["path"]) for c in report.checks]  # type: ignore[attr-defined]


def test_every_worked_examples_sources_are_present(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    packages = [materialise(name, tmp_path / name) for name in EXAMPLES]
    stores: list[SourceStore] = [
        LocalSourceStore(examples_dir() / name / "sources") for name in EXAMPLES
    ]
    for package in packages:
        catalog.register(package.root)
    for package in packages:
        report = catalog.verify_sources(package.package_id, stores)
        assert report.findings == ()
        assert len(report.checks) == len(package.manifest["sources"])
        assert {c.state for c in report.checks} == {"present"}


def test_a_changed_and_an_absent_source_are_reported(
    catalog: PostgresCatalog, sources: Path, tmp_path: Path
) -> None:
    drone = materialise("drone", tmp_path / "drone")
    catalog.register(drone.root)
    (sources / "flight.ulg").write_bytes(b"other bytes")
    report = catalog.verify_sources(drone.package_id, [LocalSourceStore(sources)])
    assert _states(report) == [("changed", "flight.ulg")]
    (sources / "flight.ulg").unlink()
    report = catalog.verify_sources(drone.package_id, [LocalSourceStore(sources)])
    assert _states(report) == [("absent", "flight.ulg")]


def test_a_moved_source_is_found_where_another_package_says_it_is(
    catalog: PostgresCatalog, sources: Path, tmp_path: Path
) -> None:
    original = materialise("drone", tmp_path / "drone")
    moved = write("moved", tmp_path / "moved", with_moved_source("drone", "moved/flight.ulg"))
    (sources / "moved").mkdir()
    (sources / "flight.ulg").rename(sources / "moved" / "flight.ulg")
    catalog.register(original.root)
    store = LocalSourceStore(sources)
    before = catalog.verify_sources(original.package_id, [store])
    assert _states(before) == [("absent", "flight.ulg")], "nothing yet says where it went"
    catalog.register(moved.root)
    report = catalog.verify_sources(original.package_id, [store])
    assert _states(report) == [("moved", "flight.ulg")]
    assert report.checks[0].found_at == '{"kind":"local","path":"moved/flight.ulg"}'
    # The moved package states the old location gone, so only the new one is checked.
    assert _states(catalog.verify_sources(moved.package_id, [store])) == [
        ("present", "moved/flight.ulg")
    ]
    # At the earlier catalog point the second package does not exist yet.
    assert _states(catalog.verify_sources(original.package_id, [store], as_of=1)) == [
        ("absent", "flight.ulg")
    ]


def test_an_object_store_serves_the_same_interface(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    drone = materialise("drone", tmp_path / "drone")
    catalog.register(drone.root)
    data = (examples_dir() / "drone" / "sources" / "flight.ulg").read_bytes()
    assert (
        "sha256:" + hashlib.sha256(data).hexdigest() == drone.manifest["sources"][0]["content_id"]
    )
    empty = FakeObjectStore({"flight.ulg": data}, "runs/2026/")
    bucket = FakeObjectStore({"runs/2026/flight.ulg": data}, "runs/2026/")
    report = catalog.verify_sources(drone.package_id, [empty, bucket])
    assert _states(report) == [("present", "flight.ulg")]
    assert "s3://bucket/runs/2026/" in report.checks[0].detail
    assert (empty.gets, bucket.gets) == (1, 1)


def test_a_local_store_never_reads_outside_its_root(
    catalog: PostgresCatalog, sources: Path, tmp_path: Path
) -> None:
    drone = materialise("drone", tmp_path / "drone")
    catalog.register(drone.root)
    outside = tmp_path / "outside.ulg"
    (sources / "flight.ulg").rename(outside)
    (sources / "flight.ulg").symlink_to(outside)
    report = catalog.verify_sources(drone.package_id, [LocalSourceStore(sources)])
    assert _states(report) == [("absent", "flight.ulg")]


def test_source_checks_of_an_unknown_package_are_a_finding(catalog: PostgresCatalog) -> None:
    report = catalog.verify_sources("sha256:" + "0" * 64, [])
    assert [f.code for f in report.findings] == ["unknown_package"]
    assert report.checks == ()


# --- the CLI -----------------------------------------------------------------------------------


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, list[dict[str, object]]]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, [json.loads(line) for line in out.splitlines()]


def test_the_cli_registers_and_verifies(
    pg_uri: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    roots = tmp_path / "packages"
    drone: WorkedPackage = materialise("drone", roots / "drone")
    db = ("--dsn", pg_uri, "--tenant", "acme")
    assert _run(capsys, *db, "migrate") == (0, [{"applied": [1, 2]}])
    code, (registration,) = _run(
        capsys, *db, "register", str(drone.root), "--package-root", str(roots)
    )
    assert (code, registration["outcome"]) == (0, "registered")
    code, (again,) = _run(capsys, *db, "register", str(drone.root), "--package-root", str(roots))
    assert (code, again["outcome"]) == (0, "already_registered")
    source_root = str(examples_dir() / "drone" / "sources")
    code, (report, sources) = _run(
        capsys, *db, "verify", drone.package_id, "--source-root", source_root
    )
    assert (code, report["verdict"]) == (0, "intact")
    checks = sources["checks"]
    assert isinstance(checks, list)
    assert [c["state"] for c in checks] == ["present"]
    code, (unknown,) = _run(capsys, *db, "verify", "sha256:" + "0" * 64)
    assert (code, unknown["verdict"]) == (1, "unknown_package")


def test_the_cli_refuses_outside_the_package_roots_and_without_configuration(
    pg_uri: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NEPTUNE_LEDGER_PACKAGE_ROOTS", raising=False)
    drone = materialise("drone", tmp_path / "elsewhere" / "drone")
    db = ("--dsn", pg_uri, "--tenant", "acme")
    main([*db, "migrate"])
    capsys.readouterr()
    code, (refused,) = _run(
        capsys, *db, "register", str(drone.root), "--package-root", str(tmp_path / "ours")
    )
    assert (code, refused["outcome"]) == (1, "refused")
    assert main([*db, "register", str(drone.root)]) == 2
    assert main([*db, "verify", drone.package_id, "--source-root", "s3://bucket/runs"]) == 2
    assert main(["register", str(drone.root)]) == 2
    assert "ledger:" in capsys.readouterr().err
