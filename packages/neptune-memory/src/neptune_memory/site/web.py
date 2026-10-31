"""The live web view of a site model (#210): ``python -m neptune_memory.site web <site_dir>``.

A stdlib ``http.server`` serves one page (``web.html``: vanilla JS and SVG, no build step) and a
small JSON API over the same query layer as the MCP tools (``query.py``, ``insights.py``):

- ``GET /api/snapshot?at=T`` or ``?seq=N``: the map (places, assets, feeds), the live state table,
  the timeline, the findings (inferred), pending proposals; at site time ``T`` or as the log stood
  at ``seq`` N (``as_of_seq``). Neither: now.
- ``GET /api/why?fact=ID``: ``query.why`` as JSON, with its sources.
- ``GET /api/source?file=F&locator=L``: the cited span: lines of a text file, rows of a table,
  a PDF page's blocks (from the compiler's package), or an image region.
- ``POST /api/confirm`` ``{"proposal", "answer"}``: appends a ``confirm`` entry (spec §7).
- ``GET /api/actions``, ``POST /api/actions/approve|reject`` ``{"id"}``: proposed actions and a
  person's answer (``actions.py``, spec §15); an approved robot action is dispatched to the
  fleet manager over MQTT.
- ``GET /api/dashboard``: the dashboard (``dashboard.py``, spec §16): counts, mission, sources,
  coverage, the latest improvement run and what changed since it, the engine's state.
- ``POST /api/engine/key`` ``{"key"}`` (kept in memory, never written), ``/api/engine/run``,
  ``/api/engine/auto`` ``{"on"}``; ``POST /api/improvements/accept|reject`` ``{"id"}``.
- ``GET /events``: server-sent events, one ``{"seq", "clock", "engine"}`` each time the log grows
  or the improvement engine starts or ends a run.

The model is folded once and then tails the log (``log.Tail``), like the MCP server, so it follows
``make site-live``. No auth: a demo on localhost.
"""

from __future__ import annotations

import csv
import json
import re
import struct
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from neptune_memory.site import actions, dashboard, improve, insights, log, query, views
from neptune_memory.site import confirm as confirm_queue
from neptune_memory.site.facts import Citation, Fact, Ref, Value

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune_memory.site.query import Answer
    from neptune_memory.site.views import Reading, SiteModel

PAGE = Path(__file__).with_name("web.html")
ASSETS = ("robot", "machine")
PLACES = ("area", "cell", "zone")
TABLE = (
    "mode",
    "health",
    "battery",
    "in_zone",
    "current_task",
    "parts_out",
    "setpoint",
    "temperature",
    "belt_tension",
    "firmware",
)
"""The live state table's predicates, in column order."""
TIMELINE = ("mode", "health", "in_zone")
TIMELINE_LIMIT = 120
CONTEXT = 4
"""Lines or rows shown either side of a cited one."""
LONG_LINE = 160
"""Past this many characters a cited line keeps only one neighbour on each side."""
RECENT_ACTIONS = 5
"""Answered actions the panel keeps showing, newest first."""


# --- the model ---------------------------------------------------------------------------------


