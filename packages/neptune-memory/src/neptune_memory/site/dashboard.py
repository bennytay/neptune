"""The dashboard (spec §16): what Neptune knows about a site, and what to do about it, as JSON.

``build`` reads one folded model and answers the web view's ``GET /api/dashboard``:

- **counts**: sources (and how many are stale), facts by kind, entities, findings, pending
  confirmations, open improvements;
- **mission**: the stated goals (metric, target) and constraints, each with its fact;
- **sources**: every evidence file or feed, what kind of evidence it gives, its facts, and for a
  feed how fresh its devices are;
- **coverage**: per robot, machine, cell and person, facts by evidence class (live feed, records,
  documents, inferred), stale readings and expected values no source states;
- **improvements**: the latest run (``improve.runs``), each item with its evidence spelled out,
  what it retired, and the material changes since it was asked;
- **engine**: whether a key is set, the engine's state, and why a run is due.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Any

from neptune_memory.site import improve, insights
from neptune_memory.site.facts import Fact, Ref
from neptune_memory.site.views import STALE_AFTER
from neptune_memory.site.vocab import entity_type

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_memory.site.views import SiteModel

COVERED = ("robot", "machine", "cell", "person")
CLASSES = ("feed", "records", "documents", "inferred")
TABLES = (".csv", ".tsv", ".xlsx", ".json", ".jsonl", ".yaml")
CHANGES = 30


def _value(fact: Fact) -> str:
    return fact.object.ref if isinstance(fact.object, Ref) else str(fact.object.value)


def _seconds(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def evidence_class(fact: Fact) -> str:
    """feed, records, documents or inferred: where a fact's evidence comes from."""
    if fact.kind == "inferred":
        return "inferred"
    if fact.kind == "observed":
        return "feed"
    file = fact.citations[0].file if fact.citations else ""
    return "records" if file.endswith(TABLES) else "documents"


def build(site_dir: Path, model: SiteModel, engine: improve.Engine) -> dict[str, Any]:
    at = model.clock()
    names = {
        f.subject: _value(f)
        for fid, f in model.facts.items()
        if f.predicate == "name" and model.live(fid)
    }
    live = [f for fid, f in model.facts.items() if model.live(fid)]
    found = insights.findings(model, at)
    every = improve.runs(site_dir, model)
    last = every[-1] if every else None
    before = every[-2] if len(every) > 1 else None
    sources = _sources(model, live, at)
    run = _run(model, last, before, names) if last is not None else None
    changes = improve.changes(model, last)
    entities = Counter(entity_type(e) for e in model.entities)
    return {
        "site": next(
            (
                {"id": e, "name": names.get(e, e)}
                for e in sorted(model.entities)
                if e.startswith("site:")
            ),
            {"id": None, "name": None},
        ),
        "at": at,
        "seq": model.seq,
        "counts": {
            "sources": len(sources),
            "stale_sources": sum(1 for s in sources if s["state"] == "stale"),
            "facts": len(live),
            "kinds": dict(Counter(f.kind for f in live)),
            "entities": sum(entities[t] for t in ("robot", "machine", "cell", "person", "zone")),
            "entity_types": {
                t: entities[t] for t in ("robot", "machine", "cell", "zone", "person")
            },
            "findings": len(found),
            "pending": len(model.pending()),
            "improvements": sum(1 for i in (run or {}).get("items", []) if i["status"] == "open"),
        },
        "mission": _mission(live),
        "sources": sources,
        "coverage": _coverage(model, live, found, at, names),
        "changes": [
            {"seq": c.seq, "t": c.t, "kind": c.kind, "text": c.text, "fact": c.fact}
            for c in reversed(changes[-CHANGES:])
        ],
        "changed": len(changes),
        "run": run,
        "runs": len(every),
        "engine": engine.status() | {"due": engine.due(model) if engine.key else None},
    }


