"""Consolidating one Ledger snapshot into a graph, and the record of it (ADR 0016 §1, §3).

A ``Registration`` names a consolidator, its resolved config, the consolidators whose claims it
reads (``after``) and its resolver priority. ``plan`` orders a set of registrations by dependency,
ties broken by consolidator id, so the order they were registered in never matters. ``consolidate``
runs the plan over one Ledger snapshot: each consolidator sees, as ``previous``, only the claims of
the consolidators it reads, directly or through them, sorted by id. It returns every
``Consolidation`` and a ``MemorySnapshot``: the snapshot's transaction, its packages, the
consolidator set with versions and config hashes, and the count and content hash of the claims and
findings. ``extend`` folds a run into a graph document with its builds (ADR 0007 §5), so an
incremental build and a full rebuild hold the same claim set.

Pure: no clock, randomness, files or environment. Same snapshot, registrations and configs give
byte-identical snapshots and documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Final

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.ids import check_token
from neptune_memory.consolidate.base import (
    EVENTS_CONSOLIDATOR_ID,
    Consolidation,
    Consolidator,
    run_consolidator,
    skip_consolidator,
)
from neptune_memory.consolidate.calibration import CalibrationHistoryConsolidator
from neptune_memory.consolidate.configuration import (
    CONFIGURATION_CONSOLIDATOR_ID,
    ConfigurationLineageConsolidator,
)
from neptune_memory.consolidate.coverage import CoverageConsolidator
from neptune_memory.consolidate.episodes import EpisodeConsolidator
from neptune_memory.consolidate.event_records import resolve_config as event_config
from neptune_memory.consolidate.events import EventConsolidator
from neptune_memory.consolidate.identity import IdentityConsolidator
from neptune_memory.consolidate.runs import RUNS_CONSOLIDATOR_ID, RunConsolidator
from neptune_memory.consolidate.time import TimeDomainConsolidator
from neptune_memory.schema import GRAPH_SCHEMA_VERSION
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES, PredicateRegistry
from neptune_memory.schema.supersede import (
    RESOLVER_ID,
    assertions,
    build_order,
    resolve,
    resolver_config,
    resolver_config_hash,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from neptune.model.ids import ConfigHash, ContentId
    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune_memory.ledger import LedgerReader, PackageRef
    from neptune_memory.schema.claim import Claim
    from neptune_memory.schema.interval import LedgerTx

MEMORY_SNAPSHOT_KIND: Final = "memory.snapshot"


class PlanError(ValueError):
    """Registrations that cannot be ordered: a repeated id, an unknown or cyclic ``after``."""


@dataclass(frozen=True)
class Registration:
    """A consolidator as registered: its resolved config, what it reads and its priority."""

    consolidator: Consolidator
    config: Mapping[str, JsonValue] = field(default_factory=dict)
    after: tuple[str, ...] = ()
    priority: int = 0

    def __post_init__(self) -> None:
        check_token("consolidator_id", self.consolidator.consolidator_id)
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise TypeError(f"priority must be an int: {self.priority!r}")
        after = tuple(self.after)
        for name in after:
            check_token("after", name)
        if list(after) != sorted(set(after)):
            raise ValueError("after must be unique and sorted")
        # A deep, canonical copy: the config is hashed into every claim's lineage.
        config = canonical_json.loads(canonical_json.dumps(dict(self.config)))
        object.__setattr__(self, "after", after)
        object.__setattr__(self, "config", config)

    @property
    def consolidator_id(self) -> str:
        return self.consolidator.consolidator_id


def default_registrations() -> tuple[Registration, ...]:
    """The deterministic consolidators of G2, in no particular order (``plan`` orders them)."""
    return (
        # Identity joins the event nodes an assertion names (ADR 0019 §1); events reads nothing.
        Registration(IdentityConsolidator(), after=(EVENTS_CONSOLIDATOR_ID,)),
        Registration(RunConsolidator()),
        Registration(TimeDomainConsolidator()),
        Registration(ConfigurationLineageConsolidator()),
        Registration(CalibrationHistoryConsolidator(), after=(CONFIGURATION_CONSOLIDATOR_ID,)),
        Registration(EpisodeConsolidator(), after=(RUNS_CONSOLIDATOR_ID,)),
        Registration(EventConsolidator(), event_config({})),
        Registration(CoverageConsolidator()),
    )


def plan(registrations: Iterable[Registration]) -> tuple[Registration, ...]:
    """Dependency order; among the ready, the smallest consolidator id first (``PlanError``)."""
    by_id: dict[str, Registration] = {}
    for registration in registrations:
        cid = registration.consolidator_id
        if cid in by_id:
            raise PlanError(f"{cid} is registered twice")
        if cid == RESOLVER_ID:
            raise PlanError(f"{RESOLVER_ID!r} is reserved for the superseding resolver")
        by_id[cid] = registration
    for cid, registration in by_id.items():
        unknown = sorted(set(registration.after) - by_id.keys())
        if unknown:
            raise PlanError(f"{cid} reads consolidators that are not registered: {unknown}")
    ordered: list[Registration] = []
    done: set[str] = set()
    while len(ordered) < len(by_id):
        ready = sorted(cid for cid, r in by_id.items() if cid not in done and set(r.after) <= done)
        if not ready:
            stuck = sorted(by_id.keys() - done)
            raise PlanError(f"consolidators read each other in a cycle: {stuck}")
        ordered.append(by_id[ready[0]])
        done.add(ready[0])
    return tuple(ordered)


def priorities(registrations: Iterable[Registration]) -> dict[str, int]:
    return {r.consolidator_id: r.priority for r in registrations}


def _hash(items: Sequence[JsonValue]) -> ContentId:
    return content_id(canonical_json.dumps(list(items)))


@dataclass(frozen=True)
class ConsolidatorEntry:
    """One consolidator of a snapshot: its transform, what it reads, its priority and output."""

    transform: JsonObject
    after: tuple[str, ...]
    priority: int
    claims: int
    findings: int
    complete: bool

    def to_json(self) -> JsonObject:
        return {
            **self.transform,
            "after": list(self.after),
            "claims": self.claims,
            "complete": self.complete,
            "findings": self.findings,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class MemorySnapshot:
    """What one consolidation of one Ledger snapshot produced (ADR 0016 §1).

    ``consolidators`` are in plan order; ``claims_hash`` is the content hash of every claim's
    canonical content in id order, ``findings_hash`` of every finding in (consolidator, id) order.
    ``generation`` is the resolver configuration hash the claims are resolved under. ``id`` hashes
    all of it, so two snapshots are the same consolidation exactly when their ids are equal.
    """

    ledger_snapshot: LedgerTx
    packages: tuple[PackageRef, ...]
    consolidators: tuple[ConsolidatorEntry, ...]
    claim_count: int
    claims_hash: ContentId
    finding_count: int
    findings_hash: ContentId
    generation: ConfigHash

    def content_json(self) -> JsonObject:
        return {
            "claim_count": self.claim_count,
            "claims_hash": self.claims_hash,
            "consolidators": [entry.to_json() for entry in self.consolidators],
            "finding_count": self.finding_count,
            "findings_hash": self.findings_hash,
            "generation": self.generation,
            "graph_schema_version": GRAPH_SCHEMA_VERSION,
            "kind": MEMORY_SNAPSHOT_KIND,
            "ledger_snapshot": self.ledger_snapshot,
            "packages": [
                {"package_id": p.package_id, "schema_version": p.schema_version}
                for p in self.packages
            ],
        }

    @cached_property
    def id(self) -> ContentId:
        return content_id(canonical_json.dumps(self.content_json()))

    def to_json(self) -> JsonObject:
        return {**self.content_json(), "id": self.id}


@dataclass(frozen=True)
class ConsolidationRun:
    """Every consolidator's output over one snapshot, in plan order, and its ``MemorySnapshot``."""

    snapshot: MemorySnapshot
    consolidations: tuple[Consolidation, ...]
    priorities: Mapping[str, int]

    @property
    def claims(self) -> tuple[Claim, ...]:
        return tuple(sorted((c for r in self.consolidations for c in r.claims), key=lambda c: c.id))

    def findings_json(self) -> list[JsonValue]:
        """Every finding, tagged with its consolidator, in (consolidator id, finding id) order."""
        tagged = [(r.transform.consolidator_id, f) for r in self.consolidations for f in r.findings]
        return [
            {"consolidator_id": cid, "finding": finding.to_json()}
            for cid, finding in sorted(tagged, key=lambda t: (t[0], t[1].id))
        ]


