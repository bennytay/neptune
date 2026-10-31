"""``layout.yaml``, the declared site, becomes stated facts citing its lines (spec §5, §9).

The file (``site/corpus/static.py`` writes it)::

    site: {id: site:p7, name: Plant 7 assembly hall}
    clock: {name: P7 local, zone: Europe/Berlin}      # stated, never applied
    areas:                                           # also cells, zones, machines, robots
      - {id: area:assembly, name: Assembly, part_of: site:p7}
      - {id: robot:arm-c3, name: Arm C3, located_in: cell:c3}
    topology:                                        # feeds / depends_on edges, one per item
      - {subject: machine:cv-2, predicate: feeds, object: cell:c3}
    sources:
      - {id: plc.cv2, name: CV-2 PLC edge node, report_interval: 5}

Every list of mappings with an ``id`` is a list of entities; ``part_of`` / ``located_in`` are one
ref, ``feeds`` / ``depends_on`` / ``covers_zone`` a ref or a list of refs, ``metric`` / ``target``
a value. ``mission.yaml`` (the site's goals and constraints) is read the same way::

    goals:
      - {id: goal:c3-output, name: Cell C3 output, metric: parts out per shift, target: ">= 2400"}
    constraints:
      - {id: constraint:headcount, name: No added headcount}
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
_VALUES = ("metric", "target")
MISSION = "mission.yaml"

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
    for section, node in top.items():
        if section in ("site", "clock", "sources", "topology"):
            continue
        if not isinstance(node, yaml.SequenceNode):
            continue
        for entity in _items(node):
            fields = _mapping(entity)
            if "id" not in fields:
                continue
            subject = _scalar(fields["id"])
            for key, value in fields.items():
                if key == "name":
                    out.append(fact(subject, "name", Value(_scalar(value)), value))
                elif key in _VALUES:
                    out.append(fact(subject, key, Value(_scalar(value)), value))
                elif key in _ONE:
                    out.append(fact(subject, key, Ref(_scalar(value)), value))
                elif key in _MANY:
                    items = _items(value) if isinstance(value, yaml.SequenceNode) else [value]
                    out.extend(fact(subject, key, Ref(_scalar(item)), item) for item in items)
    for edge in _items(top.get("topology")):
        fields = _mapping(edge)
        predicate = _scalar(fields["predicate"])
        if predicate in _ONE or predicate in _MANY:
            obj = Ref(_scalar(fields["object"]))
            out.append(fact(_scalar(fields["subject"]), predicate, obj, edge))
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