def _mission(live: list[Fact]) -> dict[str, list[dict[str, Any]]]:
    def line(fact: Fact) -> int:  # file order: "line 10" after "line 9"
        locator = fact.citations[0].locator if fact.citations else ""
        return int(locator.removeprefix("line ")) if locator.startswith("line ") else 0

    rows: dict[str, dict[str, Any]] = {}
    for fact in sorted(live, key=line):
        kind = entity_type(fact.subject)
        if kind not in ("goal", "constraint"):
            continue
        row = rows.setdefault(fact.subject, {"id": fact.subject, "type": kind, "facts": []})
        row[fact.predicate] = _value(fact)
        row["facts"].append(fact.id)
        cite = fact.citations[0]
        row["cite"] = {"file": cite.file, "locator": cite.locator, "quote": cite.quote}
    return {
        "goals": [r for r in rows.values() if r["type"] == "goal"],
        "constraints": [r for r in rows.values() if r["type"] == "constraint"],
    }


def _sources(model: SiteModel, live: list[Fact], at: str | None) -> list[dict[str, Any]]:
    """One row per evidence file; a feed's files are one row per source, with its freshness."""
    rows: dict[str, dict[str, Any]] = {}
    for fact in live:
        if fact.source in (
            improve.SOURCE,
            "neptune.actions",
            insights.SOURCE,
            "confirm",
            "identity",
        ):
            continue
        if not fact.citations:
            continue
        file = fact.citations[0].file
        feed = fact.kind == "observed" and file.startswith("feed/")
        key = f"feed:{fact.source}" if feed else file
        row = rows.setdefault(
            key,
            {
                "id": key,
                "name": fact.source if feed else file.removeprefix("static/"),
                "kind": "feed" if feed else evidence_class(fact),
                "file": file,
                "facts": 0,
                "kinds": Counter(),
                "latest": None,
                "state": "known",
                "note": "",
            },
        )
        row["facts"] += 1
        row["kinds"][fact.kind] += 1
        if fact.valid_from and (row["latest"] is None or fact.valid_from > row["latest"]):
            row["latest"] = fact.valid_from
    for row in rows.values():
        row["kinds"] = dict(row["kinds"])
        if row["kind"] == "feed":
            _freshness(model, row, at)
    return sorted(rows.values(), key=lambda r: (r["kind"] != "feed", r["kind"], r["name"]))


def _freshness(model: SiteModel, row: dict[str, Any], at: str | None) -> None:
    source = row["name"]
    interval = model.report_interval.get(source)
    devices = {k: v[-1] for k, v in model.seen.items() if v and k.split("/", 1)[0] == source}
    if not devices or at is None:
        return
    row["last_seen"] = max(devices.values())
    if interval is None:
        row["note"] = "no report interval stated"
        return
    stale = sorted(k for k, t in devices.items() if _seconds(t, at) > STALE_AFTER * interval)
    if stale:
        row["state"] = "stale"
        names = ", ".join(k.split("/", 1)[-1] for k in stale)
        row["note"] = (
            "silent"
            if len(stale) == len(devices)
            else f"{len(stale)} of {len(devices)} silent: {names}"
        )


def _coverage(
    model: SiteModel, live: list[Fact], found: list[Fact], at: str | None, names: dict[str, str]
) -> list[dict[str, Any]]:
    """Per entity: facts about it (as subject or object) by evidence class, its stale readings and
    the expected values nothing states."""
    counts: dict[str, Counter[str]] = {}
    for fact in [*live, *found]:
        if fact.source in (improve.SOURCE,):
            continue
        for entity in {fact.subject, fact.object.ref if isinstance(fact.object, Ref) else None}:
            if entity and entity_type(entity) in COVERED:
                counts.setdefault(entity, Counter())[evidence_class(fact)] += 1
    rows = []
    for entity in sorted(counts, key=lambda e: (COVERED.index(entity_type(e)), e)):
        readings = model.state_at(at, entity)
        rows.append(
            {
                "id": entity,
                "type": entity_type(entity),
                "name": names.get(entity, entity),
                "counts": {c: counts[entity][c] for c in CLASSES},
                "stale": sorted({r.predicate for r in readings if r.state == "stale"}),
                "unknown": sorted({r.predicate for r in readings if r.state == "unknown"}),
                "same_as": [e for e in model.same_as(entity) if e != entity],
            }
        )
    return rows


