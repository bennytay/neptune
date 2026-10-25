"""Feed messages become observed state intervals, emitted only on change (spec §4, §12).

A message is anything with the ``FeedMessage`` fields (#182): ``t``, ``source``, ``topic``,
``payload``, plus ``file`` (corpus-relative) and ``line`` (1-based) for the citation
``line N``. ``valid_from`` is the message time. The mapping's ``feeds`` entry for the message's
source names its decoder, its subject template and which fields become which predicates; a source
with no entry maps to nothing. Pass one ``last`` dict through a whole build or replay: a fact is
emitted only when its value differs from the last one for (subject, predicate, source); for a
``many`` predicate (``assigned_to``), only when that object is new. Without ``last`` every value is
emitted.

Decoders turn a payload into ``(field name, value, unit)`` readings and say what to quote:

- ``sparkplug``: ``{"metrics": [{"name": ..., "value": ..., "unit": ...}]}``; field names are
  metric names (globs allowed in the mapping). ``opcua``: the same over ``{"nodes": [{"name",
  "value", "eu"}]}``, ``eu`` the unit.
- ``ros2_diagnostics``: a ``DiagnosticArray``'s ``status[]`` as ``<status name>/<key>`` plus
  ``<status name>/level``; ``{hardware_id}`` names the status's device in templates.
- ``json`` / ``vda5050``: payload fields by dotted path (``batteryState.batteryCharge``).
- ``controller_log``: as ``json``; the quote is the line's ``text``, except on a heartbeat.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any, Protocol

from neptune_memory.site.facts import Citation, Fact, Ref, Value
from neptune_memory.site.mappers.config import Mapping, render
from neptune_memory.site.vocab import is_one

if TYPE_CHECKING:
    from collections.abc import Callable


class FeedMessage(Protocol):
    """The fields of #182's ``FeedMessage`` that mapping reads."""

    @property
    def t(self) -> str: ...
    @property
    def source(self) -> str: ...
    @property
    def topic(self) -> str: ...
    @property
    def payload(self) -> dict[str, Any]: ...
    @property
    def file(self) -> str: ...
    @property
    def line(self) -> int: ...


Last = dict[tuple[str, ...], Ref | Value]
_Reading = tuple[str, str, Ref | Value, Any]  # subject, predicate, object, the value as written
# A decoded field: its mapping key, value, unit as sent (or None), the quote that cites it, and
# extra template names it brings (``{hardware_id}`` for a diagnostics status).
Field = tuple[str, Any, str | None, Any, dict[str, str]]


def map_message(msg: FeedMessage, mapping: Mapping, last: Last | None = None) -> list[Fact]:
    """The observed facts in one feed message whose value changed since ``last``."""
    entry = mapping.feed(msg.source)
    if entry is None:
        return []
    readings = _readings(msg, entry, mapping)
    last = {} if last is None else last
    cite_line = f"line {msg.line}"
    out: list[Fact] = []
    for subject, predicate, obj, quote in readings:
        key: tuple[str, ...] = (subject, predicate, msg.source)
        if not is_one(predicate):
            key += (obj.ref if isinstance(obj, Ref) else str(obj.value),)
        if key in last and last[key] == obj:
            continue
        last[key] = obj
        cite = Citation(msg.file, cite_line, str(quote))
        out.append(Fact(subject, predicate, obj, msg.t, None, "observed", msg.source, (cite,)))
    return out


# --- Decoders ---------------------------------------------------------------------------------


