"""``why(claim_id)`` (ADR 0010 §3): the provenance tree of one claim, down to the bytes it cites.

The root is the claim as Memory knows it at the packet's snapshot. Below it, in pre-order, are
the claims that bear on it, each a relation Memory itself states:

- ``corroborates``: a current claim with the same subject, predicate and object, valid over an
  overlapping interval on the same clock, asserted from other evidence or by another
  consolidator;
- ``conflicts``: a claim a resolver finding names together with it (``clock_mismatch``: the
  same fact with another object on another clock; ``overridden_on_arrival``);
- ``alternative``: an undecided reading, where one of the two is a ``*_candidate`` claim of the
  same predicate family with another object, overlapping on the same clock.

Each step cites its claim's evidence refs; the engine resolves them through the Ledger into
evidence items (status and size at the snapshot), so the tree reaches source artefacts and
locators. Relations are symmetric, so the edge back to a step's parent is not repeated; any
other claim reached twice (a cycle) is a ``repeat`` step that is not expanded again. Depth,
fan-out and size are capped, and every cut is a gap naming what was not followed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from neptune_memory.schema.interval import Open
from neptune_memory.schema.predicates import SAME_AS, SAME_AS_CANDIDATE

from neptune_context import pinned
from neptune_context.explain.run import object_key, weight
from neptune_context.packets.model import GapCode
from neptune_context.packets.trails import Relation, WhyStep, WhyTrail

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim, ClaimId
    from neptune_memory.schema.supersede import FindingId

    from neptune_context.explain.run import Run

CANDIDATE: Final = "_candidate"
DECAY: Final = 0.5  # each level below the root halves a claim's score
_ORDER: Final = {Relation.CORROBORATES: 0, Relation.CONFLICTS: 1, Relation.ALTERNATIVE: 2}


def family(predicate: str) -> frozenset[str]:
    """A predicate and its ``*_candidate`` twin, as far as the pinned vocabulary has them."""
    base = predicate.removesuffix(CANDIDATE)
    if predicate == SAME_AS_CANDIDATE:
        base = SAME_AS
    return frozenset({base, base + CANDIDATE} & pinned.predicates()) | {predicate}


def _overlap(a: Claim, b: Claim) -> bool:
    return a.valid.domain_id == b.valid.domain_id and a.valid.overlaps(b.valid)


class _Tree:
    """One why tree: pre-order steps, what to carry, what was cut."""

    def __init__(self, run: Run, at: str) -> None:
        self.run = run
        self.at = at
        self.steps: list[WhyStep] = []
        self.shown: set[ClaimId] = set()
        self.withheld: set[str] = set()
        self.named: dict[str, set[str]] = {}  # reason -> claims named, not carried
        self.unfollowed: set[str] = set()  # cut by depth, fan-out or size
        self.missing: set[str] = set()  # a finding names a claim Memory cannot produce

    def related(self, claim: Claim) -> list[tuple[Relation, Claim, FindingId | None]]:
        """The claims that bear on ``claim`` at Memory's snapshot, in a fixed order."""
        run = self.run
        result = run.about_subject(claim.subject, int(run.as_of))
        pool = {c.id: c for c in (*result.claims, *result.other_clocks)}
        out: dict[ClaimId, tuple[Relation, Claim, FindingId | None]] = {}
        names = family(claim.predicate)
        for other in pool.values():
            if other.id == claim.id or not _overlap(claim, other):
                continue
            if other.predicate == claim.predicate and object_key(other) == object_key(claim):
                out[other.id] = (Relation.CORROBORATES, other, None)
            elif (
                other.predicate in names
                and object_key(other) != object_key(claim)
                and CANDIDATE in other.predicate + claim.predicate
            ):
                out.setdefault(other.id, (Relation.ALTERNATIVE, other, None))
        for finding in result.findings:
            named = {finding.claim, *finding.others}
            if claim.id not in named or pinned.finding_beyond_pin(finding) is not None:
                continue
            run.findings.setdefault(finding.id, finding)
            for other_id in sorted(named - {claim.id}):
                rival = pool.get(other_id) or (
                    run.history.version(other_id) if run.history is not None else None
                )
                if rival is None:
                    self.missing.add(other_id)
                    continue
                if other_id not in out or out[other_id][0] is not Relation.CORROBORATES:
                    out[other_id] = (Relation.CONFLICTS, rival, finding.id)
        return sorted(out.values(), key=lambda r: (_ORDER[r[0]], r[1].id))

    def visit(
        self,
        claim: Claim,
        parent: Claim | None,
        relation: Relation,
        depth: int,
        finding: FindingId | None,
    ) -> None:
        run, caps = self.run, self.run.caps
        if len(self.steps) >= caps.steps:
            self.unfollowed.add(claim.id)
            return
        repeat = claim.id in self.shown
        self.steps.append(
            WhyStep(
                claim.id,
                None if parent is None else parent.id,
                relation,
                depth,
                claim.assertion_kind,
                claim.provenance.evidence,
                finding,
                repeat,
            )
        )
        if repeat:
            return
        self.shown.add(claim.id)
        score = float(weight(claim) * DECAY**depth)
        run.cite(claim, score)
        reason = run.unplaceable(claim)
        current = isinstance(claim.superseded_at, Open) and claim.recorded_at <= run.as_of
        if reason is not None:
            self.named.setdefault(reason, set()).add(claim.id)
        elif not current:
            self.named.setdefault(
                "not current at Memory's snapshot: named, not carried", set()
            ).add(claim.id)
        else:
            run.carry(claim, score)
        children = []
        for rel, other, why in self.related(claim):
            if parent is not None and other.id == parent.id:
                continue  # relations are symmetric: the edge back to the parent is this one
            if run.withheld(other):
                self.withheld.add(other.id)
                continue
            children.append((rel, other, why))
        if not children:
            return
        if depth >= caps.depth:
            self.unfollowed.update(o.id for _, o, _ in children if o.id not in self.shown)
            return
        if len(children) > caps.fan_out:
            self.unfollowed.update(o.id for _, o, _ in children[caps.fan_out :])
            children = children[: caps.fan_out]
        for rel, other, why in children:
            self.visit(other, claim, rel, depth + 1, why)

    def finish(self) -> None:
        run, at = self.run, self.at
        if self.withheld:
            run.gap(
                GapCode.INFERRED_WITHHELD,
                at,
                self.withheld,
                "inferred claims that bear on this one, withheld because the query excludes"
                " inference",
            )
        for reason, ids in sorted(self.named.items()):
            run.gap(GapCode.NOT_COVERED, at, ids, f"in this why tree, {reason}")
        if self.unfollowed:
            caps = run.caps
            run.gap(
                GapCode.NOT_COVERED,
                at,
                self.unfollowed,
                f"the why tree stops at depth {caps.depth}, {caps.fan_out} claims per step and"
                f" {caps.steps} steps: these claims bear on it and were not followed",
            )
        if self.missing:
            run.gap(
                GapCode.UNKNOWN,
                at,
                self.missing,
                "a resolver finding names these claims and Memory produced none of them",
            )


