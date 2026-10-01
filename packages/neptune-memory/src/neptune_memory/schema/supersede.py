"""Superseding as a pure function: assertions in, the full bi-temporal history out (ADR 0002 §4).

``resolve`` folds the assertions in arrival order, ``(recorded_at, consolidator priority, claim
id)``, so its output depends only on the set of assertions and the configuration. When an arriving
claim contradicts a current one, meaning same subject, a ``one`` predicate, a different object and
overlapping valid intervals on the same clock:

1. The winner is the claim with the higher assertion rank (observed and stated outrank inferred),
   then the later ``valid_from``; on a full tie the later arrival wins.
2. The loser's current version gets ``superseded_at`` set to the arriving claim's ``recorded_at``.
   If it began before the winner, a *closure version* records it holding over
   ``[loser.valid_from, winner.valid_from)``, recorded at the same transaction. A loser is never
   resurrected after the winner's interval.
3. The arriving claim lists the claims it superseded in ``supersedes``. A closure version
   ``supersedes`` the version it narrows. Its provenance is the resolver's: the narrowed claim's
   original evidence plus the winner's. A claim that starts at or after its winner keeps nothing:
   the resolver only cuts tails, never heads.

Nothing is deleted. Claims on different clocks are never compared: the pair is reported as a
``clock_mismatch`` finding and both stay current.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from neptune.identity.canonical_json import dumps
from neptune.identity.ids import config_hash
from neptune_memory.schema.claim import Claim, ClaimId, ClaimProvenance, is_inferred
from neptune_memory.schema.interval import OPEN, LedgerTx, Open
from neptune_memory.schema.predicates import Cardinality, PredicateRegistry, check_claim

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from neptune.model.ids import RecordId
    from neptune.model.provenance import EvidenceRef
    from neptune_memory.schema.claim import ClaimAssertionKind
    from neptune_memory.schema.nodes import NodeRef

# Reserved consolidator id for closure versions; no consolidator may use it.
RESOLVER_ID: Final = "memory.supersede"
RESOLVER_VERSION: Final = "1"


class FindingCode(StrEnum):
    CLOCK_MISMATCH = "clock_mismatch"  # contradicting objects on different clocks: not compared
    # The arriving claim began at or after a winner's valid_from, so no part of it is current.
    OVERRIDDEN_ON_ARRIVAL = "overridden_on_arrival"


@dataclass(frozen=True)
class ResolutionFinding:
    """Something the resolver could not or did not apply, about ``claim`` and ``others``."""

    code: FindingCode
    claim: ClaimId
    others: tuple[ClaimId, ...]


@dataclass(frozen=True)
class Resolution:
    """Every claim version, ordered by ``(recorded_at, id)``, and the findings in arrival order."""

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
    (else ``ValueError``: no consolidator may forge resolver output). These are preconditions, not
    data findings: ``consolidate/`` turns non-conforming claims into findings before they get here.
    Every claim must conform to ``registry`` (else ``ClaimSchemaError``) and every consolidator
    must have a priority (else ``ValueError``).
    """
    claims = tuple(claims)
    inputs = assertions(claims)
    missing = sorted({c.provenance.consolidator_id for c in inputs} - priorities.keys())
    if missing:
        raise ValueError(f"no priority for consolidators {missing}")
    if RESOLVER_ID in priorities:
        raise ValueError(f"{RESOLVER_ID} is reserved for the resolver")
    for claim in inputs:
        check_claim(claim, registry)
    versions: dict[ClaimId, Claim] = {}
    origin: dict[ClaimId, Claim] = {}  # version id -> the assertion it narrows
    current: dict[tuple[NodeRef, str], list[ClaimId]] = {}
    findings: list[ResolutionFinding] = []

    for arriving in sorted(inputs, key=lambda c: arrival_key(c, priorities)):
        origin[arriving.id] = arriving
        if registry.spec(arriving.predicate).cardinality is Cardinality.MANY:
            versions[arriving.id] = arriving
            continue
        key = (arriving.subject, arriving.predicate)
        live = current.setdefault(key, [])
        claimed = _object_key(arriving)
        rivals = [versions[i] for i in live if _object_key(versions[i]) != claimed]
        domain = arriving.valid_from.domain_id
        mismatched = sorted(r.id for r in rivals if r.valid_from.domain_id != domain)
        if mismatched:
            findings.append(
                ResolutionFinding(FindingCode.CLOCK_MISMATCH, arriving.id, tuple(mismatched))
            )
        overlapping = [
            r
            for r in rivals
            if r.valid_from.domain_id == domain and r.valid.overlaps(arriving.valid)
        ]
        winners = [r for r in overlapping if not _beats(arriving, r)]
        effective: Claim | None = arriving
        stored = arriving
        if winners:
            cutter = min(winners, key=lambda w: (w.valid_from.ticks, w.recorded_at, w.id))
            stored = replace(arriving, superseded_at=arriving.recorded_at)
            if arriving.valid_from < cutter.valid_from:
                effective = _closure(arriving, arriving, cutter, arriving.recorded_at)
                origin[effective.id] = arriving
            else:
                effective = None
                findings.append(
                    ResolutionFinding(
                        FindingCode.OVERRIDDEN_ON_ARRIVAL,
                        arriving.id,
                        tuple(sorted(w.id for w in winners)),
                    )
                )
        losers = (
            []
            if effective is None
            else [
                r for r in overlapping if _beats(arriving, r) and r.valid.overlaps(effective.valid)
            ]
        )
        for loser in losers:
            versions[loser.id] = replace(loser, superseded_at=arriving.recorded_at)
            live.remove(loser.id)
            if loser.valid_from < arriving.valid_from:
                narrowed = _closure(loser, origin[loser.id], arriving, arriving.recorded_at)
                origin[narrowed.id] = origin[loser.id]
                versions[narrowed.id] = narrowed
                live.append(narrowed.id)
        versions[arriving.id] = replace(stored, supersedes=tuple(sorted(r.id for r in losers)))
        if effective is not None:
            if effective is not arriving:
                versions[effective.id] = effective
            live.append(effective.id)

    forged = sorted({c.id for c in claims if is_closure(c)} - versions.keys())
    if forged:
        raise ValueError(f"closure versions this resolution does not produce: {forged}")
    history = tuple(sorted(versions.values(), key=lambda c: (c.recorded_at, c.id)))
    return Resolution(history, tuple(findings))


