"""Compact cited text for an ``Answer`` (spec §8): one line per item, then a ``Sources:`` footer.

    - machine:cv-2 mode = "stopped" since 10:05:02 · observed [S1]

A time on the answer's own date shows as its clock time; any other shows in full. Nothing is
converted: times are the site clock's, as written.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from neptune_memory.site.facts import Ref, Value

if TYPE_CHECKING:
    from neptune_memory.site.query import Answer, Item


def render(answer: Answer) -> str:
    """The answer as compact lines with citation keys and a ``Sources:`` footer."""
    head = [answer.tool]
    if answer.at is not None:
        head.append(f"at {answer.at}")
    head.append(f"log seq {answer.as_of_seq}")
    lines = [" · ".join(head)]
    day = (answer.at or "")[:10]
    lines += [_item(item, day) for item in answer.items]
    if answer.columns:
        lines.append(" | ".join(answer.columns))
        lines += [" | ".join(_cell(v) for v in row) for row in answer.rows]
    if not answer.items and not answer.rows:
        lines.append("(nothing)")
    lines += [f"! {gap}" for gap in answer.gaps]
    if answer.omitted:
        lines.append(f"! {answer.omitted} more items not shown; ask a narrower question")
    if answer.sources:
        lines.append("Sources:")
        for key, citation in answer.sources.items():
            quote = "" if citation.quote is None else f" {_quoted(citation.quote)}"
            lines.append(f"[{key}] {citation.file} {citation.locator}{quote}")
    return "\n".join(lines) + "\n"


def _item(item: Item, day: str) -> str:
    text = f"- {item.subject} {item.predicate}"
    if item.object is not None:
        text += f" = {_object(item.object)}"
    if item.valid_from is not None:
        text += f" since {_time(item.valid_from, day)}"
    if item.valid_to is not None:
        text += f" until {_time(item.valid_to, day)}"
    tags: list[str] = [t for t in (item.kind, None if item.state == "known" else item.state) if t]
    if item.relation is not None:
        tags.append(item.relation)
    if tags:
        text += " · " + ", ".join(tags)
    if item.cite:
        text += " " + "".join(f"[{key}]" for key in item.cite)
    if item.fact_id is not None:
        text += f" ({item.fact_id})"
    return text


def _object(obj: Ref | Value) -> str:
    if isinstance(obj, Ref):
        return obj.ref
    value = _quoted(obj.value) if isinstance(obj.value, str) else str(obj.value)
    return value if obj.unit is None else f"{value} {obj.unit}"


def _time(t: str, day: str) -> str:
    return t[11:] if day and t.startswith(day + "T") else t


def _quoted(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _cell(value: Any) -> str:
    return "NULL" if value is None else str(value)
