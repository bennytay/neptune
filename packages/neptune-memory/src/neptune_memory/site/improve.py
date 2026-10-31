"""Improvements: Claude reads the site model against its mission and proposes changes.

- **The mission** is ``static/mission.yaml``: goals (a ``metric`` and a ``target``) and constraints,
  read like the layout into **stated** facts, so a goal cites its line like any record.
- **A run** (``run``) writes the model out as a dossier, one line per fact with its id, kind and
  citation (``dossier``), and asks Claude (``ask``, structured output) for 3 to 6 ranked
  improvements. Each names its evidence (fact ids from the dossier), the goals it serves, a
  respects / breaks / unclear check per constraint, a size and its basis, the unknowns, a
  confidence, and optionally one action Neptune can send now (``actions.VERBS``). Ids Claude names
  that the model does not hold are dropped and noted; an improvement left with no evidence is
  dropped.
- **Recorded, then logged**: the answer as cleaned is one line of ``improvements/runs.jsonl``
  (model, prompt and dossier hashes, the log seq it read). Each improvement is an **inferred**
  ``improvement:<key> proposed_improvement "<title>"`` fact, source ``neptune.improve``, with the
  model named, citing that line and then its evidence's citations. A rebuild reads the file and
  never calls a model, so the same log folds to the same model.
- **Re-evaluation**: ``changes`` lists what is material since a run (a new record, document or
  mission fact, a fault, a new finding). ``Engine`` re-runs when there are such changes, at most
  every ``COOLDOWN`` wall seconds, with the key a person gave it; keys stay in memory.
- **Answer**: a person accepts or rejects an improvement (a ``confirm`` entry). Accepting one that
  carries an action proposes it (citing the improvement) and approves it in the same step, so it
  is dispatched like any approved action.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neptune_memory.site import actions, insights, views
from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.log import FactLog
from neptune_memory.site.vocab import entity_type

if TYPE_CHECKING:
    from collections.abc import Callable

    from neptune_memory.site.views import SiteModel

SOURCE = "neptune.improve"
PREDICATE = "proposed_improvement"
RUNS = "improvements/runs.jsonl"
MODEL = "claude-opus-5-5"
EFFORT = "high"
MAX_TOKENS = 64000
COOLDOWN = 45.0
"""Wall seconds between two automatic runs."""
STATE = ("robot", "machine", "cell")
TIMELINE = ("mode", "health", "in_zone", "setpoint", "current_task")
VERBATIM = ("text", "unmapped")
"""Unmapped rows and blocks: searchable, never facts, so never evidence."""
OWN = (PREDICATE, "proposed_action", "intent", "dispatched", "outcome")
"""What Neptune itself logs about proposals: never a change in the site."""

SYSTEM = """\
You are Neptune's improvement engine for one robot site. The user message is the site model: \
cited facts, one per line as `id | subject predicate object | kind | citation`. Kind is observed \
(a feed reported it), stated (a record, document or the site's mission says it) or inferred (a \
rule or a model concluded it). Propose the changes that would most improve the site against its \
mission.

