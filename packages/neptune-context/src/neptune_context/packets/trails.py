"""Explain trails (ADR 0010): the structure of a ``why`` or ``diff`` answer inside a packet.

A packet's items are a flat, ranked list. An ``explain`` clause asks for structure: *why* memory
holds one claim (the claims that corroborate, conflict with or offer an alternative to it, each
down to the evidence it cites) and *what changed* about one subject between two points (claims
opened, closed and superseded, by predicate). A trail records that structure by naming claims by
id; the claims themselves are ``ClaimItem``s in the same packet whenever they are current at the
packet's snapshot. A claim a trail names but the packet does not carry (one no longer current,
cut by the budget, or not in the pinned graph-schema) is named, never described: the packet
says why in a gap at the clause's pointer.

Trails add no fact of their own. Every relation is one Memory already states: equal assertions
(corroboration), a resolver finding (conflict), a ``*_candidate`` reading (alternative), a
``supersedes`` link or a valid interval (change). The packet checks a trail's shape and that it
agrees with the items it names; ``answer.answer_problems`` checks it answers the clause it
points at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Final, TypeAlias

from neptune_memory.schema.claim import ClaimAssertionKind, ClaimId, is_inferred, parse_claim_id
from neptune_memory.schema.interval import LedgerTx, ledger_tx
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.supersede import FindingId, parse_finding_id

from neptune.model.ids import RecordId, check_token, parse_record_id
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import EvidenceRef
from neptune.model.time import INT64_MAX, INT64_MIN
from neptune_context.packets.findings import PacketError
from neptune_context.packets.findings import PacketFindingCode as Code

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue

MAX_TRAILS: Final = 16  # one per explain clause (ADR 0002 §8)
MAX_WHY_STEPS: Final = 256  # claims one why tree may name
MAX_WHY_DEPTH: Final = 8
MAX_DIFF_CLAIMS: Final = 1024  # claim ids one diff may name across its changes
MAX_DIFF_NODES: Final = 64
TRAIL_AT: Final = re.compile(r"/explain/(0|[1-9][0-9]?)")


def _fail(code: Code, message: str) -> PacketError:
    return PacketError(code, message)


def _claim(value: object, what: str) -> ClaimId:
    try:
        return parse_claim_id(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise _fail(Code.BAD_VALUE, f"{what}: {exc}") from exc


def _sorted_unique(keys: list[object], what: str) -> None:
    if len(set(keys)) != len(keys):
        raise _fail(Code.DUPLICATE, f"{what} repeat")
    if keys != sorted(keys):  # type: ignore[type-var]
        raise _fail(Code.ORDER, f"{what} must be sorted")


def _claims(values: tuple[ClaimId, ...], what: str) -> None:
    if not isinstance(values, tuple):
        raise _fail(Code.SHAPE, f"{what} must be a tuple of claim ids")
    for value in values:
        _claim(value, what)
    _sorted_unique(list(values), what)


def trail_index(at: str) -> int:
    """The explain clause a trail answers: ``/explain/3`` is clause 3."""
    if not isinstance(at, str) or not TRAIL_AT.fullmatch(at):
        raise _fail(Code.BAD_VALUE, f"a trail's at is /explain/<0..15>, got {at!r}")
    index = int(at.rsplit("/", 1)[1])
    if index >= MAX_TRAILS:
        raise _fail(Code.BAD_VALUE, f"a trail's at is /explain/<0..15>, got {at!r}")
    return index


# --- Why --------------------------------------------------------------------------------------


class Relation(StrEnum):
    """How a claim in a why tree bears on its parent. Each is a relation Memory states."""

    ROOT = "root"  # the claim asked about
    CORROBORATES = "corroborates"  # the same assertion from other evidence, overlapping, one clock
    CONFLICTS = "conflicts"  # a resolver finding names both (clock_mismatch, overridden_on_arrival)
    ALTERNATIVE = "alternative"  # an undecided reading: a *_candidate claim with another object


@dataclass(frozen=True)
class WhyStep:
    """One claim in a why tree, in pre-order.

    ``parent`` is ``None`` exactly for the root; ``depth`` is the parent's plus one. ``evidence``
    is the claim's provenance evidence in its own order, so a claim the packet cannot carry still
    cites its bytes. ``finding`` names the resolver finding behind a ``conflicts`` step. A
    ``repeat`` step names a claim shown earlier in the tree (a cycle, or a claim reached twice):
    it is not expanded again.
    """

    claim: ClaimId
    parent: ClaimId | None
    relation: Relation
    depth: int
    assertion_kind: ClaimAssertionKind
    evidence: tuple[EvidenceRef, ...]
    finding: FindingId | None = None
    repeat: bool = False

    def __post_init__(self) -> None:
        _claim(self.claim, "a why step's claim")
        if not isinstance(self.relation, Relation):
            raise _fail(Code.BAD_VALUE, f"relation must be a Relation: {self.relation!r}")
        root = self.relation is Relation.ROOT
        if (self.parent is None) != root:
            raise _fail(Code.BAD_VALUE, "exactly the root step has no parent")
        if self.parent is not None:
            _claim(self.parent, "a why step's parent")
            if self.parent == self.claim:
                raise _fail(Code.BAD_VALUE, "a claim is not its own parent")
        depth = self.depth
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_WHY_DEPTH:
            raise _fail(Code.BAD_VALUE, f"depth is an integer in [0, {MAX_WHY_DEPTH}]")
        if (depth == 0) != root:
            raise _fail(Code.BAD_VALUE, "exactly the root step is at depth 0")
        kind = self.assertion_kind
        if not (isinstance(kind, AssertionKind) or (type(kind) is str and kind == "inferred")):
            raise _fail(
                Code.BAD_VALUE, f"assertion_kind is observed, stated or 'inferred': {kind!r}"
            )
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise _fail(Code.BAD_VALUE, "a why step cites the claim's evidence (at least one ref)")
        for ref in self.evidence:
            if not isinstance(ref, EvidenceRef):
                raise _fail(Code.SHAPE, f"evidence must be EvidenceRefs, got {ref!r}")
        if len(set(self.evidence)) != len(self.evidence):
            raise _fail(Code.DUPLICATE, "a why step's evidence refs repeat")
        if (self.finding is not None) != (self.relation is Relation.CONFLICTS):
            raise _fail(Code.BAD_VALUE, "exactly a conflicts step names a resolver finding")
        if self.finding is not None:
            try:
                parse_finding_id(self.finding)
            except (TypeError, ValueError) as exc:
                raise _fail(Code.BAD_VALUE, f"finding: {exc}") from exc
        if not isinstance(self.repeat, bool):
            raise _fail(Code.SHAPE, "repeat must be a bool")
        if root and self.repeat:
            raise _fail(Code.BAD_VALUE, "the root is never a repeat")

    @property
    def is_inferred(self) -> bool:
        return is_inferred(self.assertion_kind)

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "assertion_kind": str(self.assertion_kind),
            "claim": self.claim,
            "depth": self.depth,
            "evidence": [ref.to_json() for ref in self.evidence],
            "relation": str(self.relation),
            "repeat": self.repeat,
        }
        if self.parent is not None:
            out["parent"] = self.parent
        if self.finding is not None:
            out["finding"] = self.finding
        return out


@dataclass(frozen=True)
class WhyTrail:
    """Why memory holds ``claim`` at the packet's snapshot: a tree of steps, root first."""

    kind: ClassVar[str] = "why"
    at: str
    claim: ClaimId
    steps: tuple[WhyStep, ...]

    def __post_init__(self) -> None:
        trail_index(self.at)
        _claim(self.claim, "a why trail's claim")
        steps = self.steps
        if not isinstance(steps, tuple) or not all(isinstance(s, WhyStep) for s in steps):
            raise _fail(Code.SHAPE, "steps must be a tuple of WhySteps")
        if not 1 <= len(steps) <= MAX_WHY_STEPS:
            raise _fail(Code.BAD_VALUE, f"a why tree has 1 to {MAX_WHY_STEPS} steps")
        if steps[0].relation is not Relation.ROOT or steps[0].claim != self.claim:
            raise _fail(Code.BAD_VALUE, "a why tree starts at its root: the claim asked about")
        shown: set[ClaimId] = set()  # every claim shown in full so far
        path: list[ClaimId] = []  # path[d]: the expanded claim at depth d on the current branch
        for step in steps:
            if step is not steps[0] and step.relation is Relation.ROOT:
                raise _fail(Code.BAD_VALUE, "a why tree has one root")
            if step.parent is not None and path[step.depth - 1 : step.depth] != [step.parent]:
                raise _fail(Code.ORDER, "steps are in pre-order: each follows its parent's branch")
            if step.repeat:
                if step.claim not in shown:
                    raise _fail(Code.BAD_VALUE, "a repeat names a claim shown earlier in the tree")
                continue
            if step.claim in shown:
                raise _fail(
                    Code.DUPLICATE, f"{step.claim} is shown twice; mark the second a repeat"
                )
            shown.add(step.claim)
            del path[step.depth :]
            path.append(step.claim)

    @property
    def claims(self) -> tuple[ClaimId, ...]:
        """Every claim the tree names, in step order, once each."""
        return tuple(dict.fromkeys(s.claim for s in self.steps))

    def to_json(self) -> JsonObject:
        return {
            "at": self.at,
            "claim": self.claim,
            "kind": self.kind,
            "steps": [s.to_json() for s in self.steps],
        }


