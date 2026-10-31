"""Research (#226): ideas about the mission, tested against the memory's own evidence.

A **round** reads the memory as the improvements run does (``improve.dossier``: the mission,
findings, state, changes, records, with fact ids) plus every earlier research result, and asks
Claude for hypotheses. Each hypothesis comes with a **test spec**, and Neptune runs the test
itself over the memory, so every number is computed from cited facts, never written by a model:

- ``rate_when``: how often an event happens while a condition holds versus while it does not
  (protective stops per hour while ``cycle`` is 12, 13 or 14, against the rest of the time).
- ``before_after``: how often an event happens before a moment versus after it (stops per hour
  before and after a change).

A test is ``supported`` when the rate moves the expected way by at least ``SUPPORT`` times,
``rejected`` when it moves less than ``REJECT`` times (or the other way), and ``inconclusive``
with fewer than ``MIN_EVENTS`` events or too little time on one side. The thresholds are in the
result, so a person can check the call. ``EVALUATORS`` is where a simulator test plugs in later.

**Recorded, then logged**, like improvements: a round is one line of ``research/runs.jsonl``
(model, prompt and dossier hashes, the hypotheses, the computed results and the facts they
became). Each result is an **inferred** ``hypothesis:<key> research_result`` fact citing that
line and then the facts its test counted. ``build`` re-appends the recorded results, never
calling a model, so the memory keeps what it learned and the same files give the same log.
Rejected ideas stay too, and the next round reads them, so they are not proposed again blindly.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neptune_memory.site import actions, improve, views
from neptune_memory.site.facts import Citation, Fact, Value
from neptune_memory.site.log import FactLog
from neptune_memory.site.vocab import entity_type

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune_memory.site.views import SiteModel

SOURCE = "neptune.research"
PREDICATE = "research_result"
RUNS = "research/runs.jsonl"
SUPPORT = 2.0  # the event rate moves the expected way at least this many times
REJECT = 1.25  # under this (or the other way): no real effect
MIN_EVENTS = 3
MIN_SHARE = 0.05  # each side of a comparison needs this share of the observed time
MAX_CITES = 12  # citations kept per result: its run line, then the events it counted
GAP = 900.0  # seconds with no observed fact at all: not observed time (a gap between sessions)

SYSTEM = """\
You are Neptune's research engine for one robot or robot site. The user message is its memory: \
cited facts, one per line as `id | subject predicate object | kind | citation`, the mission \
(goals and constraints), and earlier research results. Propose hypotheses that, if true, would \
move a goal, each with a test Neptune can run on the memory itself.

Rules:
- Ground each hypothesis in the memory: list the fact ids it rests on in `evidence`, only ids \
that appear there. Name the goals it serves (goal:... ids).
- Each test counts an event (a subject, predicate and value exactly as they appear in the \
memory, e.g. robot:ur3 health fault:protective_stop) and compares its rate per hour:
  - rate_when: while a condition holds (subject, predicate, op, value; op is one of = != > >= \
< <= in, `in` takes a comma-separated list) against while it does not;
  - before_after: all observed time before a site time `at` against all of it after, across \
every observed period (it cannot compare within one session); rate_when likewise counts all \
observed time.
  `expect` says which way the hypothesis predicts: "more" (higher while the condition holds, or \
after `at`) or "fewer". Leave unused fields as empty strings.
  A test is inconclusive with fewer than 3 events, or when either side has under 5% of the \
