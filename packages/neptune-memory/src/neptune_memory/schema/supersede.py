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

Withdrawal (ADR 0007 §5, ADR 0016): ``resolve`` may also be given the ``Build`` records of the
consolidator runs that produced the claims. A build is a complete statement of its lineage over
one Ledger snapshot: at a build of lineage ``L`` at transaction ``t``, every current version whose
original assertion belongs to ``L`` and that the build did not emit gets ``superseded_at = t``,
before the claims first recorded at ``t`` arrive. A build of a new lineage retires the
consolidator's other lineages at ``t`` even when it emits nothing. Without builds, ``resolve``
behaves exactly as before.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Final, NewType, TypeAlias

from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.identity.ids import config_hash
from neptune.model.ids import ConfigHash, check_text, check_token, parse_config_hash
from neptune_memory.schema.claim import (
    Claim,
    ClaimId,
    ClaimProvenance,
    is_inferred,
    parse_claim_id,
)
from neptune_memory.schema.interval import OPEN, LedgerTx, Open, ledger_tx
from neptune_memory.schema.predicates import (
    VOCABULARY_VERSION,
    Cardinality,
    PredicateRegistry,
    check_claim,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from neptune.model.ids import RecordId
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


# "finding:sha256:<64 lowercase hex>": a resolver finding's id, kept apart from claim ids by prefix.
FindingId = NewType("FindingId", str)
_FINDING_ID = re.compile(r"finding:sha256:[0-9a-f]{64}")
FINDING_ID_SCHEME: Final = "neptune-memory.finding-id/1"


def parse_finding_id(text: str) -> FindingId:
    if not isinstance(text, str) or not _FINDING_ID.fullmatch(text):
        raise ValueError(f"not a finding id (want 'finding:sha256:<64 lowercase hex>'): {text!r}")
    return FindingId(text)


@dataclass(frozen=True)
class FindingProvenance:
    """What produced a finding: the resolver's id, version and configuration hash (ADR 0006 §5)."""

    resolver_id: str
    resolver_version: str
    config_hash: ConfigHash

    def __post_init__(self) -> None:
        check_token("resolver_id", self.resolver_id)
        check_text("resolver_version", self.resolver_version)
        parse_config_hash(self.config_hash)

    def to_json(self) -> JsonObject:
        return {
            "config_hash": self.config_hash,
            "resolver_id": self.resolver_id,
            "resolver_version": self.resolver_version,
        }


@dataclass(frozen=True)
class ResolutionFinding:
    """Something the resolver could not or did not apply, about ``claim`` and ``others``.

    Bi-temporal like a claim: recorded at the transaction where it arose, and active until
    ``superseded_at``. A ``clock_mismatch`` names one pair of versions and is active exactly while
    both are current; ``overridden_on_arrival`` never stops being true.

    A contract object (ADR 0006 §5): its ``id`` hashes what it says and who said it (code, claims,
    provenance), never its bookkeeping (``recorded_at``, ``superseded_at``), so a store keys it on
    arrival and closes it in place, atomically with the claims it names.
    """

    code: FindingCode
    claim: ClaimId
    others: tuple[ClaimId, ...]
    provenance: FindingProvenance
    recorded_at: LedgerTx
    superseded_at: LedgerTx | Open = OPEN

    def __post_init__(self) -> None:
        if not isinstance(self.code, FindingCode):
            raise TypeError(f"code must be a FindingCode: {self.code!r}")
        parse_claim_id(self.claim)
        if not isinstance(self.others, tuple):
            raise TypeError("others must be a tuple of claim ids")
        for other in self.others:
            parse_claim_id(other)
        if list(self.others) != sorted(set(self.others)) or self.claim in self.others:
            raise ValueError("others must be unique, sorted and not the finding's claim")
        if not isinstance(self.provenance, FindingProvenance):
            raise TypeError(f"provenance must be a FindingProvenance: {self.provenance!r}")
        ledger_tx(self.recorded_at)
        if not isinstance(self.superseded_at, Open):
            ledger_tx(self.superseded_at)
            if self.superseded_at < self.recorded_at:
                raise ValueError("superseded_at precedes recorded_at")

    def content_json(self) -> JsonObject:
        """What the finding says and who said it: the input its id is derived from."""
        return {
            "claim": self.claim,
            "code": str(self.code),
            "others": list(self.others),
            "provenance": self.provenance.to_json(),
        }

    @cached_property
    def id(self) -> FindingId:
        payload: JsonObject = {"finding": self.content_json(), "scheme": FINDING_ID_SCHEME}
        return FindingId("finding:" + content_id(dumps(payload)))

    @property
    def is_current(self) -> bool:
        return isinstance(self.superseded_at, Open)

    def to_json(self) -> JsonObject:
        return {
            **self.content_json(),
            "id": self.id,
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


@dataclass(frozen=True)
class Build:
    """One run of one consolidator lineage over one Ledger snapshot (ADR 0007 §5.1).

    ``claims`` is every claim id the run emitted, possibly none: the build is a complete statement
    of its lineage at ``recorded_at``, so whatever the lineage held before and did not emit again
    is withdrawn there. Ids are unique and sorted.
    """

    consolidator_id: str
    version: str
    config_hash: ConfigHash
    recorded_at: LedgerTx
    claims: tuple[ClaimId, ...] = ()

    def __post_init__(self) -> None:
        check_token("consolidator_id", self.consolidator_id)
        if self.consolidator_id == RESOLVER_ID:
            raise ValueError(f"{RESOLVER_ID!r} is the resolver, never a build")
        check_text("version", self.version)
        parse_config_hash(self.config_hash)
        ledger_tx(self.recorded_at)
        if not isinstance(self.claims, tuple):
            raise TypeError("claims must be a tuple of claim ids")
        for claim_id in self.claims:
            parse_claim_id(claim_id)
        if list(self.claims) != sorted(set(self.claims)):
            raise ValueError("a build's claims must be unique and sorted")

    @property
    def lineage(self) -> Lineage:
        return (self.consolidator_id, self.version, self.config_hash)

    def to_json(self) -> JsonObject:
        return {
            "claims": list(self.claims),
            "config_hash": self.config_hash,
            "consolidator_id": self.consolidator_id,
            "recorded_at": self.recorded_at,
            "version": self.version,
        }


def build_order(builds: Iterable[Build]) -> tuple[Build, ...]:
    """Builds in their published order: ``(recorded_at, consolidator_id)``."""
    return tuple(sorted(builds, key=lambda b: (b.recorded_at, b.consolidator_id)))


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
    claims: Iterable[Claim],
    registry: PredicateRegistry,
    priorities: Mapping[str, int],
    builds: Iterable[Build] = (),
) -> Resolution:
    """The bi-temporal history of ``claims``: pure, deterministic, order-free and idempotent.

    ``claims`` may be assertions or a history ``resolve`` produced; either way only their
    ``assertions`` count, and every closure version in the input must be one this call recreates
    (else ``ValueError``: no consolidator may forge resolver output, and a history resolved under
    another configuration is re-resolved from its ``assertions``). These are preconditions, not
    data findings: ``consolidate/`` turns non-conforming claims into findings before they get here.
    Every claim must conform to ``registry`` (else ``ClaimSchemaError``) and every consolidator
    must have an integer priority (else ``ValueError``).

    ``builds`` (ADR 0007 §5) must agree with ``claims`` (else ``ValueError``): at most one per
    consolidator and transaction; each names only claims of its own lineage recorded by then; and
    a consolidator that has builds has every recording of its claims in the build at that
    transaction.
    """
    claims = tuple(claims)
    builds = build_order(builds)
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
    _check_lineages(claims, builds)
    _check_builds(claims, inputs, builds)
    state = _Resolver(registry, priorities)
    arrivals = sorted(inputs, key=lambda c: arrival_key(c, priorities))
    next_build = next_arrival = 0
    for tx in sorted({c.recorded_at for c in arrivals} | {b.recorded_at for b in builds}):
        first = next_build
        while next_build < len(builds) and builds[next_build].recorded_at == tx:
            next_build += 1
        if next_build > first:
            state.apply_builds(builds[first:next_build], tx)
        while next_arrival < len(arrivals) and arrivals[next_arrival].recorded_at == tx:
            state.arrive(arrivals[next_arrival])
            next_arrival += 1
    versions = state.versions
    forged = sorted({c.id for c in claims if is_closure(c)} - versions.keys())
    if forged:
        raise ValueError(f"closure versions this resolution does not produce: {forged}")
    findings = state.findings
    findings.extend(
        _clock_mismatches(versions.values(), state.origin, registry, priorities, state.by_resolver)
    )
    history = tuple(sorted(versions.values(), key=lambda c: (c.recorded_at, c.id)))
    ordered = tuple(sorted(findings, key=lambda f: (f.recorded_at, f.claim, f.code, f.others)))
    return Resolution(history, ordered)


def as_of(resolution: Resolution, tx: LedgerTx) -> Resolution:
    """The graph exactly as it was known at transaction ``tx`` (ADR 0006 §6).

    The claim versions and findings active at ``tx``: recorded by then and not yet superseded.
    Each is presented as it was known at ``tx``, so ``superseded_at`` is masked to ``OPEN``: a
    supersession recorded after ``tx`` is later knowledge and never leaks into the snapshot. The
    result equals the current claims and findings of resolving only what was recorded by ``tx``.
    The unmasked history stays in ``resolution`` itself.
    """
    return Resolution(
        tuple(
            c if c.is_current else replace(c, superseded_at=OPEN)
            for c in resolution.claims
            if _active(c.recorded_at, c.superseded_at, tx)
        ),
        tuple(
            f if f.is_current else replace(f, superseded_at=OPEN)
            for f in resolution.findings
            if _active(f.recorded_at, f.superseded_at, tx)
        ),
    )


# --- Internals --------------------------------------------------------------------------------


Fact: TypeAlias = "tuple[NodeRef, str]"


class _Resolver:
    """The fold behind ``resolve``: claim versions, the assertion each is a version of, and the
    current versions of each ``one`` fact, as assertions arrive and builds land."""

    def __init__(self, registry: PredicateRegistry, priorities: Mapping[str, int]) -> None:
        self.registry = registry
        self.priorities = priorities
        self.resolver_hash = resolver_config_hash(registry, priorities)
        self.by_resolver = FindingProvenance(RESOLVER_ID, RESOLVER_VERSION, self.resolver_hash)
        self.versions: dict[ClaimId, Claim] = {}
        self.origin: dict[ClaimId, Claim] = {}  # version id -> the assertion it is a version of
        self.current: dict[Fact, list[ClaimId]] = {}  # current versions of each ``one`` fact
        self.roots: dict[Fact, list[Claim]] = {}  # every assertion of each ``one`` fact, arrived
        self.findings: list[ResolutionFinding] = []
        self.latest: dict[str, Lineage] = {}  # consolidator id -> its latest lineage so far
        self.withdrawn: set[ClaimId] = set()  # assertions a build withdrew and none re-emitted
        self.by_lineage: dict[Lineage, list[Claim]] = {}  # every assertion arrived, by lineage
        self.versions_of: dict[ClaimId, list[ClaimId]] = {}  # assertion id -> its version ids

    def _add(self, version: Claim, root: Claim) -> None:
        """Record ``version`` as a version of the assertion ``root``."""
        self.versions[version.id] = version
        if version.id not in self.origin:
            self.versions_of.setdefault(root.id, []).append(version.id)
        self.origin[version.id] = root

    def _one(self, claim: Claim) -> bool:
        return self.registry.spec(claim.predicate).cardinality is Cardinality.ONE

    def arrive(self, arriving: Claim) -> None:
        """An assertion's first recording (ADR 0005), retiring its consolidator's other lineages."""
        arriving_lineage = lineage_of(arriving)
        cid = arriving.provenance.consolidator_id
        if self.latest.get(cid, arriving_lineage) != arriving_lineage:
            self._retire(arriving_lineage, arriving.recorded_at)
        self.latest[cid] = arriving_lineage
        self._add(arriving, arriving)
        self.by_lineage.setdefault(arriving_lineage, []).append(arriving)
        if not self._one(arriving):
            return
        self.roots.setdefault((arriving.subject, arriving.predicate), []).append(arriving)
        self._place(arriving, arriving.recorded_at)

    def _place(self, arriving: Claim, tx: LedgerTx) -> None:
        """Contest ``arriving`` against the current versions of its ``one`` fact (ADR 0005)."""
        versions, origin, resolver_hash = self.versions, self.origin, self.resolver_hash
        live = self.current.setdefault((arriving.subject, arriving.predicate), [])
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
                self.findings.append(
                    ResolutionFinding(
                        FindingCode.OVERRIDDEN_ON_ARRIVAL,
                        arriving.id,
                        tuple(sorted({w.id for w in winners})),
                        self.by_resolver,
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
                self._add(narrowed, root)
                live.append(narrowed.id)
        versions[arriving.id] = replace(stored, supersedes=tuple(sorted(r.id for r in losers)))
        for version in effective:
            if version is not arriving:
                self._add(version, arriving)
            live.append(version.id)

    def apply_builds(self, builds: Sequence[Build], tx: LedgerTx) -> None:
        """ADR 0007 §5 and ADR 0016 §2: the builds landing at ``tx``, before its arrivals.

        A build of a new lineage retires the consolidator's other lineages; every build withdraws
        what its lineage held and it did not emit, and restates what it emitted that an earlier
        build had withdrawn. Then each ``one`` fact that lost or regained an assertion is placed
        again, so what a withdrawn claim had cut is held again by the claims still standing.
        """
        touched: set[Fact] = set()
        for build in builds:
            if self.latest.get(build.consolidator_id, build.lineage) != build.lineage:
                touched |= self._retire(build.lineage, tx)
            self.latest[build.consolidator_id] = build.lineage
            touched |= self._withdraw(build)
            for claim_id in build.claims:
                if claim_id not in self.withdrawn:
                    continue
                self.withdrawn.discard(claim_id)
                root = self.origin[claim_id]
                if self._one(root):
                    touched.add((root.subject, root.predicate))
                else:
                    self._add(_restatement(root, root.valid, (), tx, self.resolver_hash), root)
        for fact in sorted(touched, key=lambda f: (f[0].node_type, f[0].node_id, f[1])):
            self._replace(fact, tx)

    def _live(self, root: Claim) -> bool:
        cid = root.provenance.consolidator_id
        return root.id not in self.withdrawn and self.latest.get(cid) == lineage_of(root)

    def _replace(self, fact: Fact, tx: LedgerTx) -> None:
        """Place a ``one`` fact again from its standing assertions, in arrival order.

        The standing assertions (not withdrawn, of their consolidator's latest lineage) contest
        each other as ``_place`` would have had they been the only ones to arrive. One whose
        current pieces already are what it holds keeps its versions; any other has them superseded
        at ``tx`` and is restated over the pieces it now holds.
        """
        standing = sorted(
            (r for r in self.roots.get(fact, ()) if self._live(r)),
            key=lambda c: arrival_key(c, self.priorities),
        )
        holds = _contest(standing)
        live = self.current.setdefault(fact, [])
        for root in standing:
            pieces = holds.get(root.id, [])
            cutters = _distinct(
                other
                for other in standing
                if other.id != root.id
                and _conflict(other, root)
                and any(p.overlaps(root.valid) for p in holds.get(other.id, ()))
            )
            restated = [_restatement(root, p, cutters, tx, self.resolver_hash) for p in pieces]
            held = [self.versions[vid] for vid in live if self.origin[vid].id == root.id]
            if sorted(map(_placement, held)) == sorted(map(_placement, restated)):
                continue  # the same pieces on the same grounds: its versions stand
            for version in held:
                self._end(version.id, tx)
            for version in restated:
                self._add(version, root)
                live.append(version.id)

    def _retire(self, upgrade: Lineage, tx: LedgerTx) -> set[Fact]:
        """ADR 0003 §3: a new lineage retires every current claim of the consolidator's others.

        Each such version gets ``superseded_at = tx``, closure versions included, even one
        narrowed at ``tx`` itself. Nothing is deleted and valid time is not cut, so ``as_of``
        before ``tx`` still answers from the old lineage. Returns the ``one`` facts that lost a
        version.
        """
        touched: set[Fact] = set()
        for lineage in [k for k in self.by_lineage if k[0] == upgrade[0] and k != upgrade]:
            for root in self.by_lineage[lineage]:
                for vid in self.versions_of[root.id]:
                    if isinstance(self.versions[vid].superseded_at, Open):
                        touched |= self._end(vid, tx)
        return touched

    def _withdraw(self, build: Build) -> set[Fact]:
        """ADR 0007 §5.2: a build ends every current version of its lineage that it did not emit.

        A version is its original assertion's: a split closure stays current while the build
        emits the assertion it narrows, and is withdrawn with it otherwise. An assertion with no
        current version is withdrawn too, so it stands in no later contest until a build emits it
        again. Returns the ``one`` facts that lost an assertion.
        """
        emitted = set(build.claims)
        gone = {
            root.id: root
            for root in self.by_lineage.get(build.lineage, ())
            if root.id not in emitted and root.id not in self.withdrawn
        }
        if not gone:
            return set()
        self.withdrawn.update(gone)
        touched = {(r.subject, r.predicate) for r in gone.values() if self._one(r)}
        for root_id in gone:
            for vid in self.versions_of[root_id]:
                if isinstance(self.versions[vid].superseded_at, Open):
                    self._end(vid, build.recorded_at)
        return touched

    def _end(self, vid: ClaimId, tx: LedgerTx) -> set[Fact]:
        version = self.versions[vid] = replace(self.versions[vid], superseded_at=tx)
        fact = (version.subject, version.predicate)
        live = self.current.get(fact)
        if live is not None and vid in live:
            live.remove(vid)
            return {fact}
        return set()


def _check_builds(
    claims: Sequence[Claim], inputs: Sequence[Claim], builds: Sequence[Build]
) -> None:
    """Builds agree with the claims: one per consolidator and transaction, naming only claims of
    their lineage recorded by then, and holding every recording of a consolidator that has any."""
    by_key: dict[tuple[str, LedgerTx], Build] = {}
    for build in builds:
        key = (build.consolidator_id, build.recorded_at)
        if key in by_key:
            raise ValueError(f"two builds of {key[0]} at transaction {key[1]}")
        by_key[key] = build
    first = {c.id: c for c in inputs}
    for build in builds:
        foreign = [
            i
            for i in build.claims
            if i not in first
            or lineage_of(first[i]) != build.lineage
            or first[i].recorded_at > build.recorded_at
        ]
        if foreign:
            raise ValueError(
                f"the build of {build.consolidator_id} at transaction {build.recorded_at} names"
                f" claims of another lineage, or not recorded by then: {foreign[:3]}"
            )
    built = {cid for cid, _ in by_key}
    emitted = {key: frozenset(build.claims) for key, build in by_key.items()}
    for claim in claims:
        cid = claim.provenance.consolidator_id
        if is_closure(claim) or cid not in built:
            continue
        if claim.id not in emitted.get((cid, claim.recorded_at), frozenset()):
            raise ValueError(
                f"claim {claim.id} of {cid} is recorded at transaction {claim.recorded_at}"
                " outside a build that emitted it"
            )


def _check_lineages(claims: Iterable[Claim], built: Iterable[Build] = ()) -> None:
    """One lineage per consolidator per transaction, and no lineage back after another replaced it.

    Checked over every recording (before ``assertions`` keeps only the earliest) and every build,
    so a re-run of a retired lineage is caught even when it reproduces the old claim ids or emits
    nothing.
    """
    builds: dict[str, dict[int, set[Lineage]]] = {}
    for claim in claims:
        if not is_closure(claim):
            by_tx = builds.setdefault(claim.provenance.consolidator_id, {})
            by_tx.setdefault(claim.recorded_at, set()).add(lineage_of(claim))
    for build in built:
        by_tx = builds.setdefault(build.consolidator_id, {})
        by_tx.setdefault(build.recorded_at, set()).add(build.lineage)
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
    that took the rest); its model is ``root``'s (ADR 0006 §3); its ``config_hash`` covers the
    resolver's configuration and the version it narrows, so pieces of different versions never
    share an id even when they share evidence.
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
        model=root.provenance.model,
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


def _conflict(one: Claim, other: Claim) -> bool:
    """Different objects over overlapping valid time on one clock: a contest (ADR 0005)."""
    return (
        _object_key(one) != _object_key(other)
        and one.valid_from.domain_id == other.valid_from.domain_id
        and one.valid.overlaps(other.valid)
    )


def _contest(standing: Sequence[Claim]) -> dict[ClaimId, list[Interval]]:
    """What each assertion of one ``one`` fact holds when they arrive in this order alone.

    The same contest as ``_Resolver._place``, over intervals rather than versions: an arriving
    assertion keeps what no held winner covers, and cuts what it beats out of the losers it
    overlaps.
    """
    held: list[tuple[Claim, Interval]] = []
    for root in standing:
        claimed = _object_key(root)
        domain = root.valid_from.domain_id
        overlapping = [
            (other, piece)
            for other, piece in held
            if _object_key(other) != claimed
            and piece.start.domain_id == domain
            and piece.overlaps(root.valid)
        ]
        effective = list(root.valid.minus(p for o, p in overlapping if not _beats(root, o)))
        losers = [
            (other, piece)
            for other, piece in overlapping
            if _beats(root, other) and any(piece.overlaps(e) for e in effective)
        ]
        kept = [h for h in held if h not in losers]
        for other, piece in losers:
            kept.extend((other, p) for p in piece.minus(effective))
        kept.extend((root, e) for e in effective)
        held = kept
    holds: dict[ClaimId, list[Interval]] = {}
    for root, piece in held:
        holds.setdefault(root.id, []).append(piece)
    return holds


def _valid_key(interval: Interval) -> bytes:
    return dumps(interval.to_json())


def _placement(version: Claim) -> tuple[bytes, tuple[RecordId, ...], bytes]:
    """What a re-placement compares: a version's interval and the grounds it rests on."""
    evidence = sorted(dumps(e.to_json()) for e in version.provenance.evidence)
    return (_valid_key(version.valid), version.provenance.records, b"\n".join(evidence))


def _restatement(
    root: Claim,
    valid: Interval,
    cutters: Sequence[Claim],
    recorded_at: LedgerTx,
    resolver_hash: ConfigHash,
) -> Claim:
    """``root`` held again over ``valid`` from ``recorded_at`` (ADR 0016 §2): a resolver version.

    Like a split closure, its evidence is ``root``'s then the ``cutters``', and its ``config_hash``
    covers the resolver configuration, the assertion it restates and the transaction, so a claim
    withdrawn and restated more than once never repeats an id. It ``supersedes`` ``root``.
    """
    closure = _closure(root, root, valid, cutters, recorded_at, resolver_hash)
    provenance = replace(
        closure.provenance,
        config_hash=config_hash(
            {"at": recorded_at, "resolver": resolver_hash, "restates": root.id}
        ),
    )
    return replace(closure, provenance=provenance)


def _clock_mismatches(
    versions: Iterable[Claim],
    origin: Mapping[ClaimId, Claim],
    registry: PredicateRegistry,
    priorities: Mapping[str, int],
    provenance: FindingProvenance,
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
                                FindingCode.CLOCK_MISMATCH,
                                later.id,
                                (earlier.id,),
                                provenance,
                                start,
                                end,
                            )
                        )
    return found