def _run(
    model: SiteModel, run: improve.Run, before: improve.Run | None, names: dict[str, str]
) -> dict[str, Any]:
    at = run.data.get("at")
    then = {f.id: f for f in insights.findings(model, at)} if at else {}
    items = []
    for item in run.items:
        data = item.data
        items.append(
            {
                "fact": item.fact.id,
                "key": item.key,
                "rank": item.rank,
                "move": item.move,
                "status": item.status,
                "by": item.by,
                "title": item.title,
                "opportunity": data.get("opportunity", ""),
                "change": data.get("change", ""),
                "goals": [{"id": g, "name": names.get(g, g)} for g in data.get("goals", [])],
                "constraints": [
                    c | {"name": names.get(c.get("constraint", ""), c.get("constraint"))}
                    for c in data.get("constraints", [])
                ],
                "evidence": [_evidence(model, then, fid) for fid in data.get("evidence", [])],
                "size": data.get("size", ""),
                "unknowns": data.get("unknowns", []),
                "confidence": data.get("confidence", ""),
                "action": data.get("action") or {"verb": "none"},
                "since_last": data.get("since_last", ""),
            }
        )
    return {
        "line": run.line,
        "at": at,
        "as_of_seq": run.data.get("as_of_seq"),
        "model": run.data.get("model"),
        "trigger": run.data.get("trigger"),
        "summary": run.data.get("summary", ""),
        "dropped": run.data.get("dropped", []),
        "items": items,
        "retired": improve.retired(run, before),
    }


def _evidence(model: SiteModel, then: dict[str, Fact], fid: str) -> dict[str, Any]:
    fact = model.facts.get(fid) or then.get(fid)
    if fact is None:
        return {"fact": fid, "text": fid, "kind": None, "cites": []}
    return {
        "fact": fid,
        "subject": fact.subject,
        "predicate": fact.predicate,
        "value": _value(fact),
        "kind": fact.kind,
        "valid_from": fact.valid_from,
        "logged": fid in model.facts,
        "cites": [{"file": c.file, "locator": c.locator, "quote": c.quote} for c in fact.citations],
    }


# --- The graph and one thing (the minimal page's centre) --------------------------------------

GRAPH_TYPES = (
    "site",
    "area",
    "cell",
    "zone",
    "robot",
    "machine",
    "person",
    "role",
    "qualification",
    "shift",
    "task",
    "document",
    "rule",
    "work_order",
    "change",
    "goal",
    "constraint",
    "improvement",
)
LANES = ("mode", "health", "in_zone", "current_task", "setpoint", "on_shift", "on_break")
FAULT = re.compile(r"fault|error|estop|e-stop|alarm|protective", re.IGNORECASE)
WARN = re.compile(r"starv|wait|block|stop|pause|hold|jam", re.IGNORECASE)
IDLE = re.compile(r"idle|dock|charg|park", re.IGNORECASE)


def status(model: SiteModel, entity: str, at: str | None) -> str:
    """ok, warn, fault, idle, stale, unknown, or "" for a thing with no live state."""
    readings = {r.predicate: r for r in model.state_at(at, entity)}
    mode, health = readings.get("mode"), readings.get("health")
    if mode is None and health is None:
        return ""
    if any(r is not None and r.state == "stale" for r in (mode, health)):
        return "stale"
    m = _value(mode.facts[0]) if mode is not None and mode.facts else ""
    h = _value(health.facts[0]) if health is not None and health.facts else ""
    if h.startswith("fault") or FAULT.search(m):
        return "fault"
    if not m:
        return "unknown"
    if WARN.search(m):
        return "warn"
    return "idle" if IDLE.search(m) else "ok"