def consolidate(
    ledger: LedgerReader,
    registrations: Iterable[Registration],
    snapshot: int,
    *,
    registry: PredicateRegistry = CORE_PREDICATES,
) -> ConsolidationRun:
    """Run every registered consolidator over the Ledger as of ``snapshot`` (ADR 0003 §4)."""
    tx = ledger_tx(snapshot)
    if tx < 1:
        raise ValueError("a Ledger snapshot is a transaction sequence number, 1 or more")
    ordered = plan(registrations)
    reads: dict[str, set[str]] = {}
    for registration in ordered:  # transitive closure of ``after``, in plan order
        cid = registration.consolidator_id
        reads[cid] = set(registration.after).union(*(reads[a] for a in registration.after))
    by_id: dict[str, Consolidation] = {}
    for registration in ordered:
        cid = registration.consolidator_id
        failed = sorted(dep for dep in reads[cid] if not by_id[dep].complete)
        if failed:  # its input is not what the snapshot states: it does not run
            by_id[cid] = skip_consolidator(
                registration.consolidator, registration.config, failed, recorded_at=tx
            )
            continue
        previous = sorted(
            (c for dep in sorted(reads[cid]) for c in by_id[dep].claims), key=lambda c: c.id
        )
        by_id[cid] = run_consolidator(
            registration.consolidator,
            ledger,
            previous,
            registration.config,
            recorded_at=tx,
            registry=registry,
        )
    results = tuple(by_id[r.consolidator_id] for r in ordered)
    ranks = priorities(ordered)
    claims = sorted((c for r in results for c in r.claims), key=lambda c: c.id)
    findings = [(r.transform.consolidator_id, f) for r in results for f in r.findings]
    findings.sort(key=lambda t: (t[0], t[1].id))
    entries = tuple(
        ConsolidatorEntry(
            transform=result.transform.to_json(),
            after=registration.after,
            priority=registration.priority,
            claims=len(result.claims),
            findings=len(result.findings),
            complete=result.complete,
        )
        for registration, result in zip(ordered, results, strict=True)
    )
    memory_snapshot = MemorySnapshot(
        ledger_snapshot=tx,
        packages=tuple(ledger.list_packages()),
        consolidators=entries,
        claim_count=len(claims),
        claims_hash=_hash([{"claim": c.content_json(), "id": c.id} for c in claims]),
        finding_count=len(findings),
        findings_hash=_hash(
            [{"consolidator_id": cid, "finding": f.to_json()} for cid, f in findings]
        ),
        generation=resolver_config_hash(registry, ranks),
    )
    return ConsolidationRun(memory_snapshot, results, ranks)