observed time (listed in the memory), so a condition that holds only for seconds at a time \
cannot be tested this way; pick `at` inside an observed period.
- You do not compute results: Neptune runs every test and records supported, rejected or \
inconclusive with the numbers. Do not state numbers you have not read in the memory.
- Read earlier results. Do not repeat a tested hypothesis unless the memory changed in a way \
that could change its result; then say how in `why`. Prefer tests that would settle an \
inconclusive one, or follow a supported one further.
- Correlation is not cause: phrase hypotheses as what the data would show.
- Write values as the memory has them: never add a unit, name or cause it does not state \
(a temperature with no unit stays without one; say so in `unknowns`).
- `next_step` is what a person or their agent could do if it holds: a change, an experiment, \
or data to collect. Never bypassing or changing a safety function.
- 3 to 6 hypotheses, the most valuable for the mission first.
"""

_TEXT = {"type": "string"}
_IDS = {"type": "array", "items": _TEXT}


def _object(required: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": required,
    }


SCHEMA: dict[str, Any] = _object(
    {
        "summary": {"type": "string", "description": "where the mission stands, 1-3 lines"},
        "hypotheses": {
            "type": "array",
            "items": _object(
                {
                    "key": {"type": "string", "description": "short kebab-case id, kept"},
                    "statement": {"type": "string", "description": "what the data would show"},
                    "why": _TEXT,
                    "evidence": _IDS,
                    "goals": _IDS,
                    "test": _object(
                        {
                            "type": {"type": "string", "enum": ["rate_when", "before_after"]},
                            "event": _object(
                                {"subject": _TEXT, "predicate": _TEXT, "value": _TEXT}
                            ),
                            "condition": _object(
                                {
                                    "subject": _TEXT,
                                    "predicate": _TEXT,
                                    "op": {
                                        "type": "string",
                                        "enum": ["", "=", "!=", ">", ">=", "<", "<=", "in"],
                                    },
                                    "value": _TEXT,
                                }
                            ),
                            "at": _TEXT,
                            "expect": {"type": "string", "enum": ["more", "fewer"]},
                        }
                    ),
                    "next_step": _TEXT,
                    "unknowns": _IDS,
                }
            ),
        },
    }
)


# --- Tests ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    verdict: str  # supported | rejected | inconclusive
    text: str  # the numbers, in a line
    numbers: dict[str, Any]
    cites: tuple[Citation, ...] = ()


@dataclass(frozen=True)
class _Span:
    start: datetime
    end: datetime
    fact: Fact


def _t(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return None


def _window(model: SiteModel) -> tuple[datetime, datetime] | None:
    """From the first observed fact to the latest report."""
    periods = observed(model)
    return (periods[0][0], periods[-1][1]) if periods else None


def observed(model: SiteModel) -> list[tuple[datetime, datetime]]:
    """The observed periods: observed facts with no silence longer than ``GAP`` inside, the
    last one running to the latest report. A gap between two sessions is not observed time,
    so it counts neither as time while a condition held nor as time without events."""
    times = sorted(
        t for t in (_t(f.valid_from) for f in model.facts.values() if f.kind == "observed") if t
    )
    end = _t(model.clock())
    if not times or end is None:
        return []
    periods = [[times[0], times[0]]]
    for t in times[1:]:
        if (t - periods[-1][1]).total_seconds() > GAP:
            periods.append([t, t])
        else:
            periods[-1][1] = t
    periods[-1][1] = max(end, periods[-1][1])
    return [(a, b) for a, b in periods]


def _hours(periods: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> float:
    """Observed hours between ``start`` and ``end``."""
    seconds = sum(max(0.0, (min(b, end) - max(a, start)).total_seconds()) for a, b in periods)
    return seconds / 3600


def _spans(model: SiteModel, subject: str, predicate: str, end: datetime) -> list[_Span]:
    out = []
    for interval in model.history(subject, predicate):
        start = _t(interval.start)
        stop = _t(interval.end) or end
        if start is not None and stop > start:
            out.append(_Span(start, min(stop, end), interval.fact))
    return out


def _value(fact: Fact) -> str:
    return str(fact.object.value) if isinstance(fact.object, Value) else str(fact.object)


def _holds(text: str, op: str, wanted: str) -> bool:
    if op == "in":
        return text in {w.strip() for w in wanted.split(",")}
    if op in ("=", "!="):
        return (text == wanted) == (op == "=")
    try:
        a, b = float(text), float(wanted)
    except ValueError:
        return False
    return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]


def _events(model: SiteModel, event: dict[str, str], end: datetime) -> list[_Span]:
    return [
        s
        for s in _spans(model, event["subject"], event["predicate"], end)
        if _value(s.fact) == event["value"]
    ]


def _verdict(
    events: tuple[int, int], hours: tuple[float, float], total: float, expect: str
) -> tuple[str, float | None, str]:
    """(verdict, ratio in the expected direction, why) for events and hours (with, without)."""
    if sum(events) < MIN_EVENTS:
        return "inconclusive", None, f"only {sum(events)} events (needs {MIN_EVENTS})"
    if min(hours) < MIN_SHARE * total:
        return "inconclusive", None, "too little observed time on one side"
    rate_with, rate_without = events[0] / hours[0], events[1] / hours[1]
    ratio = (rate_with / rate_without) if rate_without else math.inf
    if expect == "fewer":
        ratio = (rate_without / rate_with) if rate_with else math.inf
    if ratio >= SUPPORT:
        return "supported", ratio, f"rate moved the expected way {ratio:.1f}x (>= {SUPPORT}x)"
    if ratio < REJECT:
        return "rejected", ratio, f"rate moved {ratio:.2f}x the expected way (< {REJECT}x)"
    return "inconclusive", ratio, f"rate moved {ratio:.2f}x: between {REJECT}x and {SUPPORT}x"


def rate_when(model: SiteModel, test: dict[str, Any]) -> Result:
    window = _window(model)
    if window is None:
        return _unknown("no observed time in the memory")
    t0, t1 = window
    periods = observed(model)
    condition = test["condition"]
    holding = [
        s
        for s in _spans(model, condition["subject"], condition["predicate"], t1)
        if _holds(_value(s.fact), condition["op"], condition["value"])
    ]
    held = sum(_hours(periods, s.start, s.end) for s in holding)
    total = _hours(periods, t0, t1)
    events = _events(model, test["event"], t1)
    inside = [e for e in events if any(h.start <= e.start < h.end for h in holding)]
    counts = (len(inside), len(events) - len(inside))
    verdict, ratio, why = _verdict(counts, (held, total - held), total, test["expect"])
    numbers = {
        "events_when": counts[0],
        "events_otherwise": counts[1],
        "hours_when": round(held, 3),
        "hours_otherwise": round(total - held, 3),
        "ratio": None if ratio is None or math.isinf(ratio) else round(ratio, 2),
        "thresholds": THRESHOLDS,
    }
    rate = _rates(counts, (held, total - held))
    cond = f"{condition['subject']} {condition['predicate']} {condition['op']} {condition['value']}"
    text = (
        f"{verdict}: {counts[0]} events in {held:.2f} h while {cond} ({rate[0]}/h) vs "
        f"{counts[1]} in {total - held:.2f} h otherwise ({rate[1]}/h); {why}"
    )
    return Result(verdict, text, numbers, _cites([e.fact for e in inside or events]))


def before_after(model: SiteModel, test: dict[str, Any]) -> Result:
    window = _window(model)
    at = _t(test.get("at"))
    if window is None or at is None:
        return _unknown("no observed time, or no valid `at`")
    t0, t1 = window
    periods = observed(model)
    events = _events(model, test["event"], t1)
    after = [e for e in events if e.start >= at]
    counts = (len(after), len(events) - len(after))
    hours = (_hours(periods, at, t1), _hours(periods, t0, at))
    total = _hours(periods, t0, t1)
    verdict, ratio, why = _verdict(counts, hours, total, test["expect"])
    numbers = {
        "events_after": counts[0],
        "events_before": counts[1],
        "hours_after": round(hours[0], 3),
        "hours_before": round(hours[1], 3),
        "ratio": None if ratio is None or math.isinf(ratio) else round(ratio, 2),
        "thresholds": THRESHOLDS,
    }
    rate = _rates(counts, hours)
    text = (
        f"{verdict}: {counts[1]} events in {hours[1]:.2f} h before {test['at']} ({rate[1]}/h) vs "
        f"{counts[0]} in {hours[0]:.2f} h after ({rate[0]}/h); {why}"
    )
    return Result(verdict, text, numbers, _cites([e.fact for e in events]))


THRESHOLDS = {
    "support": SUPPORT,
    "reject": REJECT,
    "min_events": MIN_EVENTS,
    "min_share": MIN_SHARE,
    "gap_seconds": GAP,
}


def _unknown(why: str) -> Result:
    return Result("inconclusive", f"inconclusive: {why}", {})


def _rates(counts: tuple[int, int], hours: tuple[float, float]) -> tuple[str, str]:
    return tuple(f"{c / h:.2f}" if h else "n/a" for c, h in zip(counts, hours, strict=True))  # type: ignore[return-value]


def _cites(facts: list[Fact]) -> tuple[Citation, ...]:
    out: dict[Citation, None] = {}
    for fact in facts:
        out.update(dict.fromkeys(fact.citations[:1]))
        if len(out) >= MAX_CITES - 1:
            break
    return tuple(out)


EVALUATORS: dict[str, Callable[[SiteModel, dict[str, Any]], Result]] = {
    "rate_when": rate_when,
    "before_after": before_after,
}
"""Test type -> how Neptune runs it. A simulator evaluator registers here."""


def evaluate(model: SiteModel, test: dict[str, Any]) -> Result:
    """Run one test spec over the memory; an unknown subject or predicate is inconclusive."""
    run = EVALUATORS.get(str(test.get("type")))
    if run is None:
        return _unknown(f"no evaluator for test type {test.get('type')!r}")
    named = [test["event"]] + ([test["condition"]] if test["type"] == "rate_when" else [])
    for part in named:
        if part.get("subject") not in model.entities:
            return _unknown(f"{part.get('subject')!r} is not in the memory")
        if not model.history(part["subject"], part.get("predicate")):
            missing = f"{part['subject']} {part.get('predicate')}"
            return _unknown(f"the memory has no {missing!r}: unknown")
    return run(model, test)


# --- A round ----------------------------------------------------------------------------------


@dataclass
class Round:
    """One research round as recorded."""

    data: dict[str, Any]
    facts: list[Fact] = field(default_factory=list)


def rounds(site_dir: Path) -> list[Round]:
    path = Path(site_dir) / RUNS
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = json.loads(line)
            out.append(Round(data, [Fact.from_json(f) for f in data.get("facts", [])]))
    return out


def recorded(site_dir: Path) -> list[Fact]:
    """Every result fact the recorded rounds hold, in order: what ``build`` re-appends."""
    return [fact for one in rounds(site_dir) for fact in one.facts]


def dossier(model: SiteModel, site_dir: Path) -> improve.Dossier:
    """The improvements dossier, then every earlier research result (newest last)."""
    found = improve.dossier(model, site_dir)
    periods = observed(model)
    lines = ["", "## Observed time (tests count only these periods; a silent gap is not time)"]
    lines += [f"- {a.isoformat()} to {b.isoformat()}" for a, b in periods] or ["- none"]
    lines += ["", "## Earlier research (yours; Neptune ran each test)"]
    for one in rounds(site_dir):
        for item in one.data.get("hypotheses", []):
            lines.append(f"- {item['key']}: {item['statement']} -> {item['result']['text']}")
    if lines[-1].startswith("## Earlier research"):
        lines.append("(none: this is the first round)")
    text = found.text + "\n".join(lines) + "\n"
    return improve.Dossier(text, found.facts, found.at, found.seq, found.findings)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "hypothesis"


def clean(answer: dict[str, Any], found: improve.Dossier, model: SiteModel) -> dict[str, Any]:
    """Claude's hypotheses with ids checked: unknown evidence and goals dropped and listed; a
    hypothesis left with no evidence is dropped whole. Each then gets Neptune's result."""
    goals = {e for e in model.entities if entity_type(e) == "goal"}
    kept, dropped, seen = [], [], set()
    for raw in answer.get("hypotheses", []):
        item = dict(raw)
        key = _slug(str(item.get("key") or item.get("statement") or ""))
        while key in seen:
            key += "-2"
        seen.add(key)
        item["key"] = key
        evidence = [str(i) for i in item.get("evidence", [])]
        item["evidence"] = [i for i in dict.fromkeys(evidence) if i in found.facts]
        dropped += [
            f"{key}: evidence {i} is not in the memory" for i in evidence if i not in found.facts
        ]
        item["goals"] = [g for g in item.get("goals", []) if g in goals]
        if not item["evidence"]:
            dropped.append(f"{key}: dropped, no evidence from the memory")
            continue
        result = evaluate(model, item["test"])
        item["result"] = {"verdict": result.verdict, "text": result.text, **result.numbers}
        item["_cites"] = result.cites
        kept.append(item)
    return {"summary": str(answer.get("summary", "")), "hypotheses": kept, "dropped": dropped}


