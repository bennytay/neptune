"""Strict JSON parsing of graph-schema objects: claims, findings and graph documents (ADR 0006).

``to_json`` on each type writes the published shape; this module reads it back. Parsing is strict,
because a graph document is input like any other: unknown keys, missing keys, wrong types and a
stored id that does not match the content are ``ValueError``s, never silently repaired.

A *graph document* is one resolved history: every claim version, every finding, the graph-schema
version and the resolver configuration that produced it (its *generation*, ADR 0006 §7).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn, TypeVar

from neptune.identity.ids import config_hash
from neptune.model.frames import TransformDirection, frame_ref_from_json
from neptune.model.ids import ConfigHash, parse_config_hash, parse_record_id
from neptune.model.knowledge import AssertionKind, Knowledge
from neptune.model.knowledge import from_json as knowledge_from_json
from neptune.model.provenance import evidence_ref_from_json
from neptune.model.scalars import real_from_json
from neptune.model.time import timestamp_from_json
from neptune.model.units import Unit, unit_from_json
from neptune_memory.schema import GRAPH_SCHEMA_RELEASE, GRAPH_SCHEMA_VERSION
from neptune_memory.schema.claim import (
    Claim,
    ClaimAssertionKind,
    ClaimObject,
    ClaimProvenance,
    DeclaredTransform,
    Delta,
    DeltaAdjustment,
    DeltaQuantity,
    LedgerRecordRef,
    LiteralValue,
    ModelRef,
    TypedLiteral,
    ValueType,
    parse_claim_id,
)
from neptune_memory.schema.clock_map import clock_map_from_json
from neptune_memory.schema.interval import OPEN, LedgerTx, Open, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import (
    Build,
    FindingCode,
    FindingProvenance,
    Resolution,
    ResolutionFinding,
    build_order,
    parse_finding_id,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.time import Timestamp

GRAPH_DOCUMENT_KIND: Final = "memory.graph"
# A published graph-schema version, MAJOR.MINOR.PATCH (ADR 0019 §3).
_MAJOR_1: Final = 1  # a graph-schema 1.x document, read as written (ADR 0019 §3)
_RELEASE: Final = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def _object(data: object, what: str) -> Mapping[str, JsonValue]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{what} must be a JSON object, got {type(data).__name__}")
    return data


def _exact(
    data: object, what: str, required: set[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, JsonValue]:
    obj = _object(data, what)
    missing = sorted(required - obj.keys())
    extra = sorted(obj.keys() - required - optional)
    if missing or extra:
        raise ValueError(f"{what}: missing keys {missing}, unexpected keys {extra}")
    return obj


def _str(value: JsonValue, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a string")
    return value


def _list(value: JsonValue, what: str) -> Sequence[JsonValue]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{what} must be an array")
    return value


def _int(value: JsonValue, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} must be an integer")
    return value


def _no_provenance(_: JsonObject) -> NoReturn:
    raise ValueError("this value inherits the claim's provenance; it carries none of its own")


def _tx(value: JsonValue, what: str) -> LedgerTx:
    return ledger_tx(_int(value, what))


def _tx_end(value: JsonValue, what: str) -> LedgerTx | Open:
    return OPEN if value == "open" else _tx(value, what)


def node_from_json(data: JsonValue) -> NodeRef:
    obj = _exact(data, "node", {"kind", "node_id", "node_type"})
    if obj["kind"] != "node":
        raise ValueError(f"node kind must be 'node', got {obj['kind']!r}")
    return NodeRef(NodeType(_str(obj["node_type"], "node_type")), _str(obj["node_id"], "node_id"))


def delta_from_json(data: JsonValue) -> Delta:
    """A ``Delta``: a parameter's (``name``) or a transform part's (``parent``, ``child``)."""
    keys = {"earlier", "later", "quantity", "representation", "values"}
    quantity = DeltaQuantity(_str(_object(data, "delta").get("quantity", ""), "quantity"))
    edge = quantity is not DeltaQuantity.PARAMETER
    extra = {"child", "parent", "transform"} if edge else {"name"}
    if quantity is DeltaQuantity.ROTATION:
        extra.add("adjustment")
    obj = _exact(data, "delta", keys | extra)
    values = []
    for item in _list(obj["values"], "values"):
        value = real_from_json(item)
        if not isinstance(value, float):
            raise ValueError(f"a difference is a finite number, got {item!r}")
        values.append(value)
    return Delta(
        earlier=parse_record_id(_str(obj["earlier"], "earlier")),
        later=parse_record_id(_str(obj["later"], "later")),
        quantity=quantity,
        representation=_str(obj["representation"], "representation"),
        values=tuple(values),
        name=None if edge else _str(obj["name"], "name"),
        edge=(frame_ref_from_json(obj["parent"]), frame_ref_from_json(obj["child"]))
        if edge
        else None,
        transform=_declared_transform(obj["transform"]) if edge else None,
        adjustment=DeltaAdjustment(_str(obj["adjustment"], "adjustment"))
        if "adjustment" in obj
        else None,
    )


def _declared_transform(data: JsonValue) -> DeclaredTransform:
    obj = _exact(data, "transform", {"child", "direction", "parent"})
    return DeclaredTransform(
        _str(obj["parent"], "parent"),
        _str(obj["child"], "child"),
        TransformDirection(_str(obj["direction"], "direction")),
    )


def _literal_value(datatype: ValueType, value: JsonValue) -> LiteralValue:
    if datatype is ValueType.INSTANT:
        return timestamp_from_json(value)
    if datatype is ValueType.CLOCK_MAP:
        return clock_map_from_json(value)
    if datatype is ValueType.DELTA:
        return delta_from_json(value)
    if datatype in (ValueType.REAL, ValueType.QUANTITY) and not (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        return real_from_json(value)
    if isinstance(value, str | bool | int | float):
        return value
    raise ValueError(f"not a {datatype} value: {value!r}")


def object_from_json(data: JsonValue) -> ClaimObject:
    kind = _object(data, "object").get("kind")
    if kind == "node":
        return node_from_json(data)
    if kind == "record":
        obj = _exact(data, "record ref", {"kind", "record_id"})
        return LedgerRecordRef(parse_record_id(_str(obj["record_id"], "record_id")))
    if kind == "literal":
        obj = _exact(data, "literal", {"datatype", "kind", "unit", "value"})
        datatype = ValueType(_str(obj["datatype"], "datatype"))
        unit: Knowledge[Unit] = knowledge_from_json(obj["unit"], unit_from_json, _no_provenance)
        return TypedLiteral(datatype, _literal_value(datatype, obj["value"]), unit)
    raise ValueError(f"object kind must be node, record or literal, got {kind!r}")


def _assertion_kind(value: JsonValue) -> ClaimAssertionKind:
    text = _str(value, "assertion_kind")
    if text == "inferred":
        return "inferred"
    return AssertionKind(text)


def _confidence(value: float | int | str | bool | JsonValue | None) -> float:
    if not isinstance(value, float):
        raise ValueError(f"confidence must be a JSON number with a fraction: {value!r}")
    return value


def model_from_json(data: JsonValue) -> ModelRef:
    obj = _exact(data, "model", {"model_id", "model_version"})
    return ModelRef(_str(obj["model_id"], "model_id"), _str(obj["model_version"], "model_version"))


def provenance_from_json(data: JsonValue) -> ClaimProvenance:
    obj = _exact(
        data,
        "claim provenance",
        {"config_hash", "consolidator_id", "consolidator_version", "evidence", "records"},
        frozenset({"model"}),
    )
    return ClaimProvenance(
        evidence=tuple(evidence_ref_from_json(e) for e in _list(obj["evidence"], "evidence")),
        records=tuple(parse_record_id(_str(r, "record")) for r in _list(obj["records"], "records")),
        consolidator_id=_str(obj["consolidator_id"], "consolidator_id"),
        consolidator_version=_str(obj["consolidator_version"], "consolidator_version"),
        config_hash=parse_config_hash(_str(obj["config_hash"], "config_hash")),
        model=model_from_json(obj["model"]) if "model" in obj else None,
    )


def _valid(data: JsonValue) -> tuple[Timestamp, Timestamp | Open]:
    obj = _exact(data, "valid", {"end", "start"})
    end = obj["end"]
    return timestamp_from_json(obj["start"]), OPEN if end == "open" else timestamp_from_json(end)


def claim_from_json(data: JsonValue) -> Claim:
    """A claim exactly as ``Claim.to_json`` wrote it; its stored ``id`` must match its content."""
    obj = _exact(
        data,
        "claim",
        {
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
        },
    )
    valid_from, valid_to = _valid(obj["valid"])
    claim = Claim(
        subject=node_from_json(obj["subject"]),
        predicate=_str(obj["predicate"], "predicate"),
        object=object_from_json(obj["object"]),
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=_tx(obj["recorded_at"], "recorded_at"),
        assertion_kind=_assertion_kind(obj["assertion_kind"]),
        confidence=knowledge_from_json(obj["confidence"], _confidence, _no_provenance),
        provenance=provenance_from_json(obj["provenance"]),
        superseded_at=_tx_end(obj["superseded_at"], "superseded_at"),
        supersedes=tuple(
            parse_claim_id(_str(i, "supersedes")) for i in _list(obj["supersedes"], "supersedes")
        ),
    )
    if parse_claim_id(_str(obj["id"], "id")) != claim.id:
        raise ValueError(f"claim id {obj['id']!r} does not match its content ({claim.id})")
    return claim


def finding_provenance_from_json(data: JsonValue) -> FindingProvenance:
    obj = _exact(data, "finding provenance", {"config_hash", "resolver_id", "resolver_version"})
    return FindingProvenance(
        _str(obj["resolver_id"], "resolver_id"),
        _str(obj["resolver_version"], "resolver_version"),
        parse_config_hash(_str(obj["config_hash"], "config_hash")),
    )


def finding_from_json(data: JsonValue) -> ResolutionFinding:
    """A finding exactly as ``ResolutionFinding.to_json`` wrote it; its ``id`` must match."""
    obj = _exact(
        data,
        "finding",
        {"claim", "code", "id", "others", "provenance", "recorded_at", "superseded_at"},
    )
    finding = ResolutionFinding(
        code=FindingCode(_str(obj["code"], "code")),
        claim=parse_claim_id(_str(obj["claim"], "claim")),
        others=tuple(parse_claim_id(_str(o, "others")) for o in _list(obj["others"], "others")),
        provenance=finding_provenance_from_json(obj["provenance"]),
        recorded_at=_tx(obj["recorded_at"], "recorded_at"),
        superseded_at=_tx_end(obj["superseded_at"], "superseded_at"),
    )
    if parse_finding_id(_str(obj["id"], "id")) != finding.id:
        raise ValueError(f"finding id {obj['id']!r} does not match its content ({finding.id})")
    return finding


@dataclass(frozen=True)
class GraphDocument:
    """One resolved history and the resolver configuration that produced it.

    ``generation`` is ``config_hash(resolver_config)``: the store generation (ADR 0006 §7). Two
    documents with different generations are different graphs, never merged or diffed by id.

    ``head`` is the latest Ledger transaction the history covers. It is explicit, because a
    transaction can produce no claim: the highest ``recorded_at`` may be earlier than the head,
    and every ``as_of`` up to the head is answerable. No transaction in the history is later.

    ``builds`` (graph-schema 1.9.0, ADR 0016) are the consolidator runs the history was resolved
    with, in ``(recorded_at, consolidator_id)`` order: what lets a later build withdraw a claim
    (ADR 0007 §5). A document without them is a 1.8.0 document and writes no ``builds`` key.

    ``release`` is the graph-schema version the document was written to (``graph_schema``, ADR
    0019 §3): this package writes ``GRAPH_SCHEMA_RELEASE``. ``None`` is a major-1 document read as
    written, whose minor it never recorded: its ``succeeds`` has 1.x's meaning, so it is labelled
    major 1 and written back as one, never relabelled 2.x.
    """

    resolution: Resolution
    resolver_config: JsonObject
    head: LedgerTx
    builds: tuple[Build, ...] = ()
    release: str | None = GRAPH_SCHEMA_RELEASE

    def __post_init__(self) -> None:
        head = ledger_tx(self.head)
        for stamp in latest_stamps(self.resolution):
            if stamp > head:
                raise ValueError(f"the history records transaction {stamp} after its head {head}")
        builds = tuple(self.builds)
        if not all(isinstance(b, Build) for b in builds):
            raise TypeError("builds must be Builds")
        if builds != build_order(builds):
            raise ValueError("builds must be ordered by (recorded_at, consolidator_id)")
        keys = [(b.recorded_at, b.consolidator_id) for b in builds]
        if len(set(keys)) != len(keys):
            raise ValueError("a consolidator builds twice at one transaction")
        late = [b.recorded_at for b in builds if b.recorded_at > head]
        if late:
            raise ValueError(f"a build at transaction {late[0]} after the head {head}")
        object.__setattr__(self, "builds", builds)
        if self.release is not None and (
            _RELEASE.fullmatch(self.release) is None
            or int(self.release.split(".")[0]) != GRAPH_SCHEMA_VERSION
        ):
            raise ValueError(f"release {self.release!r} is not a {GRAPH_SCHEMA_VERSION}.x version")

    @property
    def generation(self) -> ConfigHash:
        return config_hash(self.resolver_config)

    @property
    def graph_schema_version(self) -> int:
        """The major the document was written to: 1 for a document read as written from 1.x."""
        return _MAJOR_1 if self.release is None else GRAPH_SCHEMA_VERSION

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "claims": [claim.to_json() for claim in self.resolution.claims],
            "findings": [finding.to_json() for finding in self.resolution.findings],
            "generation": self.generation,
            "graph_schema_version": self.graph_schema_version,
            "head": self.head,
            "kind": GRAPH_DOCUMENT_KIND,
            "resolver_config": self.resolver_config,
        }
        if self.builds:
            out["builds"] = [build.to_json() for build in self.builds]
        if self.release is not None:
            out["graph_schema"] = self.release
        return out


def latest_stamps(resolution: Resolution) -> list[LedgerTx]:
    """Every transaction a history mentions: recordings and supersessions."""
    stamps: list[LedgerTx] = []
    pairs = [(c.recorded_at, c.superseded_at) for c in resolution.claims]
    pairs += [(f.recorded_at, f.superseded_at) for f in resolution.findings]
    for recorded_at, superseded_at in pairs:
        stamps.append(recorded_at)
        if not isinstance(superseded_at, Open):
            stamps.append(superseded_at)
    return stamps


def build_from_json(data: JsonValue) -> Build:
    """A build exactly as ``Build.to_json`` wrote it (ADR 0016)."""
    obj = _exact(
        data, "build", {"claims", "config_hash", "consolidator_id", "recorded_at", "version"}
    )
    return Build(
        consolidator_id=_str(obj["consolidator_id"], "consolidator_id"),
        version=_str(obj["version"], "version"),
        config_hash=parse_config_hash(_str(obj["config_hash"], "config_hash")),
        recorded_at=_tx(obj["recorded_at"], "recorded_at"),
        claims=tuple(parse_claim_id(_str(i, "claims")) for i in _list(obj["claims"], "claims")),
    )


def graph_from_json(data: JsonValue) -> GraphDocument:
    """A graph document; its version, generation, ids and order are all checked.

    A 2.x document names its release (``graph_schema``); any minor of major 2 reads. A 1.x
    document (no ``graph_schema``) is read as written and stays labelled major 1, so a consumer
    pinned to 1.x keeps reading its graphs; its ``succeeds`` keeps 1.x's meaning (ADR 0019 §3).
    Any other major is refused before anything else.
    """
    major = data.get("graph_schema_version") if isinstance(data, dict) else None
    if major not in (_MAJOR_1, GRAPH_SCHEMA_VERSION) or isinstance(major, bool):
        raise ValueError(
            f"graph_schema_version {major!r} is not supported: this reader reads graph_schema"
            f" 1.x and {GRAPH_SCHEMA_VERSION}.x documents"
        )
    keys = {"claims", "findings", "generation", "graph_schema_version", "head", "kind"}
    keys |= {"resolver_config", *(("graph_schema",) if major == GRAPH_SCHEMA_VERSION else ())}
    obj = _exact(data, "graph document", keys, frozenset({"builds"}))
    if "builds" in obj and not _list(obj["builds"], "builds"):
        raise ValueError("an empty builds list is written as no builds key")
    if obj["kind"] != GRAPH_DOCUMENT_KIND:
        raise ValueError(f"graph document kind must be {GRAPH_DOCUMENT_KIND!r}")
    release: str | None = None
    if major == GRAPH_SCHEMA_VERSION:
        release = _str(obj["graph_schema"], "graph_schema")
        # Any minor of this major reads: a later minor only adds (ADR 0006 §2, ADR 0019 §3).
        if _RELEASE.fullmatch(release) is None or int(release.split(".")[0]) != major:
            raise ValueError(f"graph_schema {release!r} is not a {major}.x release")
    claims = tuple(claim_from_json(c) for c in _list(obj["claims"], "claims"))
    findings = tuple(finding_from_json(f) for f in _list(obj["findings"], "findings"))
    if list(claims) != sorted(claims, key=lambda c: (c.recorded_at, c.id)):
        raise ValueError("claims must be ordered by (recorded_at, id)")
    if list(findings) != sorted(findings, key=lambda f: (f.recorded_at, f.claim, f.code, f.others)):
        raise ValueError("findings must be ordered by (recorded_at, claim, code, others)")
    document = GraphDocument(
        Resolution(claims, findings),
        dict(_object(obj["resolver_config"], "resolver_config")),
        _tx(obj["head"], "head"),
        tuple(build_from_json(b) for b in _list(obj.get("builds", []), "builds")),
        release,
    )
    if parse_config_hash(_str(obj["generation"], "generation")) != document.generation:
        raise ValueError("generation does not match the resolver configuration")
    _check_consistent(document)
    return document


def _check_consistent(document: GraphDocument) -> None:
    """One history: unique ids, every reference resolvable, findings from this generation."""
    claims, findings = document.resolution.claims, document.resolution.findings
    ids = {c.id for c in claims}
    if len(ids) != len(claims):
        raise ValueError("a claim id appears twice")
    if len({f.id for f in findings}) != len(findings):
        raise ValueError("a finding id appears twice")
    dangling = sorted({i for c in claims for i in c.supersedes} - ids)
    dangling += sorted({i for f in findings for i in (f.claim, *f.others)} - ids)
    dangling += sorted({i for b in document.builds for i in b.claims} - ids)
    if dangling:
        raise ValueError(f"references to claims the document does not hold: {dangling[:3]}")
    foreign = [f.id for f in findings if f.provenance.config_hash != document.generation]
    if foreign:
        raise ValueError(f"findings from another generation: {foreign[:3]}")


_GRAPH_KEYS: Final = frozenset(
    {"claims", "findings", "generation", "graph_schema_version", "head", "kind", "resolver_config"}
)
# What decoding hostile JSON may raise: the compiler's readers raise more than ValueError.
_UNREADABLE: Final = (ValueError, TypeError, KeyError, AttributeError)
_T = TypeVar("_T")
# The canonical sort key of a claim or a finding (a claim's last two parts are always empty).
_Order = tuple[int, str, str, tuple[str, ...]]


def _decode_all(
    data: Mapping[str, JsonValue],
    name: str,
    decode: Callable[[JsonValue], _T],
    order: Callable[[_T], _Order],
    key: str,
    problems: list[str],
) -> None:
    """Decode every item of the list ``name``: one problem per item that does not decode, and one
    per decoded neighbour pair out of ``order``."""
    items = data.get(name, [])
    if not isinstance(items, list | tuple):
        problems.append(f"{name} must be an array")
        return
    decoded: list[tuple[int, _T]] = []
    for index, item in enumerate(items):
        try:
            decoded.append((index, decode(item)))
        except _UNREADABLE as exc:
            problems.append(f"{name}[{index}]: {exc}")
    problems.extend(
        f"{name}[{i}] is out of order: {name} are ordered by {key}"
        for (_, a), (i, b) in itertools.pairwise(decoded)
        if order(a) > order(b)
    )


def graph_problems(data: JsonValue) -> tuple[str, ...]:
    """Every reason ``data`` is not a graph document, one line each; empty when it is one.

    ``graph_from_json`` stops at the first problem. This reads on past it, so a consumer checking a
    document it did not write sees at once every claim or finding whose id does not match its
    content, every list out of canonical order and a wrong ``generation``. When nothing else is
    wrong, the document's consistency (unique ids, no dangling reference, builds, head) is
    ``graph_from_json``'s own last word. Empty exactly when ``graph_from_json`` accepts ``data``.
    """
    if not isinstance(data, Mapping):
        return (f"the document must be a JSON object, got {type(data).__name__}",)
    problems: list[str] = []
    version = data.get("graph_schema_version")
    # A 2.x document names its release; a 1.x one, read as written, does not (ADR 0019 §3).
    keys = _GRAPH_KEYS | ({"graph_schema"} if version == GRAPH_SCHEMA_VERSION else set())
    missing = sorted(keys - data.keys())
    extra = sorted(data.keys() - keys - {"builds"})
    if missing or extra:
        problems.append(f"graph document: missing keys {missing}, unexpected keys {extra}")
    if "kind" in data and data["kind"] != GRAPH_DOCUMENT_KIND:
        problems.append(f"kind is {data['kind']!r}, not {GRAPH_DOCUMENT_KIND!r}")
    if "graph_schema_version" in data and (
        isinstance(version, bool) or version not in (_MAJOR_1, GRAPH_SCHEMA_VERSION)
    ):
        problems.append(f"graph_schema_version {version!r} is not supported")
    release = data.get("graph_schema")
    if version == GRAPH_SCHEMA_VERSION and "graph_schema" in data:
        if not isinstance(release, str) or _RELEASE.fullmatch(release) is None:
            problems.append(f"graph_schema {release!r} is not a version")
        elif int(release.split(".")[0]) != GRAPH_SCHEMA_VERSION:
            problems.append(f"graph_schema {release!r} is not a {GRAPH_SCHEMA_VERSION}.x release")
    _decode_all(
        data,
        "claims",
        claim_from_json,
        lambda c: (c.recorded_at, c.id, "", ()),
        "(recorded_at, id)",
        problems,
    )
    _decode_all(
        data,
        "findings",
        finding_from_json,
        lambda f: (f.recorded_at, f.claim, f.code, f.others),
        "(recorded_at, claim, code, others)",
        problems,
    )
    config, given = data.get("resolver_config"), data.get("generation")
    if "resolver_config" in data and not isinstance(config, Mapping):
        problems.append("resolver_config must be a JSON object")
    elif isinstance(config, Mapping) and "generation" in data:
        expected = config_hash(dict(config))
        if given != expected:
            problems.append(
                f"generation {given!r} does not match the resolver configuration ({expected})"
            )
    if not problems:
        try:
            graph_from_json(data)
        except _UNREADABLE as exc:
            problems.append(str(exc))
    return tuple(problems)
