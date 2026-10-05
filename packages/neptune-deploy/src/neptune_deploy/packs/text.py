"""Plain-text forms of pack values for the PDF, and the WinAnsi rule (ADR 0013 §9).

The PDF uses the standard Type1 fonts with ``WinAnsiEncoding``. A character outside it is never
dropped: printable ASCII and every other character WinAnsi encodes (Latin-1 from U+00A0, and the
Windows-1252 extras such as U+20AC) are kept; anything else, control characters included, is
written as ``<U+XXXX>`` (uppercase hex, at least four digits). So that the form cannot be read
into source text that already holds it, the ``<`` of a literal ``<U+`` is escaped too
(``<U+003C>U+``). The pack's JSON keeps the original text; the PDF says where it differs.
"""

import json
from collections.abc import Callable, Mapping

from neptune.model.jsonvalue import JsonValue
from neptune_deploy.packs.snapshot import Interval, Stamp


def _escape(char: str) -> str:
    return f"<U+{ord(char):04X}>"


def winansi(text: str) -> str:
    """``text`` with every character WinAnsi cannot show replaced by ``<U+XXXX>``."""
    out: list[str] = []
    for index, char in enumerate(text):
        point = ord(char)
        if char == "<" and text.startswith("<U+", index):
            out.append(_escape(char))
            continue
        if 0x20 <= point <= 0x7E:
            out.append(char)
            continue
        if point >= 0xA0:
            try:
                char.encode("cp1252")
            except UnicodeEncodeError:
                pass
            else:
                out.append(char)
                continue
        out.append(_escape(char))
    return "".join(out)


def ascii_only(text: str) -> str:
    """For the document information dictionary: printable ASCII, everything else escaped."""
    return "".join(c if 0x20 <= ord(c) <= 0x7E else _escape(c) for c in text)


def stamp(value: Stamp) -> str:
    return f"{value.ticks} on {value.domain}"


def interval(value: Interval) -> str:
    """``[start, end) ticks on <clock>``; bounds on two clocks name both."""
    end = value.end
    if isinstance(end, str):
        return f"[{value.start.ticks}, open) ticks on {value.start.domain}"
    if end.domain == value.start.domain:
        return f"[{value.start.ticks}, {end.ticks}) ticks on {value.start.domain}"
    return f"[{stamp(value.start)}, {stamp(end)})"


def _number(value: JsonValue) -> str:
    if isinstance(value, Mapping) and "non_finite" in value:
        return str(value["non_finite"])
    if isinstance(value, float):
        return float.__repr__(value)
    return str(value)


def _unit(unit: JsonValue) -> str:
    if not isinstance(unit, Mapping):
        return "(unit unreadable)"
    state = unit.get("knowledge")
    if state == "known":
        return str(unit.get("value"))
    if state == "ambiguous":
        candidates = unit.get("candidates")
        readings = (
            [str(c.get("value")) if isinstance(c, Mapping) else "?" for c in candidates]
            if isinstance(candidates, list | tuple)
            else []
        )
        return f"(unit ambiguous: {' | '.join(readings)})"
    return f"(unit {state})"


def _state(value: JsonValue, show: Callable[[JsonValue], str]) -> str:
    """A ``Knowledge`` state of a clock map's part, as stated."""
    if not isinstance(value, Mapping):
        return "?"
    state = value.get("knowledge")
    if state == "known":
        return show(value.get("value"))
    if state == "ambiguous" and isinstance(value.get("candidates"), list | tuple):
        candidates = value.get("candidates")
        assert isinstance(candidates, list | tuple)
        readings = [show(c.get("value")) if isinstance(c, Mapping) else "?" for c in candidates]
        return "ambiguous: " + " | ".join(readings)
    return str(state)


def _fraction(value: JsonValue) -> str:
    if isinstance(value, Mapping):
        return f"{value.get('numerator')}/{value.get('denominator')}"
    return "?"


def _anchor(value: JsonValue) -> str:
    if not isinstance(value, Mapping):
        return "?"
    source, target = value.get("source"), value.get("target")
    if not isinstance(source, Mapping) or not isinstance(target, Mapping):
        return "?"
    return f"{source.get('ticks')} -> {target.get('ticks')}"


def _bound(value: JsonValue) -> str:
    if isinstance(value, Mapping):
        return f"{value.get('ticks')} ticks"
    return "?"


def _clock_map(value: Mapping[str, JsonValue]) -> str:
    """A clock map as Memory states it (ADR 0011 §2): never applied here, only shown."""
    method = value.get("method")
    head = f"clock map onto {value.get('target')} ({method})"
    if method == "composed":
        chain = value.get("chain")
        hops = ", ".join(str(c) for c in chain) if isinstance(chain, list | tuple) else "?"
        return f"{head}: chain {hops}"
    return (
        f"{head}: anchor {_state(value.get('anchor'), _anchor)}, rate"
        f" {_state(value.get('rate'), _fraction)}, residual bound"
        f" {_state(value.get('residual_bound'), _bound)}"
    )


def claim_object(value: Mapping[str, JsonValue]) -> str:
    """A claim object as text: a node by type and id, a record by id, a literal as declared."""
    kind = value.get("kind")
    if kind == "node":
        return f"{value.get('node_type')} {value.get('node_id')}"
    if kind == "record":
        return f"record {value.get('record_id')}"
    datatype = value.get("datatype")
    literal: JsonValue = value.get("value", "")
    if datatype == "text":
        return json.dumps(literal, ensure_ascii=False)
    if datatype == "boolean":
        return "true" if literal is True else "false"
    if datatype == "quantity":
        return f"{_number(literal)} {_unit(value.get('unit', ''))}"
    if datatype == "instant" and isinstance(literal, Mapping):
        return f"instant {literal.get('ticks')} on {literal.get('domain_id')}"
    if datatype == "clock_map" and isinstance(literal, Mapping):
        return _clock_map(literal)
    if datatype == "delta" and isinstance(literal, Mapping):
        values = literal.get("values")
        shown = (
            ", ".join(_number(v) for v in values) if isinstance(values, list | tuple) else "?"
        )
        return (
            f"delta {literal.get('quantity')} {json.dumps(literal.get('name', ''))}:"
            f" [{shown}] later minus earlier ({literal.get('later')} - {literal.get('earlier')})"
        )
    return _number(literal)
