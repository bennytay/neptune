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

from neptune.model.ids import LogicalId, check_text, check_token
from neptune_memory.schema.claim import ValueType, is_inferred, object_type
from neptune_memory.schema.nodes import CONTEXT_TYPES, DECLARED_ONLY, NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping

    from neptune.model.jsonvalue import JsonObject
    from neptune_memory.schema.claim import Claim

# Bumped whenever CORE_PREDICATES changes. 2: ``same_as`` and ``same_as_candidate`` joined the
# core (ADR 0006 §4). 3: ``has_name`` joined (ADR 0007 §2), and the predicates that hold for every
# node type widened to ``stream`` and ``document`` (ADR 0008 §6). 4: the configuration lineage
# predicates joined (ADR 0010 §6). 5: the run thread predicates joined (ADR 0009 §6). 6: the
# time-domain registry's ``has_clock``, ``maps_to`` and ``clock_map`` joined, and the predicates
# that hold for every node type widened to ``clock`` (ADR 0011 §1). 7: the episode predicates
# joined (ADR 0012 §5). The vocabulary is part of graph-schema (``GRAPH_SCHEMA_VERSION``).
VOCABULARY_VERSION: Final = 7

# Time-domain registry predicates (ADR 0011). Only declared or estimated mappings, and chains of
# them, ground ``maps_to`` and ``clock_map``; no consolidator estimates an offset.
HAS_CLOCK: Final = "has_clock"
MAPS_TO: Final = "maps_to"
CLOCK_MAP: Final = "clock_map"

# Identity predicates (ADR 0003 §1). Only ``memory.identity`` grounds ``same_as``, never by
# inference; ``same_as_candidate`` is pairwise, one claim each way. The runner enforces both.
SAME_AS: Final = "same_as"
SAME_AS_CANDIDATE: Final = "same_as_candidate"

# Configuration lineage predicates (ADR 0010). Missingness is the predicate, as for identity (ADR
# 0003 §1.3): a claim object is never ``Unknown`` or ``Ambiguous``.
SUCCEEDS: Final = "succeeds"
CONFIGURATION_ACTIVE_DURING: Final = "configuration_active_during"
CONFIGURATION_CANDIDATE: Final = "configuration_candidate"
CONFIGURATION_UNKNOWN: Final = "configuration_unknown"
AUTHORISED_CONFIGURATION: Final = "authorised_configuration"
NOT_COVERED_BY_AUTHORISATION: Final = "not_covered_by_authorisation"

# Run threads (ADR 0009). A ``<predicate>_candidate`` claim is one reading of an ``Ambiguous``
# value of ``<predicate>``: a claim object cannot be ``Ambiguous`` (ADR 0003 §1.3), so the
# ambiguity is the predicate, one claim per candidate, each with its own evidence.
RECORDED_BY: Final = "recorded_by"
AT_SITE: Final = "at_site"
EXECUTES_TASK: Final = "executes_task"
HAS_MEMBER: Final = "has_member"
CONTINUES: Final = "continues"
CANDIDATE_OF: Final[Mapping[str, str]] = MappingProxyType(
    {
        RECORDED_BY: "recorded_by_candidate",
        AT_SITE: "at_site_candidate",
        EXECUTES_TASK: "executes_task_candidate",
        CONTINUES: "continues_candidate",
    }
)