Rules:
- Ground every improvement in the model: list the fact ids it rests on in `evidence`, only ids \
that appear in the model. An improvement without evidence is not allowed.
- Score it against the mission's goals (goal:... ids in `goals`) and check it against every \
constraint (constraint:... ids): respects, breaks or unclear, with one line why. Prefer \
improvements that break no constraint; when one does, say so plainly.
- Never guess. Where the model is silent, stale or ambiguous, say so in `unknowns` and name the \
data that would settle it. A size says what it is computed from, using numbers in the model; \
otherwise say the size is unknown.
- Findings and other inferred facts are correlations and hypotheses, not causes.
- Never propose bypassing or changing a safety function.
- Keep an earlier run's key for the same idea and say in `since_last` what changed in the site \
that moved it (empty for a new one). List earlier keys you drop in `retired`, with the reason. \
Do not propose again what a person rejected unless the site changed in a way that answers it; \
say which.
- `action` is something Neptune can send right now (pause, resume, send_to a zone, cancel_task \
for a robot; page for a person), only when the improvement needs it now; otherwise verb "none" \
with an empty target and zone.
- 3 to 6 improvements, most valuable first, in plain, specific language a site manager can act \
on.
"""

_TEXT = {"type": "string"}
_IDS = {"type": "array", "items": _TEXT}
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "improvements", "retired"],
    "properties": {
        "summary": {"type": "string", "description": "the site against its mission, 2-4 lines"},
        "improvements": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "key",
                    "title",
                    "opportunity",
                    "change",
                    "goals",
                    "evidence",
                    "constraints",
                    "size",
                    "unknowns",
                    "confidence",
                    "action",
                    "since_last",
                ],
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "short kebab-case id, kept across runs",
                    },
                    "title": _TEXT,
                    "opportunity": {"type": "string", "description": "what is wrong or possible"},
                    "change": {"type": "string", "description": "what to restructure, concretely"},
                    "goals": _IDS,
                    "evidence": _IDS,
                    "constraints": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["constraint", "status", "note"],
                            "properties": {
                                "constraint": _TEXT,
                                "status": {
                                    "type": "string",
                                    "enum": ["respects", "breaks", "unclear"],
                                },
                                "note": _TEXT,
                            },
                        },
                    },
                    "size": {
                        "type": "string",
                        "description": "estimate and what it is computed from",
                    },
                    "unknowns": _IDS,
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "action": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["verb", "target", "zone"],
                        "properties": {
                            "verb": {"type": "string", "enum": ["none", *actions.VERBS]},
                            "target": _TEXT,
                            "zone": _TEXT,
                        },
                    },
                    "since_last": _TEXT,
                },
            },
        },
        "retired": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "reason"],
                "properties": {"key": _TEXT, "reason": _TEXT},
            },
        },
    },
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _value(fact: Fact) -> str:
    return fact.object.ref if isinstance(fact.object, Ref) else str(fact.object.value)


# --- The dossier ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Dossier:
    """The model as Claude reads it, and every fact id the text names (log facts and the
    findings computed at ``at``)."""

    text: str
    facts: dict[str, Fact]
    at: str | None
    seq: int
    findings: tuple[str, ...]


def _line(fact: Fact, note: str = "") -> str:
    unit = f" {fact.object.unit}" if isinstance(fact.object, Value) and fact.object.unit else ""
    when = ""
    if fact.valid_from:
        when = f", from {fact.valid_from}" + (f" to {fact.valid_to}" if fact.valid_to else "")
    cite = fact.citations[0] if fact.citations else None
    where = f"{cite.file} {cite.locator}" if cite else "uncited"
    if cite and cite.quote and len(cite.quote) <= 160:
        where += f' "{cite.quote}"'
    kind = fact.kind + (f", {note}" if note else "") + when
    return f"{fact.id} | {fact.subject} {fact.predicate} {_value(fact)}{unit} | {kind} | {where}"


def dossier(model: SiteModel, site_dir: Path | None = None) -> Dossier:
    """The site model as one text: mission, findings, state now, what changed, the records and
    documents, and the earlier runs (with what a person answered)."""
    at = model.clock()
    named: dict[str, Fact] = {}
    out: list[str] = []

    def add(fact: Fact, note: str = "") -> None:
        named[fact.id] = fact
        out.append(_line(fact, note))

    names = {
        f.subject: _value(f)
        for fid, f in model.facts.items()
        if f.predicate == "name" and model.live(fid)
    }
    site = next((e for e in sorted(model.entities) if e.startswith("site:")), "site")
    out += [f"# {names.get(site, site)}: site model at {at} (log seq {model.seq})", ""]

    live = [f for fid, f in model.facts.items() if model.live(fid)]
    out.append("## Mission (stated in static/mission.yaml)")
    mission = [f for f in live if entity_type(f.subject) in ("goal", "constraint")]
    for fact in sorted(mission, key=lambda f: (f.subject, f.predicate)):
        add(fact)
    if not mission:
        out.append("(no mission stated: goals and constraints are unknown)")

    out += ["", "## Findings (inferred by deterministic rules: correlations, not causes)"]
    found = insights.findings(model, at)
    for fact in found:
        add(fact)
    if not found:
        out.append("(none)")

    out += ["", "## State now (unknown: no source states it; stale: its source went quiet)"]
    for reading in model.state_at(at):
        if entity_type(reading.subject) not in STATE:
            continue
        if not reading.facts:
            out.append(f"- | {reading.subject} {reading.predicate} | {reading.state}")
            continue
        note: str = "" if reading.state == "known" else reading.state
        if reading.state == "stale":
            note += f", last report {reading.last_seen}"
        for fact in reading.facts:
            add(fact, note)

    out += ["", "## What changed (mode, health, zone, setpoint, task), oldest first"]
    spans = [
        i
        for e in sorted(model.entities)
        if entity_type(e) in STATE
        for i in model.history(e, end=at)
        if i.fact.predicate in TIMELINE and i.start is not None
    ]
    for span in sorted(spans, key=lambda i: (i.start or "", i.fact.subject)):
        add(span.fact)

    out += ["", "## Records, documents, layout and people (stated)"]
    stated = [
        f
        for f in live
        if f.kind == "stated"
        and f.predicate not in VERBATIM
        and f.predicate not in OWN
        and entity_type(f.subject) not in ("goal", "constraint")
    ]
    for fact in sorted(stated, key=lambda f: (f.subject, f.predicate, f.valid_from or "")):
        if fact.id not in named:  # already listed under state now
            add(fact)

    if site_dir is not None:
        earlier = runs(Path(site_dir), model)
        out += ["", "## Earlier improvement runs (yours; keep a key for the same idea)"]
        for run in earlier[-2:]:
            out.append(f"run at {run.data.get('at')} (log seq {run.data.get('as_of_seq')}):")
            out += [
                f"  {item.rank}. {item.key}: {item.title} [{item.status}"
                + (f" by {item.by}" if item.by else "")
                + "]"
                for item in run.items
            ]
        if not earlier:
            out.append("(none: this is the first run)")
    text = "\n".join(out) + "\n"
    return Dossier(text, named, at, model.seq, tuple(f.id for f in found))


# --- Asking Claude ----------------------------------------------------------------------------


def ask(client: Any, text: str, model: str = MODEL) -> tuple[dict[str, Any], str]:
    """One dossier to Claude; its answer (structured output, ``SCHEMA``) and the model that
    served it (a refusal falls back server-side to Anthropic's recommended model)."""
    with client.beta.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM,
        messages=[{"role": "user", "content": text}],
    ) as stream:
        message = stream.get_final_message()
    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined to answer this run")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"the answer did not fit in {MAX_TOKENS} tokens")
    body = next((b.text for b in message.content if b.type == "text"), "")
    return json.loads(body), str(message.model)


