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
from typing import Any, Final, cast

from neptune.identity import canonical_json
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import Knowledge, Known
from neptune.store.package import MANIFEST, blob_path, package_files, write_package
from neptune_ledger.api import codec
from neptune_ledger.api.types import (
    DeclaredKey,
    EvidenceAnchor,
    KindCount,
    QueryRow,
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


def world_time(record: Record) -> Record:
    """ADR 0003 §3: the JSON of ``ThreadEntry.world`` for a record.

    ``not_applicable`` for kinds without world time, ``unknown`` when no bound is Known, else
    ``Known(WorldTime)`` whose ``end`` is the package field the end comes from, verbatim.
    """
    kind = record["kind"]
    if kind in ("run", "stream"):
        start_field, end_field = record.get("first"), record.get("last")
    elif kind == "calibration":
        start_field, end_field = record.get("valid_from"), record.get("valid_until")
        if _known(start_field) is None:
            start_field = record.get("performed")
            if _known(end_field) is None and _known(start_field) is not None:
                end_field = start_field
    else:
        return {"knowledge": "not_applicable"}
    if _known(start_field) is None:
        start_field = end_field  # a point at the end
    start = _known(start_field)
    if start is None:
        return {"knowledge": "unknown"}
    point = {"domain_id": start["domain_id"], "ticks": start["ticks"]}
    return {"knowledge": "known", "value": {"end": end_field, "start": point}}


_WORLD: Final[Any] = cast("Any", Knowledge)[WorldTime]


def world_value(record: Record) -> Knowledge[WorldTime]:
    """``world_time`` as a value."""
    return codec.decode_as(_WORLD, world_time(record))  # type: ignore[no-any-return]


def timed(record: Record) -> WorldTime | None:
    """The record's ``WorldTime`` when it has a Known one."""
    value = world_value(record)
    return value.value if isinstance(value, Known) else None


def query_row(package: "WorkedPackage", kind: str, line: int, record: Record, seq: int) -> QueryRow:
    """The ``query`` row a record gives (ADR 0002 §5 columns; world time per ADR 0003 §3)."""
    anchor = evidence_anchor(record)
    world = timed(record)
    return QueryRow(
        kind=kind,
        record_id=record_key(record),
        package_id=package.package_id,
        line=line,
        registration_seq=seq,
        transform_id=transform_of(record),
        source_content_id=anchor.source if anchor else None,
        source_locator=canonical_json.dumps(list(anchor.locator)).decode() if anchor else None,
        assertion_kind=record["provenance"]["assertion_kind"],
        world_clock=world.clock if world else None,
        world_first=world.start.ticks if world else None,
        world_last=world.closed_end if world else None,
    )


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


# --- Synthetic lineage siblings and conflicts, built deterministically from a worked example ----

# Kinds whose ids do not come from a transform (or, for findings, from their own content).
_NOT_TRANSFORMED: Final = frozenset(
    {"source_artifact", "source_revision", "source_absence", "transform_record", "ingest_finding"}
)


def _replace_ids(value: Any, ids: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, list):
        return [_replace_ids(v, ids) for v in value]
    if isinstance(value, Mapping):
        return {k: _replace_ids(v, ids) for k, v in value.items()}
    return value


def _read(kind: str, data: Any) -> Any:
    _, read = RECORD_KINDS[kind]
    return read(data)


def reparse(
    name: str, adapter_version: str, config: Mapping[str, Any], directory: Path | None = None
) -> dict[str, bytes]:
    """The package one worked example gives when its adapter runs at another version or config.

    The same sources and locators, every record re-identified under the new transform (root ADR
    0003 tier 2), every tier-2 reference rewritten to match: lineage siblings of the original, as
    adapter v2 or a second config would produce them (ADR 0003 §4.1). Ingest findings are left
    out; their ids hash their own content. Only single-transform examples whose locators hold no
    tier-2 id qualify; anything else raises.
    """
    root = (directory or examples_dir()) / name / "records"
    tables = {
        path.stem: [canonical_json.loads(line) for line in path.read_bytes().splitlines()]
        for path in sorted(root.glob("*.jsonl"))
    }
    (stated,) = tables["transform_record"]
    old: Any = stated
    new = transform_record(
        adapter_id=old["adapter_id"],
        adapter_version=adapter_version,
        config=config,
        libraries=old["libraries"],
        upstream=old["upstream"],
    )
    ids: dict[str, str] = {old["id"]: new.id}
    for kind, rows in tables.items():
        if kind in _NOT_TRANSFORMED:
            continue
        for row in rows:
            evidence = _read(kind, row).provenance.evidence
            if "rec:" in canonical_json.dumps(evidence.locator_json()).decode():
                raise ValueError(f"{name}: a {kind} locator holds a tier-2 id; cannot reparse")
            ids[cast("Any", row)["id"]] = evidence_record_id(kind, evidence, new)
    records: list[Any] = [new]
    for kind, rows in tables.items():
        if kind in ("transform_record", "ingest_finding"):
            continue
        records += [_read(kind, _replace_ids(row, ids)) for row in rows]
    return package_files(records)


def with_source_size(name: str, size: int, directory: Path | None = None) -> dict[str, bytes]:
    """A worked example whose single source artifact states another ``size``.

    Same content id, different fields: registering it after the original must be refused with
    ``conflicting_id`` (ADR 0002 §6).
    """
    files = package_bytes(name, directory)
    records: list[Any] = []
    for path, data in sorted(files.items()):
        if not (path.startswith("records/") and path.endswith(".jsonl")):
            continue
        kind = path.removeprefix("records/").removesuffix(".jsonl")
        for line in data.splitlines():
            row = canonical_json.loads(line)
            assert isinstance(row, Mapping)
            if kind == "source_artifact":
                row = {**row, "size": size}
            records.append(_read(kind, row))
    return package_files(records)


def write(name: str, root: Path, files: Mapping[str, bytes]) -> WorkedPackage:
    """Write prepared package files into ``root``."""
    return WorkedPackage(name, root, write_package(root, files), files)
