"""``layout.yaml``, the declared site, becomes stated facts citing its lines (spec §5).

The file::

    site: {id: site:p7, name: Plant 7 assembly hall}
    clock: {name: P7 local, zone: Europe/Berlin}      # stated, never applied
    entities:
      - id: machine:cv-2
        name: Conveyor CV-2
        located_in: cell:c3                          # part_of / located_in: one ref
        feeds: [cell:c3]                             # feeds / depends_on / covers_zone: ref or list
    sources:
      - {id: plc.cv2, file: feed/plc_cv2.jsonl, report_interval: 5}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from neptune_memory.site.facts import Citation, Fact, Ref, Value

if TYPE_CHECKING:
    from pathlib import Path

STATIC = "static"
SOURCE = "layout"
_ONE = ("part_of", "located_in")
_MANY = ("feeds", "depends_on", "covers_zone")

_Node = yaml.Node


def map_layout(path: Path) -> list[Fact]:
    """Every entity, edge, name and source interval ``layout.yaml`` declares, in file order."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    file = f"{STATIC}/{path.name}"
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        return []

    def fact(subject: str, predicate: str, obj: Ref | Value, node: _Node) -> Fact:
        line = node.start_mark.line
        cite = Citation(file, f"line {line + 1}", lines[line].strip())
        return Fact(subject, predicate, obj, None, None, "stated", SOURCE, (cite,))

    out: list[Fact] = []
    top = _mapping(root)
    if "site" in top:
        site = _mapping(top["site"])
        if "name" in site:
            out.append(
                fact(_scalar(site["id"]), "name", Value(_scalar(site["name"])), site["name"])
            )
    for entity in _items(top.get("entities")):
        fields = _mapping(entity)
        subject = _scalar(fields["id"])
        for key, node in fields.items():
            if key == "name":
                out.append(fact(subject, "name", Value(_scalar(node)), node))
            elif key in _ONE:
                out.append(fact(subject, key, Ref(_scalar(node)), node))
            elif key in _MANY:
                for item in _items(node) if isinstance(node, yaml.SequenceNode) else [node]:
                    out.append(fact(subject, key, Ref(_scalar(item)), item))
    for source in _items(top.get("sources")):
        fields = _mapping(source)
        if "report_interval" in fields:
            node = fields["report_interval"]
            seconds = yaml.safe_load(_scalar(node))
            subject = f"source:{_scalar(fields['id'])}"
            out.append(fact(subject, "report_interval", Value(seconds, "s"), node))
    return out


def _mapping(node: _Node) -> dict[str, _Node]:
    assert isinstance(node, yaml.MappingNode), node
    return {str(key.value): value for key, value in node.value}


def _items(node: _Node | None) -> list[_Node]:
    if node is None:
        return []
    assert isinstance(node, yaml.SequenceNode), node
    return list(node.value)


def _scalar(node: _Node) -> str:
    assert isinstance(node, yaml.ScalarNode), node
    return str(node.value)
