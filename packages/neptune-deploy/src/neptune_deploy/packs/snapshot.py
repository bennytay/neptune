"""A memory snapshot: one frozen graph document of Memory's published graph-schema (ADR 0013 §2).

Deploy reads Memory only through ``contracts/graph-schema``: the ``#/$defs/Graph`` document
(``kind: memory.graph``) with its claims, resolver findings, head and generation. It never imports
Memory. The reader is strict about the shapes the compiler uses and keeps every claim's JSON
exactly as read, so a pack re-exports claims byte for byte. Two things are deliberately open, so
that a later minor release of the contract still reads: a node type is any token (graph-schema
1.6.0 adds ``event``), and a locator step is any object with a ``kind``, as the Ledger's catalog
reads it.

The contract names a graph by its ``head`` (the Ledger transaction it was resolved at) and its
``generation`` (the resolver configuration's hash); neither names the claim set, since two Ledgers
at one head differ. A snapshot's id is therefore the sha256 of the document's canonical JSON:
``snapshot:sha256:<hex>``.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Final, Literal, TypeAlias

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs._read import (
    CLAIM_ID,
    FINDING_ID,
    RECORD_ID,
    SHA256,
    TOKEN,
    Reader,
    child,
    parse_document,
)
from neptune_deploy.packs.errors import PackError

GRAPH_SCHEMA_MAJOR: Final = 1
SNAPSHOT_PREFIX: Final = "snapshot:"
MAX_SNAPSHOT_BYTES: Final = 512 * 1024 * 1024
ASSERTION_KINDS: Final = ("inferred", "observed", "stated")
LITERAL_TYPES: Final = ("boolean", "instant", "integer", "quantity", "real", "text")

_R: Final = Reader("snapshot_malformed")


@dataclass(frozen=True, order=True)
class Node:
    """A graph node: ``NodeRef`` without its ``kind`` tag."""

    node_type: str
    node_id: str

    def to_json(self) -> JsonObject:
        return {"kind": "node", "node_id": self.node_id, "node_type": self.node_type}


@dataclass(frozen=True, order=True)
class Stamp:
    """A ``Timestamp``: ticks on one clock (a ``TimestampDomain`` or Memory civil clock id)."""

    domain: str
    ticks: int

    def to_json(self) -> JsonObject:
        return {"domain_id": self.domain, "ticks": self.ticks}


OPEN: Final = "open"
End: TypeAlias = Stamp | Literal["open"]


@dataclass(frozen=True)
class Interval:
    """A valid interval ``[start, end)``; ``end`` is ``"open"`` when the claim states no end."""

    start: Stamp
    end: End

    def to_json(self) -> JsonObject:
        return {
            "end": self.end if isinstance(self.end, str) else self.end.to_json(),
            "start": self.start.to_json(),
        }

    @property
    def clock(self) -> str | None:
        """The one clock both bounds are on, or ``None`` when they are on two."""
        if isinstance(self.end, Stamp) and self.end.domain != self.start.domain:
            return None
        return self.start.domain

    def sort_key(self) -> tuple[str, int, int, int]:
        end = (1, 0) if isinstance(self.end, str) else (0, self.end.ticks)
        return (self.start.domain, self.start.ticks, *end)

    def overlaps(self, other: "Interval") -> bool:
        """Both on one clock and sharing a tick. Never true across clocks."""
        clock = self.clock
        if clock is None or clock != other.clock:
            return False
        before_other_ends = isinstance(other.end, str) or self.start.ticks < other.end.ticks
        after_other_starts = isinstance(self.end, str) or self.end.ticks > other.start.ticks
        return before_other_ends and after_other_starts


@dataclass(frozen=True)
class Claim:
    """One claim version as read; ``raw`` is its JSON exactly as the snapshot holds it."""

    id: str
    subject: Node
    predicate: str
    object: JsonObject
    valid: Interval
    assertion_kind: str
    recorded_at: int
    current: bool
    evidence: tuple[JsonObject, ...]
    records: tuple[str, ...]
    raw: JsonObject = field(compare=False, repr=False)

    @property
    def object_node(self) -> Node | None:
        if self.object.get("kind") != "node":
            return None
        return Node(str(self.object["node_type"]), str(self.object["node_id"]))

    @property
    def object_record(self) -> str | None:
        if self.object.get("kind") != "record":
            return None
        return str(self.object["record_id"])

    @property
    def inferred(self) -> bool:
        return self.assertion_kind == "inferred"

    @cached_property
    def object_key(self) -> bytes:
        """The object's canonical bytes: two claims state the same object iff these are equal."""
        return canonical_json.dumps(self.object)