# Episodes (ADR 0012). ``starts_at`` / ``ends_at`` are an episode's boundaries on one clock, each
# citing what states it; ``intervened`` names an ``Intervention`` record; ``outcome`` is a declared
# outcome, verbatim, and is never inferred. The ``_candidate`` forms are ambiguous readings; a
# start has none, since it is the earliest start the run's records state. ``starts_at`` and
# ``ends_at`` are ``many``: each claim's instant is on its own clock, so a ``one`` predicate would
# read an episode placed on two clocks as a cross-clock contradiction. Two instants on one clock
# disagree, and ``boundary_of`` reads them as ``Ambiguous``.
STARTS_AT: Final = "starts_at"
ENDS_AT: Final = "ends_at"
INTERVENED: Final = "intervened"
OUTCOME: Final = "outcome"
EPISODE_CANDIDATE_OF: Final[Mapping[str, str]] = MappingProxyType(
    {
        ENDS_AT: "ends_at_candidate",
        INTERVENED: "intervened_candidate",
    }
)



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
    # An observed or stated claim names a person by something other than a declared identifier
    # (``<namespace>:<value>``), or cites no Ledger record (ADR 0005 §5). The schema checks the
    # shape and the citation; that the identifier comes from a declaration is consolidate/'s.
    UNDECLARED_PERSON = "undeclared_person"


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
    for node in nodes:
        if node.node_type not in DECLARED_ONLY:
            continue
        if is_inferred(claim.assertion_kind):
            found.append(
                SchemaViolation(
                    ViolationCode.DECLARED_ONLY,
                    f"{node.node_type} nodes are declared only; an inferred claim cannot name"
                    f" {node.node_id!r}",
                )
            )
        else:
            problems = [
                *([] if _is_declared_identifier(node.node_id) else ["is not <namespace>:<value>"]),
                *([] if claim.provenance.records else ["is cited with no Ledger record"]),
            ]
            if problems:
                found.append(
                    SchemaViolation(
                        ViolationCode.UNDECLARED_PERSON,
                        f"{node.node_type} {node.node_id!r} {' and '.join(problems)}; a"
                        f" {claim.assertion_kind} claim names a person only by a declared"
                        " identifier from a cited Ledger record",
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


def _is_declared_identifier(node_id: str) -> bool:
    """Whether ``node_id`` is a declared logical id, ``<namespace>:<value>`` (ADR 0003 §1)."""
    namespace, colon, value = node_id.partition(":")
    if not colon:
        return False
    try:
        LogicalId(namespace, value)
    except (TypeError, ValueError):
        return False
    return is_declared_value(value)


def is_declared_value(value: str) -> bool:
    """A declared identifier's value is not blank and not padded with whitespace (ADR 0006 §9).

    The compiler's ``LogicalId`` accepts ``" "`` and ``" 4411"``; Memory does not key a node,
    or name a person, by one: a padded value is a different string that reads the same.
    """
    return bool(value.strip()) and value == value.strip()


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
    version: int = 1,
) -> PredicateSpec:
    return PredicateSpec(
        name, version, frozenset(domain), frozenset(range_), cardinality, description
    )


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
        SUCCEEDS,
        {_N.CONFIGURATION},
        {_N.CONFIGURATION},
        _MANY,
        "took over from the object on a machine's chain; valid while the subject is in force",
    ),
    _p(
        CONFIGURATION_ACTIVE_DURING,
        {_N.RUN},
        {_N.CONFIGURATION},
        _MANY,
        "a configuration the run ran with, over the bound part of the run (a snapshot binding)",
    ),
    _p(
        CONFIGURATION_CANDIDATE,
        {_N.MACHINE, _N.RUN},
        {_N.CONFIGURATION},
        _MANY,
        "ambiguous: the configuration in force could be this one; one claim per reading",
    ),
    _p(
        CONFIGURATION_UNKNOWN,
        {_N.MACHINE, _N.RUN},
        {_V.RECORD},
        _MANY,
        "no configuration is stated over the interval; the record leaves it open, never filled",
    ),
    _p(
        AUTHORISED_CONFIGURATION,
        {_N.SITE},
        {_N.CONFIGURATION},
        _MANY,
        "an authorisation envelope approves this configuration at the site over the interval",
    ),
    _p(
        NOT_COVERED_BY_AUTHORISATION,
        {_N.RUN},
        {_N.CONFIGURATION},
        _MANY,
        "observed: no authorisation envelope in the Ledger names the configuration then",
    ),
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
    _p(
        "recorded_by_candidate",
        {_N.RUN},
        {_N.MACHINE},
        _MANY,
        "ambiguous: the evidence names several machines for the run; which is undecided",
    ),
    _p("executes_task", {_N.RUN, _N.EPISODE}, {_N.TASK}, _MANY, "a task attempted"),
    _p(
        "executes_task_candidate",
        {_N.RUN, _N.EPISODE},
        {_N.TASK},
        _MANY,
        "ambiguous: the evidence names several tasks; which is undecided",
    ),
    _p("at_site", {_N.RUN}, {_N.SITE}, _ONE, "the site a run took place at, as declared"),
    _p(
        "at_site_candidate",
        {_N.RUN},
        {_N.SITE},
        _MANY,
        "ambiguous: the evidence names several sites for the run; which is undecided",
    ),
    _p(
        "has_member",
        {_N.RUN},
        {_V.RECORD},
        _MANY,
        "a source file the compiler's run assembly places in the run (its SourceRevision)",
    ),
    _p(
        "continues",
        {_N.RUN},
        {_N.RUN},
        _MANY,
        "a later part of one recording: the next part of a run its assembly states",
    ),
    _p(
        "continues_candidate",
        {_N.RUN},
        {_N.RUN},
        _MANY,
        "ambiguous: may be a later part of the same recording; the evidence does not order them",
    ),
    _p("operated_by", {_N.RUN}, {_N.PERSON}, _MANY, "a declared operator or supervisor"),
    _p("episode_of", {_N.EPISODE}, {_N.RUN}, _ONE, "the run an episode segments"),
    _p(
        "starts_at",
        {_N.EPISODE},
        {_V.INSTANT},
        _MANY,
        "where an episode starts, as its records state it: one claim per clock, never two on one",
    ),
    _p(
        "ends_at",
        {_N.EPISODE},
        {_V.INSTANT},
        _MANY,
        "where an episode ends, as its records state it: one claim per clock, never two on one",
    ),
    _p(
        "ends_at_candidate",
        {_N.EPISODE},
        {_V.INSTANT},
        _MANY,
        "ambiguous: may end here (a stated end, or a stop event inside it); which is undecided",
    ),
    _p(
        "intervened",
        {_N.EPISODE},
        {_V.RECORD},
        _MANY,
        "a human intervention during the episode (an Intervention record, by id)",
    ),
    _p(
        "intervened_candidate",
        {_N.EPISODE},
        {_V.RECORD},
        _MANY,
        "ambiguous: the intervention may have been during the episode; the evidence does not say",
    ),
    _p(
        "outcome",
        {_N.EPISODE},
        {_V.TEXT},
        _ONE,
        "the outcome a record declares for the episode, verbatim; never inferred",
    ),
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
        version=3,  # 2: every node type includes stream and document; 3: and clock
    ),
    _p(
        "has_name",
        set(NodeType),
        {_V.TEXT},
        _ONE,
        "a declared display name, verbatim; never an identifier",
        version=2,  # 2: every node type includes clock
    ),
    _p(
        SAME_AS,
        set(NodeType),
        set(NodeType),
        _MANY,
        "the same real-world thing: declared identifier, configuration lineage or operator",
        version=3,  # 2: every node type includes stream and document; 3: and clock
    ),
    _p(
        SAME_AS_CANDIDATE,
        set(NodeType),
        set(NodeType),
        _MANY,
        "ambiguous: the evidence could mean either; whether they are one thing is undecided",
        version=3,  # 2: stream and document, ambiguous identity links; 3: every type incl. clock
    ),
    _p(
        HAS_CLOCK,
        {_N.MACHINE},
        {_N.CLOCK},
        _MANY,
        "a clock the machine's records carry, over the interval they observe it",
    ),
    _p(
        MAPS_TO,
        {_N.CLOCK},
        {_N.CLOCK},
        _MANY,
        "a declared or estimated mapping, or a chain of them, takes its ticks to another clock's",
    ),
    _p(
        CLOCK_MAP,
        {_N.CLOCK},
        {_V.CLOCK_MAP},
        _MANY,
        "a maps_to's parameters as the evidence states them, or the chain it composes",
    ),
)
