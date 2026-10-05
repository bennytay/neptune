"""Compiler-shaped run records for tests, built with the compiler's own types.

Every ``run``, ``run_assembly``, ``source_revision``, ``clock_mapping``, ``timestamp_domain`` and
``site`` here is constructed as the compiler model class and serialised with its ``to_json``, so a
test can never feed the run consolidator a shape the compiler would not write (root ADRs 0018,
0050, 0066). ``run_declaration`` is the Ledger stand-in for what a manifest says a run involved
(Memory ADR 0009 §1). A file's bytes are named by a string: ``source(name)`` is its content id,
and the run a recording declares cites that content.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final

from memory_identity_records import OBSERVED, STATED, Record, cite, provenance, rid, source
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.identity.revisions import revision_id
from neptune.model.alignment import (
    ClockAnchor,
    ClockMapping,
    MappingMethod,
    MemberRole,
    RunAssembly,
    RunMember,
    ValidityWindow,
)
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import Provenance
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run
from neptune.model.source import LocalPath, SourceRevision
from neptune.model.time import Duration, Epoch, Timescale, Timestamp
from neptune.model.world import Site

if TYPE_CHECKING:
    from collections.abc import Sequence

# The grouping producer at its assembly version (root ADR 0066 §7): the rule's version.
GROUPING: Final = transform_record(
    adapter_id="neptune.grouping", adapter_version="0.2.0", config={}
)
NS: Final = Fraction(1, 10**9)


def domain(name: str, *, civil: bool, resolution: Fraction = NS) -> tuple[Record, RecordId]:
    """A ``timestamp_domain``: civil (POSIX since the Unix epoch) or a boot clock (no epoch)."""
    declared = provenance(cite(f"clock {name}"))
    record = TimestampDomain(
        id=rid("timestamp_domain", declared.evidence),
        provenance=declared,
        field="log_time",
        scope=(),
        role=Unknown(),
        resolution=Known(resolution),
        epoch=Known(Epoch.UNIX) if civil else Unknown(),
        timescale=Known(Timescale.POSIX) if civil else Unknown(),
        declared_monotonic=Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def revision(name: str) -> tuple[Record, RecordId]:
    """The ``source_revision`` of the file ``name``: its bytes are ``source(name)``."""
    location = LocalPath(name)
    content = source(name)
    record = SourceRevision(revision_id(location, content, ()), location, content, ())
    return record.to_json(), record.id  # type: ignore[return-value]


def _ids(value: LogicalId | Sequence[LogicalId] | None, name: str) -> Knowledge[LogicalId]:
    if value is None:
        return Unknown()
    if isinstance(value, LogicalId):
        return Known(value)
    return Ambiguous(
        tuple(Candidate(v, provenance(cite(name, 128 + 8 * i, 8))) for i, v in enumerate(value))
    )


def run(
    name: str,
    *,
    first: Timestamp | None = None,
    last: Timestamp | None = None,
    machine: LogicalId | Sequence[LogicalId] | None = None,
    logical_id: LogicalId | Sequence[LogicalId] | None = None,
    kind: AssertionKind = OBSERVED,
) -> tuple[Record, RecordId]:
    """The ``Run`` the file ``name`` declares (its header cites the file's bytes)."""
    declared = provenance(cite(name), kind)
    record = Run(
        id=rid("run", declared.evidence),
        provenance=declared,
        logical_id=_ids(logical_id, name),
        machine=_ids(machine, name),
        first=Known(first) if first is not None else Unknown(),
        last=Known(last) if last is not None else Unknown(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def assembly(
    name: str,
    run_id: RecordId,
    members: Sequence[tuple[str, MemberRole]],
    *,
    rule: str = "rosbag2.metadata",
    kind: AssertionKind = STATED,
) -> tuple[Record, RecordId]:
    """A ``RunAssembly`` whose evidence is ``name`` (a file list): files by name and role."""
    declared = Provenance(cite(name), GROUPING.id, kind)
    found = sorted(
        (
            RunMember(revision(file)[1], role, cite(name, 64 + 16 * i, 16))
            for i, (file, role) in enumerate(members)
        ),
        key=lambda m: m.revision,
    )
    record = RunAssembly(
        id=evidence_record_id("run_assembly", declared.evidence, GROUPING),
        provenance=declared,
        run=run_id,
        rule=rule,
        members=tuple(found),
        validity=NotApplicable(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


def mapping(
    name: str,
    source_clock: RecordId,
    target_clock: RecordId,
    *,
    anchor: tuple[int, int],
    rate: Fraction = Fraction(1),
    bound: int | None = None,
    window: tuple[int | None, int | None] | None = None,
    kind: AssertionKind = STATED,
) -> Record:
    """A stated ``ClockMapping``: ``anchor`` is one instant as (source ticks, target ticks)."""
    declared = provenance(cite(name), kind)
    validity: Knowledge[ValidityWindow] = NotCovered()
    if window is not None:

        def bound_(t: int | None) -> Knowledge[Timestamp]:
            return Known(Timestamp(t, source_clock)) if t is not None else Unknown()

        validity = Known(ValidityWindow(source_clock, bound_(window[0]), bound_(window[1])))
    return ClockMapping(  # type: ignore[return-value]
        id=rid("clock_mapping", declared.evidence),
        provenance=declared,
        source=source_clock,
        target=target_clock,
        method=MappingMethod.STATED,
        anchor=Known(
            ClockAnchor(Timestamp(anchor[0], source_clock), Timestamp(anchor[1], target_clock))
        ),
        rate=Known(rate),
        residual_bound=Known(Duration(bound, target_clock)) if bound is not None else Unknown(),
        validity=validity,
    ).to_json()


def site(name: str, *ids: LogicalId) -> Record:
    """A site register row declaring ``ids``."""
    declared = provenance(cite(name), STATED)
    return Site(  # type: ignore[return-value]
        id=rid("site", declared.evidence),
        provenance=declared,
        identifiers=tuple(Known(i) for i in sorted(ids, key=lambda i: (i.namespace, i.value))),
        name=Unknown(),
        aliases=(),
        parent=NotApplicable(),
        location=Unknown(),
    ).to_json()


def _knowledge(value: LogicalId | Sequence[LogicalId] | None) -> Record:
    if value is None:
        return {"knowledge": "unknown"}
    if isinstance(value, LogicalId):
        return {"knowledge": "known", "value": value.to_json()}
    return {"knowledge": "ambiguous", "candidates": [{"value": v.to_json()} for v in value]}


def declaration(
    name: str,
    run_id: LogicalId,
    *,
    machine: LogicalId | Sequence[LogicalId] | None = None,
    site: LogicalId | Sequence[LogicalId] | None = None,
    task: LogicalId | Sequence[LogicalId] | None = None,
) -> Record:
    """A ``run_declaration`` stand-in: a manifest's run entry ``name``."""
    return {
        "kind": "run_declaration",
        "id": rid("run_declaration", cite(f"manifest {name}")),
        "run": run_id.to_json(),
        "machine": _knowledge(machine),
        "site": _knowledge(site),
        "task": _knowledge(task),
        "evidence": [cite(f"manifest {name}").to_json()],
    }


def by_record(run_id: RecordId) -> LogicalId:
    """How a declaration names a run that declares no logical id."""
    return LogicalId("record", run_id)