def _client(key: str | None) -> Any:
    try:
        import anthropic
    except ImportError as error:
        raise RuntimeError(
            "the Anthropic SDK is missing: uv sync --all-packages --all-groups --extra anthropic"
        ) from error
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


# --- Cleaning and recording -------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "improvement"


def clean(answer: dict[str, Any], found: Dossier, model: SiteModel) -> dict[str, Any]:
    """Claude's answer with every id checked against the model: unknown evidence, goals,
    constraints and action targets are dropped and listed in ``dropped``; an improvement left
    with no evidence is dropped whole."""
    dropped: list[str] = []
    goals = {e for e in model.entities if entity_type(e) == "goal"}
    limits = {e for e in model.entities if entity_type(e) == "constraint"}
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in answer.get("improvements", []):
        item = dict(raw)
        key = _slug(str(item.get("key") or item.get("title") or ""))
        while key in seen:
            key += "-2"
        seen.add(key)
        item["key"] = key
        evidence = [str(i) for i in item.get("evidence", [])]
        item["evidence"] = [i for i in dict.fromkeys(evidence) if i in found.facts]
        dropped += [
            f"{key}: evidence {i} is not in the model" for i in evidence if i not in found.facts
        ]
        item["goals"] = [g for g in item.get("goals", []) if g in goals]
        item["constraints"] = [
            c for c in item.get("constraints", []) if c.get("constraint") in limits
        ]
        item["action"] = _action(item.get("action") or {}, model, key, dropped)
        if not item["evidence"]:
            dropped.append(f"{key}: dropped, no evidence from the model")
            continue
        kept.append(item)
    retired = [r for r in answer.get("retired", []) if isinstance(r, dict)]
    return {
        "summary": str(answer.get("summary", "")),
        "improvements": kept,
        "retired": retired,
        "dropped": dropped,
    }


