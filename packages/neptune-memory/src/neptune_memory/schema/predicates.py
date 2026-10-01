"""The controlled, extensible predicate vocabulary (ADR 0002 §5).

Every predicate is registered with its domain (subject node types), range (object node or value
types), cardinality and a version. A claim with an unregistered predicate, or a subject or object
outside the predicate's types, is refused. Extending the vocabulary adds names; a new version of an
existing name may only widen its domain and range, so every claim valid before stays valid.

``cardinality`` drives superseding: a ``one`` predicate holds at most one object per subject at any
valid instant, so a different object over an overlapping interval contradicts; ``many`` never does.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from neptune.model.ids import check_text, check_token
from neptune_memory.schema.claim import ValueType, is_inferred, object_type
from neptune_memory.schema.nodes import CONTEXT_TYPES, DECLARED_ONLY, NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonObject
    from neptune_memory.schema.claim import Claim

# Bumped whenever CORE_PREDICATES changes. GRAPH_SCHEMA_VERSION (pins.py) is published by MVL-105.
VOCABULARY_VERSION: Final = 1


class Cardinality(StrEnum):
    ONE = "one"  # at most one object per subject at any valid instant: contradictions supersede
    MANY = "many"  # any number; claims only end by their own valid_to


@dataclass(frozen=True)
class PredicateSpec:
    name: str
    version: int
    domain: frozenset[NodeType]
    range: frozenset[NodeType | ValueType]
    cardinality: Cardinality
    description: str

    def __post_init__(self) -> None:
        check_token("name", self.name)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError(f"version must be an int >= 1: {self.version!r}")
        if not isinstance(self.domain, frozenset) or not self.domain:
            raise ValueError(f"{self.name}: domain must be a non-empty frozenset")
        if not all(isinstance(t, NodeType) for t in self.domain):
            raise TypeError(f"{self.name}: domain holds node types only")
        if not isinstance(self.range, frozenset) or not self.range:
            raise ValueError(f"{self.name}: range must be a non-empty frozenset")
        if not all(isinstance(t, NodeType | ValueType) for t in self.range):
            raise TypeError(f"{self.name}: range holds node or value types only")
        if not isinstance(self.cardinality, Cardinality):
            raise TypeError(f"{self.name}: cardinality must be a Cardinality")
        check_text("description", self.description)

    def widens(self, older: PredicateSpec) -> bool:
        """Whether this spec may replace ``older``: same name and cardinality, newer, only wider."""
        return (
            self.name == older.name
            and self.version > older.version
            and self.cardinality is older.cardinality
            and self.domain >= older.domain
            and self.range >= older.range
        )

    def to_json(self) -> JsonObject:
        return {
            "cardinality": str(self.cardinality),
            "description": self.description,
            "domain": sorted(str(t) for t in self.domain),
            "name": self.name,
            "range": sorted(str(t) for t in self.range),
            "version": self.version,
        }


class UnknownPredicateError(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"predicate {name!r} is not registered")
        self.name = name


@dataclass(frozen=True)
class PredicateRegistry:
    """An immutable vocabulary, ordered by name. Build a larger one with ``extend``."""

    specs: tuple[PredicateSpec, ...]

    def __post_init__(self) -> None:
        names = [spec.name for spec in self.specs]
        if names != sorted(set(names)):
            raise ValueError(f"predicates must be unique and sorted by name: {names}")

    @cached_property
    def _by_name(self) -> Mapping[str, PredicateSpec]:
        return MappingProxyType({spec.name: spec for spec in self.specs})

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def spec(self, name: str) -> PredicateSpec:
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownPredicateError(name) from None

    def extend(self, *specs: PredicateSpec) -> PredicateRegistry:
        """A registry with ``specs`` added; a known name must ``widen`` its registered spec."""
        merged = {spec.name: spec for spec in self.specs}
        for spec in specs:
            old = merged.get(spec.name)
            if old is not None and not spec.widens(old):
                raise ValueError(
                    f"{spec.name} v{spec.version} does not widen v{old.version}: a new version may"
                    " only widen domain and range, keeping cardinality; use a new name instead"
                )
            merged[spec.name] = spec
        return PredicateRegistry(tuple(merged[name] for name in sorted(merged)))

    def to_json(self) -> JsonObject:
        return {"predicates": [spec.to_json() for spec in self.specs]}


# --- Validation -------------------------------------------------------------------------------


class ViolationCode(StrEnum):
    UNKNOWN_PREDICATE = "unknown_predicate"
    SUBJECT_TYPE = "subject_type"
    OBJECT_TYPE = "object_type"
    DECLARED_ONLY = "declared_only"  # an inferred claim names a declared-only node (a person)


@dataclass(frozen=True)
class SchemaViolation:
    code: ViolationCode
    message: str


class ClaimSchemaError(ValueError):
    def __init__(self, violations: tuple[SchemaViolation, ...]) -> None:
        super().__init__("; ".join(f"{v.code}: {v.message}" for v in violations))
        self.violations = violations


def violations(claim: Claim, registry: PredicateRegistry) -> tuple[SchemaViolation, ...]:
    """Every way ``claim`` breaks the vocabulary; empty when it conforms."""
    found: list[SchemaViolation] = []
    nodes = [claim.subject, *([claim.object] if isinstance(claim.object, NodeRef) else [])]
    if is_inferred(claim.assertion_kind):
        for node in nodes:
            if node.node_type in DECLARED_ONLY:
                found.append(
                    SchemaViolation(
                        ViolationCode.DECLARED_ONLY,
                        f"{node.node_type} nodes are declared only; an inferred claim cannot name"
                        f" {node.node_id!r}",
                    )
                )
    if claim.predicate not in registry:
        found.append(
            SchemaViolation(
                ViolationCode.UNKNOWN_PREDICATE, f"{claim.predicate!r} is not registered"
            )
        )
        return tuple(found)
    spec = registry.spec(claim.predicate)
    if claim.subject.node_type not in spec.domain:
        found.append(
            SchemaViolation(
                ViolationCode.SUBJECT_TYPE,
                f"{spec.name} takes subjects {sorted(spec.domain)}, got {claim.subject.node_type}",
            )
        )
    kind = object_type(claim.object)
    if kind not in spec.range:
        found.append(
            SchemaViolation(
                ViolationCode.OBJECT_TYPE,
                f"{spec.name} takes objects {sorted(spec.range)}, got {kind}",
            )
        )
    return tuple(found)


def check_claim(claim: Claim, registry: PredicateRegistry) -> None:
    """Refuse a claim that breaks the vocabulary: ``ClaimSchemaError`` lists every violation."""
    found = violations(claim, registry)
    if found:
        raise ClaimSchemaError(found)


# --- The core vocabulary ----------------------------------------------------------------------

_N = NodeType
_V = ValueType
_ONE, _MANY = Cardinality.ONE, Cardinality.MANY


def _p(
    name: str,
    domain: set[NodeType] | frozenset[NodeType],
    range_: set[NodeType | ValueType],
    cardinality: Cardinality,
    description: str,
) -> PredicateSpec:
    return PredicateSpec(name, 1, frozenset(domain), frozenset(range_), cardinality, description)


CORE_PREDICATES: Final = PredicateRegistry(()).extend(
    _p("located_at", {_N.MACHINE, _N.ASSET}, {_N.SITE, _N.ZONE}, _ONE, "where it is"),
    _p("zone_of", {_N.ZONE}, {_N.SITE}, _ONE, "the site a zone belongs to"),
    _p("mounted_on", {_N.SENSOR}, {_N.MACHINE, _N.ASSET}, _ONE, "what a sensor is attached to"),
    _p("has_calibration", {_N.SENSOR}, {_N.CONFIGURATION}, _ONE, "the calibration in force"),
    _p(
        "has_configuration",
        {_N.MACHINE, _N.SENSOR, _N.DEPLOYMENT},
        {_N.CONFIGURATION},
        _MANY,
        "a parameter set, description file or other configuration in force",
    ),
    _p(
        "runs_software", {_N.MACHINE, _N.SENSOR}, {_N.SOFTWARE_VERSION}, _MANY, "installed software"
    ),
    _p("runs_model", {_N.MACHINE}, {_N.MODEL_VERSION}, _MANY, "a learned model it runs"),
    _p(
        "governed_by",
        {_N.MACHINE, _N.SITE, _N.DEPLOYMENT, _N.FLEET},
        {_N.POLICY},
        _MANY,
        "an operating rule or control policy that applies",
    ),
    _p("member_of_fleet", {_N.MACHINE}, {_N.FLEET}, _ONE, "the fleet a machine belongs to"),
    _p("deployed_at", {_N.DEPLOYMENT}, {_N.SITE}, _ONE, "where a deployment takes place"),
    _p(
        "part_of_programme", {_N.DEPLOYMENT, _N.FLEET}, {_N.PROGRAMME}, _ONE, "the owning programme"
    ),
    _p("recorded_by", {_N.RUN}, {_N.MACHINE}, _ONE, "the machine whose log a run is"),
    _p("executes_task", {_N.RUN, _N.EPISODE}, {_N.TASK}, _MANY, "a task attempted"),
    _p("operated_by", {_N.RUN}, {_N.PERSON}, _MANY, "a declared operator or supervisor"),
    _p("episode_of", {_N.EPISODE}, {_N.RUN}, _ONE, "the run an episode segments"),
    _p(
        "maintenance_state",
        {_N.MACHINE, _N.SENSOR, _N.ASSET},
        {_V.TEXT},
        _ONE,
        "serviceability as a record states it, verbatim",
    ),
    _p("rated_payload", {_N.MACHINE}, {_V.QUANTITY}, _ONE, "rated payload, unit as declared"),
    _p("has_summary", CONTEXT_TYPES, {_V.TEXT}, _ONE, "a context node's summary"),
    _p(
        "evidenced_by",
        set(NodeType),
        {_V.RECORD},
        _MANY,
        "a Ledger record about the node (Episode tier, by id)",
    ),
)
