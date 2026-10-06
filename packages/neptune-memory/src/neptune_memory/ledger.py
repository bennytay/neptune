"""The only way Memory reads packages: a typed ``LedgerReader`` and an in-memory ``StubLedger``.

This Protocol is the minimal surface Memory needs of the Ledger catalog API; the real client
implements it. ``tests/test_ledger_contract_memory.py`` runs the same contract against any reader.
Nothing in ``neptune_memory`` opens package files or imports ``neptune.store``.

Thread membership is the catalog API's ``threads_of`` (catalog-api 1.7.0, Ledger ADR 0003 §1.4):
``ThreadsOf`` mirrors its answer without the bookkeeping fields (``api_version``, ``as_of``,
``findings``), which say when and by which version it was answered, not what it says (ADR 0018).
Memory parses the published wire form itself and never imports the Ledger.

``LedgerExport`` is a Ledger's records with the transaction each package was registered at, and,
from catalog-api 1.7.0, the Ledger's ``threads_of`` answer for each record at the export's head:
the input of the ``memory`` CLI (ADR 0016 §4, ADR 0018 §1). It is parsed here and read through
``StubLedger``; the CLI opens the file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from neptune.model.ids import LogicalId, logical_id_from_json, parse_content_id
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

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

    def threads_of(self, record_id: str) -> ThreadsOf | None:
        """The catalog's ``threads_of(record_id)`` at this reader's snapshot (ADR 0018 §1).

        ``None`` when this reader answers no thread queries (a stand-in Ledger, or an export made
        without them): thread membership is then not covered, never "in no thread". A record the
        Ledger does not hold is ``status == "unknown_record"`` with no memberships.
        """
        ...


# Catalog-api ``ThreadKey.kind`` values (Ledger ADR 0003 §2).
THREAD_KINDS: Final = frozenset(
    {
        "asset",
        "configuration",
        "document",
        "machine",
        "person",
        "run",
        "sensor",
        "site",
        "software_version",
        "stream",
        "task",
        "zone",
    }
)
ROLES: Final = frozenset({"cites", "part_of", "subject"})
Status = Literal["found", "unknown_record"]


@dataclass(frozen=True, slots=True)
class ThreadKey:
    """A catalog ``ThreadKey``: ``declared`` (a ``LogicalId``) or ``anchor`` (a record-level
    evidence ref), exactly one of them (Ledger ADR 0003 §1)."""

    kind: str
    declared: LogicalId | None
    anchor: EvidenceRef | None


@dataclass(frozen=True, slots=True)
class Membership:
    """One thread a record is a member of, in one registering package, with its roles."""

    thread_id: str
    key: ThreadKey
    package_id: str
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnresolvedMembership:
    """A thread a record's ``Ambiguous`` field (at ``pointer``) names as one candidate."""

    thread_id: str
    key: ThreadKey
    package_id: str
    pointer: str


@dataclass(frozen=True, slots=True)
class ThreadsOf:
    """The catalog's answer to ``threads_of(record_id)``: memberships sorted by thread id then
    package id. ``found`` with no memberships is a record the Ledger holds in no thread."""

    record_id: str
    status: Status
    memberships: tuple[Membership, ...]
    unresolved: tuple[UnresolvedMembership, ...]

    def at(self, packages: frozenset[str], held: bool) -> ThreadsOf:
        """This answer as of a snapshot holding ``packages``; ``held``: the record is in one."""
        if not held:
            return ThreadsOf(self.record_id, "unknown_record", (), ())
        return ThreadsOf(
            self.record_id,
            self.status,
            tuple(m for m in self.memberships if m.package_id in packages),
            tuple(u for u in self.unresolved if u.package_id in packages),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "memberships": [
                {
                    "key": _key_json(m.key),
                    "package_id": m.package_id,
                    "roles": list(m.roles),
                    "thread_id": m.thread_id,
                }
                for m in self.memberships
            ],
            "record_id": self.record_id,
            "status": self.status,
            "unresolved": [
                {
                    "key": _key_json(u.key),
                    "package_id": u.package_id,
                    "pointer": u.pointer,
                    "thread_id": u.thread_id,
                }
                for u in self.unresolved
            ],
        }


