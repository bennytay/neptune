"""Insights (#203): deterministic rules that write cited **inferred** findings, and a shift report.

No model is called. Each rule reads the views (``history``, ``topology``, ``seen``) through the
generic vocabulary and topology, never a site's own ids, so it works on any site:

- **starvation chain:** a ``fault:`` health on an entity, then something downstream of it (the
  topology, robots in a reached zone, their tasks) goes idle, starved, waiting or blocked within
  ``STARVE_WITHIN`` seconds;
- **qualification gap:** a fault whose required qualification (a document or rule that names the
  entity, or covers its zone) no on-shift, not-on-break person holds;
- **stale source:** a source (or one device of a fleet) silent for over ``STALE_AFTER`` report
  intervals while other sources keep reporting;
- **change before fault:** a stated change, work order, setpoint or firmware change on the same
  entity within ``CHANGE_WITHIN`` seconds before a fault: a correlation, never more;
- **regulation check:** a work order against a rule clause that shares its words: lockout recorded
  when the clause asks for it, done by a holder of the qualification the rule requires.

A finding is a ``Fact`` (predicate ``finding``, source ``neptune.insights``, kind ``inferred``,
model ``None``) whose citations are its supporting facts' citations. ``findings(model, at)`` only
reads what was true by ``at``, so a live server computes them as the log grows; ``append`` writes
the new ones to the log (``python -m neptune_memory.site insights <site_dir>``).
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING

from neptune_memory.site import log as fact_log
from neptune_memory.site.facts import Fact, Ref, Value
from neptune_memory.site.query import STATUS, Answer, _at, _text
from neptune_memory.site.views import FORWARD, STALE_AFTER, Hop, load
from neptune_memory.site.vocab import entity_type

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable
    from pathlib import Path

    from neptune_memory.site.views import SiteModel

SOURCE = "neptune.insights"
PREDICATE = "finding"
STARVE_WITHIN = 120
"""Seconds after an upstream fault in which a downstream wait counts as part of its chain."""
CHANGE_WITHIN = 24 * 3600
"""Seconds before a fault in which a change on the same entity is reported beside it."""
DEPTH = 3

WAIT = frozenset({"idle", "starved", "starving", "starve", "waiting", "wait", "blocked"})
DOWN = WAIT - {"idle"} | {"stopped", "stop", "fault", "faulted", "error", "estop", "down"}
LOCKOUT = frozenset({"lockout", "loto", "lock"})
YES = frozenset({"yes", "y", "true", "1"})
STOP_WORDS = frozenset(
    {"the", "and", "only", "under", "for", "per", "with", "person", "are", "shall", "after"}
    | {"must", "when", "who", "this", "that", "than", "from", "into", "each", "any", "all", "not"}
)
CHANGES = ("change", "work_order", "setpoint", "firmware")


@dataclass(frozen=True)
class Span:
    """A fact's interval as known at ``at``: ``end`` is ``None`` while it still holds."""

    fact: Fact
    start: str
    end: str | None

    @property
    def value(self) -> str:
        return _text(self.fact.object)


# --- Small helpers ---------------------------------------------------------------------------


def _dt(t: str) -> datetime:
    return datetime.fromisoformat(t)


def _plus(t: str, seconds: float) -> str:
    return (_dt(t) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _seconds(start: str, end: str) -> float:
    return (_dt(end) - _dt(start)).total_seconds()


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.casefold()) if w}


