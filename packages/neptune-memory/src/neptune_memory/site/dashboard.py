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
