"""The site query API (spec §8): ten questions over a folded ``SiteModel``, each a cited ``Answer``.

Every function takes the model views (#185) fold from the log and returns an ``Answer`` whose items
point at numbered sources (``S1`` ...), in order of first use. Times are site-clock strings,
compared as strings and never converted; ``at=None`` means the latest ``seen`` time in the log.

What this module needs from a model is the ``SiteModel`` protocol below: the spec §6 views
(``state_at``, ``history``, ``topology``, ``identity``) plus the log's facts, ``seen`` entries and
pending proposals. It reads them and never folds anything itself.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from neptune_memory.site.facts import Citation, Fact, Kind, Ref, Seen, State, Value

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# --- What a model must offer (spec §6) ---------------------------------------------------------


class Cell(Protocol):
    """One (subject, predicate) at a time ``t``. ``facts`` holds the winning fact, the tied facts
    when ``ambiguous``, the last fact when ``stale`` and nothing when ``unknown``. A ``many``
    predicate gives one known cell per current fact."""

    @property
    def subject(self) -> str: ...
    @property
    def predicate(self) -> str: ...
    @property
    def state(self) -> State: ...
    @property
    def facts(self) -> tuple[Fact, ...]: ...


class Interval(Protocol):
    """One history interval: ``fact`` held over ``[start, end)``; ``end`` is ``None`` while open."""

    @property
    def fact(self) -> Fact: ...
    @property
    def start(self) -> str | None: ...
    @property
    def end(self) -> str | None: ...


class SiteModel(Protocol):
    """The folded site model (spec §6) as the query API reads it."""

    @property
    def as_of_seq(self) -> int:
        """The last log ``seq`` folded in."""
        ...

    @property
    def facts(self) -> Mapping[str, Fact]:
        """Every fact in the log by id, in log order (proposals and confirmed copies included)."""
        ...

    @property
    def seen(self) -> Sequence[Seen]:
        """Every ``seen`` entry, in log order."""
        ...

    def state_at(self, t: str) -> Iterable[Cell]:
        """Every (subject, predicate) cell at ``t``, and ``unknown`` ones the vocabulary expects."""
        ...

    def history(self, subject: str) -> Iterable[Interval]:
        """Every interval of every predicate of ``subject``, in site-clock order."""
        ...

    def topology(self) -> Iterable[Fact]:
        """The edge facts (``feeds``, ``depends_on``, ``located_in``, ``part_of``,
        ``assigned_to``, ``covers_zone``)."""
        ...

    def identity(self, entity: str) -> frozenset[str]:
        """``entity`` and every entity a stated ``same_as`` joins it to."""
        ...

    def pending(self) -> Sequence[Fact]:
        """Inferred proposals nobody has answered yet."""
        ...


# --- Answers -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One line of an answer. ``cite`` keys into ``Answer.sources``; ``relation`` says how a
    fact relates to the one asked about (``why``) and is ``None`` elsewhere."""

    fact_id: str | None
    subject: str
    predicate: str
    object: Ref | Value | None
    valid_from: str | None
    valid_to: str | None
    kind: Kind | None
    state: State
    cite: tuple[str, ...]
    relation: str | None = None


@dataclass
class Answer:
    """A cited answer: items, what it could not cover (``gaps``), and numbered sources. ``sql``
    also fills ``columns`` and ``rows``."""

    tool: str
    at: str | None
    as_of_seq: int
    items: list[Item] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    sources: dict[str, Citation] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)

    def cite(self, citations: Iterable[Citation]) -> tuple[str, ...]:
        """Number ``citations`` in order of first use and return their keys."""
        keys: list[str] = []
        for citation in citations:
            key = next((k for k, c in self.sources.items() if c == citation), None)
            if key is None:
                key = f"S{len(self.sources) + 1}"
                self.sources[key] = citation
            if key not in keys:
                keys.append(key)
        return tuple(keys)

    def add(self, fact: Fact, state: State = "known", relation: str | None = None) -> Item:
        item = Item(
            fact_id=fact.id,
            subject=fact.subject,
            predicate=fact.predicate,
            object=fact.object,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            kind=fact.kind,
            state=state,
            cite=self.cite(fact.citations),
            relation=relation,
        )
        self.items.append(item)
        return item

    def add_cell(self, cell: Cell) -> None:
        if not cell.facts:
            self.items.append(
                Item(None, cell.subject, cell.predicate, None, None, None, None, cell.state, ())
            )
            self.gaps.append(f"{cell.subject} {cell.predicate}: {cell.state}, no source states it")
            return
        for fact in cell.facts:
            self.add(fact, cell.state)
        if cell.state == "stale":
            last = cell.facts[0]
            self.gaps.append(
                f"{cell.subject} {cell.predicate}: stale, {last.source} has gone quiet; "
                f"the last value holds since {last.valid_from}"
            )
        elif cell.state == "ambiguous":
            self.gaps.append(f"{cell.subject} {cell.predicate}: ambiguous, sources disagree")