def _action(
    action: dict[str, Any], model: SiteModel, key: str, dropped: list[str]
) -> dict[str, str]:
    none = {"verb": "none", "target": "", "zone": ""}
    verb = str(action.get("verb") or "none")
    if verb == "none":
        return none
    target, zone = str(action.get("target") or ""), str(action.get("zone") or "")
    ok = verb in actions.VERBS and entity_type(target) == actions.VERBS[verb]
    ok = ok and target in model.entities
    if verb == "send_to":
        ok = ok and entity_type(zone) == "zone" and zone in model.entities
    if not ok:
        dropped.append(
            f"{key}: action {verb} {target} {zone}".rstrip() + " is not one Neptune can send"
        )
        return none
    return {"verb": verb, "target": target, "zone": zone if verb == "send_to" else ""}


def record(
    site_dir: Path,
    found: Dossier,
    cleaned: dict[str, Any],
    *,
    model: str,
    trigger: str,
) -> list[Fact]:
    """Write the run as a line of ``RUNS`` and log one inferred fact per improvement."""
    data = {
        "at": found.at,
        "as_of_seq": found.seq,
        "model": model,
        "prompt_sha256": _sha(SYSTEM),
        "dossier_sha256": _sha(found.text),
        "trigger": trigger,
        "findings": list(found.findings),
        **cleaned,
    }
    line = actions._record(site_dir, RUNS, data, f"improvement run at {found.at}")
    facts = []
    for item in cleaned["improvements"]:
        support = [c for fid in item["evidence"] for c in found.facts[fid].citations]
        cite = Citation(RUNS, line.locator, item["title"])
        facts.append(
            Fact(
                f"improvement:{item['key']}",
                PREDICATE,
                Value(item["title"]),
                found.at,
                None,
                "inferred",
                SOURCE,
                (cite, *dict.fromkeys(support)),
                model=model,
            )
        )
    with FactLog(site_dir) as log:
        for fact in facts:
            log.fact(fact)
    return facts


def run(site_dir: Path, client: Any, *, trigger: str = "asked", model: str = MODEL) -> list[Fact]:
    """One run over the log as it stands: dossier, ask, clean, record."""
    site_dir = Path(site_dir)
    site = views.load(site_dir)
    found = dossier(site, site_dir)
    answer, served = ask(client, found.text, model)
    return record(site_dir, found, clean(answer, found, site), model=served, trigger=trigger)


# --- Reading runs -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One improvement of a run, as logged, with what a person answered."""

    rank: int
    fact: Fact
    data: dict[str, Any]
    status: str
    by: str | None
    move: str

    @property
    def key(self) -> str:
        return self.fact.subject.removeprefix("improvement:")

    @property
    def title(self) -> str:
        return _value(self.fact)


@dataclass(frozen=True)
class Run:
    line: int
    seq: int
    data: dict[str, Any]
    items: tuple[Item, ...] = field(default=())