@dataclass(frozen=True)
class ResolutionFinding:
    """A resolver finding (``clock_mismatch``, ``overridden_on_arrival``) as read."""

    id: str
    code: str
    claim: str
    others: tuple[str, ...]
    current: bool
    raw: JsonObject = field(compare=False, repr=False)


@dataclass(frozen=True)
class Snapshot:
    """A frozen graph document and what the compiler reads from it."""

    id: str
    head: int
    generation: str
    vocabulary_version: int
    cardinality: Mapping[str, str]  # predicate -> "one" | "many", from the resolver's vocabulary
    claims: tuple[Claim, ...]  # every version, in document order
    findings: tuple[ResolutionFinding, ...]

    @cached_property
    def current(self) -> tuple[Claim, ...]:
        """Current claims at the head, one per id, ordered by id."""
        by_id = {claim.id: claim for claim in self.claims if claim.current}
        return tuple(by_id[key] for key in sorted(by_id))

    @cached_property
    def versions(self) -> Mapping[str, Claim]:
        """Each claim id's current version, else its latest recorded one."""
        out: dict[str, Claim] = {}
        for claim in self.claims:
            held = out.get(claim.id)
            if (
                held is None
                or (claim.current and not held.current)
                or (claim.current == held.current and claim.recorded_at > held.recorded_at)
            ):
                out[claim.id] = claim
        return out

    @cached_property
    def by_subject(self) -> Mapping[Node, tuple[Claim, ...]]:
        index: dict[Node, list[Claim]] = defaultdict(list)
        for claim in self.current:
            index[claim.subject].append(claim)
        return {node: tuple(claims) for node, claims in index.items()}

    @cached_property
    def by_object(self) -> Mapping[Node, tuple[Claim, ...]]:
        index: dict[Node, list[Claim]] = defaultdict(list)
        for claim in self.current:
            node = claim.object_node
            if node is not None:
                index[node].append(claim)
        return {node: tuple(claims) for node, claims in index.items()}


def snapshot_id(document: JsonValue) -> str:
    """``snapshot:sha256:<hex>`` of the document's canonical JSON."""
    try:
        return SNAPSHOT_PREFIX + content_id(canonical_json.dumps(document))
    except canonical_json.CanonicalJsonError as exc:
        raise PackError(
            "snapshot_malformed", f"not representable as canonical JSON: {exc}"
        ) from exc


def load_snapshot(data: bytes, *, max_bytes: int = MAX_SNAPSHOT_BYTES) -> Snapshot:
    """Read a graph document (graph-schema 1.x ``#/$defs/Graph``) from its bytes."""
    return read_snapshot(parse_document(data, "snapshot_malformed", max_bytes))


def read_snapshot(document: JsonValue) -> Snapshot:
    """Read a parsed graph document; refuse anything outside the contract's shapes."""
    graph = _R.obj(
        document,
        "",
        (
            "claims",
            "findings",
            "generation",
            "graph_schema_version",
            "head",
            "kind",
            "resolver_config",
        ),
    )
    if graph["kind"] != "memory.graph":
        raise _R.fail("kind is not memory.graph", "/kind")
    if graph["graph_schema_version"] != GRAPH_SCHEMA_MAJOR or isinstance(
        graph["graph_schema_version"], bool
    ):
        raise PackError(
            "snapshot_unsupported",
            f"graph_schema_version {graph['graph_schema_version']!r}: Deploy reads graph-schema"
            f" {GRAPH_SCHEMA_MAJOR}",
            "/graph_schema_version",
        )
    head = _R.integer(graph["head"], "/head", 0)
    generation = _R.string(graph["generation"], "/generation", SHA256)
    vocabulary_version, cardinality = _resolver(graph["resolver_config"])
    claims = tuple(
        _claim(item, child("/claims", i), head)
        for i, item in enumerate(_R.array(graph["claims"], "/claims"))
    )
    findings = tuple(
        _finding(item, child("/findings", i), head)
        for i, item in enumerate(_R.array(graph["findings"], "/findings"))
    )
    return Snapshot(
        id=snapshot_id(document),
        head=head,
        generation=generation,
        vocabulary_version=vocabulary_version,
        cardinality=cardinality,
        claims=claims,
        findings=findings,
    )


