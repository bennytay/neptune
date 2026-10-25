"""Human-approved actions (#212): Claude proposes, a person approves, the fleet manager acts.

- **Propose** (``propose``, MCP ``neptune_site_propose_action``): an **inferred** fact
  ``<target> proposed_action "<verb> [<zone>]"``, source ``neptune.actions``. Verbs: ``pause``,
  ``resume``, ``send_to`` (a zone), ``cancel_task`` (robots) and ``page`` (a person). The request
  is recorded as one line of ``actions/proposed.jsonl`` and cited first, its quote being the
  reason; the facts it cites follow with their own citations.
- **Approve / reject** (``approve`` / ``reject``; the CLI ``approve`` / ``reject``, or the web
  view): a ``confirm`` entry, like an identity proposal. No MCP tool approves: a person does. The
  fold turns a yes into the **intent**: a stated copy, by whom, citing the confirm line.
- **Dispatch**: an approved robot action goes to the fleet manager over MQTT, VDA 5050-style, on
  the target's own topic (its state topic, ``.../state`` -> ``.../instantActions`` or
  ``.../order``). The message as sent is a line of ``actions/dispatched.jsonl``; the log gets an
  **observed** ``dispatched`` fact citing it and the approval. ``page`` is logged only.
- **Outcome**: ``live`` watches the log (``Watcher``). The first observed change on the target that
  the action asked for (``mode = PAUSED``, ``in_zone = <zone>`` ...) gets an **inferred**
  ``outcome`` fact, ``done``, citing the message and that observation; none within
  ``OUTCOME_WITHIN`` site seconds gets ``not observed``.
- **The simulator** (``Simulator``, used by ``publish``) answers like a fleet manager would: a
  paused AMR reports ``operatingMode: PAUSED``; ``send_to`` drives it into the zone
  ``SEND_TO_SECONDS`` later.

Actions never touch safety functions (e-stops, protective stops, interlocks, speed and separation
limits): they only go through the fleet manager's own protocol, which keeps every safety layer in
the loop. No auth: this is a demo.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from neptune_memory.site import views
from neptune_memory.site.confirm import stated_from_confirm
from neptune_memory.site.facts import Citation, Fact, LogEntry, Ref, Value
from neptune_memory.site.log import FactLog, Tail
from neptune_memory.site.query import Answer
from neptune_memory.site.vocab import entity_type

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from neptune_memory.site.replay import FeedMessage
    from neptune_memory.site.views import SiteModel

SOURCE = "neptune.actions"
VERBS: dict[str, str] = {
    "pause": "robot",
    "resume": "robot",
    "send_to": "robot",
    "cancel_task": "robot",
    "page": "person",
}
"""Each verb and the entity type it targets."""
INSTANT = {"pause": "startPause", "resume": "stopPause", "cancel_task": "cancelOrder"}
"""VDA 5050 ``instantActions`` action types; ``send_to`` is an ``order``."""
OUTCOME_WITHIN = 120
"""Site seconds after dispatch in which the target's state must follow, else ``not observed``."""
SEND_TO_SECONDS = 30
"""Site seconds the simulator takes to drive an AMR into the zone it was sent to."""
PROPOSED = "actions/proposed.jsonl"
DISPATCHED = "actions/dispatched.jsonl"
DEFAULT_BROKER = "mqtt://127.0.0.1:1883"
COMMANDS = ("instantActions", "order")
"""The last topic segment of a message to a robot: a command, never a report."""

Status = Literal["pending", "rejected", "approved", "dispatched", "done", "not observed", "logged"]


@dataclass(frozen=True)
class Action:
    """One proposal and what became of it. ``id`` is the proposal's fact id."""

    id: str
    proposal: Fact
    verb: str
    target: str
    zone: str | None
    reason: str
    status: Status
    by: str | None = None
    intent: Fact | None = None
    dispatched: Fact | None = None
    outcome: Fact | None = None


# --- Propose ----------------------------------------------------------------------------------


