"""Superseding as a pure function: assertions in, the full bi-temporal history out.

ADR 0002 §4 as superseded by ADR 0005. ``resolve`` folds the assertions in arrival order,
``(recorded_at, consolidator priority, claim id)``, so its output depends only on the set of
assertions and the configuration. When an arriving claim contradicts a current version, meaning
same subject, a ``one`` predicate, a different object and overlapping valid intervals on the same
clock:

1. The winner is the claim with the higher assertion rank (observed and stated outrank inferred),
   then the later ``valid_from`` of the original assertion; on a full tie the later arrival wins.
2. The loser keeps every part of its valid interval that no winner covers (a *split closure*):
   its version gets ``superseded_at`` set to the arriving claim's ``recorded_at`` and each
   uncovered sub-interval becomes a closure version recorded at that transaction, so a source's
   evidence for ``[4, 10)`` survives a rival claim over ``[2, 4)``. Nothing a winner covers is
   resurrected later.
3. The arriving claim lists the claims it superseded in ``supersedes``. A closure version
   ``supersedes`` the version it narrows; its provenance is the resolver's: the original
   assertion's evidence plus the evidence of the claims that cut it, and a ``config_hash`` over
   the resolver's configuration (priorities, vocabulary) and the version it narrows.

Nothing is deleted. Claims on different clocks are never compared: while both versions of such a
pair are current, a ``clock_mismatch`` finding marks them. Findings are bi-temporal like claims.

Consolidator upgrades (ADR 0003 §3): a lineage is (consolidator id, version, config hash). One
lineage per consolidator per transaction, and a replaced lineage never returns, else
``LineageError``. A consolidator's first claim in a new lineage retires, at that transaction, every
current version of its other lineages, split closures included (``superseded_at`` set, valid time
untouched).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final, TypeAlias

from neptune.identity.canonical_json import dumps
from neptune.identity.ids import config_hash
from neptune_memory.schema.claim import Claim, ClaimId, ClaimProvenance, is_inferred
from neptune_memory.schema.interval import OPEN, LedgerTx, Open
from neptune_memory.schema.predicates import (
    VOCABULARY_VERSION,
    Cardinality,
    PredicateRegistry,
    check_claim,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from neptune.model.ids import ConfigHash, RecordId
    from neptune.model.jsonvalue import JsonObject
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.schema.claim import ClaimAssertionKind
    from neptune_memory.schema.interval import Interval
    from neptune_memory.schema.nodes import NodeRef

# Reserved consolidator id for closure versions; no consolidator may use it.
RESOLVER_ID: Final = "memory.supersede"
# 2: split closures, transaction-ordered findings, config-covering closure hashes (ADR 0005).
RESOLVER_VERSION: Final = "2"


# (consolidator id, version, config hash): what one build of one consolidator produced.
Lineage: TypeAlias = tuple[str, str, str]


def lineage_of(claim: Claim) -> Lineage:
    p = claim.provenance
    return (p.consolidator_id, p.consolidator_version, p.config_hash)


class LineageError(ValueError):
    """Claims whose lineages cannot be ordered (ADR 0003 §3). ``code``: clash or reuse."""

    def __init__(self, code: str, consolidator_id: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.consolidator_id = consolidator_id


class FindingCode(StrEnum):
    # Contradicting objects on different clocks, never compared. Active while both are current.
    CLOCK_MISMATCH = "clock_mismatch"
    # Winners cover the arriving claim's whole valid interval, so no part of it was ever current.
    OVERRIDDEN_ON_ARRIVAL = "overridden_on_arrival"


@dataclass(frozen=True)
class ResolutionFinding:
    """Something the resolver could not or did not apply, about ``claim`` and ``others``.

    Bi-temporal like a claim: recorded at the transaction where it arose, and active until
    ``superseded_at``. A ``clock_mismatch`` names one pair of versions and is active exactly while
    both are current; ``overridden_on_arrival`` never stops being true.
    """

    code: FindingCode
    claim: ClaimId
    others: tuple[ClaimId, ...]
    recorded_at: LedgerTx
    superseded_at: LedgerTx | Open = OPEN

    def to_json(self) -> JsonObject:
        return {
            "claim": self.claim,
            "code": str(self.code),
            "others": list(self.others),
            "recorded_at": self.recorded_at,
            "superseded_at": self.superseded_at
            if not isinstance(self.superseded_at, Open)
            else self.superseded_at.to_json(),
        }


@dataclass(frozen=True)
class Resolution:
    """Every claim version, ordered by ``(recorded_at, id)``, and every finding, ordered by
    ``(recorded_at, claim, code, others)``."""

    claims: tuple[Claim, ...]
    findings: tuple[ResolutionFinding, ...]


def assertion_rank(kind: ClaimAssertionKind) -> int:
    """Observed and stated evidence outrank inference; neither outranks the other."""
    return 0 if is_inferred(kind) else 1


def arrival_key(claim: Claim, priorities: Mapping[str, int]) -> tuple[int, int, str]:
    """Transaction order, then consolidator priority (higher arrives later and wins), then id."""
    return (claim.recorded_at, priorities[claim.provenance.consolidator_id], claim.id)


def is_closure(claim: Claim) -> bool:
    return claim.provenance.consolidator_id == RESOLVER_ID


def resolver_config(registry: PredicateRegistry, priorities: Mapping[str, int]) -> JsonObject:
    """Everything that steers ``resolve``: priorities, the vocabulary and its version."""
    return {
        "priorities": {cid: priorities[cid] for cid in sorted(priorities)},
        "vocabulary": registry.to_json(),
        "vocabulary_version": VOCABULARY_VERSION,
    }


def resolver_config_hash(registry: PredicateRegistry, priorities: Mapping[str, int]) -> ConfigHash:
    return config_hash(resolver_config(registry, priorities))


def assertions(history: Iterable[Claim]) -> tuple[Claim, ...]:
    """The assertions a history was resolved from: closures dropped, bookkeeping cleared.

    A claim recorded more than once (same id) keeps its earliest ``recorded_at``.
    """
    earliest: dict[ClaimId, Claim] = {}
    for claim in history:
        if is_closure(claim):
            continue
        plain = replace(claim, superseded_at=OPEN, supersedes=())
        seen = earliest.get(plain.id)
        if seen is None or plain.recorded_at < seen.recorded_at:
            earliest[plain.id] = plain
    return tuple(sorted(earliest.values(), key=lambda c: (c.recorded_at, c.id)))


def resolve(
    claims: Iterable[Claim], registry: PredicateRegistry, priorities: Mapping[str, int]
) -> Resolution:
    """The bi-temporal history of ``claims``: pure, deterministic, order-free and idempotent.

    ``claims`` may be assertions or a history ``resolve`` produced; either way only their
    ``assertions`` count, and every closure version in the input must be one this call recreates
    (else ``ValueError``: no consolidator may forge resolver output, and a history resolved under
    another configuration is re-resolved from its ``assertions``). These are preconditions, not
    data findings: ``consolidate/`` turns non-conforming claims into findings before they get here.
    Every claim must conform to ``registry`` (else ``ClaimSchemaError``) and every consolidator
    must have an integer priority (else ``ValueError``).
    """
    claims = tuple(claims)
    inputs = assertions(claims)
    missing = sorted({c.provenance.consolidator_id for c in inputs} - priorities.keys())
    if missing:
        raise ValueError(f"no priority for consolidators {missing}")
    if RESOLVER_ID in priorities:
        raise ValueError(f"{RESOLVER_ID} is reserved for the resolver")
    bad = sorted(c for c, p in priorities.items() if isinstance(p, bool) or not isinstance(p, int))
    if bad:
        raise ValueError(f"priorities must be integers: {bad}")
    for claim in inputs:
        check_claim(claim, registry)
    _check_lineages(claims)
    resolver_hash = resolver_config_hash(registry, priorities)
    versions: dict[ClaimId, Claim] = {}
    origin: dict[ClaimId, Claim] = {}  # version id -> the assertion it is a version of
    current: dict[tuple[NodeRef, str], list[ClaimId]] = {}
    findings: list[ResolutionFinding] = []

    latest: dict[str, Lineage] = {}  # consolidator id -> lineage of its latest build so far

    for arriving in sorted(inputs, key=lambda c: arrival_key(c, priorities)):
        arriving_lineage = lineage_of(arriving)
        cid = arriving.provenance.consolidator_id
        if latest.get(cid, arriving_lineage) != arriving_lineage:
            _retire(versions, origin, current, arriving_lineage, arriving.recorded_at)
        latest[cid] = arriving_lineage
        origin[arriving.id] = arriving
        if registry.spec(arriving.predicate).cardinality is Cardinality.MANY:
            versions[arriving.id] = arriving
            continue
        tx = arriving.recorded_at
        live = current.setdefault((arriving.subject, arriving.predicate), [])
        claimed = _object_key(arriving)
        domain = arriving.valid_from.domain_id
        overlapping = [
            versions[i]
            for i in live
            if _object_key(versions[i]) != claimed
            and versions[i].valid_from.domain_id == domain
            and versions[i].valid.overlaps(arriving.valid)
        ]
        winners = [r for r in overlapping if not _beats(arriving, origin[r.id])]
        effective: list[Claim] = [arriving]
        stored = arriving
        if winners:
            stored = replace(arriving, superseded_at=tx)
            cutters = _distinct(origin[w.id] for w in winners)
            effective = [
                _closure(arriving, arriving, piece, cutters, tx, resolver_hash)
                for piece in arriving.valid.minus(w.valid for w in winners)
            ]
            if not effective:
                findings.append(
                    ResolutionFinding(
                        FindingCode.OVERRIDDEN_ON_ARRIVAL,
                        arriving.id,
                        tuple(sorted(w.id for w in winners)),
                        tx,
                    )
                )
        losers = [
            r
            for r in overlapping
            if _beats(arriving, origin[r.id]) and any(r.valid.overlaps(e.valid) for e in effective)
        ]
        for loser in losers:
            versions[loser.id] = replace(loser, superseded_at=tx)
            live.remove(loser.id)
            root = origin[loser.id]
            for piece in loser.valid.minus(e.valid for e in effective):
                narrowed = _closure(loser, root, piece, (arriving,), tx, resolver_hash)
                origin[narrowed.id] = root
                versions[narrowed.id] = narrowed
                live.append(narrowed.id)
        versions[arriving.id] = replace(stored, supersedes=tuple(sorted(r.id for r in losers)))
        for version in effective:
            if version is not arriving:
                origin[version.id] = arriving
                versions[version.id] = version
            live.append(version.id)

    forged = sorted({c.id for c in claims if is_closure(c)} - versions.keys())
    if forged:
        raise ValueError(f"closure versions this resolution does not produce: {forged}")
    findings.extend(_clock_mismatches(versions.values(), origin, registry, priorities))
    history = tuple(sorted(versions.values(), key=lambda c: (c.recorded_at, c.id)))
    ordered = tuple(sorted(findings, key=lambda f: (f.recorded_at, f.claim, f.code, f.others)))
    return Resolution(history, ordered)


def as_of(resolution: Resolution, tx: LedgerTx) -> Resolution:
    """The claim versions and findings active at transaction ``tx``: recorded by then and not yet
    superseded. Equal to resolving only the claims recorded by ``tx`` (bookkeeping aside)."""
    return Resolution(
        tuple(c for c in resolution.claims if _active(c.recorded_at, c.superseded_at, tx)),
        tuple(f for f in resolution.findings if _active(f.recorded_at, f.superseded_at, tx)),
    )


# --- Internals --------------------------------------------------------------------------------


def _retire(
    versions: dict[ClaimId, Claim],
    origin: Mapping[ClaimId, Claim],
    current: Mapping[tuple[NodeRef, str], list[ClaimId]],
    upgrade: Lineage,
    tx: LedgerTx,
) -> None:
    """ADR 0003 §3: a consolidator's new lineage retires every current claim of its other lineages.

    Each such version gets ``superseded_at = tx``, closure versions included, even one narrowed at
    ``tx`` itself. Nothing is deleted and valid time is not cut, so ``as_of`` before ``tx`` still
    answers from the old lineage.
    """
    for vid, version in list(versions.items()):
        root = lineage_of(origin[vid])
        if root[0] == upgrade[0] and root != upgrade and isinstance(version.superseded_at, Open):
            versions[vid] = replace(version, superseded_at=tx)
            for live in current.values():
                if vid in live:
                    live.remove(vid)


def _check_lineages(claims: Iterable[Claim]) -> None:
    """One lineage per consolidator per transaction, and no lineage back after another replaced it.

    Checked over every recording (before ``assertions`` keeps only the earliest), so a re-run of
    a retired lineage is caught even when it reproduces the old claim ids.
    """
    builds: dict[str, dict[int, set[Lineage]]] = {}
    for claim in claims:
        if not is_closure(claim):
            by_tx = builds.setdefault(claim.provenance.consolidator_id, {})
            by_tx.setdefault(claim.recorded_at, set()).add(lineage_of(claim))
    for cid, by_tx in sorted(builds.items()):
        clashes = sorted(tx for tx, lineages in by_tx.items() if len(lineages) > 1)
        if clashes:
            raise LineageError(
                "lineage_clash", cid, f"two lineages of {cid} at transactions {clashes}"
            )
        sequence = [next(iter(by_tx[tx])) for tx in sorted(by_tx)]
        retired: set[Lineage] = set()
        for before, after in itertools.pairwise(sequence):
            if after != before:
                retired.add(before)
                if after in retired:
                    raise LineageError(
                        "lineage_reuse",
                        cid,
                        f"lineage (consolidator {after[0]!r}, version {after[1]!r}, config hash"
                        f" {after[2]!r}) reappears after it was replaced; a rollback is a new"
                        " version",
                    )


def _beats(arriving: Claim, held: Claim) -> bool:
    """Whether ``arriving`` wins the overlap against ``held``, the assertion a current version is
    of: a split closure competes with its original ``valid_from``, never its piece's."""
    return (assertion_rank(arriving.assertion_kind), arriving.valid_from.ticks) >= (
        assertion_rank(held.assertion_kind),
        held.valid_from.ticks,
    )


