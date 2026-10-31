"""Views over the fact log: now / ``state_at``, history, topology and identity.

``SiteModel`` is a fold over log entries. ``apply`` takes one entry and returns the model, so a live
replay folds each entry as it lands and ``fold`` folds a whole log; both give the same model.
``apply`` updates the model in place (folding a site's whole log by copying would be quadratic), so
keep a model you still need by loading it again.

Choices:

- Cardinality ``one``: each source's latest fact with ``valid_from <= t`` is current (a fact with no
  ``valid_from`` is current from the start; a stated ``valid_to <= t`` ends it). The highest kind
  wins; a tie with different objects reads ``ambiguous``. ``many`` facts are each read on their own.
- Freshness reads ``seen`` entries of ``<source>/<entity id>`` when the log has any (a fleet that
  reports per robot, e.g. ``fleet/amr-03``), else of ``<source>``; the interval is the source's
  stated ``report_interval``.
- A confirmed proposal is replaced by its stated copy (source ``confirm``, citing the log line of
  the ``confirm`` entry); a rejected one leaves every view. An unanswered inferred ``same_as`` reads
  ``pending``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from neptune_memory.site import log as fact_log
from neptune_memory.site.confirm import stated_from_confirm
from neptune_memory.site.facts import (
    KIND_RANK,
    Confirm,
    Fact,
    LogEntry,
    Ref,
    State,
    Value,
)
from neptune_memory.site.vocab import EXPECTED, PREDICATES, TOPOLOGY, entity_type

if TYPE_CHECKING:
    from collections.abc import Iterable

STALE_AFTER = 3
"""A source is stale once its last ``seen`` is older than this many report intervals."""

PROPOSALS = ("same_as", "proposed_action")
"""Inferred predicates a person answers (``confirm``, ``actions.approve``/``reject``)."""

FORWARD = ("feeds",)
"""Topology predicates whose impact flows subject -> object; the rest flow object -> subject
(a zone's rule, a cell's robot, a robot's task, what depends on a thing)."""


@dataclass(frozen=True)
class Reading:
    """One (subject, predicate) at a site time: its state and the facts behind it.

    ``facts`` holds the current fact (both or all for ``ambiguous``; none for ``unknown``).
    ``since`` is when the current value began; ``last_seen`` is its source's last report (stale).
    """

    subject: str
    predicate: str
    state: State
    facts: tuple[Fact, ...]
    since: str | None = None
    last_seen: str | None = None


@dataclass(frozen=True)
class Interval:
    """A fact's span on the site clock: ``[start, end)``; ``end`` is ``None`` while it holds."""

    fact: Fact
    start: str | None
    end: str | None


@dataclass(frozen=True)
class Hop:
    """An entity reached from another through topology, ``depth`` edges away, via ``edge``."""

    entity: str
    depth: int
    edge: Fact


@dataclass(frozen=True)
class Topology:
    """The topology edges current at one site time."""

    edges: tuple[Fact, ...]

    def downstream(self, entity: str, depth: int = 3) -> list[Hop]:
        """What ``entity`` affects within ``depth`` edges, breadth first, each entity once."""
        step: dict[str, list[tuple[str, Fact]]] = {}
        for edge in self.edges:
            target = _ref(edge)
            if edge.predicate in FORWARD:
                step.setdefault(edge.subject, []).append((target, edge))
            else:
                step.setdefault(target, []).append((edge.subject, edge))
        reached = {entity}
        frontier = [entity]
        hops: list[Hop] = []
        for level in range(1, depth + 1):
            following: list[str] = []
            for node in frontier:
                for nxt, edge in step.get(node, []):
                    if nxt not in reached:
                        reached.add(nxt)
                        following.append(nxt)
                        hops.append(Hop(nxt, level, edge))
            frontier = following
        return hops


@dataclass
class SiteModel:
    """Everything folded from the log up to ``seq``."""

    seq: int = 0
    facts: dict[str, Fact] = field(default_factory=dict)
    """Every fact (and confirmed copy) by id, in log order."""
    fact_seq: dict[str, int] = field(default_factory=dict)
    by_key: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    entities: set[str] = field(default_factory=set)
    seen: dict[str, list[str]] = field(default_factory=dict)
    """Report times per seen source, sorted."""
    report_interval: dict[str, float] = field(default_factory=dict)
    confirms: dict[str, tuple[int, Confirm]] = field(default_factory=dict)
    """The latest answer per proposal id, with the seq of its ``confirm`` entry."""
    parent: dict[str, str] = field(default_factory=dict)
    """Union-find over stated ``same_as``; each root is its group's smallest ref."""

    @staticmethod
    def empty() -> SiteModel:
        return SiteModel()

    # -- folding ---------------------------------------------------------------------------

    def apply(self, entry: LogEntry) -> SiteModel:
        """Fold one log entry in (in place) and return the model."""
        self.seq = max(self.seq, entry.seq)
        if entry.type == "fact" and entry.fact is not None:
            self._add(entry.fact, entry.seq)
        elif entry.type == "seen" and entry.seen is not None:
            bisect.insort(self.seen.setdefault(entry.seen.source, []), entry.seen.t)
        elif entry.type == "confirm" and entry.confirm is not None:
            self._confirm(entry.confirm, entry.seq)
        return self

    def _add(self, fact: Fact, seq: int) -> None:
        fid = fact.id
        if fid in self.facts:
            return
        self.facts[fid] = fact
        self.fact_seq[fid] = seq
        self.by_key.setdefault((fact.subject, fact.predicate), []).append(fid)
        self.entities.add(fact.subject)
        if isinstance(fact.object, Ref):
            self.entities.add(fact.object.ref)
        if fact.predicate == "report_interval" and isinstance(fact.object, Value):
            self.report_interval[fact.subject.removeprefix("source:")] = float(fact.object.value)
        if fact.predicate == "same_as" and fact.kind != "inferred" and isinstance(fact.object, Ref):
            self._union(fact.subject, fact.object.ref)

    def _confirm(self, confirm: Confirm, seq: int) -> None:
        self.confirms[confirm.proposal] = (seq, confirm)
        proposal = self.facts.get(confirm.proposal)
        if confirm.answer != "yes" or proposal is None:
            return
        entry = LogEntry(seq=seq, type="confirm", confirm=confirm)
        self._add(stated_from_confirm(proposal, entry, seq), seq)

    def _find(self, entity: str) -> str:
        while self.parent.get(entity, entity) != entity:
            entity = self.parent[entity]
        return entity

    def _union(self, a: str, b: str) -> None:
        ra, rb = self._find(a), self._find(b)
        self.parent.setdefault(ra, ra)
        self.parent.setdefault(rb, rb)
        if ra != rb:
            low, high = sorted((ra, rb))
            self.parent[high] = low

    # -- reading ---------------------------------------------------------------------------

    def clock(self) -> str | None:
        """The latest site time any source reported: what ``now`` means. A site with no feed (a
        machine's export, mapped as a table) has no ``seen``: then its latest observed fact."""
        seen = max((times[-1] for times in self.seen.values() if times), default=None)
        if seen is not None:
            return seen
        observed = (f.valid_from for f in self.facts.values() if f.kind == "observed")
        return max((t for t in observed if t), default=None)

    def live(self, fact_id: str) -> bool:
        """False once a proposal is answered: yes swaps in its stated copy, no drops it."""
        return fact_id not in self.confirms

    def pending(self) -> list[Fact]:
        """Inferred ``same_as`` and ``proposed_action`` proposals nobody has answered yet."""
        return [
            f
            for fid, f in self.facts.items()
            if f.predicate in PROPOSALS and f.kind == "inferred" and self.live(fid)
        ]

    def now(self, subject: str | None = None) -> list[Reading]:
        return self.state_at(self.clock(), subject)

    def state_at(self, t: str | None = None, subject: str | None = None) -> list[Reading]:
        """Every (subject, predicate) at site time ``t`` (``None`` = ``clock()``), sorted."""
        t = t if t is not None else self.clock()
        subjects = [subject] if subject is not None else sorted(self.entities)
        out: list[Reading] = []
        for s in subjects:
            predicates = sorted({p for (subj, p) in self.by_key if subj == s})
            for p in predicates:
                out.extend(self._readings(s, p, t))
            stated = {r.predicate for r in out if r.subject == s}
            for p in EXPECTED.get(entity_type(s), ()):
                if p not in stated:
                    out.append(Reading(s, p, "unknown", ()))
        return out

    def _current(self, subject: str, predicate: str, t: str | None) -> list[Fact]:
        out = []
        for fid in self.by_key.get((subject, predicate), []):
            f = self.facts[fid]
            if not self.live(fid):
                continue
            if t is not None and f.valid_from is not None and f.valid_from > t:
                continue
            if t is not None and f.valid_to is not None and f.valid_to <= t:
                continue
            out.append(f)
        return out

    def _readings(self, subject: str, predicate: str, t: str | None) -> list[Reading]:
        current = self._current(subject, predicate, t)
        if not current:
            return []
        spec = PREDICATES.get(predicate)
        if spec is None or spec.cardinality == "many":
            return [self._reading(f, t) for f in current]
        latest: dict[str, list[Fact]] = {}  # per source, its facts at its latest valid_from
        for f in current:
            mine = latest.get(f.source)
            when, theirs = f.valid_from or "", (mine[0].valid_from or "") if mine else ""
            if mine is None or when > theirs:
                latest[f.source] = [f]
            elif when == theirs:
                mine.append(f)
        candidates = [f for facts in latest.values() for f in facts]
        top = max(KIND_RANK[f.kind] for f in candidates)
        winners: dict[object, Fact] = {}
        for f in candidates:
            if KIND_RANK[f.kind] == top:
                winners.setdefault(f.object, f)
        if len(winners) > 1:
            return [Reading(subject, predicate, "ambiguous", tuple(winners.values()))]
        best = max(
            (f for f in candidates if KIND_RANK[f.kind] == top),
            key=lambda f: (f.valid_from or "", self.fact_seq[f.id]),
        )
        return [self._reading(best, t)]

    def _reading(self, fact: Fact, t: str | None) -> Reading:
        state: State = "known"
        if fact.predicate == "same_as" and fact.kind == "inferred":
            state = "pending"
        last = self.last_seen(fact.source, fact.subject, t)
        interval = self.report_interval.get(fact.source)
        if state == "known" and t is not None and last is not None and interval is not None:
            age = (datetime.fromisoformat(t) - datetime.fromisoformat(last)).total_seconds()
            if age > STALE_AFTER * interval:
                state = "stale"
        return Reading(fact.subject, fact.predicate, state, (fact,), fact.valid_from, last)

    def last_seen(self, source: str, subject: str, t: str | None) -> str | None:
        """The last report at or before ``t`` from ``source`` about ``subject``'s device."""
        device = f"{source}/{subject.split(':', 1)[-1]}"
        times = self.seen.get(device) or self.seen.get(source) or []
        i = len(times) if t is None else bisect.bisect_right(times, t)
        return times[i - 1] if i else None

    def history(
        self,
        subject: str,
        predicate: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[Interval]:
        """Intervals of ``subject`` overlapping ``[start, end]``, by (predicate, start).

        For a ``one`` predicate each source's value holds until that source's next change; a
        ``many`` fact holds until its stated ``valid_to``.
        """
        out: list[Interval] = []
        keys = sorted(k for k in self.by_key if k[0] == subject and predicate in (None, k[1]))
        for key in keys:
            live = [self.facts[i] for i in self.by_key[key] if self.live(i)]
            spec = PREDICATES.get(key[1])
            chains: dict[str, list[Fact]] = {}
            for f in live:
                one = spec is not None and spec.cardinality == "one"
                chains.setdefault(f.source if one else f.id, []).append(f)
            spans: list[Interval] = []
            for chain in chains.values():
                chain.sort(key=lambda f: (f.valid_from or "", self.fact_seq[f.id]))
                spans.extend(_intervals(chain))
            spans.sort(key=lambda i: (i.start or "", self.fact_seq[i.fact.id]))
            out.extend(
                i
                for i in spans
                if (end is None or i.start is None or i.start <= end)
                and (start is None or i.end is None or i.end > start)
            )
        return out

    def topology(self, t: str | None = None) -> Topology:
        """The topology edges current at ``t`` (``None`` = every live edge).

        A fleet never says an order ended, only that a robot moved on to the next: an
        ``assigned_to`` edge counts only while its robot's ``current_task`` is that task."""
        edges = [
            f
            for (subject, predicate) in sorted(self.by_key)
            if predicate in TOPOLOGY
            for f in self._current(subject, predicate, t)
            if predicate != "assigned_to" or self._on_task(f, t)
        ]
        return Topology(tuple(edges))

    def _on_task(self, assigned: Fact, t: str | None) -> bool:
        robot = _ref(assigned)
        if (robot, "current_task") not in self.by_key:
            return True
        current = {_ref(f) for r in self._readings(robot, "current_task", t) for f in r.facts}
        return assigned.subject in current

    def same_as(self, entity: str) -> tuple[str, ...]:
        """``entity`` and every entity stated (or confirmed) to be the same, sorted."""
        root = self._find(entity)
        group = {e for e in self.parent if self._find(e) == root} | {entity}
        return tuple(sorted(group))

    def canonical(self, entity: str) -> str:
        """The smallest ref among the entities joined with ``entity``."""
        return self._find(entity)


def _intervals(chain: list[Fact]) -> list[Interval]:
    """One source's values in time order: each holds until the next different one."""
    out: list[Interval] = []
    for i, f in enumerate(chain):
        before = chain[i - 1] if i else None
        if before is not None and before.object == f.object and before.valid_to is None:
            continue  # the same value reported again
        end = next((g.valid_from for g in chain[i + 1 :] if g.object != f.object), None)
        if f.valid_to is not None and (end is None or f.valid_to < end):
            end = f.valid_to
        out.append(Interval(f, f.valid_from, end))
    return out


def _ref(fact: Fact) -> str:
    return fact.object.ref if isinstance(fact.object, Ref) else str(fact.object.value)


def fold(entries: Iterable[LogEntry], model: SiteModel | None = None) -> SiteModel:
    """Apply every entry in order, from ``model`` or from empty."""
    model = model if model is not None else SiteModel.empty()
    for entry in entries:
        model.apply(entry)
    return model


def load(site_dir: Path, *, as_of_seq: int | None = None) -> SiteModel:
    """The site's model from its log, up to and including ``as_of_seq``."""
    return fold(fact_log.read(Path(site_dir), as_of_seq=as_of_seq))
