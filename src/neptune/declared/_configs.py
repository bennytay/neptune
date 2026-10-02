"""Manifests: a configuration document whose root names sections of declarations (ADR 0063 §2).

A YAML, JSON or TOML document whose root is a mapping may hold sections: ``site`` or ``sites``,
``assets``, ``tasks``, ``requirements`` and ``work_orders`` (singular or plural, keys compared by
``field_key``). A section is one mapping or a sequence of mappings, each an entry citing its
JSON pointer. Nesting is the only relation read from structure: a site's own ``assets`` are at
that site, and a task's own ``requirements`` are for that task. Neptune's own manifest (a root
``neptune`` key, ADR 0047) is not read here.
"""

import math
from collections.abc import Sequence
from typing import Final

from neptune.declared._emit import Output, field_key
from neptune.declared._entries import Value, build
from neptune.model.configuration import (
    CollectionType,
    ConfigCollection,
    ConfigScalar,
    ConfigurationSnapshot,
    ConfigurationValue,
    ScalarType,
)
from neptune.model.finding import FindingCategory
from neptune.model.ids import LogicalId
from neptune.model.knowledge import Knowledge, Known, KnownAbsent, Unknown
from neptune.model.provenance import EvidenceRef, Provenance
from neptune.model.scalars import NonFinite

_SECTIONS: Final = {
    "site": "site",
    "sites": "site",
    "asset": "asset",
    "assets": "asset",
    "task": "task",
    "tasks": "task",
    "requirement": "requirement",
    "requirements": "requirement",
    "work_order": "work_order",
    "work_orders": "work_order",
}
# What an entry of each kind may hold nested, and what the nested entries inherit from it.
_NESTED: Final = {"site": ("asset", "site"), "task": ("requirement", "task")}


def _double(number: float) -> float | None:
    """A number as a double, or ``None`` when no finite double holds it (a 400-digit int)."""
    try:
        value = float(number)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _unknown(out: Output, evidence: EvidenceRef) -> Knowledge[object]:
    return Unknown(out.prov(evidence))