def _object_key(claim: Claim) -> bytes:
    """Objects compare as their canonical JSON: ``5`` and ``5.0`` differ, as they do in ids."""
    return dumps(claim.object.to_json())


def _distinct(claims: Iterable[Claim]) -> tuple[Claim, ...]:
    return tuple(sorted({c.id: c for c in claims}.values(), key=lambda c: c.id))


def _active(recorded_at: LedgerTx, superseded_at: LedgerTx | Open, tx: LedgerTx) -> bool:
    return recorded_at <= tx and (isinstance(superseded_at, Open) or tx < superseded_at)


def _closure(
    version: Claim,
    root: Claim,
    valid: Interval,
    cutters: Sequence[Claim],
    recorded_at: LedgerTx,
    resolver_hash: ConfigHash,
) -> Claim:
    """The part of ``version`` over ``valid`` that no winner covers, recorded at ``recorded_at``.

    Its evidence is ``root``'s (the assertion ``version`` is of) then the ``cutters``' (the claims
    that took the rest); its ``config_hash`` covers the resolver's configuration and the version it
    narrows, so pieces of different versions never share an id even when they share evidence.
    """
    evidence: list[EvidenceRef] = []
    for ref in (*root.provenance.evidence, *(r for c in cutters for r in c.provenance.evidence)):
        if ref not in evidence:
            evidence.append(ref)
    records: set[RecordId] = {*root.provenance.records}
    for cutter in cutters:
        records.update(cutter.provenance.records)
    provenance = ClaimProvenance(
        evidence=tuple(evidence),
        records=tuple(sorted(records)),
        consolidator_id=RESOLVER_ID,
        consolidator_version=RESOLVER_VERSION,
        config_hash=config_hash({"narrows": version.id, "resolver": resolver_hash}),
    )
    return replace(
        version,
        valid_from=valid.start,
        valid_to=valid.end,
        recorded_at=recorded_at,
        provenance=provenance,
        superseded_at=OPEN,
        supersedes=(version.id,),
    )