# --- Diff -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TxPoint:
    """A Ledger transaction: what Memory knew then."""

    tx: LedgerTx

    def __post_init__(self) -> None:
        try:
            ledger_tx(self.tx)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"a diff point: {exc}") from exc

    def to_json(self) -> JsonObject:
        return {"tx": self.tx}


@dataclass(frozen=True)
class WorldPoint:
    """``ticks`` on one clock (a ``TimestampDomain`` id): what held then."""

    clock: RecordId
    ticks: int

    def __post_init__(self) -> None:
        try:
            parse_record_id(self.clock)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, f"a diff point's clock: {exc}") from exc
        t = self.ticks
        if isinstance(t, bool) or not isinstance(t, int) or not INT64_MIN <= t <= INT64_MAX:
            raise _fail(Code.BAD_VALUE, "a diff point's ticks are an int64")

    def to_json(self) -> JsonObject:
        return {"clock": self.clock, "ticks": self.ticks}


DiffPoint: TypeAlias = TxPoint | WorldPoint


class Change(StrEnum):
    """What happened to a claim between the two points."""

    OPENED = "opened"  # holds at after, did not at before
    CLOSED = "closed"  # held at before; at after over a strictly narrower interval, or not at all
    SUPERSEDED = "superseded"  # held at before; at after other versions took its place
    BETWEEN = "between"  # held at neither point: opened after before and closed by after