class _Document:
    def __init__(
        self, out: Output, snapshot: ConfigurationSnapshot, values: Sequence[ConfigurationValue]
    ) -> None:
        self.out, self.snapshot = out, snapshot
        self.nodes: dict[tuple[str | int, ...], ConfigurationValue] = {}
        self.children: dict[tuple[str | int, ...], list[ConfigurationValue]] = {}
        # A repeated key is the configuration adapter's finding; only first entries are read.
        for value in sorted(values, key=lambda v: (len(v.path), v.order, v.id)):
            if any(value.occurrence) or value.path in self.nodes:
                continue
            self.nodes[value.path] = value
            if value.path:
                self.children.setdefault(value.path[:-1], []).append(value)

    def collection(self, value: ConfigurationValue) -> CollectionType | None:
        node = value.value
        if isinstance(node, Known) and isinstance(node.value, ConfigCollection):
            return node.value.type
        return None

    def value(self, node: ConfigurationValue) -> Value:
        evidence = node.provenance.evidence
        state = node.value
        if isinstance(state, KnownAbsent) and isinstance(state.provenance, Provenance):
            return Value(state.provenance.evidence, absent=True)
        if not isinstance(state, Known):
            return Value(evidence)
        found = state.value
        if isinstance(found, ConfigCollection):
            if found.type is CollectionType.SEQUENCE:
                items = tuple(self.value(child) for child in self.children.get(node.path, []))
                return Value(evidence, items=items)
            return Value(evidence, not_text=True)
        if not isinstance(found, ConfigScalar):  # an alias: a reference, never expanded
            return Value(evidence, not_text=True)
        written = node.text.value if isinstance(node.text, Known) else None
        if found.type is ScalarType.STRING:
            return (
                Value(evidence, found.value or None)
                if isinstance(found.value, str)
                else Value(evidence)
            )
        if found.type is ScalarType.BOOL or found.type is ScalarType.BINARY:
            return Value(evidence, not_text=True)
        if found.type is ScalarType.INT and isinstance(found.value, int):
            return Value(evidence, written, number=_double(found.value))
        if found.type is ScalarType.FLOAT:
            number = found.value
            usable = isinstance(number, float) and not isinstance(number, NonFinite)
            double = _double(number) if usable and isinstance(number, float) else None
            return Value(evidence, written, number=double)
        return Value(evidence, written)  # a date or time: its text as written

    def entry(self, node: ConfigurationValue) -> dict[str, Value]:
        entry: dict[str, Value] = {}
        for child in self.children.get(node.path, []):
            key = child.path[-1]
            if not isinstance(key, str):
                continue
            name = field_key(key)
            if name == "location" and self.collection(child) is CollectionType.MAPPING:
                for part in self.children.get(child.path, []):
                    if isinstance(part.path[-1], str):
                        entry.setdefault(field_key(part.path[-1]), self.value(part))
                continue
            entry.setdefault(name, self.value(child))
        return entry

    def section(
        self,
        kind: str,
        node: ConfigurationValue,
        site: Knowledge[LogicalId] | None = None,
        task: Knowledge[LogicalId] | None = None,
    ) -> None:
        shape = self.collection(node)
        if shape is CollectionType.MAPPING:
            entries = [node]
        elif shape is CollectionType.SEQUENCE:
            entries = self.children.get(node.path, [])
        else:
            entries = []
        if not entries or any(self.collection(e) is not CollectionType.MAPPING for e in entries):
            if shape is not None and not entries:
                return  # an empty section declares nothing
            self.out.finding(
                "section_not_entries",
                FindingCategory.UNSUPPORTED,
                node.provenance.evidence,
                f"the {kind} section is not a mapping or a sequence of mappings; what is not a"
                " mapping is not read",
                {"kind": kind},
            )
        for item in entries:
            if self.collection(item) is not CollectionType.MAPPING:
                continue
            entry = self.entry(item)
            record = build(
                self.out,
                kind,
                entry,
                item.provenance.evidence,
                self.snapshot.id,
                _unknown,
                ("id", f"{kind}_id"),
                site=site,
                task=task,
            )
            if record is None:
                continue
            self.out.add(record)
            nested = _NESTED.get(kind)
            if nested is None:
                continue
            child_kind, role = nested
            own = _own_id(self.out, entry, role)
            for child in self.children.get(item.path, []):
                if not isinstance(child.path[-1], str) or self.collection(child) is None:
                    continue
                if _SECTIONS.get(field_key(child.path[-1])) != child_kind:
                    continue
                if not _holds_entries(self, child):
                    continue  # a list of ids (a task's assets) is a reference, read above
                self.section(
                    child_kind,
                    child,
                    site=own if role == "site" else site,
                    task=own if role == "task" else task,
                )


def _holds_entries(document: _Document, node: ConfigurationValue) -> bool:
    if document.collection(node) is CollectionType.MAPPING:
        return True
    items = document.children.get(node.path, [])
    return bool(items) and all(document.collection(i) is CollectionType.MAPPING for i in items)


def _own_id(out: Output, entry: dict[str, Value], role: str) -> Knowledge[LogicalId] | None:
    """The id an enclosing entry states, citing where: what its nested entries inherit."""
    value = entry.get("id") or entry.get(f"{role}_id")
    if value is None or value.text is None:
        return None
    return out.known(LogicalId(role, value.text), value.evidence)


def read_config(
    out: Output, snapshot: ConfigurationSnapshot, values: Sequence[ConfigurationValue]
) -> None:
    document = _Document(out, snapshot, values)  # a snapshot is one document (ADR 0037 §2)
    root = document.nodes.get(())
    if root is None or document.collection(root) is not CollectionType.MAPPING:
        return
    keys = {
        field_key(child.path[-1]): child
        for child in document.children.get((), [])
        if isinstance(child.path[-1], str)
    }
    if "neptune" in keys:
        return  # Neptune's own manifest: ADR 0047 reads it
    for key, child in sorted(keys.items(), key=lambda item: item[1].order):
        kind = _SECTIONS.get(key)
        if kind is not None:
            document.section(kind, child)