# --- Helpers -----------------------------------------------------------------------------------

_FAR_FUTURE = "9999-12-31T23:59:59"
STATUS = frozenset(
    {"mode", "health", "task_state", "battery", "current_task", "in_zone", "parts_out", "setpoint"}
)


def _at(model: SiteModel, at: str | None) -> str:
    if at is not None:
        return at
    return max((s.t for s in model.seen), default=_FAR_FUTURE)


def _new(tool: str, model: SiteModel, at: str | None) -> Answer:
    return Answer(tool=tool, at=at, as_of_seq=model.as_of_seq)


def _text(obj: Ref | Value) -> str:
    return obj.ref if isinstance(obj, Ref) else str(obj.value)


def _entities(model: SiteModel) -> list[str]:
    seen: dict[str, None] = {}
    for fact in model.facts.values():
        seen.setdefault(fact.subject)
        if isinstance(fact.object, Ref):
            seen.setdefault(fact.object.ref)
    return list(seen)


def _cells(model: SiteModel, at: str, entities: Iterable[str]) -> list[Cell]:
    wanted = set(entities)
    return [c for c in model.state_at(at) if c.subject in wanted]


# --- The ten questions -------------------------------------------------------------------------


def resolve(model: SiteModel, name: str) -> Answer:
    """Names and ids to entities: stated ``same_as`` joined, pending proposals listed."""
    answer = _new("resolve", model, None)
    needle = name.strip().casefold()
    matched: list[str] = []
    for entity in _entities(model):
        if needle in (entity.casefold(), entity.split(":", 1)[-1].casefold()):
            matched.append(entity)
    for fact in model.facts.values():
        if fact.predicate == "name" and str(_text(fact.object)).casefold() == needle:
            matched.append(fact.subject)
    group: set[str] = set()
    for entity in matched:
        group |= model.identity(entity)
    pending = {f.id for f in model.pending()}
    for fact in model.facts.values():
        touches = fact.subject in group or (
            isinstance(fact.object, Ref) and fact.object.ref in group
        )
        if fact.predicate == "same_as" and touches:
            if fact.id in pending:
                answer.add(fact, "pending")
            elif fact.kind != "inferred":
                answer.add(fact)
        elif fact.subject in group and fact.predicate in ("name", "serial"):
            answer.add(fact)
    if not group:
        answer.gaps.append(f"no entity is named or identified as {name!r}")
    return answer


def state_at(model: SiteModel, entity: str, at: str | None = None) -> Answer:
    """Every predicate's state for ``entity`` (and what a stated ``same_as`` joins it to) at t."""
    t = _at(model, at)
    answer = _new("state_at", model, t)
    for cell in _cells(model, t, model.identity(entity)):
        answer.add_cell(cell)
    return answer


def find(model: SiteModel, predicate: str, obj: str, at: str | None = None) -> Answer:
    """Who or what has ``predicate`` = ``obj`` at t (``obj`` a ref, or the value as text), and,
    when ``obj`` is an entity, what is known about it then."""
    t = _at(model, at)
    answer = _new("find", model, t)
    cells = list(model.state_at(t))
    for cell in cells:
        if cell.predicate == predicate and any(_text(f.object) == obj for f in cell.facts):
            answer.add_cell(cell)
    if ":" in obj:
        for cell in cells:
            if cell.subject == obj:
                answer.add_cell(cell)
    if not answer.items:
        answer.gaps.append(f"nothing has {predicate} = {obj!r} at {t}")
    return answer


def timeline(
    model: SiteModel,
    entity: str,
    start: str,
    end: str,
    predicates: list[str] | None = None,
) -> Answer:
    """Every interval of ``entity`` that overlaps ``[start, end]``, in site-clock order."""
    answer = _new("timeline", model, end)
    for interval in model.history(entity):
        fact = interval.fact
        if predicates is not None and fact.predicate not in predicates:
            continue
        begins = interval.start or ""
        if begins > end or (interval.end is not None and interval.end <= start):
            continue
        answer.items.append(
            Item(
                fact_id=fact.id,
                subject=fact.subject,
                predicate=fact.predicate,
                object=fact.object,
                valid_from=interval.start,
                valid_to=interval.end,
                kind=fact.kind,
                state="known",
                cite=answer.cite(fact.citations),
            )
        )
    if not answer.items:
        answer.gaps.append(f"no interval of {entity} overlaps {start} to {end}")
    return answer