@dataclass(frozen=True)
class DiffChange:
    """One change to one claim (or, opened, one new claim): ``before`` held at the earlier point,
    ``after`` holds at the later one. ``closed`` names the narrowed versions (same object, a
    strictly narrower interval) in ``after``, or nothing when the claim stopped holding at all.
    ``superseded`` names the versions that took its place: another object, or (in transaction
    time) the same object over an interval that is not narrower. ``between`` names, in
    ``after``, one claim that held at neither point but in between: opened and closed inside the
    interval, so a diff never hides what came and went."""

    predicate: str
    change: Change
    before: tuple[ClaimId, ...]
    after: tuple[ClaimId, ...]

    def __post_init__(self) -> None:
        try:
            check_token("predicate", self.predicate)
        except (TypeError, ValueError) as exc:
            raise _fail(Code.BAD_VALUE, str(exc)) from exc
        if not isinstance(self.change, Change):
            raise _fail(Code.BAD_VALUE, f"change must be a Change: {self.change!r}")
        _claims(self.before, "a change's before claims")
        _claims(self.after, "a change's after claims")
        if set(self.before) & set(self.after):
            raise _fail(Code.BAD_VALUE, "a claim is not both before and after a change")
        shape = (len(self.before), bool(self.after))
        if self.change is Change.OPENED and shape != (0, True):
            raise _fail(
                Code.BAD_VALUE, "an opened change names one or more claims after, none before"
            )
        if self.change is Change.BETWEEN:
            if self.before or len(self.after) != 1:
                raise _fail(Code.BAD_VALUE, "a between change names exactly one claim after")
            return
        if self.change is not Change.OPENED and shape[0] != 1:
            raise _fail(Code.BAD_VALUE, f"a {self.change} change names exactly one claim before")
        if self.change is Change.SUPERSEDED and not self.after:
            raise _fail(Code.BAD_VALUE, "a superseded change names what took its place")

    def sort_key(self) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        return (self.predicate, str(self.change), self.before, self.after)

    def to_json(self) -> JsonObject:
        return {
            "after": list(self.after),
            "before": list(self.before),
            "change": str(self.change),
            "predicate": self.predicate,
        }