def _resolver(value: JsonValue) -> tuple[int, Mapping[str, str]]:
    config = _R.obj(value, "/resolver_config", ("priorities", "vocabulary", "vocabulary_version"))
    version = _R.integer(config["vocabulary_version"], "/resolver_config/vocabulary_version", 1)
    vocabulary = _R.obj(config["vocabulary"], "/resolver_config/vocabulary", ("predicates",))
    pointer = "/resolver_config/vocabulary/predicates"
    cardinality: dict[str, str] = {}
    for i, item in enumerate(_R.array(vocabulary["predicates"], pointer)):
        at = child(pointer, i)
        spec = _R.obj(
            item, at, ("cardinality", "description", "domain", "name", "range", "version")
        )
        name = _R.string(spec["name"], child(at, "name"), TOKEN)
        if name in cardinality:
            raise _R.fail(f"predicate {name!r} is declared twice", at)
        cardinality[name] = _R.choice(
            spec["cardinality"], child(at, "cardinality"), ("many", "one")
        )
    return version, cardinality


def _node(value: JsonValue, pointer: str) -> Node:
    ref = _R.obj(value, pointer, ("kind", "node_id", "node_type"))
    if ref["kind"] != "node":
        raise _R.fail("kind is not node", child(pointer, "kind"))
    return Node(
        _R.string(ref["node_type"], child(pointer, "node_type"), TOKEN),
        _R.text(ref["node_id"], child(pointer, "node_id")),
    )


def _stamp(value: JsonValue, pointer: str) -> Stamp:
    stamp = _R.obj(value, pointer, ("domain_id", "ticks"))
    return Stamp(
        _R.string(stamp["domain_id"], child(pointer, "domain_id"), RECORD_ID),
        _R.integer(stamp["ticks"], child(pointer, "ticks")),
    )


def read_interval(value: JsonValue, pointer: str, reader: Reader = _R) -> Interval:
    """An ``Interval``: ``start`` a timestamp, ``end`` a timestamp or ``"open"``."""
    interval = reader.obj(value, pointer, ("end", "start"))
    start = _stamp(interval["start"], child(pointer, "start"))
    end: End = OPEN
    if interval["end"] != OPEN:
        end = _stamp(interval["end"], child(pointer, "end"))
    return Interval(start, end)