def as_of(history: Iterable[Claim], tx: LedgerTx) -> tuple[Claim, ...]:
    """The versions current at transaction ``tx``: recorded by then and not yet superseded."""
    return tuple(
        claim
        for claim in history
        if claim.recorded_at <= tx
        and (isinstance(claim.superseded_at, Open) or tx < claim.superseded_at)
    )


# --- Internals --------------------------------------------------------------------------------


def _beats(arriving: Claim, held: Claim) -> bool:
    """Whether ``arriving`` wins the overlap against the already-current ``held``."""
    return (assertion_rank(arriving.assertion_kind), arriving.valid_from.ticks) >= (
        assertion_rank(held.assertion_kind),
        held.valid_from.ticks,
    )


def _object_key(claim: Claim) -> bytes:
    """Objects compare as their canonical JSON: ``5`` and ``5.0`` differ, as they do in ids."""
    return dumps(claim.object.to_json())


def _closure(version: Claim, root: Claim, winner: Claim, recorded_at: LedgerTx) -> Claim:
    """``version`` narrowed to end where ``winner`` begins, recorded at ``recorded_at``.

    The closure's ``config_hash`` hashes the decision it records, ``{narrows, winner}``, so two
    narrowings never share an id and unrelated configuration never changes it.
    """
    evidence: list[EvidenceRef] = []
    for ref in (*root.provenance.evidence, *winner.provenance.evidence):
        if ref not in evidence:
            evidence.append(ref)
    records: set[RecordId] = {*root.provenance.records, *winner.provenance.records}
    provenance = ClaimProvenance(
        evidence=tuple(evidence),
        records=tuple(sorted(records)),
        consolidator_id=RESOLVER_ID,
        consolidator_version=RESOLVER_VERSION,
        config_hash=config_hash({"narrows": version.id, "winner": winner.id}),
    )
    return replace(
        version,
        valid_to=winner.valid_from,
        recorded_at=recorded_at,
        provenance=provenance,
        superseded_at=OPEN,
        supersedes=(version.id,),
    )