class GraphExtendError(ValueError):
    """A run that cannot extend a graph: an earlier snapshot, or a consolidator dropped."""


def extend(
    document: GraphDocument | None,
    run: ConsolidationRun,
    *,
    registry: PredicateRegistry = CORE_PREDICATES,
) -> GraphDocument:
    """The graph after ``run``: ``document``'s assertions and builds plus the run's, resolved.

    A consolidator that built ``document`` must be in the run: dropping one would leave its claims
    current forever, so that takes a rebuild. One that did not complete records no build, so what
    it held stands until a later build of it completes. Lineage rules are ``resolve``'s
    (``LineageError``).
    """
    tx = run.snapshot.ledger_snapshot
    builds = [r.build for r in run.consolidations if r.complete]
    claims: list[Claim] = list(run.claims)
    if document is not None:
        if tx <= document.head:
            raise GraphExtendError(
                f"Ledger snapshot {tx} is not after the graph's head {document.head}"
            )
        dropped = sorted({b.consolidator_id for b in document.builds} - run.priorities.keys())
        if dropped:
            raise GraphExtendError(
                f"consolidators no longer registered: {dropped}; rebuild the graph instead"
            )
        claims = [*assertions(document.resolution.claims), *claims]
        builds = [*document.builds, *builds]
    resolution = resolve(claims, registry, run.priorities, builds)
    config = resolver_config(registry, run.priorities)
    return GraphDocument(resolution, config, tx, build_order(builds))
