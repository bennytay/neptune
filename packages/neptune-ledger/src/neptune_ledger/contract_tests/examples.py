"""The four worked-example packages, and what the catalog contract says about each of them.

The packages are built by the compiler itself (``neptune.store.package``) from its committed worked
examples (``tests/fixtures/model/<name>/records/``: a drone, a manipulator, a mobile robot and a
quadruped), so they are byte-identical to the compiler's golden packages. Everything an
implementation must answer about them is derived here from the package bytes by the rules of
Ledger ADRs 0002 and 0003, never from an implementation.

Set ``NEPTUNE_WORKED_EXAMPLES`` to the examples directory when running outside this repository.
"""

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from neptune.identity import canonical_json
from neptune.model.kinds import RECORD_KINDS
from neptune.store.package import MANIFEST, blob_path, package_files, write_package
from neptune_ledger.api.types import (
    DeclaredKey,
    EvidenceAnchor,
    KindCount,
    RecordRef,
    ThreadKey,
    WorldTime,
)

EXAMPLES: Final = ("drone", "manipulator", "mobile_robot", "quadruped")
ENV: Final = "NEPTUNE_WORKED_EXAMPLES"
Record = Mapping[str, Any]

# ADR 0003 §2: the fields through which a record cites a machine thread.
MACHINE_CITES: Final = (
    "run",
    "hardware_configuration",
    "software_configuration",
    "calibration",
)


def examples_dir() -> Path:
    """The compiler's worked-examples directory: ``$NEPTUNE_WORKED_EXAMPLES`` or found upward."""
    configured = os.environ.get(ENV)
    if configured:
        candidates = [Path(configured)]
    else:
        here = Path(__file__).resolve().parents
        cwd = Path.cwd().resolve()
        candidates = [p / "tests" / "fixtures" / "model" for p in (*here, cwd, *cwd.parents)]
    for candidate in candidates:
        if all((candidate / name / "records").is_dir() for name in EXAMPLES):
            return candidate
    raise FileNotFoundError(
        f"the compiler's worked examples were not found; set {ENV} to tests/fixtures/model"
    )


def package_bytes(name: str, directory: Path | None = None) -> dict[str, bytes]:
    """Every file of one worked-example package, built by the compiler's own package writer."""
    root = (directory or examples_dir()) / name / "records"
    records: list[Any] = []
    for path in sorted(root.glob("*.jsonl")):
        _, read = RECORD_KINDS[path.stem]
        records += [read(canonical_json.loads(line)) for line in path.read_bytes().splitlines()]
    return package_files(records)


@dataclass(frozen=True)
class WorkedPackage:
    """One materialised package: where it is, its id, its files and its records by kind."""

    name: str
    root: Path
    package_id: str
    files: Mapping[str, bytes]

    @property
    def manifest(self) -> Record:
        value = canonical_json.loads(self.files[MANIFEST])
        assert isinstance(value, Mapping)
        return value

    def records(self, kind: str) -> list[Record]:
        data = self.files.get(f"records/{kind}.jsonl", b"")
        out = [canonical_json.loads(line) for line in data.splitlines()]
        return [r for r in out if isinstance(r, Mapping)]

    def every_record(self) -> Iterator[tuple[str, int, Record]]:
        """``(kind, 1-based line, record)`` for every record, kinds in name order."""
        for kind in sorted(RECORD_KINDS):
            for line, record in enumerate(self.records(kind), start=1):
                yield kind, line, record

    def ref(self, kind: str, line: int, record: Record) -> RecordRef:
        return RecordRef(self.package_id, kind, record_key(record), line)

    def record_counts(self) -> tuple[KindCount, ...]:
        """Every kind with at least one record, sorted by kind (``Registration.record_counts``)."""
        counts = ((kind, len(self.records(kind))) for kind in sorted(RECORD_KINDS))
        return tuple(KindCount(kind, count) for kind, count in counts if count)

    def listed_files(self) -> int:
        return len(self.manifest["files"])

    def source(self, content_id: str) -> Record | None:
        for entry in self.manifest["sources"]:
            if entry["content_id"] == content_id:
                return entry  # type: ignore[no-any-return]
        return None

    def blob(self, content_id: str) -> str:
        return blob_path(content_id)  # type: ignore[arg-type]

    def locations(self, content_id: str) -> tuple[Record, ...]:
        """The locations this package's source revisions state for the content, in table order."""
        revisions = self.records("source_revision")
        return tuple(r["location"] for r in revisions if r["content_id"] == content_id)


