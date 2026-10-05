"""The only way Memory reads packages: a typed ``LedgerReader`` and an in-memory ``StubLedger``.

The real Ledger catalog API (MVL-85) is not built yet. This Protocol is the minimal surface Memory
needs and the seam the real client will implement; ``tests/test_ledger_contract_memory.py`` runs the
same contract against any reader, so the real Ledger drops in by adding a fixture. Nothing in
``neptune_memory`` opens package files or imports ``neptune.store``.

``LedgerExport`` is a Ledger's records with the transaction each package was registered at: the
input of the ``memory`` CLI until the catalog API is adopted (ADR 0016 §4). It is parsed here and
read through ``StubLedger``; the CLI opens the file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence


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


LEDGER_EXPORT_KIND: Final = "memory.ledger_export"


@dataclass(frozen=True)
class ExportedPackage:
    """One package of a Ledger export: when the catalog registered it, and its records."""

    package_id: str
    schema_version: int
    registered_at: int
    records: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class LedgerExport:
    """A Ledger's packages with the catalog transaction each was registered at (ADR 0016 §4).

    The stand-in the ``memory`` CLI reads until Memory adopts the catalog API: ``at(snapshot)`` is
    the Ledger as of that transaction, every package registered by then. ``head`` is the latest
    transaction the export covers; a snapshot after it is not knowable from the export.
    """

    head: int
    catalog_api_version: str
    packages: tuple[ExportedPackage, ...]

    def at(self, snapshot: int) -> StubLedger:
        if isinstance(snapshot, bool) or not isinstance(snapshot, int) or snapshot < 1:
            raise ValueError(f"a Ledger snapshot is a transaction number, 1 or more: {snapshot!r}")
        if snapshot > self.head:
            raise ValueError(f"Ledger snapshot {snapshot} is after the export's head {self.head}")
        return StubLedger(
            {
                p.package_id: (p.schema_version, p.records)
                for p in self.packages
                if p.registered_at <= snapshot
            },
            self.catalog_api_version,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "catalog_api_version": self.catalog_api_version,
            "head": self.head,
            "kind": LEDGER_EXPORT_KIND,
            "packages": [
                {
                    "package_id": p.package_id,
                    "records": [dict(r) for r in p.records],
                    "registered_at": p.registered_at,
                    "schema_version": p.schema_version,
                }
                for p in self.packages
            ],
        }


MAX_PACKAGE_ID: Final = 1024


def _positive(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be an integer, 1 or more")
    return value


def _keys(value: object, what: str, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a JSON object")
    if set(value) != keys:
        raise ValueError(f"{what}: keys must be {sorted(keys)}, got {sorted(value)}")
    return value


def ledger_export_from_json(data: object) -> LedgerExport:
    """A Ledger export, strictly: unknown or missing keys, bad ids or records are ``ValueError``."""
    obj = _keys(data, "ledger export", {"catalog_api_version", "head", "kind", "packages"})
    if obj["kind"] != LEDGER_EXPORT_KIND:
        raise ValueError(f"ledger export kind must be {LEDGER_EXPORT_KIND!r}")
    head = _positive(obj["head"], "head")
    api = obj["catalog_api_version"]
    if not isinstance(api, str) or not api:
        raise ValueError("catalog_api_version must be a non-empty string")
    packages = obj["packages"]
    if not isinstance(packages, list):
        raise ValueError("packages must be an array")
    out: list[ExportedPackage] = []
    for entry in packages:
        item = _keys(entry, "package", {"package_id", "records", "registered_at", "schema_version"})
        package_id = item["package_id"]
        if (
            not isinstance(package_id, str)
            or not package_id.isprintable()
            or not 0 < len(package_id) <= MAX_PACKAGE_ID
        ):
            raise ValueError(
                f"package_id must be printable text, 1 to 1024 characters: {package_id!r}"
            )
        registered_at = _positive(item["registered_at"], "registered_at")
        if registered_at > head:
            raise ValueError(f"{package_id} is registered at {registered_at}, after the head")
        records = item["records"]
        if not isinstance(records, list):
            raise ValueError(f"{package_id}: records must be an array")
        for record in records:
            if not isinstance(record, Mapping) or not isinstance(record.get("kind"), str):
                raise ValueError(f"{package_id}: every record is an object with a string kind")
        out.append(
            ExportedPackage(
                package_id,
                _positive(item["schema_version"], "schema_version"),
                registered_at,
                tuple(records),
            )
        )
    ids = [p.package_id for p in out]
    if ids != sorted(set(ids)):
        raise ValueError("packages must be unique and ordered by package_id")
    return LedgerExport(head, api, tuple(out))