def why(model: SiteModel, fact_id: str) -> Answer:
    """One fact, its citations, and the facts it replaced, was replaced by, beat or ties with."""
    answer = _new("why", model, None)
    fact = model.facts.get(fact_id)
    if fact is None:
        answer.gaps.append(f"no fact {fact_id} in the log")
        return answer
    pending = {f.id for f in model.pending()}
    answer.add(fact, "pending" if fact.id in pending else "known", relation="asked")
    intervals = [i for i in model.history(fact.subject) if i.fact.predicate == fact.predicate]
    ids = [i.fact.id for i in intervals]
    if fact.id in ids:
        index = ids.index(fact.id)
        if index > 0:
            answer.add(intervals[index - 1].fact, relation="replaced")
        if index + 1 < len(intervals):
            answer.add(intervals[index + 1].fact, relation="replaced by")
    for other in model.facts.values():
        if (
            other.id != fact.id
            and other.subject == fact.subject
            and other.predicate == fact.predicate
            and other.valid_from == fact.valid_from
            and other.object != fact.object
        ):
            relation = _contest(fact, other)
            answer.add(other, "pending" if other.id in pending else "known", relation=relation)
    return answer


def _contest(fact: Fact, other: Fact) -> str:
    rank = {"observed": 1, "stated": 1, "inferred": 0}
    if rank[fact.kind] > rank[other.kind]:
        return "beaten"
    if rank[fact.kind] < rank[other.kind]:
        return "beats it"
    return "ties"


def diff(model: SiteModel, start: str, end: str, entities: list[str] | None = None) -> Answer:
    """Facts that began in ``(start, end]``, optionally only about ``entities``."""
    answer = _new("diff", model, end)
    wanted: set[str] | None = None
    if entities is not None:
        wanted = set()
        for entity in entities:
            wanted |= model.identity(entity)
    pending = {f.id for f in model.pending()}
    for fact in model.facts.values():
        if fact.valid_from is None or not start < fact.valid_from <= end:
            continue
        if wanted is not None and fact.subject not in wanted:
            continue
        answer.add(fact, "pending" if fact.id in pending else "known")
    if not answer.items:
        answer.gaps.append(f"no fact began between {start} and {end}")
    return answer


def gaps(model: SiteModel, at: str | None = None) -> Answer:
    """What is stale, unknown or ambiguous at t, and every pending proposal."""
    t = _at(model, at)
    answer = _new("gaps", model, t)
    for cell in model.state_at(t):
        if cell.state in ("stale", "unknown", "ambiguous"):
            answer.add_cell(cell)
    for fact in model.pending():
        answer.add(fact, "pending")
    return answer


def impact(model: SiteModel, entity: str, at: str | None = None, depth: int = 3) -> Answer:
    """What lies downstream of ``entity`` at t, with the state of each.

    A hop follows ``feeds`` forward or ``depends_on`` backward. Each place reached also brings
    what sits in it (``located_in``, ``in_zone``) and those robots' tasks (``current_task``,
    ``assigned_to``), at no extra depth.
    """
    t = _at(model, at)
    answer = _new("impact", model, t)
    cells = list(model.state_at(t))
    edges = list(model.topology())
    occupants: dict[str, list[Fact]] = {}
    for fact in edges:
        if fact.predicate == "located_in" and isinstance(fact.object, Ref):
            occupants.setdefault(fact.object.ref, []).append(fact)
        if fact.predicate == "assigned_to" and isinstance(fact.object, Ref):
            occupants.setdefault(fact.object.ref, []).append(fact)
    for cell in cells:
        if cell.state == "known" and cell.predicate == "in_zone":
            for fact in cell.facts:
                if isinstance(fact.object, Ref):
                    occupants.setdefault(fact.object.ref, []).append(fact)
    reached: dict[str, None] = {}
    edge_items: list[Fact] = []

    def attach(node: str) -> None:
        if node in reached:
            return
        reached[node] = None
        for fact in occupants.get(node, []):
            edge_items.append(fact)
            attach(fact.subject)
        for cell in cells:
            if cell.subject == node and cell.predicate == "current_task" and cell.facts:
                obj = cell.facts[0].object
                if isinstance(obj, Ref):
                    attach(obj.ref)

    frontier = [entity]
    attach(entity)
    for _ in range(depth):
        following: list[str] = []
        for node in frontier:
            for fact in edges:
                target = None
                if fact.predicate == "feeds" and fact.subject == node:
                    target = fact.object.ref if isinstance(fact.object, Ref) else None
                elif (
                    fact.predicate == "depends_on"
                    and isinstance(fact.object, Ref)
                    and fact.object.ref == node
                ):
                    target = fact.subject
                if target is None:
                    continue
                edge_items.append(fact)
                if target not in reached:
                    attach(target)
                    following.append(target)
        frontier = following
    for fact in dict.fromkeys(edge_items):
        answer.add(fact)
    for cell in cells:
        if cell.subject in reached and cell.predicate in STATUS:
            answer.add_cell(cell)
    return answer