def record(
    site_dir: Path,
    found: improve.Dossier,
    cleaned: dict[str, Any],
    *,
    model: str,
    trigger: str,
    usage: dict[str, int] | None = None,
) -> list[Fact]:
    """Write the round as a line of ``RUNS`` (with its result facts) and log those facts."""
    site_dir = Path(site_dir)
    path = site_dir / RUNS
    number = (len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0) + 1
    cite_run = Citation(RUNS, f"line {number}", None)
    facts = []
    for item in cleaned["hypotheses"]:
        support = [c for fid in item["evidence"] for c in found.facts[fid].citations[:1]]
        cites = (
            Citation(RUNS, f"line {number}", item["statement"]),
            *item.pop("_cites"),
            *support,
        )
        facts.append(
            Fact(
                f"hypothesis:{item['key']}",
                PREDICATE,
                Value(item["result"]["text"]),
                found.at,
                None,
                "inferred",
                SOURCE,
                tuple(dict.fromkeys(cites))[:MAX_CITES],
                model=model,
            )
        )
    data = {
        "at": found.at,
        "as_of_seq": found.seq,
        "model": model,
        "prompt_sha256": improve._sha(SYSTEM),
        "dossier_sha256": improve._sha(found.text),
        "trigger": trigger,
        "usage": usage or {},
        **cleaned,
        "facts": [f.to_json() for f in facts],
    }
    line = actions._record(site_dir, RUNS, data, f"research round at {found.at}")
    assert line.locator == cite_run.locator, (line.locator, cite_run.locator)
    with FactLog(site_dir) as log:
        for fact in facts:
            log.fact(fact)
    return facts


