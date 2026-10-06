"""``diff(subject, before, after)`` (ADR 0010 §4): what changed about one subject, by predicate.

The claims compared are those whose subject or object is the subject's node, or a node it is
declared ``same_as`` (up to the subject's ``same_as_depth``; candidates are never followed).

- **Two transactions** (what Memory knew): the claims current at ``before`` against those current
  at ``after``. A claim current at ``before`` and not at ``after`` is ``closed`` when the versions
  that replaced it (through Memory's ``supersedes`` chain, or a new lineage's restatement with
  the same start) keep its object over a strictly narrower interval, or when nothing replaced it
  (a retired lineage); it is ``superseded`` when any replacing version differs otherwise. A claim
  current at ``after`` that replaced nothing is ``opened``.
- **Two instants on one clock** (what held, as known at the packet's snapshot): the facts
  (subject, predicate, object) valid at ``before`` against those valid at ``after``; a fact that
  holds at both is no change, whichever claims carry it. A claim whose fact held only at
  ``before`` is ``closed``, or ``superseded`` when a claim with the same subject and predicate
  (and so another object) took over at the very tick it ended; one whose fact holds only at
  ``after`` is ``opened``. Claims on other clocks are named, never compared; instants on two
  clocks are not compared at all (no conversion is assumed).

Claims current at the packet's snapshot are carried as claim items; others (old versions) are
named by id, and ``why`` with ``as_of`` before the change shows them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from neptune_memory.schema.interval import Open
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.predicates import SAME_AS

from neptune_context import pinned
from neptune_context.answer import domain_id
from neptune_context.explain.run import object_key, weight
from neptune_context.packets.model import GapCode
from neptune_context.packets.trails import (
    MAX_DIFF_NODES,
    Change,
    DiffChange,
    DiffPoint,
    DiffTrail,
    TxPoint,
    WorldPoint,
)
from neptune_context.query.model import Instant

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim, ClaimId

    from neptune_context.explain.run import Run
    from neptune_context.query.model import Diff


def _node_key(node: NodeRef) -> tuple[str, str]:
    return (str(node.node_type), node.node_id)


def _within(inner: Claim, outer: Claim) -> bool:
    """``inner``'s valid interval is a strict part of ``outer``'s, on the same clock."""
    if inner.valid.domain_id != outer.valid.domain_id:
        return False
    a_end, b_end = inner.valid_to, outer.valid_to
    starts_inside = inner.valid_from.ticks >= outer.valid_from.ticks
    ends_inside = isinstance(b_end, Open) or (
        not isinstance(a_end, Open) and a_end.ticks <= b_end.ticks
    )
    same = inner.valid_from == outer.valid_from and a_end == b_end
    return starts_inside and ends_inside and not same


def _inside(claim: Claim, t1: int, t2: int) -> bool:
    """``claim`` starts after ``t1`` and before ``t2`` (so, held at neither, it ends by ``t2``)."""
    return t1 < claim.valid_from.ticks < t2


def _held(claim: Claim, ticks: int) -> bool:
    end = claim.valid_to
    return claim.valid_from.ticks <= ticks and (isinstance(end, Open) or ticks < end.ticks)


class _Diff:
    def __init__(self, run: Run, index: int, clause: Diff) -> None:
        self.run = run
        self.at = f"/explain/{index}"
        self.clause = clause
        self.withheld: set[str] = set()
        self.named: dict[str, set[str]] = {}
        self.nodes: tuple[NodeRef, ...] = ()
        self.passing: dict[ClaimId, Claim] = {}  # versions recorded and replaced in between

    def identities(self, subject: NodeRef) -> tuple[NodeRef, ...]:
        """``subject`` and the nodes it is declared ``same_as`` within the subject's depth, at
        most ``MAX_DIFF_NODES`` (a gap names the rest); the ``same_as`` claims followed are
        carried, as the graph channel carries them."""
        run, depth = self.run, self.clause.subject.same_as_depth
        seen, frontier = {subject}, [subject]
        beyond: set[str] = set()
        for _ in range(depth):
            reached: list[NodeRef] = []
            for here in sorted(frontier, key=_node_key):
                for claim in run.touching(here, int(run.as_of)):
                    if claim.predicate != SAME_AS or run.withheld(claim):
                        continue
                    if run.unplaceable(claim) is not None:
                        continue
                    other = claim.object if claim.subject == here else claim.subject
                    if not isinstance(other, NodeRef) or other in seen:
                        continue
                    if len(seen) >= MAX_DIFF_NODES:
                        beyond.add(other.node_id)
                        continue
                    seen.add(other)
                    reached.append(other)
                    run.carry(claim, weight(claim))
            frontier = reached
        if beyond:
            run.gap(
                GapCode.NOT_COVERED,
                f"{self.at}/subject",
                beyond,
                f"a diff compares at most {MAX_DIFF_NODES} identities of its subject; these"
                " declared identities were not compared",
            )
        return tuple(sorted(seen, key=_node_key))

    def about(self, nodes: tuple[NodeRef, ...], tx: int) -> dict[ClaimId, Claim]:
        """Claims touching ``nodes`` current at ``tx``; withheld and beyond-pin ones set aside."""
        out: dict[ClaimId, Claim] = {}
        for node in nodes:
            for claim in self.run.touching(node, tx):
                if self.run.withheld(claim):
                    self.withheld.add(claim.id)
                elif (reason := pinned.claim_beyond_pin(claim)) is not None:
                    self.named.setdefault(
                        f"{reason} is newer than Context's pinned graph-schema: not compared",
                        set(),
                    ).add(claim.id)
                else:
                    out[claim.id] = claim
        return out

    def successors(self, old: Claim, after: dict[ClaimId, Claim], tx: int) -> set[ClaimId]:
        """Versions current at ``tx`` that replaced ``old``, through ``supersedes`` chains."""
        history = self.run.history
        if history is None:
            return {c.id for c in after.values() if old.id in c.supersedes}
        found: set[ClaimId] = set()
        seen, todo = {old.id}, [old.id]
        while todo:
            for later in history.superseded_by(todo.pop()):
                if later.recorded_at > tx or later.id in seen:
                    continue
                seen.add(later.id)
                if later.id in after:
                    found.add(later.id)
                else:
                    self.passing.setdefault(later.id, later)
                    todo.append(later.id)
        return found

    @staticmethod
    def restated(old: Claim, after: dict[ClaimId, Claim]) -> set[ClaimId]:
        """Claims current at ``after`` that restate ``old`` over a narrower interval: same
        subject, predicate, object, clock and start, an earlier end. A consolidator's new
        lineage retires the old one without ``supersedes`` links (Memory ADR 0003 §3); this is
        how its narrowing still reads as ``closed``, never as an unrelated claim opening."""
        end = old.valid_to

        def narrower(claim: Claim) -> bool:
            stop = claim.valid_to
            if isinstance(stop, Open):
                return False
            return isinstance(end, Open) or stop.ticks < end.ticks

        return {
            c.id
            for c in after.values()
            if c.subject == old.subject
            and c.predicate == old.predicate
            and object_key(c) == object_key(old)
            and c.valid_from == old.valid_from
            and narrower(c)
        }

    def transactions(self, nodes: tuple[NodeRef, ...], before: int, after: int) -> list[DiffChange]:
        held_before, held_after = self.about(nodes, before), self.about(nodes, after)
        changes: list[DiffChange] = []
        replacements: set[ClaimId] = set()
        fresh = {i: c for i, c in held_after.items() if i not in held_before}
        for old in sorted(held_before.values(), key=lambda c: c.id):
            if old.id in held_after:
                continue
            later = self.successors(old, held_after, after) or self.restated(old, fresh)
            # Memory supersedes within one subject and predicate; anything else opens on its own.
            later = {i for i in later if held_after[i].predicate == old.predicate}
            replacements |= later
            narrowed = all(
                object_key(held_after[i]) == object_key(old) and _within(held_after[i], old)
                for i in later
            )
            kind = Change.CLOSED if narrowed else Change.SUPERSEDED
            changes.append(DiffChange(old.predicate, kind, (old.id,), tuple(sorted(later))))
        for new in sorted(held_after.values(), key=lambda c: c.id):
            if new.id not in held_before and new.id not in replacements:
                changes.append(DiffChange(new.predicate, Change.OPENED, (), (new.id,)))
        # Versions recorded and replaced between the two transactions, met on a chain.
        for passing in sorted(self.passing.values(), key=lambda c: c.id):
            if self.run.withheld(passing):
                self.withheld.add(passing.id)
            elif passing.id not in held_before and passing.id not in held_after:
                changes.append(DiffChange(passing.predicate, Change.BETWEEN, (), (passing.id,)))
        return changes

    def instants(
        self, nodes: tuple[NodeRef, ...], clock: str, t1: int, t2: int
    ) -> list[DiffChange]:
        claims = self.about(nodes, int(self.run.as_of))
        elsewhere = {i for i, c in claims.items() if str(c.valid.domain_id) != clock}
        if elsewhere:
            self.run.gap(
                GapCode.OTHER_CLOCK,
                self.at,
                elsewhere,
                f"claims about the subject on clocks other than the diff's ({clock}): named,"
                " not compared",
            )
        on = [c for i, c in sorted(claims.items()) if i not in elsewhere]

        def fact(claim: Claim) -> tuple[NodeRef, str, bytes]:
            return (claim.subject, claim.predicate, object_key(claim))

        at_before = [c for c in on if _held(c, t1)]
        at_after = [c for c in on if _held(c, t2)]
        facts_before = {fact(c) for c in at_before}
        facts_after = {fact(c) for c in at_after}
        # A fact that holds at both instants did not change there, whichever claims carry it.
        closed = [c for c in at_before if fact(c) not in facts_after]
        opened = [c for c in at_after if fact(c) not in facts_before]
        changes: list[DiffChange] = []
        took_over: set[ClaimId] = set()
        for old in closed:
            end = old.valid_to
            later = sorted(
                c.id
                for c in opened
                if not isinstance(end, Open)
                and c.subject == old.subject
                and c.predicate == old.predicate
                and c.valid_from.ticks == end.ticks
            )
            took_over.update(later)
            kind = Change.SUPERSEDED if later else Change.CLOSED
            changes.append(DiffChange(old.predicate, kind, (old.id,), tuple(later)))
        for new in opened:
            if new.id not in took_over:
                changes.append(DiffChange(new.predicate, Change.OPENED, (), (new.id,)))
        # Claims valid at neither instant but in between: opened and closed inside the window.
        for passing in on:
            if not _held(passing, t1) and not _held(passing, t2) and _inside(passing, t1, t2):
                changes.append(DiffChange(passing.predicate, Change.BETWEEN, (), (passing.id,)))
        return changes

    def run_diff(self) -> DiffTrail | None:
        run, at, clause = self.run, self.at, self.clause
        subject = clause.subject
        if subject.kind not in pinned.node_types() or subject.declared_id is None:
            run.gap(
                GapCode.NOT_COVERED,
                f"{at}/subject",
                [subject.declared_id or subject.kind],
                f"{subject.kind!r} is not a Memory node type: Memory holds no claims about it",
            )
            return None
        node = NodeRef(NodeType(subject.kind), subject.declared_id)
        as_of = int(run.as_of)
        before, after = clause.before, clause.after
        point_before: DiffPoint
        point_after: DiffPoint
        if isinstance(before, Instant) and isinstance(after, Instant):
            clock, other = domain_id(before.clock), domain_id(after.clock)
            if clock != other:
                run.gap(
                    GapCode.NOT_COVERED,
                    at,
                    [clock, other],
                    "the diff's instants are on two clocks; Context does not carry an instant"
                    " from one clock to another, so they are not compared",
                )
                return None
            nodes = self.nodes = self.identities(node)
            changes = self.instants(nodes, clock, before.ticks, after.ticks)
            point_before = WorldPoint(clock, before.ticks)  # type: ignore[arg-type]
            point_after = WorldPoint(clock, after.ticks)  # type: ignore[arg-type]
        else:
            assert isinstance(before, int) and isinstance(after, int)
            if after > as_of:
                run.gap(
                    GapCode.NOT_COVERED,
                    f"{at}/after",
                    [],
                    f"Memory's snapshot is transaction {as_of}; it cannot say what it knew at"
                    f" {after}",
                )
                return None
            nodes = self.nodes = self.identities(node)
            changes = self.transactions(nodes, before, after)
            point_before, point_after = TxPoint(before), TxPoint(after)  # type: ignore[arg-type]
        if all(not run.touching(n, as_of) for n in nodes) and not changes:
            run.gap(
                GapCode.NOT_COVERED,
                f"{at}/subject",
                [node.node_id],
                f"Memory holds no claim about {node.node_type} {node.node_id!r} at transaction"
                f" {as_of}",
            )
        changes = self.capped(sorted(changes, key=lambda c: c.sort_key()))
        self.carry(changes, as_of)
        self.finish()
        return DiffTrail(at, node, nodes, point_before, point_after, tuple(changes))

    def capped(self, changes: list[DiffChange]) -> list[DiffChange]:
        cap = self.run.caps.diff_claims
        kept: list[DiffChange] = []
        named = 0
        cut: set[str] = set()
        for change in changes:
            size = len(change.before) + len(change.after)
            if named + size > cap:
                cut.update((*change.before, *change.after))
                continue
            named += size
            kept.append(change)
        if cut:
            self.run.gap(
                GapCode.NOT_COVERED,
                self.at,
                cut,
                f"the diff names at most {cap} claims; these changed and are not listed",
            )
        return kept

    def carry(self, changes: list[DiffChange], as_of: int) -> None:
        """Carry each changed claim current at Memory's snapshot; name the rest."""
        run = self.run
        current: dict[ClaimId, Claim] = {}
        for node in self.nodes:
            for claim in run.touching(node, as_of):
                current[claim.id] = claim
        older: set[str] = set()
        for change in changes:
            for claim_id in (*change.before, *change.after):
                held = current.get(claim_id)
                if held is None:
                    older.add(claim_id)
                    continue
                reason = run.unplaceable(held)
                if reason is not None:
                    self.named.setdefault(reason, set()).add(claim_id)
                    continue
                run.carry(held, weight(held))
                run.cite(held, weight(held))
        if older:
            self.named.setdefault(
                "no longer current at Memory's snapshot: named, not carried (why with an earlier"
                " as_of shows each)",
                set(),
            ).update(older)

    def finish(self) -> None:
        run, at = self.run, self.at
        if self.withheld:
            run.gap(
                GapCode.INFERRED_WITHHELD,
                at,
                self.withheld,
                "inferred claims about the subject, withheld because the query excludes inference",
            )
        for reason, ids in sorted(self.named.items()):
            run.gap(GapCode.NOT_COVERED, at, ids, f"in this diff, {reason}")


def explain_diff(run: Run, index: int, clause: Diff) -> DiffTrail | None:
    """The diff trail for ``clause`` at Memory's snapshot, or ``None`` and a gap saying why not."""
    return _Diff(run, index, clause).run_diff()