class Site:
    """One site directory: the live model (tailing the log) and what the API answers from it."""

    def __init__(
        self,
        site_dir: Path,
        broker: str = actions.DEFAULT_BROKER,
        send: Callable[[str, bytes], None] | None = None,
        client: Callable[[str | None], Any] | None = None,
    ) -> None:
        self.site_dir = Path(site_dir)
        self.broker = broker
        self.send = send
        self.lock = threading.Lock()
        self.engine = improve.Engine(self.site_dir, client or improve._client)
        self._tail = log.Tail(self.site_dir)
        self._model = views.SiteModel.empty()
        self._prefix: tuple[int, SiteModel] | None = None
        self._package: tuple[float, dict[str, Any]] | None = None

    def model(self) -> SiteModel:
        """The model with every entry appended since the last call (call it under ``lock``)."""
        new = self._tail.new()
        if new is None:  # the log was started again (a fresh build or live): fold from scratch
            self._tail = log.Tail(self.site_dir)
            self._model = views.SiteModel.empty()
            new = self._tail.new() or []
        return views.fold(new, self._model)

    def as_of(self, seq: int) -> SiteModel:
        """The model as the log stood at ``seq`` (folded once per seq asked)."""
        if self._prefix is None or self._prefix[0] != seq:
            self._prefix = (seq, views.load(self.site_dir, as_of_seq=seq))
        return self._prefix[1]

    # -- answers -------------------------------------------------------------------------------

    def snapshot(self, at: str | None, seq: int | None) -> dict[str, Any]:
        with self.lock:
            live = self.model()
            model = self.as_of(seq) if seq is not None and seq < live.seq else live
            return _snapshot(model, at, live)

    def why(self, fact_id: str) -> dict[str, Any]:
        with self.lock:
            return answer_json(query.why(self.model(), fact_id))

    def head(self) -> dict[str, Any]:
        with self.lock:
            model = self.model()
            return {"seq": model.seq, "clock": model.clock(), "engine": self.engine.state}

    # -- the dashboard and improvements (spec §16) -----------------------------------------------

    def dashboard(self) -> dict[str, Any]:
        with self.lock:
            return dashboard.build(self.site_dir, self.model(), self.engine)

    def due(self) -> str | None:
        """Why an automatic improvement run is due now (the engine's watcher asks)."""
        with self.lock:
            return self.engine.due(self.model())

    def engine_call(self, verb: str, body: dict[str, Any]) -> dict[str, Any]:
        if verb == "key":
            self.engine.set_key(str(body.get("key") or ""))
        elif verb == "auto":
            self.engine.auto = bool(body.get("on"))
        elif verb == "run":
            self.engine.error = None
            if not self.engine.start("asked from the dashboard"):
                why = "no API key" if self.engine.key is None else "a run is already going"
                raise ValueError(f"not started: {why}")
        else:
            raise ValueError(f"no engine call {verb!r}")
        return self.engine.status()

    def improvement(self, verb: str, fact_id: str, by: str) -> dict[str, Any]:
        """A person's accept or reject; an accepted action is proposed, approved and dispatched."""
        if verb not in ("accept", "reject"):
            raise ValueError(f"no improvement answer {verb!r}")
        return improve.answer(
            self.site_dir, fact_id, verb == "accept", by, broker=self.broker, send=self.send
        )

    def confirm(self, proposal: str, answer: str, by: str) -> dict[str, Any]:
        if answer not in ("yes", "no"):
            raise ValueError("answer is yes or no")
        entry = confirm_queue.confirm(self.site_dir, proposal, answer, by=by)  # type: ignore[arg-type]
        return {"seq": entry.seq, "proposal": proposal, "answer": answer}

    # -- sources -------------------------------------------------------------------------------

    def source(self, file: str, locator: str) -> dict[str, Any]:
        """The cited span of ``file`` (relative to the site directory) at ``locator``."""
        path = (self.site_dir / file).resolve()
        if not path.is_relative_to(self.site_dir.resolve()) or not path.is_file():
            return {"kind": "none", "message": f"{file} is not in the site directory"}
        if m := re.match(r"region (\d+),(\d+),(\d+),(\d+)", locator):
            return {
                "kind": "image",
                "url": f"/file?path={file}",
                "size": _png_size(path),
                "box": [int(g) for g in m.groups()],
            }
        if m := re.match(r"page (\d+) block (\d+)", locator):
            return self._page(file, int(m[1]), int(m[2]))
        if m := re.match(r"row (\d+) col \"(.*)\"", locator):
            return self._rows(path, file, int(m[1]), m[2])
        if m := re.match(r"json (/\S*)", locator):
            return _json_span(path, m[1])
        if m := re.search(r"line (\d+)", locator):
            return _lines(path, int(m[1]))
        return {"kind": "none", "message": f"no viewer for {locator!r}"}

    def _rows(self, path: Path, file: str, row: int, column: str) -> dict[str, Any]:
        if path.suffix.lower() in (".csv", ".tsv"):
            with path.open(encoding="utf-8", newline="") as handle:
                delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
                rows = list(csv.reader(handle, delimiter=delimiter))
        else:  # XLSX and the like: the rows the compiler read
            rows = self.package().get("rows", {}).get(file.removeprefix("static/"), [])
        if not rows:
            return {"kind": "none", "message": f"no rows read from {file}"}
        header = rows[0]
        lo, hi = max(2, row - CONTEXT), min(len(rows), row + CONTEXT)
        hit_col = next((i for i, h in enumerate(header) if h == column), None)
        return {
            "kind": "table",
            "header": header,
            "hit_row": row,
            "hit_col": hit_col,
            "rows": [{"n": n, "cells": rows[n - 1]} for n in range(lo, hi + 1)],
        }

    def _page(self, file: str, page: int, block: int) -> dict[str, Any]:
        pages = self.package().get("pages", {}).get(file.removeprefix("static/"), {})
        blocks = pages.get(page, [])
        if not blocks:
            return {"kind": "none", "message": f"page {page} of {file} is not in the package"}
        return {
            "kind": "page",
            "page": page,
            "pages": len(pages),
            "hit": block,
            "blocks": [{"n": i, "text": t} for i, t in enumerate(blocks, start=1)],
        }

    def package(self) -> dict[str, Any]:
        """Table rows and PDF page blocks per static path, from the compiler's package."""
        manifest = self.site_dir / ".neptune" / "package" / "manifest.json"
        if not manifest.exists():
            return {}
        stamp = manifest.stat().st_mtime
        if self._package is None or self._package[0] != stamp:
            self._package = (stamp, _read_package(self.site_dir))
        return self._package[1]

    # -- actions (#212) ------------------------------------------------------------------------

    def actions(self) -> dict[str, Any]:
        """Proposals waiting for a person, then the latest answered ones and what became of them."""
        with self.lock:
            every = actions.load_actions(self.model())
        answered = [a for a in every if a.status != "pending"][-RECENT_ACTIONS:]
        return {
            "available": True,
            "items": [_action(a) for a in every if a.status == "pending"],
            "recent": [_action(a) for a in reversed(answered)],
        }

    def act(self, verb: str, action_id: str, by: str) -> dict[str, Any]:
        """A person's approve or reject; an approved robot action goes to the fleet manager."""
        if verb == "approve":
            done = actions.approve(self.site_dir, action_id, by, broker=self.broker, send=self.send)
        elif verb == "reject":
            done = actions.reject(self.site_dir, action_id, by)
        else:
            raise ValueError(f"no action {verb!r}")
        return {"result": _action(done)}