def _node_key(node: NodeRef) -> tuple[str, str]:
    return (str(node.node_type), node.node_id)


@dataclass(frozen=True)
class DiffTrail:
    """What changed about ``subject`` (and the ``same_as`` identities in ``nodes``) between two
    points on one axis: two transactions, or two instants on one clock."""

    kind: ClassVar[str] = "diff"
    at: str
    subject: NodeRef
    nodes: tuple[NodeRef, ...]
    before: DiffPoint
    after: DiffPoint
    changes: tuple[DiffChange, ...]

    def __post_init__(self) -> None:
        trail_index(self.at)
        if not isinstance(self.subject, NodeRef):
            raise _fail(Code.SHAPE, f"subject must be a NodeRef: {self.subject!r}")
        nodes = self.nodes
        if not isinstance(nodes, tuple) or not all(isinstance(n, NodeRef) for n in nodes):
            raise _fail(Code.SHAPE, "nodes must be a tuple of NodeRefs")
        if not 1 <= len(nodes) <= MAX_DIFF_NODES or self.subject not in nodes:
            raise _fail(Code.BAD_VALUE, f"nodes holds the subject, at most {MAX_DIFF_NODES} in all")
        _sorted_unique([_node_key(n) for n in nodes], "diff nodes")
        before, after = self.before, self.after
        if not isinstance(before, TxPoint | WorldPoint) or type(after) is not type(before):
            raise _fail(Code.SHAPE, "both diff points are transactions, or both world instants")
        if isinstance(before, WorldPoint) and before.clock != after.clock:  # type: ignore[union-attr]
            raise _fail(Code.BAD_VALUE, "a diff trail compares two instants on one clock")
        if _point_key(before) >= _point_key(after):
            raise _fail(Code.BAD_VALUE, "before is earlier than after")
        changes = self.changes
        if not isinstance(changes, tuple) or not all(isinstance(c, DiffChange) for c in changes):
            raise _fail(Code.SHAPE, "changes must be a tuple of DiffChanges")
        _sorted_unique([c.sort_key() for c in changes], "diff changes")
        named = [i for c in changes for i in (*c.before, *c.after)]
        if len(named) > MAX_DIFF_CLAIMS:
            raise _fail(Code.BAD_VALUE, f"a diff names at most {MAX_DIFF_CLAIMS} claims")
        befores = [i for c in changes for i in c.before]
        if len(set(befores)) != len(befores):
            raise _fail(Code.DUPLICATE, "a claim changes at most once")
        opened = [
            i for c in changes if c.change in (Change.OPENED, Change.BETWEEN) for i in c.after
        ]
        if len(set(opened)) != len(opened) or set(opened) & set(befores):
            raise _fail(
                Code.DUPLICATE, "an opened or between claim is named once and held nothing before"
            )

    @property
    def claims(self) -> tuple[ClaimId, ...]:
        return tuple(dict.fromkeys(i for c in self.changes for i in (*c.before, *c.after)))

    def to_json(self) -> JsonObject:
        return {
            "after": self.after.to_json(),
            "at": self.at,
            "before": self.before.to_json(),
            "changes": [c.to_json() for c in self.changes],
            "kind": self.kind,
            "nodes": [n.to_json() for n in self.nodes],
            "subject": self.subject.to_json(),
        }


def _point_key(point: DiffPoint) -> int:
    return point.tx if isinstance(point, TxPoint) else point.ticks


Trail: TypeAlias = WhyTrail | DiffTrail
TRAIL_KINDS: Final = (WhyTrail, DiffTrail)