def runs(site_dir: Path, model: SiteModel) -> list[Run]:
    """Every run the log holds, oldest first; each item ranked and compared with the run before."""
    groups: dict[int, list[Fact]] = {}
    for fact in model.facts.values():
        if fact.predicate != PREDICATE or fact.source != SOURCE or not fact.citations:
            continue
        cite = fact.citations[0]
        if cite.file == RUNS and cite.locator.startswith("line "):
            groups.setdefault(int(cite.locator.removeprefix("line ")), []).append(fact)
    if not groups:
        return []
    lines = (Path(site_dir) / RUNS).read_text(encoding="utf-8").splitlines()
    out: list[Run] = []
    before: dict[str, int] = {}
    for number in sorted(groups):
        data = json.loads(lines[number - 1]) if number <= len(lines) else {}
        order = {
            item["key"]: (rank, item) for rank, item in enumerate(data.get("improvements", []), 1)
        }
        items = []
        for fact in groups[number]:
            key = fact.subject.removeprefix("improvement:")
            rank, item = order.get(key, (len(order) + 1, {}))
            answered = model.confirms.get(fact.id)
            status = (
                "open"
                if answered is None
                else "accepted"
                if answered[1].answer == "yes"
                else "rejected"
            )
            move = "new" if key not in before else _move(before[key], rank)
            by = answered[1].by if answered else None
            items.append(Item(rank, fact, item, status, by, move))
        items.sort(key=lambda i: i.rank)
        seq = min(model.fact_seq[i.fact.id] for i in items)
        out.append(Run(number, seq, data, tuple(items)))
        before = {i.key: i.rank for i in items}
    return out


def _move(was: int, now: int) -> str:
    if now < was:
        return f"up {was - now}"
    if now > was:
        return f"down {now - was}"
    return "same"


# --- What changed since a run -----------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    seq: int
    t: str | None
    kind: str
    text: str
    fact: str


def changes(model: SiteModel, since: Run | None) -> list[Change]:
    """What is material in the site since ``since`` was asked (everything, with no run): new
    mission, record and document facts, faults, and findings it did not see."""
    after = since.data.get("as_of_seq", since.seq) if since is not None else 0
    seen = set(since.data.get("findings", [])) if since is not None else set()
    out: list[Change] = []
    for fid, fact in model.facts.items():
        seq = model.fact_seq[fid]
        if seq <= after or fact.predicate in OWN or fact.predicate in VERBATIM:
            continue
        kind = _material(fact)
        if kind is not None:
            text = f"{fact.subject} {fact.predicate} {_value(fact)}"
            out.append(Change(seq, fact.valid_from, kind, text, fid))
    if since is not None:
        for fact in insights.findings(model):
            if fact.id not in seen:
                out.append(Change(model.seq, fact.valid_from, "finding", _value(fact), fact.id))
    out.sort(key=lambda c: (c.seq, c.t or ""))
    return out


def _material(fact: Fact) -> str | None:
    if entity_type(fact.subject) in ("goal", "constraint"):
        return "mission"
    if fact.kind == "stated":
        file = fact.citations[0].file if fact.citations else ""
        return "document" if file.endswith((".pdf", ".png", ".json")) else "record"
    if fact.kind == "observed" and fact.predicate == "health" and _value(fact).startswith("fault"):
        return "fault"
    return None


# --- A person's answer ------------------------------------------------------------------------


def answer(
    site_dir: Path,
    fact_id: str,
    yes: bool,
    by: str = "operator",
    *,
    broker: str = actions.DEFAULT_BROKER,
    send: Callable[[str, bytes], None] | None = None,
) -> dict[str, Any]:
    """Accept or reject an improvement. Accepting one with an action proposes the action, citing
    the improvement, and approves it: an approved robot action is dispatched."""
    site_dir = Path(site_dir)
    model = views.load(site_dir)
    item = next((i for r in runs(site_dir, model) for i in r.items if i.fact.id == fact_id), None)
    if item is None:
        raise ValueError(f"no improvement {fact_id} in the log")
    if item.status != "open":
        raise ValueError(f"{fact_id} is already {item.status}")
    with FactLog(site_dir) as log:
        log.confirm(fact_id, "yes" if yes else "no", by)
    result: dict[str, Any] = {"id": fact_id, "status": "accepted" if yes else "rejected"}
    action = item.data.get("action") or {}
    if yes and action.get("verb", "none") != "none":
        proposal = actions.propose(
            site_dir,
            action["verb"],
            action["target"],
            f"{item.title} (improvement {item.key})",
            [fact_id],
            action.get("zone") or None,
            by=SOURCE,
        )
        result["action"] = {"id": proposal.id, "status": "proposed"}
        done = actions.approve(site_dir, proposal.id, by, broker=broker, send=send)
        result["action"]["status"] = done.status
    return result


