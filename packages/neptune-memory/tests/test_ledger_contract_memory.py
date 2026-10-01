"""Ledger reader contract, parametrised over readers; add the real Ledger client when it lands."""

from collections.abc import Callable

import pytest

from neptune_memory.ledger import LedgerReader, PackageRef, StubLedger


def _stub() -> StubLedger:
    return StubLedger(
        {
            "pkg-b": (1, [{"kind": "observation", "n": 1}, {"kind": "frame", "n": 2}]),
            "pkg-a": (1, [{"kind": "observation", "n": 3}, {"kind": "observation", "n": 4}]),
            "pkg-empty": (1, []),
        }
    )


READERS: list[Callable[[], LedgerReader]] = [_stub]


@pytest.fixture(params=READERS, ids=lambda f: f.__name__)
def ledger(request: pytest.FixtureRequest) -> LedgerReader:
    reader: LedgerReader = request.param()
    return reader


def test_implements_protocol(ledger: LedgerReader) -> None:
    assert isinstance(ledger, LedgerReader)
    assert isinstance(ledger.catalog_api_version, str)


def test_packages_listed_in_id_order(ledger: LedgerReader) -> None:
    ids = [ref.package_id for ref in ledger.list_packages()]
    assert ids == sorted(ids)
    assert all(isinstance(ref, PackageRef) for ref in ledger.list_packages())


def test_unknown_package_is_none_not_empty(ledger: LedgerReader) -> None:
    assert ledger.read_records("no-such-package", "observation") is None


def test_known_package_without_kind_is_empty_not_none(ledger: LedgerReader) -> None:
    assert ledger.read_records("pkg-empty", "observation") == ()


def test_records_filtered_by_kind_in_file_order(ledger: LedgerReader) -> None:
    records = ledger.read_records("pkg-a", "observation")
    assert records is not None
    assert [r["n"] for r in records] == [3, 4]


def test_reads_are_deterministic(ledger: LedgerReader) -> None:
    assert ledger.list_packages() == ledger.list_packages()
    assert ledger.read_records("pkg-b", "frame") == ledger.read_records("pkg-b", "frame")