def _path(payload: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(payload, dict) or part not in payload:
            return None
        payload = payload[part]
    return payload


def _match(name: str, names: list[str]) -> str | None:
    """The first mapping key (a glob) that ``name`` matches."""
    return next((key for key in names if fnmatchcase(name, key)), None)


def _named(items: list[Any], names: list[str], unit: str) -> list[Field]:
    """``[{"name", "value", <unit>}]`` lists: Sparkplug metrics, OPC UA nodes."""
    out: list[Field] = []
    for item in items:
        key = _match(str(item["name"]), names)
        if key is not None:
            out.append((key, item["value"], item.get(unit), item["value"], {}))
    return out


def _sparkplug(msg: FeedMessage, names: list[str]) -> list[Field]:
    return _named(msg.payload.get("metrics", []), names, "unit")


def _opcua(msg: FeedMessage, names: list[str]) -> list[Field]:
    return _named(msg.payload.get("nodes", []), names, "eu")


_LEVELS = {0: "", 1: "WARN", 2: "ERROR", 3: "STALE"}


def _ros2_diagnostics(msg: FeedMessage, names: list[str]) -> list[Field]:
    """A ``DiagnosticArray``: each status's values as ``<status name>/<key>``, its level as
    ``<status name>/level`` (0 = no fault code, else ``WARN`` / ``ERROR`` / ``STALE``)."""
    out: list[Field] = []
    for status in msg.payload.get("status", []):
        extra = {"hardware_id": str(status.get("hardware_id", ""))}
        level = int(status.get("level", 0))
        entries = [(f"{status['name']}/{v['key']}", v["value"]) for v in status.get("values", [])]
        entries.append((f"{status['name']}/level", _LEVELS.get(level, str(level))))
        for name, value in entries:
            key = _match(name, names)
            if key is not None:
                quote = status.get("message") if name.endswith("/level") and level else value
                out.append((key, value, None, _written(quote), extra))
    return out


def _json(msg: FeedMessage, names: list[str]) -> list[Field]:
    out: list[Field] = []
    for name in names:
        value = _path(msg.payload, name)
        if value is not None:
            out.append((name, value, None, _written(value), {}))
    return out


def _controller_log(msg: FeedMessage, names: list[str]) -> list[Field]:
    fields = _json(msg, names)
    if msg.payload.get("event") == "heartbeat" or "text" not in msg.payload:
        return fields
    text = msg.payload["text"]
    return [(name, value, unit, text, extra) for name, value, unit, _, extra in fields]


DECODERS: dict[str, Callable[[FeedMessage, list[str]], list[Field]]] = {
    "sparkplug": _sparkplug,
    "opcua": _opcua,
    "json": _json,
    "vda5050": _json,
    "controller_log": _controller_log,
    "ros2_diagnostics": _ros2_diagnostics,
}
"""Decoder name -> function; register a new payload shape here."""


# --- Fields to readings -----------------------------------------------------------------------


def _written(value: Any) -> Any:
    """A value as it reads in the message: text as is, empty text or lists as JSON."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, str | list | dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def _fault(value: Any) -> str:
    """A fault code: ``""`` / ``[]`` = none; a list's first ``errorType``."""
    if isinstance(value, list):
        return str(value[0].get("errorType", "error")) if value else ""
    return str(value or "")


def _lookup(msg: FeedMessage, extra: dict[str, str]) -> Callable[[str], str]:
    """Template names in a feed: ``source``, ``topic``, ``topic[N]``, ``payload.a.b``, extras."""

    def lookup(name: str) -> str:
        if name in extra:
            return extra[name]
        if name == "source":
            return msg.source
        if name == "topic":
            return msg.topic
        if name.startswith("topic[") and name.endswith("]"):
            return msg.topic.rstrip("/").split("/")[int(name[6:-1])]
        if name.startswith("payload."):
            return str(_path(msg.payload, name.removeprefix("payload.")))
        raise KeyError(name)

    return lookup


def device(msg: FeedMessage, mapping: Mapping) -> str | None:
    """The device one message reports for, when its feed names it per message
    (``robot:{topic[-2]}`` -> ``amr-01``): freshness is then per device. ``None`` otherwise."""
    entry = mapping.feed(msg.source)
    subject = str((entry or {}).get("subject", ""))
    if "{" not in subject:
        return None
    return render(subject, _lookup(msg, {}), mapping).split(":", 1)[-1]


def _readings(msg: FeedMessage, entry: dict[str, Any], mapping: Mapping) -> list[_Reading]:
    fields: dict[str, Any] = {
        str(name): spec if isinstance(spec, dict) else {"predicate": spec}
        for name, spec in (entry.get("fields") or {}).items()
    }
    decode = DECODERS[str(entry.get("decoder", "json"))]

    extra: dict[str, str] = {}
    lookup = _lookup(msg, extra)
    default = render(entry.get("subject", ""), lookup, mapping)
    out: list[_Reading] = []
    for name, value, sent_unit, quote, names in decode(msg, list(fields)):
        extra.clear()
        extra.update(names)
        spec = fields[name]
        subject = render(spec["subject"], lookup, mapping) if "subject" in spec else default
        predicate = str(spec["predicate"])
        unit = spec.get("unit", sent_unit)
        if predicate == "health":
            code = _fault(value)
            if isinstance(value, list) and code:
                quote = code
            out.append((subject, predicate, Value(f"fault:{code}" if code else "ok"), quote))
        elif predicate == "current_task":
            out.extend(_task(subject, value, quote, spec, msg))
        elif "ref" in spec:
            text = str(value)
            prefix = f"{spec['ref']}:"
            out.append(
                (subject, predicate, Ref(text if text.startswith(prefix) else prefix + text), quote)
            )
        else:
            out.append((subject, predicate, Value(value, unit), quote))
    return out


def _task(
    subject: str, value: Any, quote: Any, spec: dict[str, Any], msg: FeedMessage
) -> list[_Reading]:
    """``current_task`` -> ``task:<value>``, its ``assigned_to`` back, and its ``task_state``."""
    if not value:
        return [(subject, "current_task", Value("none"), quote)]
    task = f"task:{value}"
    out: list[_Reading] = [
        (subject, "current_task", Ref(task), quote),
        (task, "assigned_to", Ref(subject), quote),
    ]
    state = _path(msg.payload, str(spec["state"])) if "state" in spec else None
    if state:
        out.append((task, "task_state", Value(state), state))
    return out