def propose(
    site_dir: Path,
    action: str,
    target: str,
    reason: str,
    cites: list[str],
    zone: str | None = None,
    *,
    model: SiteModel | None = None,
    by: str = "claude",
) -> Fact:
    """Log an inferred ``proposed_action`` on ``target``, citing its reason and the facts in
    ``cites`` (fact ids). Raises ``ValueError`` on a verb, target, zone or fact it does not know."""
    site_dir = Path(site_dir)
    model = model if model is not None else views.load(site_dir)
    if action not in VERBS:
        raise ValueError(f"unknown action {action!r}; one of {', '.join(VERBS)}")
    if entity_type(target) != VERBS[action] or target not in model.entities:
        raise ValueError(f"{action} needs a known {VERBS[action]} as target, not {target!r}")
    if action == "send_to":
        if zone is None or entity_type(zone) != "zone" or zone not in model.entities:
            raise ValueError(f"send_to needs a known zone (zone:...), not {zone!r}")
    elif zone is not None:
        raise ValueError(f"{action} takes no zone")
    if not reason.strip():
        raise ValueError("an action needs a reason")
    missing = [c for c in cites if c not in model.facts]
    if not cites or missing:
        raise ValueError(f"cite at least one fact id from the site model; unknown: {missing}")
    at = model.clock()
    record: dict[str, Any] = {"action": action, "target": target, "zone": zone, "reason": reason}
    record |= {"cites": cites, "by": by, "at": at}
    support = [c for fid in cites for c in model.facts[fid].citations]
    citations = (_record(site_dir, PROPOSED, record, reason), *dict.fromkeys(support))
    text = action if zone is None else f"{action} {zone}"
    fact = Fact(target, "proposed_action", Value(text), at, None, "inferred", SOURCE, citations)
    with FactLog(site_dir) as log:
        log.fact(fact)
    return fact