def run(
    site_dir: Path, client: Any, *, trigger: str = "asked", model: str = improve.MODEL
) -> list[Fact]:
    """One round over the memory as it stands: dossier, ask, test, record."""
    site_dir = Path(site_dir)
    memory = views.load(site_dir)
    found = dossier(memory, site_dir)
    answer, served, usage = improve.ask(client, found.text, model, system=SYSTEM, schema=SCHEMA)
    cleaned = clean(answer, found, memory)
    return record(site_dir, found, cleaned, model=served, trigger=trigger, usage=usage)


# --- Results for people and agents (#227) -----------------------------------------------------

BRIEF_RULE = "Check with a person before acting on a robot. Never change a safety function."


def _cite_json(citation: Citation) -> dict[str, Any]:
    return {"file": citation.file, "locator": citation.locator, "quote": citation.quote}


def _test_text(test: dict[str, Any]) -> str:
    event = test.get("event", {})
    what = f"{event.get('subject')} {event.get('predicate')} = {event.get('value')} per hour"
    if test.get("type") == "before_after":
        return f"{what}, before vs after {test.get('at')} (expects {test.get('expect')} after)"
    cond = test.get("condition", {})
    held = f"{cond.get('subject')} {cond.get('predicate')} {cond.get('op')} {cond.get('value')}"
    return f"{what}, while {held} vs otherwise (expects {test.get('expect')} while it holds)"