def _action(action: actions.Action) -> dict[str, Any]:
    def text(fact: Fact | None) -> str | None:
        if fact is None:
            return None
        value = fact.object.ref if isinstance(fact.object, Ref) else fact.object.value
        return str(value).removeprefix(f"{action.id}: ")

    return {
        "id": action.id,
        "verb": action.verb,
        "target": action.target,
        "zone": action.zone,
        "reason": action.reason,
        "status": action.status,
        "by": action.by,
        "dispatched": text(action.dispatched),
        "outcome": text(action.outcome),
    }


# --- the snapshot ------------------------------------------------------------------------------


def _snapshot(model: SiteModel, at: str | None, live: SiteModel) -> dict[str, Any]:
    t = at or model.clock()
    names = _names(model)
    readings = {
        (r.subject, r.predicate): r
        for e in sorted(model.entities)
        if e.split(":", 1)[0] in (*ASSETS, "cell")
        for r in model.state_at(t, e)
        if r.predicate in (*TABLE, "located_in")
    }
    places = _places(model, t, names)
    place_ids = {p["id"] for p in places}
    edges = [
        {"from": f.subject, "to": f.object.ref, "predicate": f.predicate, "fact": f.id}
        for f in model.topology(t).edges
        if isinstance(f.object, Ref) and f.predicate in ("feeds", "depends_on")
    ]
    assets = _joined(
        model,
        [
            _asset(model, e, t, names, readings, edges, place_ids)
            for e in sorted(model.entities)
            if e.split(":", 1)[0] in ASSETS
        ],
    )
    shown = {a["id"] for a in assets} | {e for e in model.entities if e.startswith("cell:")}
    findings = insights.insights(model, t)
    pending = query.Answer(tool="pending", at=t, as_of_seq=model.seq)
    for proposal in model.pending():
        pending.add(proposal, "pending")
    times = [ts for times in live.seen.values() for ts in times[:1]]
    site = next((e for e in sorted(model.entities) if e.startswith("site:")), None)
    return {
        "site": {"id": site, "name": names.get(site or "", site)},
        "seq": model.seq,
        "live_seq": live.seq,
        "at": t,
        "clock": live.clock(),
        "first": min(times) if times else None,
        "places": places,
        "assets": assets,
        "edges": edges,
        "table": [
            _reading(r) for (s, p), r in sorted(readings.items()) if p in TABLE and s in shown
        ],
        "timeline": _timeline(model, t),
        "findings": answer_json(findings),
        "pending": answer_json(pending),
    }