def _record(site_dir: Path, rel: str, data: dict[str, Any], quote: str) -> Citation:
    """Append ``data`` as one JSON line of ``rel`` and cite that line."""
    path = Path(site_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8", newline="\n") as handle:
        handle.seek(0)
        number = sum(1 for _ in handle) + 1
        handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    return Citation(rel, f"line {number}", quote)


# --- Read -------------------------------------------------------------------------------------


def load_actions(model: SiteModel) -> list[Action]:
    """Every proposed action in the model, in log order, with what became of it."""
    return [
        _action(model, fact)
        for fact in model.facts.values()
        if fact.predicate == "proposed_action" and fact.source == SOURCE
    ]


def _action(model: SiteModel, proposal: Fact) -> Action:
    verb, _, zone = str(_value(proposal)).partition(" ")
    reason = proposal.citations[0].quote or ""
    base = Action(proposal.id, proposal, verb, proposal.subject, zone or None, reason, "pending")
    answered = model.confirms.get(proposal.id)
    if answered is None:
        return base
    seq, confirm = answered
    if confirm.answer == "no":
        return replace(base, status="rejected", by=confirm.by)
    intent = stated_from_confirm(proposal, LogEntry(seq, "confirm", confirm=confirm), seq)
    dispatched = _linked(model, proposal, "dispatched")
    outcome = _linked(model, proposal, "outcome")
    status: Status = "logged" if verb not in INSTANT and verb != "send_to" else "approved"
    if dispatched is not None:
        status = "dispatched"
    if outcome is not None:
        status = "done" if ": done" in str(_value(outcome)) else "not observed"
    return replace(
        base, status=status, by=confirm.by, intent=intent, dispatched=dispatched, outcome=outcome
    )


def _value(fact: Fact) -> str | int | float | bool:
    return fact.object.ref if isinstance(fact.object, Ref) else fact.object.value


def _linked(model: SiteModel, proposal: Fact, predicate: str) -> Fact | None:
    """The ``dispatched`` / ``outcome`` fact whose text opens with the proposal's id."""
    for fid in model.by_key.get((proposal.subject, predicate), []):
        fact = model.facts[fid]
        if str(_value(fact)).startswith(proposal.id + ":"):
            return fact
    return None


def find(model: SiteModel, action_id: str) -> Action:
    for action in load_actions(model):
        if action.id == action_id:
            return action
    raise ValueError(f"no proposed action {action_id} in the log")


def pending(site_dir: Path) -> list[Action]:
    """Actions waiting for a person, in log order (for the web view)."""
    return [a for a in load_actions(views.load(Path(site_dir))) if a.status == "pending"]


def actions(model: SiteModel, ids: Iterable[str] | None = None) -> Answer:
    """Every proposed action (or those in ``ids``) as a cited answer: the proposal, then its
    intent, the message as dispatched and the outcome (``neptune_site_actions``)."""
    wanted = None if ids is None else set(ids)
    answer = Answer(tool="actions", at=model.clock(), as_of_seq=model.seq)
    for action in load_actions(model):
        if wanted is not None and action.id not in wanted:
            continue
        state: Literal["pending", "known"] = "pending" if action.status == "pending" else "known"
        relation = "proposed" if state == "pending" else f"proposed, {action.status}"
        answer.add(action.proposal, state, relation=relation)
        if action.intent is not None:
            answer.add(action.intent, relation=f"intent, approved by {action.by}")
        if action.dispatched is not None:
            answer.add(action.dispatched, relation="dispatched")
        if action.outcome is not None:
            answer.add(action.outcome, relation="outcome")
        if action.status == "pending":
            answer.gaps.append(
                f"{action.id}: waiting for a person "
                f"(python -m neptune_memory.site approve <site_dir> {action.id}, or reject)"
            )
        elif action.status == "rejected":
            answer.gaps.append(f"{action.id}: rejected by {action.by}")
        elif action.status == "dispatched":
            answer.gaps.append(
                f"{action.id}: dispatched; watching {action.target} for {OUTCOME_WITHIN} s"
            )
    if not answer.items:
        answer.gaps.append("no actions proposed")
    return answer


# --- Approve, reject, dispatch ----------------------------------------------------------------


def approve(
    site_dir: Path,
    action_id: str,
    by: str = "operator",
    *,
    broker: str = DEFAULT_BROKER,
    send: Callable[[str, bytes], None] | None = None,
) -> Action:
    """A person's yes: log it (the intent), then dispatch a robot action to the fleet manager
    and log the message as sent. ``send(topic, payload)`` defaults to MQTT on ``broker``."""
    site_dir = Path(site_dir)
    model = views.load(site_dir)
    action = _pending(model, action_id)
    at = model.clock()
    message = None if action.verb == "page" else _message(site_dir, model, action, at)
    with FactLog(site_dir) as log:
        entry = log.confirm(action_id, "yes", by)
    intent = stated_from_confirm(action.proposal, entry, entry.seq)
    action = replace(action, status="approved", by=by, intent=intent)
    if message is None:
        return replace(action, status="logged")
    topic, body = message
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    (send or _mqtt(broker))(topic, payload.encode("utf-8"))
    sent = _record(site_dir, DISPATCHED, {"t": at, "topic": topic, "payload": body}, payload)
    kind = "order" if action.verb == "send_to" else "instantActions"
    text = f"{action_id}: {kind} on {topic}"
    fact = Fact(
        action.target,
        "dispatched",
        Value(text),
        at,
        None,
        "observed",
        SOURCE,
        (sent, intent.citations[-1]),
    )
    with FactLog(site_dir) as log:
        log.fact(fact)
    return replace(action, status="dispatched", dispatched=fact)


def reject(site_dir: Path, action_id: str, by: str = "operator") -> Action:
    """A person's no: log it; the proposal leaves the views."""
    site_dir = Path(site_dir)
    action = _pending(views.load(site_dir), action_id)
    with FactLog(site_dir) as log:
        log.confirm(action_id, "no", by)
    return replace(action, status="rejected", by=by)


def _pending(model: SiteModel, action_id: str) -> Action:
    action = find(model, action_id)
    if action.status != "pending":
        raise ValueError(f"{action_id} is already {action.status}")
    return action


def _message(
    site_dir: Path, model: SiteModel, action: Action, at: str | None
) -> tuple[str, dict[str, Any]]:
    """The VDA 5050-style topic and message for a robot action."""
    base = _fleet_topic(site_dir, model, action.target)
    if base is None:
        raise ValueError(f"{action.target} reports on no VDA 5050 state topic: cannot dispatch")
    parts = base.split("/")
    header = {
        "headerId": _lines(site_dir / DISPATCHED) + 1,
        "timestamp": at,
        "version": "2.0.0",
        "manufacturer": parts[-2],
        "serialNumber": parts[-1],
    }
    short = action.id.removeprefix("fact:")
    if action.verb == "send_to":
        node: dict[str, Any] = {
            "nodeId": action.zone,
            "sequenceId": 0,
            "released": True,
            "actions": [],
        }
        order = {"orderId": f"neptune-{short}", "orderUpdateId": 0, "nodes": [node], "edges": []}
        return f"{base}/order", header | order
    instant = {"actionType": INSTANT[action.verb], "actionId": short, "blockingType": "HARD"}
    return f"{base}/instantActions", header | {"actions": [instant]}


def _fleet_topic(site_dir: Path, model: SiteModel, target: str) -> str | None:
    """``<interface>/<version>/<manufacturer>/<serial>`` from the target's latest observed
    report on a ``.../state`` topic (the envelope on the feed line its fact cites)."""
    observed = [
        (model.fact_seq[fid], fact)
        for (subject, _), fids in model.by_key.items()
        if subject == target
        for fid in fids
        for fact in (model.facts[fid],)
        if fact.kind == "observed" and fact.source != SOURCE
    ]
    for _, fact in sorted(observed, key=lambda pair: pair[0], reverse=True):
        cite = fact.citations[0]
        number = int(cite.locator.removeprefix("line ")) if cite.locator.startswith("line ") else 0
        envelope = _line(site_dir / cite.file, number) if cite.file.endswith(".jsonl") else None
        topic = str(envelope.get("topic", "")) if envelope else ""
        if topic.endswith("/state") and topic.count("/") >= 3:
            return topic.removesuffix("/state")
    return None


def _line(path: Path, number: int) -> dict[str, Any] | None:
    if number < 1 or not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        for n, text in enumerate(handle, start=1):
            if n == number:
                found: dict[str, Any] = json.loads(text)
                return found
    return None


def _lines(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def _mqtt(broker: str) -> Callable[[str, bytes], None]:
    def send(topic: str, payload: bytes) -> None:
        from neptune_memory.site.live import _client, _connect

        client = _client("neptune-actions")
        _connect(client, broker, wait=3.0)
        client.loop_start()
        try:
            client.publish(topic, payload, qos=1).wait_for_publish(5)
        finally:
            client.loop_stop()
            client.disconnect()

    return send


# --- Outcome ----------------------------------------------------------------------------------


def _plus(t: str, seconds: float) -> str:
    return (datetime.fromisoformat(t) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _expects(action: Action, fact: Fact) -> bool:
    """True when ``fact`` is the change ``action`` asked for."""
    value = str(_value(fact))
    if action.verb == "pause":
        return fact.predicate == "mode" and value.upper() == "PAUSED"
    if action.verb == "resume":
        return fact.predicate == "mode" and value.upper() != "PAUSED"
    if action.verb == "cancel_task":
        return fact.predicate == "current_task" and value.rsplit(":", 1)[-1] == "none"
    return fact.predicate == "in_zone" and value == action.zone


WATCHED = {"pause": "mode", "resume": "mode", "cancel_task": "current_task", "send_to": "in_zone"}


def outcome(model: SiteModel, action: Action) -> Fact | None:
    """The ``outcome`` a dispatched action has by the model's clock, if it has one yet."""
    sent = action.dispatched
    if sent is None or action.outcome is not None or sent.valid_from is None:
        return None
    since = sent.valid_from
    for fid in model.by_key.get((action.target, WATCHED[action.verb]), []):
        fact = model.facts[fid]
        if (
            fact.kind == "observed"
            and fact.valid_from is not None
            and fact.valid_from > since
            and _expects(action, fact)
        ):
            text = f"{action.id}: done, {fact.predicate} = {_value(fact)}"
            cites = (sent.citations[0], *fact.citations)
            return Fact(
                action.target,
                "outcome",
                Value(text),
                fact.valid_from,
                None,
                "inferred",
                SOURCE,
                cites,
            )
    clock = model.clock()
    deadline = _plus(since, OUTCOME_WITHIN)
    if clock is not None and clock > deadline:
        text = f"{action.id}: not observed within {OUTCOME_WITHIN} s"
        return Fact(
            action.target,
            "outcome",
            Value(text),
            deadline,
            None,
            "inferred",
            SOURCE,
            sent.citations,
        )
    return None


class Watcher:
    """Follows the log as it grows (``live`` steps it after each message): appends each
    dispatched action's outcome once it has one, and describes every action event it reads."""

    def __init__(self, site_dir: Path) -> None:
        self.site_dir = Path(site_dir)
        self._start()

    def _start(self) -> None:
        self.tail = Tail(self.site_dir)
        self.model = views.SiteModel.empty()
        self.waiting: dict[str, Fact] = {}

    def step(self, log: FactLog) -> list[str]:
        new = self.tail.new()
        if new is None:  # the log was started again
            self._start()
            new = self.tail.new() or []
        events: list[str] = []
        for entry in new:
            self.model.apply(entry)
            events += self._event(entry)
        for pid, proposal in list(self.waiting.items()):
            found = outcome(self.model, _action(self.model, proposal))
            if found is not None:
                del self.waiting[pid]
                log.fact(found)
        return events

    def _event(self, entry: LogEntry) -> list[str]:
        fact, confirm = entry.fact, entry.confirm
        if confirm is not None and confirm.proposal in self.model.facts:
            proposal = self.model.facts[confirm.proposal]
            if proposal.predicate == "proposed_action":
                verdict = "approved" if confirm.answer == "yes" else "rejected"
                return [f"{verdict} by {confirm.by}: {proposal.subject} {_value(proposal)}"]
        if fact is None or fact.source != SOURCE:
            return []
        if fact.predicate == "proposed_action":
            return [
                f"proposed {fact.id}: {fact.subject} {_value(fact)} ({fact.citations[0].quote})"
            ]
        pid = str(_value(fact))[: len("fact:") + 16]
        if fact.predicate == "dispatched" and pid in self.model.facts:
            self.waiting[pid] = self.model.facts[pid]
        if fact.predicate == "outcome":
            self.waiting.pop(pid, None)
        return [f"{fact.predicate}: {fact.subject} {_value(fact)}"]


# --- The simulator ----------------------------------------------------------------------------


@dataclass
class _Robot:
    paused: bool = False
    cancelled: bool = False
    goto: tuple[str, str, str | None] | None = None  # (zone, orderId, arrival site time)


class Simulator:
    """What ``publish`` does with commands to robots: it changes their later state messages the
    way a fleet manager would. Topics are VDA 5050's ``.../<serial>/instantActions|order``."""

    TOPICS = tuple(f"+/+/+/+/{command}" for command in COMMANDS)

    def __init__(self) -> None:
        self.now: str | None = None
        self.robots: dict[str, _Robot] = {}
        self._lock = threading.Lock()

    def receive(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        body = json.loads(payload)
        with self._lock:
            robot = self.robots.setdefault(parts[-2], _Robot())
            if parts[-1] == "order":
                arrive = None if self.now is None else _plus(self.now, SEND_TO_SECONDS)
                robot.goto = (str(body["nodes"][-1]["nodeId"]), str(body["orderId"]), arrive)
                robot.cancelled = False
                return
            for item in body.get("actions", []):
                kind = item.get("actionType")
                if kind in ("startPause", "stopPause"):
                    robot.paused = kind == "startPause"
                elif kind == "cancelOrder":
                    robot.cancelled, robot.goto = True, None

    def apply(self, message: FeedMessage) -> FeedMessage:
        """``message`` as the commanded robot would now report it."""
        parts = message.topic.split("/")
        with self._lock:
            self.now = message.t
            robot = self.robots.get(parts[-2]) if parts[-1] == "state" else None
            if robot is None:
                return message
            payload = dict(message.payload)
            if robot.paused:
                payload |= {"operatingMode": "PAUSED", "paused": True}
            if robot.cancelled:
                payload |= {"orderId": "", "orderState": "idle"}
            if robot.goto is not None:
                zone, order, arrive = robot.goto
                if arrive is None or message.t >= arrive:
                    payload |= {"zone": zone, "orderId": "", "orderState": "idle"}
                else:
                    payload |= {"orderId": order, "orderState": "driving"}
        return replace(message, payload=payload)


def is_command(topic: str) -> bool:
    """A message to a robot (``.../instantActions``, ``.../order``), not a report from one."""
    return topic.rsplit("/", 1)[-1] in COMMANDS