def search_evidence(model: SiteModel, text: str, limit: int = 20) -> Answer:
    """Facts whose citation quotes or text values hold every word of ``text``, best first."""
    answer = _new("search_evidence", model, None)
    words = [w for w in re.split(r"\s+", text.casefold()) if w]
    scored: list[tuple[int, int, Fact]] = []
    for order, fact in enumerate(model.facts.values()):
        haystack = " ".join(
            [*(c.quote or "" for c in fact.citations), *(c.file for c in fact.citations)]
            + ([str(fact.object.value)] if isinstance(fact.object, Value) else [])
        ).casefold()
        hits = sum(1 for w in words if w in haystack)
        if words and hits == len(words):
            scored.append((-haystack.count(words[0]), order, fact))
    pending = {f.id for f in model.pending()}
    for _, _, fact in sorted(scored)[:limit]:
        answer.add(fact, "pending" if fact.id in pending else "known")
    if not answer.items:
        answer.gaps.append(f"no cited text holds {text!r}")
    return answer


SQL_ROW_LIMIT = 200
SQL_TABLES = """
CREATE TABLE facts (fact_id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
  object_is_ref INTEGER, unit TEXT, valid_from TEXT, valid_to TEXT, kind TEXT, source TEXT,
  model TEXT, pending INTEGER, citations TEXT);
CREATE TABLE intervals (fact_id TEXT, subject TEXT, predicate TEXT, object TEXT, start TEXT,
  "end" TEXT, kind TEXT);
CREATE TABLE seen (source TEXT, t TEXT);
CREATE TABLE entities (ref TEXT PRIMARY KEY, type TEXT, id TEXT, names TEXT);
"""


def sql(model: SiteModel, query: str) -> Answer:
    """A read-only ``sqlite3`` query over tables ``facts``, ``intervals``, ``seen`` and
    ``entities``; rows that select a ``fact_id`` cite that fact."""
    answer = _new("sql", model, None)
    db = _database(model)
    try:
        cursor = db.execute(query)
        answer.columns = [d[0] for d in cursor.description or ()]
        answer.rows = [tuple(r) for r in cursor.fetchmany(SQL_ROW_LIMIT + 1)]
    except sqlite3.Error as error:
        answer.gaps.append(f"sql error: {error}")
        return answer
    finally:
        db.close()
    if len(answer.rows) > SQL_ROW_LIMIT:
        answer.rows = answer.rows[:SQL_ROW_LIMIT]
        answer.gaps.append(f"only the first {SQL_ROW_LIMIT} rows are shown")
    if "fact_id" in answer.columns:
        column = answer.columns.index("fact_id")
        for row in answer.rows:
            fact = model.facts.get(str(row[column]))
            if fact is not None:
                answer.add(fact)
    return answer


def _database(model: SiteModel) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(SQL_TABLES)
    pending = {f.id for f in model.pending()}
    db.executemany(
        "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                f.id,
                f.subject,
                f.predicate,
                _text(f.object),
                isinstance(f.object, Ref),
                f.object.unit if isinstance(f.object, Value) else None,
                f.valid_from,
                f.valid_to,
                f.kind,
                f.source,
                f.model,
                f.id in pending,
                json.dumps([[c.file, c.locator, c.quote] for c in f.citations]),
            )
            for f in model.facts.values()
        ],
    )
    entities = _entities(model)
    names: dict[str, list[str]] = {}
    for fact in model.facts.values():
        if fact.predicate == "name":
            names.setdefault(fact.subject, []).append(_text(fact.object))
    db.executemany(
        "INSERT INTO entities VALUES (?,?,?,?)",
        [
            (e, e.split(":", 1)[0], e.split(":", 1)[-1], "; ".join(names.get(e, [])))
            for e in entities
        ],
    )
    db.executemany(
        "INSERT INTO intervals VALUES (?,?,?,?,?,?,?)",
        [
            (
                i.fact.id,
                i.fact.subject,
                i.fact.predicate,
                _text(i.fact.object),
                i.start,
                i.end,
                i.fact.kind,
            )
            for e in entities
            for i in model.history(e)
        ],
    )
    db.executemany("INSERT INTO seen VALUES (?,?)", [(s.source, s.t) for s in model.seen])
    db.commit()
    db.execute("PRAGMA query_only = ON")
    return db