def explain_why(run: Run, index: int, claim_id: str) -> WhyTrail | None:
    """The why trail for ``claim_id`` at Memory's snapshot, or ``None`` and a gap saying why not."""
    at = f"/explain/{index}"
    history, as_of = run.history, int(run.as_of)
    if history is None:
        run.gap(
            GapCode.NOT_COVERED,
            at,
            [claim_id],
            "this Memory reader cannot look a claim up by id (no ClaimHistory): why is not"
            " answered",
        )
        return None
    stored = history.version(claim_id)
    if stored is None:
        run.gap(
            GapCode.NOT_COVERED,
            at,
            [claim_id],
            f"Memory holds no claim with this id (generation {run.memory.generation})",
        )
        return None
    if stored.recorded_at > as_of:
        run.gap(
            GapCode.NOT_COVERED,
            at,
            [claim_id],
            f"Memory recorded this claim at transaction {stored.recorded_at}, after its snapshot"
            f" {as_of}: ask with as_of {stored.recorded_at} or later",
        )
        return None
    if not isinstance(stored.superseded_at, Open) and stored.superseded_at <= as_of:
        later = sorted(
            c.id for c in history.superseded_by(claim_id) if c.recorded_at == stored.superseded_at
        )
        run.gap(
            GapCode.NOT_COVERED,
            at,
            [claim_id, *later],
            f"superseded at transaction {stored.superseded_at}, so not held at {as_of}; ask with"
            f" as_of {stored.superseded_at - 1} to see why it was held"
            + (" (the other refs superseded it)" if later else ""),
        )
        return None
    present = run.about_subject(stored.subject, as_of)
    root = next((c for c in (*present.claims, *present.other_clocks) if c.id == claim_id), None)
    if root is None:
        run.gap(
            GapCode.UNKNOWN,
            at,
            [claim_id],
            f"Memory's history says this claim is current at {as_of}, but its reader does not"
            " return it",
        )
        return None
    if run.withheld(root):
        run.gap(
            GapCode.INFERRED_WITHHELD,
            at,
            [claim_id],
            "the claim is inferred and the query excludes inference",
        )
        return None
    tree = _Tree(run, at)
    tree.visit(root, None, Relation.ROOT, 0, None)
    tree.finish()
    return WhyTrail(at, root.id, tuple(tree.steps))