def results(site_dir: Path, model: SiteModel) -> dict[str, Any]:
    """The research view: the latest result per hypothesis (cards), every result in order (log),
    each goal with its supported / rejected / inconclusive counts, and the gaps: what the memory
    could not answer and the setup questions nobody has answered."""
    from neptune_memory.site import onboard

    names = {
        f.subject: str(f.object.value)
        for f in model.facts.values()
        if f.predicate == "name" and isinstance(f.object, Value)
    }
    cards: dict[str, dict[str, Any]] = {}
    log: list[dict[str, Any]] = []
    for number, one in enumerate(rounds(site_dir), start=1):
        facts = {f.subject: f for f in one.facts}
        for item in one.data.get("hypotheses", []):
            fact = facts.get(f"hypothesis:{item['key']}")
            card = {
                "key": item["key"],
                "fact": fact.id if fact else None,
                "statement": item["statement"],
                "why": item.get("why", ""),
                "verdict": item["result"]["verdict"],
                "result": item["result"]["text"],
                "numbers": {
                    k: v for k, v in item["result"].items() if k not in ("verdict", "text")
                },
                "test": _test_text(item.get("test", {})),
                "goals": [{"id": g, "name": names.get(g, g)} for g in item.get("goals", [])],
                "evidence": [_cite_json(c) for c in (fact.citations[1:] if fact else ())],
                "next_step": item.get("next_step", ""),
                "unknowns": item.get("unknowns", []),
                "round": number,
                "at": one.data.get("at"),
                "model": one.data.get("model"),
            }
            cards[item["key"]] = card
            log.append(
                {k: card[k] for k in ("round", "at", "key", "statement", "verdict", "result")}
            )
    order = {"supported": 0, "inconclusive": 1, "rejected": 2}
    latest = sorted(cards.values(), key=lambda c: (order.get(c["verdict"], 3), -c["round"]))
    goals = []
    for goal in sorted(e for e in model.entities if entity_type(e) == "goal"):
        tally = {v: 0 for v in order}
        for card in latest:
            if any(g["id"] == goal for g in card["goals"]):
                tally[card["verdict"]] = tally.get(card["verdict"], 0) + 1
        goals.append({"id": goal, "name": names.get(goal, goal), **tally})
    gaps = [
        {"about": c["key"], "text": c["result"].removeprefix("inconclusive: ")}
        for c in latest
        if c["verdict"] == "inconclusive"
    ]
    gaps += [{"about": c["key"], "text": u} for c in latest for u in c["unknowns"]]
    gaps += [
        {"about": "setup", "text": q["question"]}
        for q in onboard.open_questions(site_dir)
        if "answer" not in q
    ]
    return {
        "rounds": len(rounds(site_dir)),
        "cards": latest,
        "log": log[::-1],
        "goals": goals,
        "gaps": gaps,
    }


