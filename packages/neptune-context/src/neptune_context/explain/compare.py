"""``compare_runs(before, after)`` (ADR 0015): what memory states differs between two runs.

The clause carries, as claim items, only what Memory states; it never says what caused what:

- **Configurations.** Each run's ``configuration_active_during`` claims, and for each bound
  configuration its ``has_name`` and ``declared_value`` claims over that binding (Memory claims a
  configuration's values once per binding, Memory ADR 0025, so each run's own copy is read). A
  value is carried when its (key path, value) pair is not stated for the other run: a changed
  value is carried on both sides, a value only one run's configurations state on that side.
  Values both runs state alike are not carried: they did not change.
- **Maintenance.** Every ``maintenance`` event that ``involves`` the later run's machine (the
  run's ``recorded_by`` machine and the ids memory states are ``same_as`` it), with its name,
  stated cause and each action's description. Placed on the runs' clock, only events between
  the two runs' starts count; on a clock no stated relation ties to the runs' (a CMMS's), the
  event is carried and a gap says it is not ordered against the runs. No clock is converted.
- The runs' own names and machines.

Nothing is inferred and nothing is ranked by meaning: every carried claim scores 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import TypedLiteral
from neptune_memory.schema.nodes import NodeRef, NodeType

from neptune.identity.canonical_json import dumps
from neptune_context.packets.model import GapCode

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune.model.time import Timestamp
    from neptune_context.explain.run import Run
    from neptune_context.packets.trails import Trail
    from neptune_context.query.model import CompareRuns, Subject

ACTIVE: Final = "configuration_active_during"
NAME: Final = "has_name"
VALUE: Final = "declared_value"
RECORDED_BY: Final = "recorded_by"
SAME_AS: Final = "same_as"
INVOLVES: Final = "involves"
EVENT_KIND: Final = "event_kind"
CAUSE: Final = "stated_cause"
DESCRIPTION: Final = "has_description"
MAINTENANCE: Final = "maintenance"
ACTIONS: Final = "/actions/"


def _node(subject: Subject) -> NodeRef:
    assert subject.declared_id is not None  # validate refuses a compare without ids
    return NodeRef(NodeType(subject.kind), subject.declared_id)


def _same_interval(claim: Claim, binding: Claim) -> bool:
    return claim.valid_from == binding.valid_from and claim.valid_to == binding.valid_to


def _value_key(claim: Claim) -> tuple[bytes, bytes] | None:
    """(key path, value) of a ``declared_value`` claim, as canonical JSON."""
    obj = claim.object
    if not isinstance(obj, TypedLiteral):
        return None
    encoded = obj.to_json()["value"]
    assert isinstance(encoded, dict)
    return dumps(encoded["path"]), dumps({"unit": obj.to_json()["unit"], "value": encoded})


class _Side:
    """One run's bound configurations: their names and declared values over its bindings."""

    def __init__(self, run: Run, node: NodeRef, tx: int) -> None:
        self.node = node
        self.own = [c for c in run.touching(node, tx) if c.subject == node]
        self.start: Timestamp | None = min(
            (c.valid_from for c in self.own if c.predicate == RECORDED_BY),
            key=lambda t: (t.domain_id, t.ticks),
            default=None,
        )
        self.names: list[Claim] = []
        self.values: list[Claim] = []
        for binding in (c for c in self.own if c.predicate == ACTIVE):
            target = binding.object
            assert isinstance(target, NodeRef)
            for claim in run.touching(target, tx):
                if claim.subject != target or not _same_interval(claim, binding):
                    continue
                if claim.predicate == NAME:
                    self.names.append(claim)
                elif claim.predicate == VALUE:
                    self.values.append(claim)
        self.pairs = {k for c in self.values if (k := _value_key(c)) is not None}
        self.paths = {k[0] for k in self.pairs}

    def machines(self) -> set[NodeRef]:
        return {
            c.object
            for c in self.own
            if c.predicate == RECORDED_BY and isinstance(c.object, NodeRef)
        }


def explain_compare(run: Run, index: int, clause: CompareRuns) -> Trail | None:
    """Carry what differs between the two runs (module docstring); a gap for what cannot be
    read. Returns no trail: the claims themselves are the answer."""
    at = f"/explain/{index}"
    tx = int(run.as_of)
    before = _Side(run, _node(clause.before), tx)
    after = _Side(run, _node(clause.after), tx)
    for name, side in (("before", before), ("after", after)):
        if not side.own:
            run.gap(
                GapCode.UNKNOWN,
                f"{at}/{name}",
                [side.node.node_id],
                "memory states nothing about this run",
            )
    for side, other in ((before, after), (after, before)):
        for claim in side.own:
            if claim.predicate in (NAME, ACTIVE):
                _carry(run, claim)
        for claim in side.names:
            _carry(run, claim)
        for claim in side.values:
            key = _value_key(claim)
            if key is not None and key not in other.pairs:
                _carry(run, claim)
    _maintenance(run, at, tx, before, after)
    return None


def _carry(run: Run, claim: Claim) -> None:
    if run.withheld(claim) or run.unplaceable(claim) is not None:
        return
    run.carry(claim, 1.0)
    run.cite(claim, 1.0)


def _machines(run: Run, tx: int, side: _Side) -> set[NodeRef]:
    found = side.machines()
    for machine in sorted(found, key=lambda n: n.node_id):
        for claim in run.touching(machine, tx):
            if claim.predicate == SAME_AS and not run.withheld(claim):
                found |= {n for n in (claim.subject, claim.object) if isinstance(n, NodeRef)}
    return {n for n in found if n.node_type is NodeType.MACHINE}


def _between(start: Timestamp, before: _Side, after: _Side) -> bool | None:
    """Whether ``start`` is in [before's start, after's start) on one clock; ``None`` when the
    clocks differ (never converted)."""
    lo, hi = before.start, after.start
    if lo is None or hi is None or not (start.domain_id == lo.domain_id == hi.domain_id):
        return None
    return lo.ticks <= start.ticks < hi.ticks


def _maintenance(run: Run, at: str, tx: int, before: _Side, after: _Side) -> None:
    events: dict[str, Claim] = {}  # event node id -> its involves claim (placed on its clock)
    for machine in sorted(_machines(run, tx, after), key=lambda n: n.node_id):
        for claim in run.touching(machine, tx):
            if claim.predicate == INVOLVES and claim.object == machine:
                events.setdefault(claim.subject.node_id, claim)
    unordered: set[str] = set()
    for node_id, involves in sorted(events.items()):
        parent = node_id.split(ACTIONS)[0]
        parent_claims = [c for c in run.touching(NodeRef(NodeType.EVENT, parent), tx)]
        if not any(
            c.predicate == EVENT_KIND
            and isinstance(c.object, TypedLiteral)
            and c.object.value == MAINTENANCE
            for c in parent_claims
        ):
            continue
        placed = _between(involves.valid_from, before, after)
        if placed is False:
            continue
        if placed is None:
            unordered.add(parent)
        node = NodeRef(NodeType.EVENT, node_id)
        for claim in run.touching(node, tx):
            if claim.subject == node and claim.predicate in (NAME, CAUSE, DESCRIPTION):
                _carry(run, claim)
    if unordered:
        run.gap(
            GapCode.NOT_COVERED,
            at,
            sorted(unordered),
            "maintenance on a clock no stated mapping relates to the runs' clocks: carried, not"
            " ordered against the two runs",
        )
