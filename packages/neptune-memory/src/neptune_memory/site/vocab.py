"""The site vocabulary (spec §4): entity types, predicates, and what each entity type should have.

Mappers only emit predicates named here; views read cardinality and expectations from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ENTITY_TYPES: tuple[str, ...] = (
    "site",
    "area",
    "cell",
    "zone",
    "robot",
    "machine",
    "person",
    "role",
    "qualification",
    "shift",
    "task",
    "work_order",
    "change",
    "document",
    "rule",
    "source",
)


@dataclass(frozen=True)
class Predicate:
    """A predicate: the entity types it applies to (``()`` = any), its object and cardinality."""

    name: str
    subjects: tuple[str, ...]
    object: Literal["ref", "value"]
    cardinality: Literal["one", "many"]


_PLACES = ("area", "cell", "zone")
_ASSETS = ("robot", "machine")
_WO = ("work_order",)

PREDICATES: dict[str, Predicate] = {
    p.name: p
    for p in (
        Predicate("part_of", _PLACES, "ref", "one"),
        Predicate("located_in", _ASSETS, "ref", "one"),
        Predicate("feeds", ("machine", "cell"), "ref", "many"),
        Predicate("depends_on", (), "ref", "many"),
        Predicate("covers_zone", ("rule", "role"), "ref", "many"),
        # A task can be handed to several robots at once (two AMRs on one order), so `many`.
        Predicate("assigned_to", ("task",), "ref", "many"),
        Predicate("name", (), "value", "many"),
        Predicate("serial", _ASSETS, "value", "one"),
        Predicate("mode", ("robot", "machine", "cell"), "value", "one"),
        Predicate("health", _ASSETS, "value", "one"),
        Predicate("current_task", ("robot",), "ref", "one"),
        Predicate("in_zone", ("robot",), "ref", "one"),
        Predicate("battery", ("robot",), "value", "one"),
        Predicate("setpoint", ("machine",), "value", "one"),
        Predicate("belt_tension", ("machine",), "value", "one"),
        Predicate("temperature", _ASSETS, "value", "one"),
        Predicate("firmware", _ASSETS, "value", "one"),
        Predicate("parts_out", ("cell",), "value", "one"),
        Predicate("task_state", ("task",), "value", "one"),
        Predicate("has_role", ("person",), "ref", "many"),
        Predicate("holds_qualification", ("person",), "ref", "many"),
        Predicate("on_shift", ("person",), "ref", "many"),
        Predicate("on_break", ("person",), "value", "many"),
        Predicate("requires_qualification", ("rule", "document"), "ref", "many"),
        Predicate("states_limit", ("document",), "value", "many"),
        Predicate("rule_text", ("rule",), "value", "one"),
        Predicate("work_order", _ASSETS, "ref", "many"),
        Predicate("wo_action", _WO, "value", "one"),
        Predicate("wo_cause", _WO, "value", "one"),
        Predicate("wo_by", _WO, "ref", "one"),
        Predicate("wo_loto", _WO, "value", "one"),
        Predicate("wo_opened", _WO, "value", "one"),
        Predicate("wo_closed", _WO, "value", "one"),
        Predicate("wo_problem", _WO, "value", "one"),
        Predicate("wo_note", _WO, "value", "one"),
        Predicate("change", _ASSETS, "ref", "many"),
        Predicate("change_from", ("change",), "value", "one"),
        Predicate("change_to", ("change",), "value", "one"),
        Predicate("change_by", ("change",), "ref", "one"),
        Predicate("change_reason", ("change",), "value", "one"),
        Predicate("note", (), "value", "many"),
        Predicate("finding", (), "value", "many"),  # inferred by site/insights.py
        Predicate("same_as", (), "ref", "many"),
        Predicate("report_interval", ("source",), "value", "one"),
        # An unmapped file (spec §12): its rows or blocks verbatim, and the "not mapped" marker.
        Predicate("text", (), "value", "many"),
        Predicate("unmapped", ("document",), "value", "one"),
    )
}

TOPOLOGY: tuple[str, ...] = (
    "feeds",
    "depends_on",
    "located_in",
    "part_of",
    "assigned_to",
    "covers_zone",
)
"""The predicates the topology view walks (spec §6)."""

EXPECTED: dict[str, tuple[str, ...]] = {
    "robot": ("mode", "health", "battery"),
    "machine": ("mode", "health", "belt_tension"),
    "cell": ("parts_out",),
}
"""Per entity type, the predicates a view reports as ``unknown`` when no source states them."""


def ref(entity_type: str, entity_id: str) -> str:
    """``"<type>:<id>"``, the id kept as the source writes it."""
    if entity_type not in ENTITY_TYPES:
        raise ValueError(f"not a site entity type: {entity_type!r}")
    return f"{entity_type}:{entity_id}"


def entity_type(entity: str) -> str:
    """The type part of an entity ref."""
    return entity.split(":", 1)[0]


def is_one(predicate: str) -> bool:
    """True when two current facts for this predicate can disagree (cardinality ``one``)."""
    return PREDICATES[predicate].cardinality == "one"
