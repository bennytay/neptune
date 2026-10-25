"""The site query API (spec §8): ten questions over a folded ``SiteModel``, each a cited ``Answer``.

Every function takes the model views (#185) fold from the log and returns an ``Answer`` whose items
point at numbered sources (``S1`` ...), in order of first use. Times are site-clock strings,
compared as strings and never converted; ``at=None`` means the latest ``seen`` time in the log.

It reads the views (``state_at``, ``history``, ``topology``, ``same_as``, ``pending``) and the
model's facts and ``seen`` times, and never folds anything itself. A proposal someone has answered
is no longer live: its stated copy (on yes) speaks for it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from neptune_memory.site.facts import KIND_RANK, Citation, Fact, Kind, Ref, State, Value

if TYPE_CHECKING:
    from collections.abc import Iterable

    from neptune_memory.site.views import Reading, SiteModel

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

    def add_cell(self, cell: Reading) -> None:
        if not cell.facts:
            self.items.append(
                Item(None, cell.subject, cell.predicate, None, None, None, None, cell.state, ())
            )
            self.gaps.append(f"{cell.subject} {cell.predicate}: {cell.state}, no source states it")
            return
        for fact in cell.facts:
            self.add(fact, cell.state)
        if cell.state == "stale":
            self.gaps.append(
                f"{cell.subject} {cell.predicate}: stale, {cell.facts[0].source} last reported "
                f"at {cell.last_seen}; the last value holds since {cell.since}"
            )
        elif cell.state == "ambiguous":
            self.gaps.append(f"{cell.subject} {cell.predicate}: ambiguous, sources disagree")


# --- Helpers -----------------------------------------------------------------------------------

_FAR_FUTURE = "9999-12-31T23:59:59"
STATUS = frozenset(
    {"mode", "health", "task_state", "battery", "current_task", "in_zone", "parts_out", "setpoint"}
)


def _at(model: SiteModel, at: str | None) -> str:
    return at if at is not None else (model.clock() or _FAR_FUTURE)


def _new(tool: str, model: SiteModel, at: str | None) -> Answer:
    return Answer(tool=tool, at=at, as_of_seq=model.seq)


def _text(obj: Ref | Value) -> str:
    return obj.ref if isinstance(obj, Ref) else str(obj.value)


def _live(model: SiteModel) -> list[Fact]:
    return [f for fid, f in model.facts.items() if model.live(fid)]


def _state(model: SiteModel, fact: Fact, pending: set[str]) -> State:
    return "pending" if fact.id in pending else "known"


# --- The ten questions -------------------------------------------------------------------------


def resolve(model: SiteModel, name: str) -> Answer:
    """Names and ids to entities: stated ``same_as`` joined, pending proposals listed."""
    answer = _new("resolve", model, None)
    needle = name.strip().casefold()
    matched = [
        e
        for e in sorted(model.entities)
        if needle in (e.casefold(), e.split(":", 1)[-1].casefold())
    ]
    live = _live(model)
    matched += [
        f.subject for f in live if f.predicate == "name" and _text(f.object).casefold() == needle
    ]
    group: set[str] = set()
    for entity in matched:
        group.update(model.same_as(entity))
    pending = {f.id for f in model.pending()}
    for fact in live:
        touches = fact.subject in group or (
            isinstance(fact.object, Ref) and fact.object.ref in group
        )
        if fact.predicate == "same_as" and touches:
            answer.add(fact, _state(model, fact, pending))
        elif fact.subject in group and fact.predicate in ("name", "serial"):
            answer.add(fact)
    if not group:
        answer.gaps.append(f"no entity is named or identified as {name!r}")
    return answer


def state_at(model: SiteModel, entity: str, at: str | None = None) -> Answer:
    """Every predicate's state for ``entity`` (and what a stated ``same_as`` joins it to) at t."""
    t = _at(model, at)
    answer = _new("state_at", model, t)
    for subject in model.same_as(entity):
        for reading in model.state_at(t, subject):
            answer.add_cell(reading)
    return answer