def _joined(model: SiteModel, assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One glyph per stated ``same_as`` group: the member with a place or a state carries the
    others' ids as ``aliases`` (a CMMS asset id confirmed to be a fleet robot)."""
    by_id = {a["id"]: a for a in assets}
    out = []
    for asset in assets:
        group = [by_id[e] for e in model.same_as(asset["id"]) if e in by_id]
        lead = max(group, key=lambda a: (a["place"] is not None, len(a["state"]), a["id"]))
        if lead is asset:
            asset["aliases"] = [a["id"] for a in group if a is not asset]
            out.append(asset)
    return out


def _names(model: SiteModel) -> dict[str, str]:
    names: dict[str, str] = {}
    for fid, fact in model.facts.items():
        if fact.predicate == "name" and model.live(fid) and isinstance(fact.object, Value):
            names.setdefault(fact.subject, str(fact.object.value))
    return names


def _places(model: SiteModel, t: str | None, names: dict[str, str]) -> list[dict[str, Any]]:
    parents = {
        r.subject: _object(r.facts[0])
        for e in sorted(model.entities)
        if e.split(":", 1)[0] in PLACES
        for r in model.state_at(t, e)
        if r.predicate == "part_of" and r.facts
    }
    return [
        {"id": e, "type": e.split(":", 1)[0], "name": names.get(e, e), "parent": parents.get(e)}
        for e in sorted(model.entities)
        if e.split(":", 1)[0] in PLACES
    ]


def _asset(
    model: SiteModel,
    entity: str,
    t: str | None,
    names: dict[str, str],
    readings: dict[tuple[str, str], Reading],
    edges: list[dict[str, Any]],
    places: set[str],
) -> dict[str, Any]:
    state = {p: _reading(r) for (s, p), r in readings.items() if s == entity}
    place, how = None, None
    for predicate in ("in_zone", "located_in"):
        value = state.get(predicate, {}).get("value")
        if value in places:
            place, how = value, predicate
            break
    if place is None:  # not placed: beside what it feeds
        place = next((e["to"] for e in edges if e["from"] == entity and e["to"] in places), None)
        how = "feeds" if place else None
    task = state.get("current_task", {}).get("value")
    task_state = None
    if task:
        cells = [r for r in model.state_at(t, task) if r.predicate == "task_state" and r.facts]
        task_state = _object(cells[0].facts[0]) if cells else None
    return {
        "id": entity,
        "type": entity.split(":", 1)[0],
        "name": names.get(entity, entity),
        "place": place,
        "placed_by": how,
        "state": state,
        "task_state": task_state,
        "status": _status(state, task_state),
    }


FAULT = re.compile(r"fault|error|estop|e-stop|alarm", re.IGNORECASE)
WARN = re.compile(r"starv|wait|block|stop|protect|pause|hold|jam", re.IGNORECASE)
IDLE = re.compile(r"idle|dock|charg|park", re.IGNORECASE)
IDLE_TASK = re.compile(r"none|done|finish|complete|cancel|fail", re.IGNORECASE)


def _status(state: dict[str, dict[str, Any]], task_state: str | None) -> str:
    """ok, warn, fault, idle, stale or unknown: what the map colours an asset by."""
    mode = state.get("mode", {})
    health = state.get("health", {})
    if "stale" in (mode.get("state"), health.get("state")):
        return "stale"
    if str(health.get("value") or "").startswith("fault") or FAULT.search(str(mode.get("value"))):
        return "fault"
    if mode.get("value") is None:
        return "unknown"
    if WARN.search(str(mode["value"])) or WARN.search(task_state or ""):
        return "warn"
    if IDLE.search(str(mode["value"])):
        busy = task_state is not None and not IDLE_TASK.search(task_state)
        return "warn" if busy else "idle"  # idle on an open task: waiting for something
    return "ok"


def _object(fact: Fact) -> str:
    return fact.object.ref if isinstance(fact.object, Ref) else str(fact.object.value)


def _reading(reading: Reading) -> dict[str, Any]:
    fact = reading.facts[0] if reading.facts else None
    unit = fact.object.unit if fact is not None and isinstance(fact.object, Value) else None
    return {
        "subject": reading.subject,
        "predicate": reading.predicate,
        "value": _object(fact) if fact is not None else None,
        "unit": unit,
        "state": reading.state,
        "kind": fact.kind if fact is not None else None,
        "since": reading.since,
        "last_seen": reading.last_seen,
        "fact": fact.id if fact is not None else None,
        "facts": [f.id for f in reading.facts],
    }


def _timeline(model: SiteModel, t: str | None) -> list[dict[str, Any]]:
    """Mode, health and zone changes of every asset and cell up to ``t``, newest first."""
    out = [
        {
            "t": i.start,
            "end": i.end if i.end is None or t is None or i.end <= t else None,
            "subject": i.fact.subject,
            "predicate": i.fact.predicate,
            "value": _object(i.fact),
            "kind": i.fact.kind,
            "fact": i.fact.id,
            "fault": i.fact.predicate == "health" and _object(i.fact).startswith("fault"),
        }
        for e in sorted(model.entities)
        if e.split(":", 1)[0] in (*ASSETS, "cell")
        for i in model.history(e, end=t)
        if i.fact.predicate in TIMELINE and i.start is not None
    ]
    out.sort(key=lambda row: (row["t"], row["subject"]), reverse=True)
    return out[:TIMELINE_LIMIT]


def answer_json(answer: Answer) -> dict[str, Any]:
    """An ``Answer`` as plain JSON: items, gaps and numbered sources."""
    return {
        "tool": answer.tool,
        "at": answer.at,
        "as_of_seq": answer.as_of_seq,
        "items": [
            {
                "fact": i.fact_id,
                "subject": i.subject,
                "predicate": i.predicate,
                "value": None if i.object is None else _text(i.object),
                "unit": i.object.unit if isinstance(i.object, Value) else None,
                "valid_from": i.valid_from,
                "valid_to": i.valid_to,
                "kind": i.kind,
                "state": i.state,
                "cite": list(i.cite),
                "relation": i.relation,
            }
            for i in answer.items
        ],
        "gaps": answer.gaps,
        "sources": {k: _citation(c) for k, c in answer.sources.items()},
    }


def _text(obj: Ref | Value) -> str:
    return obj.ref if isinstance(obj, Ref) else str(obj.value)


def _citation(citation: Citation) -> dict[str, Any]:
    return {"file": citation.file, "locator": citation.locator, "quote": citation.quote}


# --- source spans ------------------------------------------------------------------------------


def _lines(path: Path, line: int) -> dict[str, Any]:
    lo = max(1, line - CONTEXT)
    with path.open(encoding="utf-8", errors="replace") as handle:
        rows = list(islice(handle, lo - 1, line + CONTEXT))
    if any(len(r) > LONG_LINE for r in rows):  # feed lines: the cited one and its neighbours
        start = max(0, line - lo - 1)
        rows, lo = rows[start : line - lo + 2], lo + start
    return {
        "kind": "lines",
        "hit": line,
        "lines": [{"n": lo + i, "text": r.rstrip("\n")} for i, r in enumerate(rows)],
    }


def _json_span(path: Path, pointer: str) -> dict[str, Any]:
    """The object holding the pointed-at value, pretty-printed; ``key`` names the value."""
    node: Any = json.loads(path.read_text(encoding="utf-8"))
    steps = [s.replace("~1", "/").replace("~0", "~") for s in pointer.strip("/").split("/") if s]
    parent: Any = node
    for step in steps[:-1]:
        parent = parent[int(step)] if isinstance(parent, list) else parent[step]
    return {
        "kind": "json",
        "pointer": pointer,
        "key": steps[-1] if steps else None,
        "text": json.dumps(parent, indent=2, ensure_ascii=False),
    }


def _png_size(path: Path) -> list[int] | None:
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", head[16:24])
    return [width, height]


def _read_package(site_dir: Path) -> dict[str, Any]:
    from neptune.model.knowledge import Known
    from neptune.model.provenance import Page
    from neptune.model.source import LocalPath, SourceRevision
    from neptune.model.world import DocumentBlock, StructuredRecord
    from neptune_memory.site.build import static_package

    package = static_package(site_dir)
    paths: dict[str, str] = {}
    tables: dict[str, list[StructuredRecord]] = {}
    blocks: dict[str, list[tuple[int, int, str]]] = {}
    for record in package.records:
        if isinstance(record, SourceRevision) and isinstance(record.location, LocalPath):
            paths[str(record.content_id)] = record.location.path
        elif isinstance(record, StructuredRecord):
            tables.setdefault(str(record.provenance.evidence.source), []).append(record)
        elif isinstance(record, DocumentBlock) and isinstance(record.text, Known):
            page = next(
                (s.index for s in record.provenance.evidence.locator if isinstance(s, Page)), 0
            )
            key = str(record.provenance.evidence.source)
            blocks.setdefault(key, []).append((page + 1, record.order, str(record.text.value)))
    rows: dict[str, list[list[str]]] = {}
    for source, records in tables.items():
        first = records[0].table  # one sheet: the first the compiler read
        sheet = sorted((r for r in records if r.table == first), key=lambda r: r.row)
        rows[paths.get(source, source)] = [
            [str(c.value) if isinstance(c, Known) else "" for c in r.cells] for r in sheet
        ]
    pages: dict[str, dict[int, list[str]]] = {}
    for source, found in blocks.items():
        doc = pages.setdefault(paths.get(source, source), {})
        for page, _, text in sorted(found):
            doc.setdefault(page, []).append(text)
    return {"rows": rows, "pages": pages}


# --- HTTP --------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """The page, the JSON API and the event stream over one ``Site``."""

    site: Site
    poll: float = 0.5

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet: the live demo prints its own lines

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        args = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/events":
                self._events()
            elif url.path == "/file":
                self._file(args.get("path", ""))
            elif url.path == "/api/snapshot":
                seq = int(args["seq"]) if args.get("seq") else None
                self._json(self.site.snapshot(args.get("at") or None, seq))
            elif url.path == "/api/why":
                self._json(self.site.why(args.get("fact", "")))
            elif url.path == "/api/source":
                self._json(self.site.source(args.get("file", ""), args.get("locator", "")))
            elif url.path == "/api/actions":
                self._json(self.site.actions())
            elif url.path == "/api/dashboard":
                self._json(self.site.dashboard())
            elif url.path == "/api/engine":
                self._json(self.site.engine.status())
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        by = str(body.get("by") or "web-operator")
        try:
            if url.path == "/api/confirm":
                self._json(self.site.confirm(body["proposal"], body["answer"], by))
            elif m := re.fullmatch(r"/api/actions/(approve|reject)", url.path):
                self._json(self.site.act(m[1], str(body["id"]), by))
            elif m := re.fullmatch(r"/api/engine/(key|run|auto)", url.path):
                self._json(self.site.engine_call(m[1], body))
            elif m := re.fullmatch(r"/api/improvements/(accept|reject)", url.path):
                self._json(self.site.improvement(m[1], str(body["id"]), by))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (KeyError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except OSError as error:  # the broker is down: the approval is logged, not dispatched
            self._json({"error": f"not dispatched: {error}"}, HTTPStatus.BAD_GATEWAY)

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json", status)

    def _send(self, body: bytes, kind: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, file: str) -> None:
        path = (self.site.site_dir / file).resolve()
        if not path.is_relative_to(self.site.site_dir.resolve()) or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        kind = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
        self._send(path.read_bytes(), kind)

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last: dict[str, Any] | None = None
        while True:
            head = self.site.head()
            if head != last:
                self.wfile.write(f"data: {json.dumps(head)}\n\n".encode())
                self.wfile.flush()
                last = head
            time.sleep(self.poll)


def serve(
    site_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    broker: str = actions.DEFAULT_BROKER,
    send: Callable[[str, bytes], None] | None = None,
    client: Callable[[str | None], Any] | None = None,
    watch: float = 3.0,
) -> ThreadingHTTPServer:
    """A server for ``site_dir`` (not yet serving: call ``serve_forever``). Approved actions go to
    the fleet manager over MQTT on ``broker`` (or through ``send``). ``client(key)`` makes the
    Anthropic client for improvement runs; every ``watch`` seconds the engine checks whether the
    site changed enough to run again (0: never on its own)."""
    site = Site(Path(site_dir), broker, send, client)
    if watch > 0:
        site.engine.watch(site.due, watch)
    handler = type("SiteHandler", (Handler,), {"site": site})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
