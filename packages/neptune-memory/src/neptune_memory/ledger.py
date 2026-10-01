"""The only way Memory reads packages: a typed ``LedgerReader`` and an in-memory ``StubLedger``.

The real Ledger catalog API (MVL-85) is not built yet. This Protocol is the minimal surface Memory
needs and the seam the real client will implement; ``tests/test_ledger_contract_memory.py`` runs the
same contract against any reader, so the real Ledger drops in by adding a fixture. Nothing in
``neptune_memory`` opens package files or imports ``neptune.store``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


@dataclass(frozen=True, slots=True)
class PackageRef:
    """One ingest package as the Ledger lists it."""

    package_id: str
    schema_version: int


@runtime_checkable
class LedgerReader(Protocol):
    """Read-only catalog access. Results are deterministic: same Ledger state, same order."""

    @property
    def catalog_api_version(self) -> str:
        """The catalog API version this reader implements."""
        ...

    def list_packages(self) -> Sequence[PackageRef]:
        """Every package, ordered by ``package_id``."""
        ...

    def read_records(self, package_id: str, kind: str) -> Sequence[Mapping[str, object]] | None:
        """Records of ``kind`` in file order; ``None`` if the package is unknown.

        ``None`` (not in the Ledger) and ``()`` (in the Ledger, none of that kind) are different
        facts and must never be conflated.
        """
        ...


class StubLedger:
    """In-memory Ledger for tests and CI, built from ``{package_id: (schema_version, records)}``."""

    def __init__(
        self,
        packages: Mapping[str, tuple[int, Sequence[Mapping[str, object]]]],
        catalog_api_version: str = "stub",
    ) -> None:
        self._packages = dict(packages)
        self._catalog_api_version = catalog_api_version

    @property
    def catalog_api_version(self) -> str:
        return self._catalog_api_version

    def list_packages(self) -> Sequence[PackageRef]:
        return tuple(
            PackageRef(package_id, version)
            for package_id, (version, _) in sorted(self._packages.items())
        )

    def read_records(self, package_id: str, kind: str) -> Sequence[Mapping[str, object]] | None:
        entry = self._packages.get(package_id)
        if entry is None:
            return None
        return tuple(record for record in entry[1] if record.get("kind") == kind)