def find(model: SiteModel, predicate: str, obj: str, at: str | None = None) -> Answer:
    """Who or what has ``predicate`` = ``obj`` at t (``obj`` a ref, or the value as text), and,
    when ``obj`` is an entity, what is known about it then."""
    t = _at(model, at)
    answer = _new("find", model, t)
    for reading in model.state_at(t):
        if reading.predicate == predicate and any(_text(f.object) == obj for f in reading.facts):
            answer.add_cell(reading)
    if obj in model.entities:
        for reading in model.state_at(t, obj):
            answer.add_cell(reading)
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
    intervals = [
        i
        for i in model.history(entity, start=start, end=end)
        if predicates is None or i.fact.predicate in predicates
    ]
    for interval in sorted(intervals, key=lambda i: i.start or ""):
        fact = interval.fact
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
    asked = "asked" if model.live(fact_id) else "asked; answered by an operator"
    answer.add(fact, _state(model, fact, pending), relation=asked)
    intervals = list(model.history(fact.subject, fact.predicate))
    ids = [i.fact.id for i in intervals]
    if fact.id in ids:
        index = ids.index(fact.id)
        if index > 0:
            answer.add(intervals[index - 1].fact, relation="replaced")
        if index + 1 < len(intervals):
            answer.add(intervals[index + 1].fact, relation="replaced by")
    for other in _live(model):
        if (
            other.id != fact.id
            and (other.subject, other.predicate) == (fact.subject, fact.predicate)
            and other.valid_from == fact.valid_from
            and other.object != fact.object
        ):
            answer.add(other, _state(model, other, pending), relation=_contest(fact, other))
    return answer


def _contest(fact: Fact, other: Fact) -> str:
    if KIND_RANK[fact.kind] > KIND_RANK[other.kind]:
        return "beaten"
    if KIND_RANK[fact.kind] < KIND_RANK[other.kind]:
        return "beats it"
    return "ties"


def diff(model: SiteModel, start: str, end: str, entities: list[str] | None = None) -> Answer:
    """Facts that began in ``(start, end]``, optionally only about ``entities``."""
    answer = _new("diff", model, end)
    wanted = None if entities is None else {s for e in entities for s in model.same_as(e)}
    pending = {f.id for f in model.pending()}
    for fact in _live(model):
        if fact.valid_from is None or not start < fact.valid_from <= end:
            continue
        if wanted is None or fact.subject in wanted:
            answer.add(fact, _state(model, fact, pending))
    if not answer.items:
        answer.gaps.append(f"no fact began between {start} and {end}")
    return answer


def gaps(model: SiteModel, at: str | None = None) -> Answer:
    """What is stale, unknown or ambiguous at t, and every pending proposal."""
    t = _at(model, at)
    answer = _new("gaps", model, t)
    for reading in model.state_at(t):
        if reading.state in ("stale", "unknown", "ambiguous"):
            answer.add_cell(reading)
    for fact in model.pending():
        answer.add(fact, "pending")
    return answer


def impact(model: SiteModel, entity: str, at: str | None = None, depth: int = 3) -> Answer:
    """What lies downstream of ``entity`` at t (the topology view), with the state of each.

    Each robot whose current zone was reached comes too, with its tasks (``current_task``,
    ``assigned_to``), at no extra depth: a zone's robots and their orders are what wait on it.
    """
    t = _at(model, at)
    answer = _new("impact", model, t)
    topology = model.topology(t)
    hops = topology.downstream(entity, depth)
    reached = {entity, *(h.entity for h in hops)}
    edges = [h.edge for h in hops]
    for reading in model.state_at(t):
        objects = {_text(f.object) for f in reading.facts}
        if reading.predicate == "in_zone" and objects & reached and reading.subject not in reached:
            reached.add(reading.subject)
            edges += reading.facts
    for reading in model.state_at(t):
        if reading.predicate == "current_task" and reading.subject in reached:
            reached |= {_text(f.object) for f in reading.facts}
            edges += reading.facts
    for edge in topology.edges:
        if edge.predicate == "assigned_to" and _text(edge.object) in reached:
            reached.add(edge.subject)
            edges.append(edge)
    for fact in dict.fromkeys(edges):
        answer.add(fact)
    for subject in sorted(reached):
        for reading in model.state_at(t, subject):
            if reading.predicate in STATUS:
                answer.add_cell(reading)
    return answer


def search_evidence(model: SiteModel, text: str, limit: int = 20) -> Answer:
    """Facts whose citation quotes or text values hold every word of ``text``, best first."""
    answer = _new("search_evidence", model, None)
    words = [w for w in re.split(r"\s+", text.casefold()) if w]
    scored: list[tuple[int, int, Fact]] = []
    for order, fact in enumerate(_live(model)):
        haystack = " ".join(
            [*(c.quote or "" for c in fact.citations), *(c.file for c in fact.citations)]
            + ([str(fact.object.value)] if isinstance(fact.object, Value) else [])
        ).casefold()
        if words and all(w in haystack for w in words):
            scored.append((-haystack.count(words[0]), order, fact))
    pending = {f.id for f in model.pending()}
    for _, _, fact in sorted(scored)[:limit]:
        answer.add(fact, _state(model, fact, pending))
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
            for f in _live(model)
        ],
    )
    entities = sorted(model.entities)
    names: dict[str, list[str]] = {}
    for fact in _live(model):
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
    db.executemany(
        "INSERT INTO seen VALUES (?,?)",
        [(source, t) for source, times in model.seen.items() for t in times],
    )
    db.commit()
    db.execute("PRAGMA query_only = ON")
    return db