# --- The engine -------------------------------------------------------------------------------


class Engine:
    """Runs improvement passes for the web view: a key a person gives it (memory only, never
    written), on demand or automatically when the site changes materially."""

    def __init__(
        self,
        site_dir: Path,
        client: Callable[[str | None], Any] = _client,
        *,
        model: str = MODEL,
        cooldown: float = COOLDOWN,
    ) -> None:
        self.site_dir = Path(site_dir)
        self.model = model
        self.cooldown = cooldown
        self.auto = True
        self.state = "idle"
        self.error: str | None = None
        self.last = 0.0
        self._key: str | None = None
        self._client = client
        self._busy = threading.Lock()

    def set_key(self, key: str | None) -> None:
        self._key = (key or "").strip() or None
        self.error = None

    @property
    def key(self) -> str | None:
        """``page`` (set here), ``env`` (``ANTHROPIC_API_KEY``) or None: where the key is from."""
        if self._key:
            return "page"
        return "env" if os.environ.get("ANTHROPIC_API_KEY") else None

    def client(self) -> Any:
        """An Anthropic client on the person's key (the chat uses it too)."""
        if self.key is None:
            raise ValueError("no API key: paste yours in the improvement engine")
        return self._client(self._key)

    def status(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "auto": self.auto,
            "state": self.state,
            "error": self.error,
            "model": self.model,
        }

    def start(self, trigger: str) -> bool:
        """Run in the background; False when a run is already going or there is no key."""
        if self.key is None or not self._busy.acquire(blocking=False):
            return False
        self.state, self.error = "running", None
        threading.Thread(target=self._run, args=(trigger,), daemon=True).start()
        return True

    def _run(self, trigger: str) -> None:
        try:
            run(self.site_dir, self._client(self._key), trigger=trigger, model=self.model)
        except Exception as error:  # shown on the page: a bad key, no network, a refusal
            self.error = f"{type(error).__name__}: {error}"
        finally:
            self.last = time.monotonic()
            self.state = "idle"
            self._busy.release()

    def due(self, model: SiteModel) -> str | None:
        """Why an automatic run is due now, or None."""
        if not self.auto or self.key is None or self.state == "running" or self.error:
            return None
        if time.monotonic() - self.last < self.cooldown:
            return None
        earlier = runs(self.site_dir, model)
        if not earlier:
            return "first run"
        found = changes(model, earlier[-1])
        if not found:
            return None
        kinds = ", ".join(sorted({c.kind for c in found}))
        return f"{len(found)} change(s) since the last run: {kinds}"

    def watch(self, due: Callable[[], str | None], poll: float = 3.0) -> threading.Thread:
        """Ask ``due()`` (``Engine.due`` over the live model) every ``poll`` seconds and start a
        run when one is due."""

        def loop() -> None:
            while True:
                time.sleep(poll)
                with contextlib.suppress(Exception):  # a log being rebuilt: try again next poll
                    why = due()
                    if why is not None:
                        self.start(why)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread


def retired(current: Run, before: Run | None) -> list[dict[str, str]]:
    """The earlier run's improvements this run dropped, with Claude's reason when it gave one."""
    if before is None:
        return []
    reasons = {str(r.get("key")): str(r.get("reason", "")) for r in current.data.get("retired", [])}
    kept = {i.key for i in current.items}
    return [
        {"key": i.key, "title": i.title, "fact": i.fact.id, "reason": reasons.get(i.key, "")}
        for i in before.items
        if i.key not in kept
    ]