def _stems(text: str) -> set[str]:
    out = set()
    for word in _words(text):
        for suffix in ("ing", "ed", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
        if len(word) >= 3 and word not in STOP_WORDS:
            out.add(word)
    return out


def _clock(t: str, day: str) -> str:
    """``t`` as its clock time when it falls on ``day``, else in full."""
    return t[11:] if t.startswith(day + "T") else t


def _facts(model: SiteModel, subject: str, predicate: str) -> list[Fact]:
    return [model.facts[i] for i in model.by_key.get((subject, predicate), []) if model.live(i)]


def _subjects(model: SiteModel, predicate: str) -> list[str]:
    return sorted({s for (s, p) in model.by_key if p == predicate})


def _spans(model: SiteModel, subject: str, predicate: str, at: str) -> list[Span]:
    """``subject``'s ``predicate`` intervals that began by ``at``, cut at ``at``."""
    out = []
    for interval in model.history(subject, predicate):
        if interval.start is None or interval.start > at:
            continue
        end = interval.end if interval.end is not None and interval.end <= at else None
        out.append(Span(interval.fact, interval.start, end))
    return out


def _changes(spans: list[Span]) -> list[Span]:
    """A one-valued predicate's spans that replaced an earlier, different value."""
    return [s for i, s in enumerate(spans) if i and spans[i - 1].value != s.value]


def _covers(fact: Fact, t: str) -> bool:
    return (fact.valid_from is None or fact.valid_from <= t) and (
        fact.valid_to is None or t < fact.valid_to
    )


def _names(model: SiteModel, entity: str) -> list[str]:
    return sorted(
        {_text(f.object) for e in model.same_as(entity) for f in _facts(model, e, "name")}
    )


def _mentions(text: str, names: Iterable[str]) -> bool:
    folded = text.casefold()
    return any(
        len(name) >= 3 and re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", folded)
        for name in names
    )


def _first(model: SiteModel, subject: str, predicate: str) -> Fact | None:
    facts = _facts(model, subject, predicate)
    return facts[0] if facts else None


def _finding(subject: str, text: str, start: str, end: str | None, support: Iterable[Fact]) -> Fact:
    citations = tuple(dict.fromkeys(c for f in support for c in f.citations))
    return Fact(subject, PREDICATE, Value(text), start, end, "inferred", SOURCE, citations, None)


def _faults(model: SiteModel, at: str) -> list[Span]:
    return [
        span
        for subject in _subjects(model, "health")
        for span in _spans(model, subject, "health", at)
        if span.value.startswith("fault:")
    ]


def _until(span: Span, day: str) -> str:
    return f"until {_clock(span.end, day)}" if span.end is not None else "still"


# --- Rule 1: starvation chain ----------------------------------------------------------------


def starvation_chains(model: SiteModel, at: str) -> list[Fact]:
    out = []
    for fault in _faults(model, at):
        root, start = fault.fact.subject, fault.start
        horizon = _plus(start, STARVE_WITHIN)
        hops = {h.entity: h for h in model.topology(start).downstream(root, DEPTH)}
        via: dict[str, list[Fact]] = {e: _path(hops, e, root) for e in hops}
        for robot in _subjects(model, "in_zone"):  # robots standing in a reached zone
            for span in _spans(model, robot, "in_zone", horizon):
                if span.value in hops and robot not in via and _overlaps(span, start, horizon):
                    via[robot] = [*_path(hops, span.value, root), span.fact]
        links: list[tuple[str, str, str, list[Fact]]] = []  # (time, who, value, support)
        for entity in sorted(via):
            for span in _spans(model, entity, "mode", horizon):
                if start <= span.start <= horizon and _words(span.value) & WAIT:
                    links.append((span.start, entity, span.value, [*via[entity], span.fact]))
        tasks: dict[tuple[str, Fact], list[tuple[str, list[Fact]]]] = {}
        for robot in sorted(e for e in via if entity_type(e) == "robot"):
            for current in _spans(model, robot, "current_task", horizon):
                if not _overlaps(current, start, horizon):
                    continue
                for state in _spans(model, current.value, "task_state", horizon):
                    if start <= state.start <= horizon and _words(state.value) & WAIT:
                        key = (current.value, state.fact)
                        tasks.setdefault(key, []).append((robot, [*via[robot], current.fact]))
        for (task, state_fact), robots in tasks.items():
            who = f"{task} ({', '.join(r for r, _ in robots)})"
            support = [f for _, path in robots for f in path] + [state_fact]
            links.append((state_fact.valid_from or start, who, _text(state_fact.object), support))
        if not links:
            continue
        links.sort(key=lambda link: (link[0], link[1]))
        day = start[:10]
        steps = [f"{root} {fault.value} at {_clock(start, day)}"] + [
            f"{who} {value} at {_clock(t, day)}" for t, who, value, _ in links
        ]
        text = (
            f"starvation chain: {' → '.join(steps)} (downstream in the topology, within "
            f"{STARVE_WITHIN} s; the fault holds {_until(fault, day)})"
        )
        support = [fault.fact] + [f for *_, s in links for f in s]
        out.append(_finding(root, text, start, fault.end, support))
    return out


def _overlaps(span: Span, start: str, end: str) -> bool:
    return span.start <= end and (span.end is None or span.end > start)


def _path(hops: dict[str, Hop], entity: str, root: str) -> list[Fact]:
    """The topology edges from ``root`` down to ``entity``, root first."""
    edges: list[Fact] = []
    while entity != root and entity in hops:
        edge = hops[entity].edge
        edges.append(edge)
        entity = edge.subject if edge.predicate in FORWARD else _text(edge.object)
    return edges[::-1]


# --- Rule 2: qualification gap ---------------------------------------------------------------


def _requirements(model: SiteModel, entity: str, t: str) -> dict[str, list[Fact]]:
    """The qualifications a document or rule requires for ``entity``: those that name it (by a
    stated name) or cover the zone it stands in. Qualification ref -> requirement facts."""
    names = _names(model, entity)
    zones = {
        _text(f.object)
        for e in model.same_as(entity)
        for p in ("located_in", "in_zone")
        for f in _facts(model, e, p)
        if _covers(f, t)
    }
    out: dict[str, list[Fact]] = {}
    for holder in _subjects(model, "requires_qualification"):
        text = " ".join(
            [_text(f.object) for p in ("name", "rule_text") for f in _facts(model, holder, p)]
        )
        covered = {_text(f.object) for f in _facts(model, holder, "covers_zone")}
        for req in _facts(model, holder, "requires_qualification"):
            quotes = " ".join(c.quote or "" for c in req.citations)
            if _mentions(f"{text} {quotes}", names) or covered & zones:
                out.setdefault(_text(req.object), []).append(req)
    return out


def _holders(model: SiteModel, qualification: str, t: str) -> list[Fact]:
    return [
        f
        for person in _subjects(model, "holds_qualification")
        for f in _facts(model, person, "holds_qualification")
        if _text(f.object) == qualification and (f.valid_from is None or f.valid_from[:19] <= t)
    ]


def _free(model: SiteModel, person: str, t: str) -> bool:
    on_shift = any(_covers(f, t) for f in _facts(model, person, "on_shift"))
    on_break = any(_covers(f, t) for f in _facts(model, person, "on_break"))
    return on_shift and not on_break


def _status(model: SiteModel, person: str, t: str, day: str) -> tuple[str, list[Fact]]:
    breaks = [f for f in _facts(model, person, "on_break") if _covers(f, t)]
    shifts = [f for f in _facts(model, person, "on_shift") if _covers(f, t)]
    if breaks:
        b = breaks[0]
        span = f"{_clock(b.valid_from or '', day)} to {_clock(b.valid_to or '', day)}"
        return f"on break {span}", shifts + breaks
    return ("on shift" if shifts else "not on shift"), shifts


def qualification_gaps(model: SiteModel, at: str) -> list[Fact]:
    out = []
    for fault in _faults(model, at):
        entity, start = fault.fact.subject, fault.start
        end = fault.end or at
        for qualification, reqs in sorted(_requirements(model, entity, start).items()):
            held = _holders(model, qualification, start)
            people = sorted({f.subject for f in held})
            marks = {start, end}
            for person in people:
                for p in ("on_shift", "on_break"):
                    for f in _facts(model, person, p):
                        marks |= {t for t in (f.valid_from, f.valid_to) if t and start < t < end}
            points = sorted(marks)
            gaps: list[tuple[str, str]] = []
            for a, b in pairwise(points):
                if any(_free(model, p, a) for p in people):
                    continue
                if gaps and gaps[-1][1] == a:
                    gaps[-1] = (gaps[-1][0], b)
                else:
                    gaps.append((a, b))
            day = start[:10]
            documents = ", ".join(sorted({r.subject for r in reqs}))
            for a, b in gaps:
                gap_end = None if b == end and fault.end is None else b
                status = [(p, *_status(model, p, a, day)) for p in people]
                who = (
                    "; ".join(
                        f"{p}, is {s}" if len(people) == 1 else f"{p} is {s}" for p, s, _ in status
                    )
                    if people
                    else "nobody holds it"
                )
                only = "its only holder, " if len(people) == 1 else "holders: "
                text = (
                    f"qualification gap: {entity} {fault.value} needs {qualification} "
                    f"({documents}) and no on-shift holder is free from {_clock(a, day)} to "
                    f"{_clock(b, day) if gap_end else 'now'}; {only if people else ''}{who}"
                )
                support = [fault.fact, *reqs, *held] + [f for *_, fs in status for f in fs]
                out.append(_finding(entity, text, a, gap_end, support))
    return out


# --- Rule 3: stale source --------------------------------------------------------------------


def stale_sources(model: SiteModel, at: str) -> list[Fact]:
    out = []
    devices = {key.split("/", 1)[0] for key in model.seen if "/" in key}
    keys = sorted(k for k in model.seen if "/" in k or k not in devices)
    for key in keys:
        source, _, device = key.partition("/")
        interval = model.report_interval.get(source)
        if interval is None:
            continue
        limit = STALE_AFTER * interval
        times = model.seen[key][: bisect.bisect_right(model.seen[key], at)]
        for i, last in enumerate(times):
            following = times[i + 1] if i + 1 < len(times) else None
            if _seconds(last, following or at) <= limit:
                continue
            stale_from = _plus(last, limit)
            active = _active(model, key, stale_from, following or at, closed=following is None)
            if not active:
                continue  # the whole site was quiet: no shift, no finding
            out.append(_stale(model, key, source, device, last, stale_from, following, active))
    return out


def _active(model: SiteModel, key: str, start: str, end: str, *, closed: bool) -> list[str]:
    """The other sources that reported after ``start`` and before ``end`` (or at it, when the
    silence is still open)."""
    cut = bisect.bisect_right if closed else bisect.bisect_left
    return sorted(
        other
        for other, times in model.seen.items()
        if other != key and "/" not in other and cut(times, end) > bisect.bisect_right(times, start)
    )


def _stale(
    model: SiteModel,
    key: str,
    source: str,
    device: str,
    last: str,
    stale_from: str,
    resumed: str | None,
    active: list[str],
) -> Fact:
    owners = sorted(
        {
            f.subject
            for f in model.facts.values()
            if device and f.source == source and f.subject.split(":", 1)[-1] == device
        }
    )
    subject = owners[0] if owners else f"source:{source}"
    known: list[Fact] = []
    for reading in model.state_at(last, subject):
        if reading.predicate in STATUS and reading.facts and reading.facts[0].source == source:
            known.append(reading.facts[0])
    interval = _first(model, f"source:{source}", "report_interval")
    day = stale_from[:10]
    values = ", ".join(f"{f.predicate} {_value(f)}" for f in known)
    text = (
        f"stale source: {subject} ({key}) has not reported since {_clock(last, day)} "
        f"(over {STALE_AFTER} report intervals) while {', '.join(active)} kept reporting"
        + (f"; resumed at {_clock(resumed, day)}" if resumed else "")
        + (f"; last known {values}" if values else "")
    )
    support = known + ([interval] if interval else [])
    return _finding(subject, text, stale_from, resumed, support)


def _value(fact: Fact) -> str:
    if isinstance(fact.object, Ref):
        return fact.object.ref
    unit = f" {fact.object.unit}" if fact.object.unit else ""
    return f"{fact.object.value}{unit}"


# --- Rule 4: change before fault -------------------------------------------------------------


def changes_before_faults(model: SiteModel, at: str) -> list[Fact]:
    out = []
    for fault in _faults(model, at):
        entity, start = fault.fact.subject, fault.start
        since = _plus(start, -CHANGE_WITHIN)
        found: list[tuple[str, str, list[Fact]]] = []
        for e in model.same_as(entity):
            for predicate in CHANGES:
                spans = _spans(model, e, predicate, start)
                if predicate in ("setpoint", "firmware"):
                    spans = _changes(spans)
                for span in spans:
                    if since <= span.start < start:
                        found.append((span.start, *_describe(model, span, predicate)))
        if not found:
            continue
        found.sort(key=lambda c: (c[0], c[1]))
        day = start[:10]
        listed = "; ".join(f"{text} at {_clock(t, day)}" for t, text, _ in found)
        text = (
            f"change before fault: {entity} {fault.value} at {_clock(start, day)} came within "
            f"{CHANGE_WITHIN // 3600} h after {listed} (a correlation only)"
        )
        support = [fault.fact] + [f for *_, fs in found for f in fs]
        out.append(_finding(entity, text, start, None, support))
    return out


def _describe(model: SiteModel, span: Span, predicate: str) -> tuple[str, list[Fact]]:
    fact = span.fact
    if predicate in ("setpoint", "firmware"):
        return f"{predicate} {_value(fact)} ({fact.kind})", [fact]
    target = span.value
    detail = []
    support = [fact]
    for p in ("name", "change_from", "change_to", "wo_action"):
        f = _first(model, target, p)
        if f is not None:
            support.append(f)
            detail.append(_text(f.object))
    if len(detail) >= 3:  # name, from, to
        text = f"{target} {detail[0]} {detail[1]} → {detail[2]}"
    else:
        text = f"{target} {' '.join(detail)}".strip()
    return f"{text} ({fact.kind})", support


# --- Rule 5: regulation check ----------------------------------------------------------------


def regulation_checks(model: SiteModel, at: str) -> list[Fact]:
    out = []
    equipment: dict[str, list[Fact]] = {}
    for asset in _subjects(model, "work_order"):
        for f in _facts(model, asset, "work_order"):
            equipment.setdefault(_text(f.object), []).append(f)
    for wo in _subjects(model, "wo_action"):
        action = _first(model, wo, "wo_action")
        if action is None or (action.valid_from or "") > at:
            continue
        problem = _first(model, wo, "wo_problem")
        wo_words = _stems(_text(action.object) + " " + (_text(problem.object) if problem else ""))
        assets = equipment.get(wo, [])
        asset_words = {w for f in assets for n in _names(model, f.subject) for w in _stems(n)}
        for rule in _subjects(model, "rule_text"):
            clause = _first(model, rule, "rule_text")
            if clause is None:
                continue
            words = _stems(_text(clause.object))
            if not (words & wo_words) or len(words & (wo_words | asset_words)) < 2:
                continue
            out.append(_check(model, wo, action, assets, rule, clause))
    return out


def _check(
    model: SiteModel, wo: str, action: Fact, assets: list[Fact], rule: str, clause: Fact
) -> Fact:
    when = action.valid_from or ""
    met: list[str] = []
    unmet: list[str] = []
    support = [clause, action, *assets]
    if _words(_text(clause.object)) & LOCKOUT:
        loto = _first(model, wo, "wo_loto")
        if loto is None:
            unmet.append("lockout is not recorded")
        else:
            support.append(loto)
            said = _text(loto.object)
            (met if said.casefold() in YES else unmet).append(f"lockout recorded {said!r}")
    by = _first(model, wo, "wo_by")
    for req in _facts(model, rule, "requires_qualification"):
        qualification = _text(req.object)
        support.append(req)
        if by is None:
            unmet.append(f"no person recorded, so {qualification} cannot be checked")
            continue
        support.append(by)
        person = _text(by.object)
        held = [f for f in _holders(model, qualification, when) if f.subject == person]
        support += held
        (met if held else unmet).append(
            f"done by {person}, who {'holds' if held else 'is not recorded holding'} "
            f"{qualification}"
        )
    on = ", ".join(f.subject for f in assets)
    verdict = (
        "the records meet the clause"
        if not unmet
        else f"the records do not show the clause met: {'; '.join(unmet)}"
    )
    text = (
        f"regulation check: {wo} ({_text(action.object)}{' on ' + on if on else ''}) against "
        f"{rule}: {'; '.join(met) or 'nothing recorded'}; {verdict}"
    )
    return _finding(wo, text, when, None, support)


# --- All rules, the log, the tools -----------------------------------------------------------

RULES = (
    starvation_chains,
    qualification_gaps,
    stale_sources,
    changes_before_faults,
    regulation_checks,
)


def findings(model: SiteModel, at: str | None = None) -> list[Fact]:
    """Every rule's findings from what was true by ``at`` (``None`` = the log's clock), in
    site-clock order; each finding once."""
    t = _at(model, at)
    found = {f.id: f for rule in RULES for f in rule(model, t)}
    return sorted(found.values(), key=lambda f: (f.valid_from or "", f.subject, f.id))


def append(site_dir: Path) -> list[Fact]:
    """Append the findings at the log's clock that the log does not hold yet; return them.
    Running it again appends nothing."""
    model = load(site_dir)
    new = [f for f in findings(model) if f.id not in model.facts]
    with fact_log.FactLog(site_dir) as log:
        for fact in new:
            log.fact(fact)
    return new


RECENT = timedelta(hours=2)
"""``insights`` shows findings that began this long before ``at``, and those still open."""
OPEN_ENDED = ("stale source", "qualification gap")
"""Findings that hold with no end until the state changes (the others are events)."""


def _current(fact: Fact, t: str, since: str) -> bool:
    """Began since ``since``, or still holds at ``t``."""
    if (fact.valid_from or "") >= since:
        return True
    if fact.valid_to is not None:
        return fact.valid_to > t
    return str(getattr(fact.object, "value", "")).startswith(OPEN_ENDED)


def insights(model: SiteModel, at: str | None = None, since: str | None = None) -> Answer:
    """The findings at ``at`` as a cited answer (``neptune_site_insights``): those still open at
    ``at`` and those that began since ``since`` (default: ``RECENT`` before ``at``). Earlier
    ones are counted, not shown, so the answer stays the size of the moment asked about; pass
    an earlier ``since`` (or use ``shift_report``) for history."""
    t = _at(model, at)
    if since is None:
        since = (datetime.fromisoformat(t) - RECENT).isoformat(timespec="seconds") if at else ""
    answer = Answer(tool="insights", at=t, as_of_seq=model.seq)
    earlier = 0
    for fact in findings(model, t):
        if _current(fact, t, since):
            answer.add(fact)
        else:
            earlier += 1
    if earlier:
        answer.gaps.append(
            f"{earlier} earlier findings, closed before {since}, not shown: pass since to see them"
        )
    if not answer.items:
        answer.gaps.append(f"no rule finds anything open at {t} or since {since}")
    answer.gaps.append("findings are inferred by deterministic rules: correlations, not causes")
    return answer


# --- The shift report ------------------------------------------------------------------------


def _window(
    model: SiteModel, shift_id: str | None, start: str | None, end: str | None
) -> tuple[str, str, str]:
    """(label, start, end) of the report: the given times, or the latest window of the shift
    (``shift:A`` or ``A``) that began by the log's clock."""
    clock = model.clock() or "9999-12-31T23:59:59"
    shift = None if shift_id is None else shift_id if ":" in shift_id else f"shift:{shift_id}"
    windows = sorted(
        {
            (f.valid_from, f.valid_to)
            for person in _subjects(model, "on_shift")
            for f in _facts(model, person, "on_shift")
            if f.valid_from and f.valid_to and (shift is None or _text(f.object) == shift)
        }
    )
    begun = [w for w in windows if w[0] is not None and w[0] <= clock] or windows
    if start is None or end is None:
        if not begun:
            raise ValueError(f"no shift {shift_id!r} in the roster; pass start and end")
        w_start, w_end = begun[-1]
        start, end = start or w_start, end or w_end
    assert start is not None and end is not None
    return (f"{shift or 'shift'} {start} to {end}" if shift_id else f"{start} to {end}"), start, end


def shift_report(
    model: SiteModel,
    shift_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> Answer:
    """A compact cited report of one shift (or ``start``..``end``): who was on, what happened,
    the findings (chains, gaps, stale sources, changes before faults) and what is unknown."""
    label, start, end = _window(model, shift_id, start, end)
    clock = model.clock() or end
    at = min(end, clock)
    answer = Answer(tool="shift_report", at=at, as_of_seq=model.seq)
    if clock < end:
        answer.gaps.append(f"the log ends at {clock}, before the window ends at {end}")

    def section(title: str, facts: Iterable[Fact | tuple[Fact, str | None, str | None]]) -> None:
        answer.sections.append((len(answer.items), title))
        for item in facts:
            if isinstance(item, Fact):
                answer.add(item)
            else:
                fact, a, b = item
                answer.add(fact, span=(a, b))

    section(
        f"On shift ({label})",
        [
            f
            for person in _subjects(model, "on_shift")
            for p in ("on_shift", "on_break")
            for f in _facts(model, person, p)
            if (f.valid_from or "") < end and (f.valid_to is None or f.valid_to > start)
        ],
    )
    section("What happened", _happened(model, start, end, at))
    found = [
        f
        for f in findings(model, at)
        if (f.valid_from or "") <= end and (f.valid_to is None or f.valid_to > start)
    ]
    for title, prefix in (
        ("Chains (inferred)", ("starvation chain",)),
        ("Gaps and checks (inferred)", ("qualification gap", "regulation check")),
        ("Stale sources (inferred)", ("stale source",)),
        ("Changes before faults (inferred, correlation only)", ("change before fault",)),
    ):
        section(title, [f for f in found if _text(f.object).startswith(prefix)])
    previous = max(
        (
            f.valid_to
            for person in _subjects(model, "on_shift")
            for f in _facts(model, person, "on_shift")
            if f.valid_to and f.valid_to <= start
        ),
        default=_plus(start, -CHANGE_WITHIN),
    )
    section(f"Changes since {previous}", _changed(model, previous, start, at))
    mentioned = {i.subject for i in answer.items} | {
        r for i in answer.items if isinstance(i.object, Ref) for r in (i.object.ref,)
    }
    section("Unknown and pending", [])
    for fact in model.pending():
        answer.add(fact, "pending")
    for reading in model.state_at(at):
        if reading.state in ("unknown", "ambiguous") and reading.subject in mentioned:
            answer.add_cell(reading)
    return answer


def _happened(
    model: SiteModel, start: str, end: str, at: str
) -> list[Fact | tuple[Fact, str | None, str | None]]:
    """Faults and stops on robots, machines and cells, each cell's counter at the start and the
    end, and the work orders opened in the window, in site-clock order."""
    out: list[tuple[str, Fact | tuple[Fact, str | None, str | None]]] = []
    for subject in sorted(model.entities):
        if entity_type(subject) not in ("robot", "machine", "cell"):
            continue
        for predicate in ("health", "mode"):
            for span in _spans(model, subject, predicate, at):
                down = span.value.startswith("fault:") or _words(span.value) & DOWN
                if down and span.start <= end and (span.end is None or span.end > start):
                    out.append((span.start, (span.fact, span.start, span.end)))
        counts = _spans(model, subject, "parts_out", at)
        first = [s for s in counts if s.start <= start or s is counts[0]]
        within = [s for s in counts if s.start <= at]
        if counts and within:
            begin = first[-1] if first else counts[0]
            out.append((start, (begin.fact, begin.start, None)))
            if within[-1] is not begin:
                out.append((at, (within[-1].fact, within[-1].start, None)))
    for asset in _subjects(model, "work_order"):
        for edge in _facts(model, asset, "work_order"):
            when = edge.valid_from or ""
            if start <= when <= end and when <= at:
                wo = _text(edge.object)
                out.append((when, edge))
                for p in ("wo_action", "wo_cause", "wo_closed"):
                    f = _first(model, wo, p)
                    if f is not None:
                        out.append((when, f))
    out.sort(key=lambda pair: pair[0])
    return [item for _, item in out]


def _changed(model: SiteModel, since: str, start: str, at: str) -> list[Fact]:
    out: list[Fact] = []
    for subject in sorted(model.entities):
        for predicate in CHANGES:
            spans = _spans(model, subject, predicate, at)
            if predicate in ("setpoint", "firmware"):
                spans = _changes(spans)
            for span in spans:
                in_window = predicate == "work_order" and span.start >= start
                if since < span.start <= at and not in_window:
                    out.append(span.fact)
    return sorted(out, key=lambda f: (f.valid_from or "", f.subject))


# --- Analysis tables for ``sql`` -------------------------------------------------------------

TABLES = """
CREATE TABLE downtime (entity TEXT, state TEXT, start TEXT, "end" TEXT, seconds REAL,
  cause TEXT, work_order TEXT, fact_id TEXT);
CREATE TABLE utilisation (robot TEXT, mode TEXT, seconds REAL);
CREATE TABLE fault_clear_time (entity TEXT, fault TEXT, start TEXT, "end" TEXT, seconds REAL,
  fact_id TEXT);
"""


def analysis_tables(model: SiteModel, db: sqlite3.Connection) -> None:
    """Add ``downtime``, ``utilisation`` and ``fault_clear_time`` to ``sql``'s database.

    ``downtime``: each interval a robot, machine or cell spent stopped, starved, waiting or
    blocked, with the cause a work order opened in it states. ``utilisation``: seconds per robot
    and mode, counting only time its source was reporting. ``fault_clear_time``: each ``fault:``
    health interval and how long it took to clear (``NULL`` while it holds)."""
    clock = model.clock() or "9999-12-31T23:59:59"
    db.executescript(TABLES)
    wos: dict[str, list[tuple[str, str, str | None]]] = {}
    for asset in _subjects(model, "work_order"):
        for edge in _facts(model, asset, "work_order"):
            cause = _first(model, _text(edge.object), "wo_cause")
            wos.setdefault(asset, []).append(
                (edge.valid_from or "", _text(edge.object), _text(cause.object) if cause else None)
            )
    downtime = []
    for subject in sorted(model.entities):
        if entity_type(subject) not in ("robot", "machine", "cell"):
            continue
        for span in _spans(model, subject, "mode", clock):
            if not _words(span.value) & DOWN:
                continue
            end = span.end or clock
            orders = [
                w
                for e in model.same_as(subject)
                for w in wos.get(e, [])
                if span.start <= w[0] <= end
            ]
            wo = orders[0] if orders else None
            downtime.append(
                (
                    subject,
                    span.value,
                    span.start,
                    span.end,
                    _seconds(span.start, end),
                    wo[2] if wo else None,
                    wo[1] if wo else None,
                    span.fact.id,
                )
            )
    db.executemany("INSERT INTO downtime VALUES (?,?,?,?,?,?,?,?)", downtime)
    usage: dict[tuple[str, str], float] = {}
    for robot in sorted(e for e in model.entities if entity_type(e) == "robot"):
        for span in _spans(model, robot, "mode", clock):
            key = (robot, span.value)
            covered = _reported(model, span.fact.source, robot, span.start, span.end or clock)
            usage[key] = usage.get(key, 0.0) + covered
    db.executemany(
        "INSERT INTO utilisation VALUES (?,?,?)", [(r, m, s) for (r, m), s in sorted(usage.items())]
    )
    db.executemany(
        "INSERT INTO fault_clear_time VALUES (?,?,?,?,?,?)",
        [
            (
                f.fact.subject,
                f.value,
                f.start,
                f.end,
                _seconds(f.start, f.end) if f.end else None,
                f.fact.id,
            )
            for f in _faults(model, clock)
        ],
    )


def _reported(model: SiteModel, source: str, subject: str, start: str, end: str) -> float:
    """Seconds of ``[start, end)`` in which ``source`` kept reporting about ``subject``."""
    device = f"{source}/{subject.split(':', 1)[-1]}"
    times = model.seen.get(device) or model.seen.get(source) or []
    limit = STALE_AFTER * model.report_interval.get(source, 0.0)
    total = 0.0
    for a, b in pairwise(times):
        if b <= start or a >= end or _seconds(a, b) > limit:
            continue
        total += _seconds(max(a, start), min(b, end))
    return total