def graph(site_dir: Path, model: SiteModel) -> dict[str, Any]:
    """Every thing and every live edge between two things; improvements link to what their
    evidence is about and to the goals they serve."""
    at = model.clock()
    names = _names(model)
    edges: dict[tuple[str, str, str], str] = {}
    for fid, fact in model.facts.items():
        if not model.live(fid) or not isinstance(fact.object, Ref):
            continue
        a, b = fact.subject, fact.object.ref
        if entity_type(a) in GRAPH_TYPES and entity_type(b) in GRAPH_TYPES and a != b:
            edges.setdefault((a, b, fact.predicate), fid)
    every = improve.runs(site_dir, model)
    if every:
        then = {f.id: f for f in insights.findings(model, every[-1].data.get("at"))}
        for item in every[-1].items:
            for fid in item.data.get("evidence", []):
                cited = model.facts.get(fid) or then.get(fid)
                if cited is not None and entity_type(cited.subject) in GRAPH_TYPES:
                    edges.setdefault((item.fact.subject, cited.subject, "evidence"), fid)
            for goal in item.data.get("goals", []):
                edges.setdefault((item.fact.subject, goal, "serves"), item.fact.id)
    nodes = {e for e in model.entities if entity_type(e) in GRAPH_TYPES}
    nodes |= {e for edge in edges for e in edge[:2]}
    if every:
        nodes |= {i.fact.subject for i in every[-1].items}
        names |= {i.fact.subject: i.title for i in every[-1].items}
    degree = Counter(e for edge in edges for e in edge[:2])
    return {
        "at": at,
        "nodes": [
            {
                "id": e,
                "type": entity_type(e),
                "name": names.get(e, e.split(":", 1)[-1]),
                "status": status(model, e, at) if entity_type(e) in COVERED else "",
                "degree": degree[e],
            }
            for e in sorted(nodes)
        ],
        "edges": [
            {"from": a, "to": b, "predicate": p, "fact": fid} for (a, b, p), fid in edges.items()
        ],
    }


def thing(site_dir: Path, model: SiteModel, entity: str) -> dict[str, Any]:
    """One thing: what is known now (cited), its state lanes over the site's span, what it links
    to, the findings about it and the improvements that cite it."""
    at = model.clock()
    names = _names(model)
    now = []
    for reading in model.state_at(at, entity):
        fact = reading.facts[0] if reading.facts else None
        now.append(
            {
                "predicate": reading.predicate,
                "value": _value(fact) if fact else None,
                "unit": getattr(fact.object, "unit", None) if fact else None,
                "state": reading.state,
                "kind": fact.kind if fact else None,
                "since": reading.since,
                "fact": fact.id if fact else None,
                "cite": _cite(fact) if fact else None,
            }
        )
    times = [t for ts in model.seen.values() for t in ts[:1]]
    start = min(times) if times else None
    lanes: dict[str, list[dict[str, Any]]] = {}
    for span in model.history(entity, end=at):
        if span.fact.predicate in LANES and span.start is not None:
            lanes.setdefault(span.fact.predicate, []).append(
                {
                    "start": span.start,
                    "end": span.end,
                    "value": _value(span.fact),
                    "kind": span.fact.kind,
                    "fact": span.fact.id,
                }
            )
    links = []
    for fid, fact in model.facts.items():
        if not model.live(fid) or not isinstance(fact.object, Ref):
            continue
        if fact.subject == entity or fact.object.ref == entity:
            other = fact.object.ref if fact.subject == entity else fact.subject
            direction = "out" if fact.subject == entity else "in"
            links.append(
                {"predicate": fact.predicate, "other": other, "dir": direction, "fact": fid}
            )
    found = [
        {"text": _value(f), "valid_from": f.valid_from, "cites": [_cite_of(c) for c in f.citations]}
        for f in insights.findings(model, at)
        if f.subject == entity
    ]
    cited_by = []
    every = improve.runs(site_dir, model)
    if every:
        for item in every[-1].items:
            about = {
                model.facts[i].subject for i in item.data.get("evidence", []) if i in model.facts
            }
            if entity in about or item.fact.subject == entity:
                cited_by.append({"fact": item.fact.id, "title": item.title, "rank": item.rank})
    return {
        "id": entity,
        "type": entity_type(entity),
        "name": names.get(entity, entity),
        "status": status(model, entity, at),
        "at": at,
        "start": start,
        "now": now,
        "lanes": [{"predicate": p, "spans": lanes[p]} for p in LANES if p in lanes],
        "links": sorted(links, key=lambda x: (x["predicate"], x["other"])),
        "findings": found,
        "improvements": cited_by,
        "same_as": [e for e in model.same_as(entity) if e != entity],
    }


def _names(model: SiteModel) -> dict[str, str]:
    return {
        f.subject: _value(f)
        for fid, f in model.facts.items()
        if f.predicate == "name" and model.live(fid)
    }


def _cite(fact: Fact) -> dict[str, Any] | None:
    return _cite_of(fact.citations[0]) if fact.citations else None


def _cite_of(citation: Any) -> dict[str, Any]:
    return {"file": citation.file, "locator": citation.locator, "quote": citation.quote}
