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

    from neptune_memory.site.views import Interval, Reading, SiteModel

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
    also fills ``columns`` and ``rows``. Past ``MAX_ITEMS`` items are counted in ``omitted``,
    not kept, so one answer stays small enough for an agent to read."""

    tool: str
    at: str | None
    as_of_seq: int
    items: list[Item] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    sources: dict[str, Citation] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    omitted: int = 0
    sections: list[tuple[int, str]] = field(default_factory=list)
    """``(index of its first item, title)``: headings ``render`` prints (the shift report)."""
    _stale: dict[tuple[str, str, str | None], tuple[int, list[str]]] = field(
        default_factory=dict, repr=False, compare=False
    )

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

    def full(self) -> bool:
        """True once ``MAX_ITEMS`` are kept; counts the item that did not fit."""
        if len(self.items) < MAX_ITEMS:
            return False
        self.omitted += 1
        return True

    def add(
        self,
        fact: Fact,
        state: State = "known",
        relation: str | None = None,
        span: tuple[str | None, str | None] | None = None,
    ) -> None:
        """One fact as an item; ``span`` replaces its stated validity with a history interval."""
        if self.full():
            return
        start, end = span if span is not None else (fact.valid_from, fact.valid_to)
        self.items.append(
            Item(
                fact_id=fact.id,
                subject=fact.subject,
                predicate=fact.predicate,
                object=fact.object,
                valid_from=start,
                valid_to=end,
                kind=fact.kind,
                state=state,
                cite=self.cite(fact.citations),
                relation=relation,
            )
        )

    def add_cell(self, cell: Reading) -> None:
        if not cell.facts:  # the item says "unknown"; a gap line only when it did not fit
            if self.full():
                self.gaps.append(f"{cell.subject} {cell.predicate}: {cell.state}, no source says")
            else:
                self.items.append(
                    Item(None, cell.subject, cell.predicate, None, None, None, None, cell.state, ())
                )
            return
        for fact in cell.facts:
            self.add(fact, cell.state)
        if cell.state == "stale":  # one line per subject and source: "a, b: stale, ..."
            key = (cell.subject, cell.facts[0].source, cell.last_seen)
            tail = f": stale, {key[1]} last reported at {cell.last_seen}"
            if key in self._stale:
                index, names = self._stale[key]
                names.append(cell.predicate)
                self.gaps[index] = f"{cell.subject} {', '.join(names)}{tail}"
            else:
                self._stale[key] = (len(self.gaps), [cell.predicate])
                self.gaps.append(f"{cell.subject} {cell.predicate}{tail}")
        elif cell.state == "ambiguous":
            self.gaps.append(f"{cell.subject} {cell.predicate}: ambiguous, sources disagree")


# --- Helpers -----------------------------------------------------------------------------------

_FAR_FUTURE = "9999-12-31T23:59:59"
MAX_ITEMS = 200
SERIES_LIMIT = 3
"""``diff`` shows a (subject, predicate) that changed more often than this by its ends only."""
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
    """Names and ids to entities (a name matches on any part of it, "Plant 7" finds "Plant 7
    assembly hall"): stated ``same_as`` joined, pending proposals listed."""
    answer = _new("resolve", model, None)
    needle = name.strip().casefold()
    matched = [
        e
        for e in sorted(model.entities)
        if needle in (e.casefold(), e.split(":", 1)[-1].casefold())
    ]
    live = _live(model)
    matched += [
        f.subject for f in live if f.predicate == "name" and needle in _text(f.object).casefold()
    ]
    group: set[str] = set()
    for entity in matched:
        group.update(model.same_as(entity))
    pending = {f.id for f in model.pending()}
    said: set[tuple[str, str, Ref | Value, str]] = set()
    for fact in live:
        said_once = (fact.subject, fact.predicate, fact.object, fact.kind)
        if said_once in said:
            continue  # a feed repeating a serial or name says nothing new
        said.add(said_once)
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
        answer.add(interval.fact, span=(interval.start, interval.end))
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
    """Values that began in ``(start, end]`` (history intervals, so a value a feed repeats counts
    once), optionally only about ``entities``, in site-clock order."""
    answer = _new("diff", model, end)
    wanted = model.entities if entities is None else {s for e in entities for s in model.same_as(e)}
    pending = {f.id for f in model.pending()}
    began = [
        i
        for subject in sorted(wanted)
        for i in model.history(subject, start=start, end=end)
        if i.start is not None and start < i.start <= end
    ]
    series: dict[tuple[str, str], list[Interval]] = {}
    for interval in began:
        series.setdefault((interval.fact.subject, interval.fact.predicate), []).append(interval)
    tasks = sorted({subject for subject, _ in series if subject.startswith("task:")})
    if entities is None and len(tasks) > SERIES_LIMIT:  # fleet order churn: count, don't list
        answer.gaps.append(
            f"{len(tasks)} tasks began ({tasks[0]} to {tasks[-1]}); "
            "state_at or timeline on one gives its detail"
        )
        series = {key: v for key, v in series.items() if not key[0].startswith("task:")}
    shown: list[Interval] = []
    for (subject, predicate), changes in series.items():
        if len(changes) > SERIES_LIMIT:  # a battery or counter: its first and last change
            answer.gaps.append(
                f"{subject} {predicate} changed {len(changes)} times; first and last shown "
                "(timeline gives every one)"
            )
            changes = [changes[0], changes[-1]]
        shown += changes
    for interval in sorted(shown, key=lambda i: i.start or ""):
        fact = interval.fact
        answer.add(fact, _state(model, fact, pending), span=(interval.start, interval.end))
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
    shown = dict.fromkeys(edges)
    for fact in shown:
        answer.add(fact)
    for subject in sorted(reached):
        for reading in model.state_at(t, subject):
            if reading.predicate not in STATUS:
                continue
            if reading.state == "known" and all(f in shown for f in reading.facts):
                continue  # already listed as an edge
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
    ``entities``, and the analysis tables ``downtime``, ``utilisation`` and ``fault_clear_time``
    (``insights.analysis_tables``); rows that select a ``fact_id`` cite that fact."""
    answer = _new("sql", model, None)
    db = _database(model)
    try:
        cursor = db.execute(query)
        answer.columns = [d[0] for d in cursor.description or ()]
        answer.rows = [tuple(r) for r in cursor.fetchmany(SQL_ROW_LIMIT + 1)]
    except sqlite3.Error as error:
        answer.gaps.append(f"sql error: {error}")
        return answer
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


_DATABASE: dict[str, tuple[SiteModel, int, sqlite3.Connection]] = {}


def _database(model: SiteModel) -> sqlite3.Connection:
    """The tables for ``model``, built once per model and log position (one cached at a time)."""
    cached = _DATABASE.get("last")
    if cached is not None and cached[0] is model and cached[1] == model.seq:
        return cached[2]
    if cached is not None:
        cached[2].close()
    db = sqlite3.connect(":memory:", check_same_thread=False)
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
    from neptune_memory.site.insights import analysis_tables  # it imports this module

    analysis_tables(model, db)
    db.commit()
    db.execute("PRAGMA query_only = ON")
    _DATABASE["last"] = (model, model.seq, db)
    return db