def materialise(name: str, root: Path, directory: Path | None = None) -> WorkedPackage:
    """Write one worked-example package into ``root`` (which must not exist or be empty)."""
    files = package_bytes(name, directory)
    package_id = write_package(root, files)
    return WorkedPackage(name, root, package_id, files)


# --- What the contract says about a record (ADR 0002 §5, ADR 0003 §2-§3) ------------------------


def record_key(record: Record) -> str:
    """The table key: ``id``, or ``content_id`` for a source artifact."""
    key = record.get("id", record.get("content_id"))
    assert isinstance(key, str)
    return key


def evidence_anchor(record: Record) -> EvidenceAnchor | None:
    """The record-level evidence anchor, when the record cites a content-id source."""
    provenance = record.get("provenance")
    if not isinstance(provenance, Mapping) or record.get("kind") == "ingest_finding":
        return None
    evidence = provenance.get("evidence")
    if not isinstance(evidence, Mapping) or not isinstance(evidence.get("source"), str):
        return None
    return EvidenceAnchor(evidence["source"], tuple(evidence["locator"]))


def transform_of(record: Record) -> str | None:
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping) and isinstance(provenance.get("transform"), str):
        return provenance["transform"]  # type: ignore[no-any-return]
    return None


def _known(value: Any) -> Any | None:
    if isinstance(value, Mapping) and value.get("knowledge") == "known":
        return value["value"]
    return None


def _grounded(value: Any) -> bool:
    """Known, with stated or observed provenance (its own, or inherited from the record)."""
    if _known(value) is None:
        return False
    provenance = value.get("provenance")
    return provenance is None or provenance.get("assertion_kind") in ("observed", "stated")


def world_time(record: Record) -> WorldTime | str | None:
    """ADR 0003 §3: a ``WorldTime``; ``"unknown"`` when no bound is Known; ``None`` if the kind
    has no world time."""
    kind = record["kind"]
    if kind in ("run", "stream"):
        start, end = _known(record.get("first")), _known(record.get("last"))
    elif kind == "calibration":
        start = _known(record.get("valid_from"))
        end = _known(record.get("valid_until"))
        if start is None:
            start = _known(record.get("performed"))
            if end is None and start is not None:
                end = start
    else:
        return None
    if start is None:
        start = end
    if start is None:
        return "unknown"
    if end is None or end["domain_id"] != start["domain_id"]:
        return WorldTime(start["domain_id"], start["ticks"])
    return WorldTime(start["domain_id"], start["ticks"], end["ticks"])


def machine_keys(record: Record) -> list[tuple[str, DeclaredKey]]:
    """``(role, key)`` of every machine thread a record joins (ADR 0003 §2, machine row)."""
    out: list[tuple[str, DeclaredKey]] = []
    if record["kind"] == "machine":
        for identifier in record.get("identifiers", []):
            value = _known(identifier)
            if value is not None and _grounded(identifier):
                out.append(("subject", DeclaredKey(value["namespace"], value["value"])))
    elif record["kind"] in MACHINE_CITES:
        field = record.get("machine")
        value = _known(field)
        if value is not None and _grounded(field):
            out.append(("cites", DeclaredKey(value["namespace"], value["value"])))
    return out


def machine_threads(packages: list[WorkedPackage]) -> dict[ThreadKey, set[tuple[str, str, str]]]:
    """Every machine thread over ``packages``: ``{key: {(package id, record id, role)}}``."""
    threads: dict[ThreadKey, set[tuple[str, str, str]]] = {}
    for package in packages:
        for _, _, record in package.every_record():
            for role, declared in machine_keys(record):
                key = ThreadKey("machine", declared)
                threads.setdefault(key, set()).add((package.package_id, record_key(record), role))
    return threads