def _clock_mismatches(
    versions: Iterable[Claim],
    origin: Mapping[ClaimId, Claim],
    registry: PredicateRegistry,
    priorities: Mapping[str, int],
) -> list[ResolutionFinding]:
    """One ``clock_mismatch`` per pair of versions of a ``one`` fact with different objects on
    different clocks, active exactly while both are current. ``claim`` is the version recorded
    later (on a tie, the one whose assertion arrives later); ``others`` is the earlier one.

    Versions are grouped by clock, so only pairs across clocks are compared.
    """
    facts: dict[tuple[NodeRef, str], dict[RecordId, list[tuple[Claim, bytes]]]] = {}
    for version in versions:
        if registry.spec(version.predicate).cardinality is Cardinality.ONE:
            clocks = facts.setdefault((version.subject, version.predicate), {})
            clocks.setdefault(version.valid_from.domain_id, []).append(
                (version, _object_key(version))
            )

    def order(v: Claim) -> tuple[int, tuple[int, int, str], str]:
        return (v.recorded_at, arrival_key(origin[v.id], priorities), v.id)

    found: list[ResolutionFinding] = []
    for clocks in facts.values():
        for one, two in itertools.combinations(sorted(clocks), 2):
            for a, a_key in clocks[one]:
                for b, b_key in clocks[two]:
                    if a_key == b_key:
                        continue
                    earlier, later = sorted((a, b), key=order)
                    start = later.recorded_at
                    ends = [
                        e for e in (a.superseded_at, b.superseded_at) if not isinstance(e, Open)
                    ]
                    end: LedgerTx | Open = min(ends) if ends else OPEN
                    if isinstance(end, Open) or start < end:
                        found.append(
                            ResolutionFinding(
                                FindingCode.CLOCK_MISMATCH, later.id, (earlier.id,), start, end
                            )
                        )
    return found