def brief(site_dir: Path, model: SiteModel, key: str) -> str:
    """One result as a brief an agent can act on: the goal, the constraints, the finding, how
    it was tested, the cited evidence, the suggested next step and what is unknown."""
    view = results(site_dir, model)
    card = next(
        (c for c in view["cards"] if c["key"] in (key, key.removeprefix("hypothesis:"))), None
    )
    if card is None:
        known = ", ".join(c["key"] for c in view["cards"]) or "none yet"
        raise ValueError(f"no research result {key!r}; results: {known}")
    goals = {f.subject: f for f in model.facts.values() if entity_type(f.subject) == "goal"}
    lines = [f"# Research result: {card['statement']}", ""]
    lines.append("## Goal")
    served = card["goals"] or [{"id": g, "name": g} for g in sorted(goals)]
    for goal in served:
        metric = _first(model, goal["id"], "metric")
        target = _first(model, goal["id"], "target")
        lines.append(f"- {goal['name']} ({goal['id']}): {metric} {target}".rstrip())
    limits = sorted(
        {_first(model, e, "name") for e in model.entities if entity_type(e) == "constraint"}
    )
    lines += ["", "## Constraints (any change must respect them)"]
    lines += [f"- {c}" for c in limits] or ["- none stated"]
    lines += ["", f"## Finding: {card['verdict']}", card["result"], "", f"Tested: {card['test']}"]
    lines.append(f"Why it was proposed: {card['why']}")
    lines += ["", "## Evidence (cited)"]
    lines += [
        f"- {c['file']} {c['locator']}" + (f' "{c["quote"]}"' if c["quote"] else "")
        for c in card["evidence"]
    ]
    lines += ["", "## Suggested next step", card["next_step"] or "none"]
    if card["unknowns"]:
        lines += ["", "## Unknown"] + [f"- {u}" for u in card["unknowns"]]
    lines += [
        "",
        f"Neptune research round {card['round']} at {card['at']} ({card['model']}).",
        BRIEF_RULE,
    ]
    return "\n".join(lines) + "\n"


def _first(model: SiteModel, subject: str, predicate: str) -> str:
    for fact in model.facts.values():
        if (
            fact.subject == subject
            and fact.predicate == predicate
            and isinstance(fact.object, Value)
        ):
            return str(fact.object.value)
    return ""


def summary(site_dir: Path) -> str:
    """The research view as text (``neptune_site_research``)."""
    model = views.load(Path(site_dir))
    view = results(site_dir, model)
    if not view["cards"]:
        return "No research yet: run a round (`python -m neptune_memory.site research <site>`)."
    lines = [f"Research: {view['rounds']} round(s), {len(view['cards'])} hypotheses."]
    for goal in view["goals"]:
        lines.append(
            f"Goal {goal['name']}: {goal['supported']} supported, {goal['rejected']} rejected, "
            f"{goal['inconclusive']} inconclusive"
        )
    lines.append("Results (neptune_site_research_result {id} for the full brief):")
    lines += [f"- {c['key']}: {c['statement']} -> {c['result']}" for c in view["cards"]]
    if view["gaps"]:
        lines.append("Gaps (what Neptune needs from you):")
        lines += [f"- {g['about']}: {g['text']}" for g in view["gaps"]]
    return "\n".join(lines)


def brief_for(site_dir: Path, id: str) -> str:
    """``brief`` over the memory as it stands (``neptune_site_research_result``)."""
    return brief(Path(site_dir), views.load(Path(site_dir)), id)