def _object(value: JsonValue, pointer: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise _R.fail("expected an object", pointer)
    kind = value.get("kind")
    if kind == "node":
        _node(value, pointer)
    elif kind == "record":
        ref = _R.obj(value, pointer, ("kind", "record_id"))
        _R.string(ref["record_id"], child(pointer, "record_id"), RECORD_ID)
    elif kind == "literal":
        literal = _R.obj(value, pointer, ("datatype", "kind", "unit", "value"))
        _R.choice(literal["datatype"], child(pointer, "datatype"), LITERAL_TYPES)
        _R.obj(literal["unit"], child(pointer, "unit"), ("knowledge",), ("candidates", "value"))
    else:
        raise _R.fail("an object is a node, a record or a literal", child(pointer, "kind"))
    return value


def _evidence(value: JsonValue, pointer: str) -> JsonObject:
    ref = _R.obj(value, pointer, ("locator", "source"))
    source = ref["source"]
    if isinstance(source, str):
        _R.string(source, child(pointer, "source"), SHA256)
    else:
        external = _R.obj(
            source,
            child(pointer, "source"),
            ("connector_id", "kind", "object_id", "revision_token"),
        )
        if external["kind"] != "external":
            raise _R.fail(
                "a source is a content id or an external object", child(pointer, "source")
            )
        for key in ("connector_id", "object_id", "revision_token"):
            _R.string(external[key], child(child(pointer, "source"), key))
    for i, step in enumerate(_R.array(ref["locator"], child(pointer, "locator"))):
        at = child(child(pointer, "locator"), i)
        if not isinstance(step, Mapping) or not isinstance(step.get("kind"), str):
            raise _R.fail("a locator step is an object with a kind", at)
    return ref


def _tx_end(value: JsonValue, pointer: str) -> bool:
    """Whether a ``superseded_at`` leaves the version current."""
    if value == OPEN:
        return True
    _R.integer(value, pointer, 0)
    return False


def _claim(value: JsonValue, pointer: str, head: int) -> Claim:
    claim = _R.obj(
        value,
        pointer,
        (
            "assertion_kind",
            "confidence",
            "id",
            "object",
            "predicate",
            "provenance",
            "recorded_at",
            "subject",
            "superseded_at",
            "supersedes",
            "valid",
        ),
    )
    kind = _R.choice(claim["assertion_kind"], child(pointer, "assertion_kind"), ASSERTION_KINDS)
    at = child(pointer, "provenance")
    provenance = _R.obj(
        claim["provenance"],
        at,
        ("config_hash", "consolidator_id", "consolidator_version", "evidence", "records"),
        ("model",),
    )
    if (kind == "inferred") != ("model" in provenance):
        raise _R.fail("an inferred claim, and only an inferred claim, names its model", at)
    if kind == "inferred":
        model = _R.obj(provenance["model"], child(at, "model"), ("model_id", "model_version"))
        _R.text(model["model_id"], child(child(at, "model"), "model_id"))
        _R.text(model["model_version"], child(child(at, "model"), "model_version"))
    _R.obj(claim["confidence"], child(pointer, "confidence"), ("knowledge",), ("value",))
    evidence = tuple(
        _evidence(item, child(child(at, "evidence"), i))
        for i, item in enumerate(_R.array(provenance["evidence"], child(at, "evidence")))
    )
    if not evidence:
        raise _R.fail("a claim cites at least one evidence ref", child(at, "evidence"))
    records = tuple(
        _R.string(item, child(child(at, "records"), i), RECORD_ID)
        for i, item in enumerate(_R.array(provenance["records"], child(at, "records")))
    )
    recorded_at = _R.integer(claim["recorded_at"], child(pointer, "recorded_at"), 0)
    if recorded_at > head:
        raise _R.fail(f"recorded at {recorded_at}, after the graph's head {head}", pointer)
    for i, item in enumerate(_R.array(claim["supersedes"], child(pointer, "supersedes"))):
        _R.string(item, child(child(pointer, "supersedes"), i), CLAIM_ID)
    return Claim(
        id=_R.string(claim["id"], child(pointer, "id"), CLAIM_ID),
        subject=_node(claim["subject"], child(pointer, "subject")),
        predicate=_R.string(claim["predicate"], child(pointer, "predicate"), TOKEN),
        object=_object(claim["object"], child(pointer, "object")),
        valid=read_interval(claim["valid"], child(pointer, "valid")),
        assertion_kind=kind,
        recorded_at=recorded_at,
        current=_tx_end(claim["superseded_at"], child(pointer, "superseded_at")),
        evidence=evidence,
        records=records,
        raw=claim,
    )


def _finding(value: JsonValue, pointer: str, head: int) -> ResolutionFinding:
    finding = _R.obj(
        value,
        pointer,
        ("claim", "code", "id", "others", "provenance", "recorded_at", "superseded_at"),
    )
    recorded_at = _R.integer(finding["recorded_at"], child(pointer, "recorded_at"), 0)
    if recorded_at > head:
        raise _R.fail(f"recorded at {recorded_at}, after the graph's head {head}", pointer)
    others: Sequence[JsonValue] = _R.array(finding["others"], child(pointer, "others"))
    return ResolutionFinding(
        id=_R.string(finding["id"], child(pointer, "id"), FINDING_ID),
        code=_R.string(finding["code"], child(pointer, "code"), TOKEN),
        claim=_R.string(finding["claim"], child(pointer, "claim"), CLAIM_ID),
        others=tuple(
            _R.string(item, child(child(pointer, "others"), i), CLAIM_ID)
            for i, item in enumerate(others)
        ),
        current=_tx_end(finding["superseded_at"], child(pointer, "superseded_at")),
        raw=finding,
    )