def _key_json(key: ThreadKey) -> dict[str, object]:
    inner = key.declared.to_json() if key.declared is not None else key.anchor.to_json()  # type: ignore[union-attr]
    return {"key": inner, "kind": key.kind}


def unknown_record(record_id: str) -> ThreadsOf:
    return ThreadsOf(record_id, "unknown_record", (), ())


class StubLedger:
    """In-memory Ledger for tests and CI, built from ``{package_id: (schema_version, records)}``.

    ``threads`` holds the catalog's ``threads_of`` answers by record id; ``None`` (the default)
    is a Ledger that answers no thread queries. With answers, a record id without one is a record
    the Ledger does not hold.
    """

    def __init__(
        self,
        packages: Mapping[str, tuple[int, Sequence[Mapping[str, object]]]],
        catalog_api_version: str = "stub",
        threads: Mapping[str, ThreadsOf] | None = None,
    ) -> None:
        self._packages = dict(packages)
        self._catalog_api_version = catalog_api_version
        self._threads = None if threads is None else dict(threads)

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

    def threads_of(self, record_id: str) -> ThreadsOf | None:
        if self._threads is None:
            return None
        return self._threads.get(record_id) or unknown_record(record_id)


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

    What the ``memory`` CLI reads: ``at(snapshot)`` is the Ledger as of that transaction, every
    package registered by then. ``head`` is the latest transaction the export covers; a snapshot
    after it is not knowable from the export. ``threads`` holds the catalog's ``threads_of``
    answer for each record, at ``head``, sorted by record id (ADR 0018 §1); ``None`` for an export
    made without them, whose Ledger then answers no thread queries.
    """

    head: int
    catalog_api_version: str
    packages: tuple[ExportedPackage, ...]
    threads: tuple[ThreadsOf, ...] | None = None

    def at(self, snapshot: int) -> StubLedger:
        if isinstance(snapshot, bool) or not isinstance(snapshot, int) or snapshot < 1:
            raise ValueError(f"a Ledger snapshot is a transaction number, 1 or more: {snapshot!r}")
        if snapshot > self.head:
            raise ValueError(f"Ledger snapshot {snapshot} is after the export's head {self.head}")
        held = [p for p in self.packages if p.registered_at <= snapshot]
        threads: dict[str, ThreadsOf] | None = None
        if self.threads is not None:
            ids = frozenset(p.package_id for p in held)
            records = {r.get("id") for p in held for r in p.records}
            threads = {t.record_id: t.at(ids, t.record_id in records) for t in self.threads}
        return StubLedger(
            {p.package_id: (p.schema_version, p.records) for p in held},
            self.catalog_api_version,
            threads,
        )

    def to_json(self) -> dict[str, object]:
        document: dict[str, object] = {
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
        if self.threads is not None:
            document["threads"] = [t.to_json() for t in self.threads]
        return document


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


def _text(value: object, what: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isprintable()
        or not 0 < len(value) <= MAX_PACKAGE_ID
    ):
        raise ValueError(f"{what} must be printable text, 1 to {MAX_PACKAGE_ID} characters")
    return value


def _thread_key(obj: Mapping[str, object], what: str) -> tuple[str, ThreadKey]:
    """``(thread_id, key)`` of a catalog ``Membership`` or ``UnresolvedMembership`` object."""
    thread_id = obj["thread_id"]
    if not isinstance(thread_id, str):
        raise ValueError(f"{what}: thread_id must be a content id")
    parse_content_id(thread_id)
    key = _keys(obj["key"], f"{what}: key", {"key", "kind"})
    kind = key["kind"]
    if kind not in THREAD_KINDS:
        raise ValueError(f"{what}: key kind must be one of {sorted(THREAD_KINDS)}: {kind!r}")
    inner = key["key"]
    try:
        if isinstance(inner, Mapping) and set(inner) == {"namespace", "value"}:
            return thread_id, ThreadKey(str(kind), logical_id_from_json(dict(inner)), None)
        return thread_id, ThreadKey(str(kind), None, evidence_ref_from_json(inner))  # type: ignore[arg-type]
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            f"{what}: key is neither a declared id nor an evidence anchor: {exc}"
        ) from exc


def threads_of_from_json(data: object) -> ThreadsOf:
    """A ``threads_of`` answer, strictly: catalog-api 1.7.0's ``ThreadsOf`` without ``api_version``,
    ``as_of`` and ``findings`` (ADR 0018 §1). Anything else is ``ValueError``."""
    obj = _keys(data, "threads_of", {"memberships", "record_id", "status", "unresolved"})
    record_id = _text(obj["record_id"], "threads_of record_id")
    status = obj["status"]
    if status not in ("found", "unknown_record"):
        raise ValueError(f"{record_id}: status must be found or unknown_record: {status!r}")
    memberships, unresolved = obj["memberships"], obj["unresolved"]
    if not isinstance(memberships, list) or not isinstance(unresolved, list):
        raise ValueError(f"{record_id}: memberships and unresolved must be arrays")
    found: list[Membership] = []
    for item in memberships:
        entry = _keys(item, f"{record_id} membership", {"key", "package_id", "roles", "thread_id"})
        thread_id, key = _thread_key(entry, f"{record_id} membership")
        package_id = entry["package_id"]
        if not isinstance(package_id, str):
            raise ValueError(f"{record_id}: membership package_id must be a content id")
        parse_content_id(package_id)
        roles = entry["roles"]
        if (
            not isinstance(roles, list)
            or not roles
            or len(set(roles)) != len(roles)
            or not set(roles) <= ROLES
        ):
            raise ValueError(f"{record_id}: roles must be distinct, from {sorted(ROLES)}")
        found.append(Membership(thread_id, key, package_id, tuple(roles)))
    named: list[UnresolvedMembership] = []
    for item in unresolved:
        entry = _keys(
            item, f"{record_id} unresolved", {"key", "package_id", "pointer", "thread_id"}
        )
        thread_id, key = _thread_key(entry, f"{record_id} unresolved")
        package_id, pointer = entry["package_id"], entry["pointer"]
        if not isinstance(package_id, str) or not isinstance(pointer, str):
            raise ValueError(f"{record_id}: unresolved package_id and pointer must be strings")
        parse_content_id(package_id)
        named.append(UnresolvedMembership(thread_id, key, package_id, pointer))
    if status == "unknown_record" and (found or named):
        raise ValueError(f"{record_id}: a record the Ledger does not hold is in no thread")
    if [(m.thread_id, m.package_id) for m in found] != sorted(
        {(m.thread_id, m.package_id) for m in found}
    ):
        raise ValueError(f"{record_id}: memberships must be unique, by thread id then package id")
    return ThreadsOf(record_id, status, tuple(found), tuple(named))


def _threads(value: object) -> tuple[ThreadsOf, ...]:
    if not isinstance(value, list):
        raise ValueError("threads must be an array")
    answers = tuple(threads_of_from_json(item) for item in value)
    ids = [t.record_id for t in answers]
    if ids != sorted(set(ids)):
        raise ValueError("threads must be unique and ordered by record_id")
    return answers


def ledger_export_from_json(data: object) -> LedgerExport:
    """A Ledger export, strictly: unknown or missing keys, bad ids or records are ``ValueError``.
    ``threads`` is optional (ADR 0018 §1); every other key is required."""
    base = {"catalog_api_version", "head", "kind", "packages"}
    keys = base | {"threads"} if isinstance(data, Mapping) and "threads" in data else base
    obj = _keys(data, "ledger export", keys)
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
    threads = _threads(obj["threads"]) if "threads" in obj else None
    return LedgerExport(head, api, tuple(out), threads)
