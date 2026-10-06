"""The packet's canonical JSON (ADR 0003 §6): ``canonical_bytes``, ``packet_id`` and ``decode``.

``ContextPacket.to_json`` writes every member in one fixed shape (lists always present, absent
single members omitted, no ``null``), and ``canonical_bytes`` is the compiler's canonical JSON of
it: the same packet gives the same bytes on every machine. ``decode`` reads any JSON text of a
packet strictly (exact keys, exact types, no duplicate keys, no NaN) and rebuilds it through the
model's constructors, so a decoded packet passed every check a built one did; then it recomputes
every item id and the packet id and refuses a document whose ids do not match its content.
Upstream values (claims, findings, evidence refs, frames, timestamps) are read by their owners'
codecs, so a packet carries them exactly as Memory and the compiler publish them; then each
claim, node and finding is checked against Context's pinned graph-schema (``pinned``), so a value
Memory added after the pin is refused as ``shape`` instead of passing because the live codec
knows it (ADR 0007 §6).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, NoReturn, TypeVar

from neptune_memory.schema.claim import parse_claim_id
from neptune_memory.schema.codec import (
    claim_from_json,
    finding_from_json,
    model_from_json,
    node_from_json,
)
from neptune_memory.schema.interval import LedgerTx
from neptune_memory.schema.supersede import parse_finding_id

from neptune.identity.canonical_json import dumps
from neptune.model.frames import FrameRef, frame_ref_from_json
from neptune.model.ids import ConfigHash, RecordId, parse_config_hash, parse_record_id
from neptune.model.knowledge import AssertionKind
from neptune.model.knowledge import from_json as knowledge_from_json
from neptune.model.provenance import evidence_ref_from_json
from neptune.model.time import timestamp_from_json
from neptune_context.packets.findings import (
    PacketError,
    PacketFinding,
    PacketFindingCode,
    PacketRefused,
)
from neptune_context.packets.model import (
    MAX_PACKET_BYTES,
    PACKET_KIND,
    PACKET_VERSION,
    ArrowHandle,
    BudgetUse,
    Channel,
    ChannelHit,
    ClaimItem,
    ConfigurationItem,
    ContextPacket,
    DocumentSpanItem,
    During,
    Engine,
    EvidenceItem,
    EvidenceStatus,
    FrameItem,
    Gap,
    GapCode,
    Item,
    ItemProvenance,
    LedgerSnapshot,
    Limit,
    Limits,
    MemorySnapshot,
    Relevance,
    SceneItem,
    SeriesWindowItem,
    Superseded,
    Transform,
)
from neptune_context.packets.trails import (
    Change,
    DiffChange,
    DiffPoint,
    DiffTrail,
    Relation,
    Trail,
    TxPoint,
    WhyStep,
    WhyTrail,
    WorldPoint,
)
from neptune_context.pinned import (
    claim_beyond_pin,
    finding_beyond_pin,
    node_beyond_pin,
    predicates,
)
from neptune_context.pins import GRAPH_SCHEMA_VERSION

if TYPE_CHECKING:
    from neptune_memory.schema.claim import ClaimAssertionKind, ClaimId

    from neptune.model.jsonvalue import JsonObject, JsonValue
    from neptune.model.knowledge import Knowledge

T = TypeVar("T")
Code = PacketFindingCode
_ENVELOPE: Final = {"assertion_kind", "confidence", "id", "kind", "provenance", "relevance"}


def canonical_bytes(packet: ContextPacket) -> bytes:
    """The compiler's canonical JSON of ``packet.to_json()``: byte-identical everywhere."""
    return dumps(packet.to_json())


def packet_id(packet: ContextPacket) -> str:
    return packet.id


# --- Strict JSON readers ----------------------------------------------------------------------


class _Bad(Exception):
    """A decoding failure at a JSON pointer; ``decode`` turns it into one finding."""

    def __init__(self, code: PacketFindingCode, at: str, message: str) -> None:
        super().__init__(message)
        self.code, self.at, self.message = code, at, message


def _exact(
    data: JsonValue, at: str, required: set[str], optional: frozenset[str] = frozenset()
) -> Mapping[str, JsonValue]:
    if not isinstance(data, Mapping):
        raise _Bad(Code.SHAPE, at, f"must be a JSON object, got {type(data).__name__}")
    keys = set(data)
    missing, extra = required - keys, keys - required - optional
    if missing or extra:
        raise _Bad(Code.SHAPE, at, f"missing {sorted(missing)}, unexpected {sorted(extra)}")
    return data


def _str(value: JsonValue, at: str) -> str:
    if not isinstance(value, str):
        raise _Bad(Code.SHAPE, at, f"must be a string, got {type(value).__name__}")
    return value


def _int(value: JsonValue, at: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Bad(Code.SHAPE, at, f"must be an integer, got {value!r}")
    return value


def _float(value: JsonValue, at: str) -> float:
    if not isinstance(value, float):
        raise _Bad(Code.SHAPE, at, f"must be a JSON number with a fraction or exponent: {value!r}")
    return value


def _bool(value: JsonValue, at: str) -> bool:
    if not isinstance(value, bool):
        raise _Bad(Code.SHAPE, at, f"must be a boolean, got {type(value).__name__}")
    return value


def _list(value: JsonValue, at: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise _Bad(Code.SHAPE, at, f"must be an array, got {type(value).__name__}")
    return value


def _each(value: JsonValue, at: str, read: Callable[[JsonValue, str], T]) -> tuple[T, ...]:
    return tuple(read(member, f"{at}/{i}") for i, member in enumerate(_list(value, at)))


def _upstream(at: str, read: Callable[[], T]) -> T:
    """Run an owner's codec or constructor; its refusal is a finding here, at ``at``."""
    try:
        return read()
    except _Bad:
        raise
    except PacketError as exc:
        raise _Bad(exc.code, at, str(exc)) from exc
    except (ValueError, TypeError, KeyError) as exc:
        raise _Bad(Code.SHAPE, at, str(exc)) from exc


def _no_provenance(_: JsonObject) -> NoReturn:
    raise ValueError("an item's Knowledge fields inherit the item's provenance")


def _knowledge(value: JsonValue, at: str, read: Callable[[JsonValue], T]) -> Knowledge[T]:
    return _upstream(at, lambda: knowledge_from_json(value, read, _no_provenance))


def _as_str(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ValueError(f"must be a string, got {type(value).__name__}")
    return value


def _as_int(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"must be an integer, got {value!r}")
    return value


def _as_float(value: JsonValue) -> float:
    if not isinstance(value, float):
        raise ValueError(f"confidence must be a JSON number with a fraction: {value!r}")
    return value


def _as_record(value: JsonValue) -> RecordId:
    return parse_record_id(_as_str(value))


def _enum(value: JsonValue, at: str, enum: type[T]) -> T:
    text = _str(value, at)
    try:
        return enum(text)  # type: ignore[call-arg]
    except ValueError as exc:
        raise _Bad(Code.BAD_VALUE, at, f"not a {enum.__name__}: {text!r}") from exc


# --- Readers ----------------------------------------------------------------------------------


def _assertion_kind(value: JsonValue, at: str) -> ClaimAssertionKind:
    text = _str(value, at)
    if text == "inferred":
        return "inferred"
    try:
        return AssertionKind(text)
    except ValueError as exc:
        raise _Bad(Code.BAD_VALUE, at, f"not an assertion kind: {text!r}") from exc


def _hit(value: JsonValue, at: str) -> ChannelHit:
    obj = _exact(value, at, {"channel", "rank", "score"})
    channel = _enum(obj["channel"], f"{at}/channel", Channel)
    rank, score = _int(obj["rank"], f"{at}/rank"), _float(obj["score"], f"{at}/score")
    return _upstream(at, lambda: ChannelHit(channel, rank, score))


def _relevance(value: JsonValue, at: str) -> Relevance:
    obj = _exact(value, at, {"hits", "score"})
    hits = _each(obj["hits"], f"{at}/hits", _hit)
    score = _float(obj["score"], f"{at}/score")
    return _upstream(at, lambda: Relevance(score, hits))


def _transform(value: JsonValue, at: str) -> Transform:
    obj = _exact(value, at, {"config_hash", "producer_id", "producer_version"})
    return _upstream(
        at,
        lambda: Transform(
            _str(obj["producer_id"], f"{at}/producer_id"),
            _str(obj["producer_version"], f"{at}/producer_version"),
            _upstream(f"{at}/config_hash", lambda: _config_hash(obj["config_hash"])),
        ),
    )


def _config_hash(value: JsonValue) -> ConfigHash:
    return parse_config_hash(_as_str(value))


def _provenance(value: JsonValue, at: str) -> ItemProvenance:
    obj = _exact(value, at, {"evidence", "records", "transform"}, frozenset({"model"}))
    evidence = _each(
        obj["evidence"],
        f"{at}/evidence",
        lambda v, a: _upstream(a, lambda: evidence_ref_from_json(v)),
    )
    records = _each(
        obj["records"], f"{at}/records", lambda v, a: _upstream(a, lambda: _as_record(v))
    )
    transform = _transform(obj["transform"], f"{at}/transform")
    model = (
        _upstream(f"{at}/model", lambda: model_from_json(obj["model"])) if "model" in obj else None
    )
    return _upstream(at, lambda: ItemProvenance(evidence, records, transform, model))


def _claim_ids(value: JsonValue, at: str) -> tuple[ClaimId, ...]:
    return _each(value, at, lambda v, a: _upstream(a, lambda: parse_claim_id(_str(v, a))))


def _record_ids(value: JsonValue, at: str) -> tuple[RecordId, ...]:
    return _each(value, at, lambda v, a: _upstream(a, lambda: _as_record(v)))


def _beyond_pin(reason: str | None) -> None:
    """Refuse a value the pinned graph-schema does not describe (ADR 0007 §6): Memory's live
    codec would accept it, but this reader reads packets at Context's pin."""
    if reason is not None:
        raise ValueError(f"{reason} is not in the pinned graph-schema {GRAPH_SCHEMA_VERSION}")


def _node(value: JsonValue) -> Any:
    node = node_from_json(value)
    _beyond_pin(node_beyond_pin(node))
    return node


def _claim(value: JsonValue) -> Any:
    claim = claim_from_json(value)
    _beyond_pin(claim_beyond_pin(claim))
    return claim


def _finding(value: JsonValue) -> Any:
    finding = finding_from_json(value)
    _beyond_pin(finding_beyond_pin(finding))
    return finding


def _frame(value: JsonValue) -> FrameRef:
    return frame_ref_from_json(value)


_BODY_KEYS: Final[Mapping[str, set[str]]] = {
    "claim": {"claim"},
    "configuration": {"claims", "record", "record_kind", "subject"},
    "document_span": {"document", "evidence", "text"},
    "evidence": {"evidence", "size", "status"},
    "frame": {"at", "encoding", "evidence", "frame", "stream"},
    "scene": {"claims", "frame", "nodes", "records", "site"},
    "series_window": {"arrow", "clock", "end", "start", "stream"},
}


def _item(value: JsonValue, at: str) -> Item:
    if not isinstance(value, Mapping):
        raise _Bad(Code.SHAPE, at, "an item must be a JSON object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _BODY_KEYS:
        raise _Bad(Code.SHAPE, f"{at}/kind", f"unknown item kind {kind!r}")
    obj = _exact(value, at, _ENVELOPE | _BODY_KEYS[kind])
    relevance = _relevance(obj["relevance"], f"{at}/relevance")
    envelope: dict[str, Any] = {
        "assertion_kind": _assertion_kind(obj["assertion_kind"], f"{at}/assertion_kind"),
        "confidence": _knowledge(obj["confidence"], f"{at}/confidence", _as_float),
        "provenance": _provenance(obj["provenance"], f"{at}/provenance"),
        "relevance": relevance,
    }
    item = _upstream(at, lambda: _build(kind, obj, at, envelope))
    stated, actual = _str(obj["id"], f"{at}/id"), _upstream(at, lambda: item.id)
    if stated != actual:
        raise _Bad(Code.ID_MISMATCH, f"{at}/id", f"item id does not match its content ({actual})")
    return item


def _build(kind: str, obj: Mapping[str, JsonValue], at: str, envelope: dict[str, Any]) -> Item:
    def ev(key: str) -> Any:
        return _upstream(f"{at}/{key}", lambda: evidence_ref_from_json(obj[key]))

    if kind == "claim":
        claim = _upstream(f"{at}/claim", lambda: _claim(obj["claim"]))
        return ClaimItem(**envelope, claim=claim)
    if kind == "evidence":
        return EvidenceItem(
            **envelope,
            evidence=ev("evidence"),
            status=_enum(obj["status"], f"{at}/status", EvidenceStatus),
            size=_knowledge(obj["size"], f"{at}/size", _as_int),
        )
    if kind == "series_window":
        arrow = _exact(obj["arrow"], f"{at}/arrow", {"package_id", "path"})
        return SeriesWindowItem(
            **envelope,
            stream=_upstream(f"{at}/stream", lambda: _as_record(obj["stream"])),
            clock=_upstream(f"{at}/clock", lambda: _as_record(obj["clock"])),
            start=_int(obj["start"], f"{at}/start"),
            end=_int(obj["end"], f"{at}/end"),
            arrow=_upstream(
                f"{at}/arrow",
                lambda: ArrowHandle(
                    _str(arrow["package_id"], f"{at}/arrow/package_id"),  # type: ignore[arg-type]
                    _str(arrow["path"], f"{at}/arrow/path"),
                ),
            ),
        )
    if kind == "frame":
        return FrameItem(
            **envelope,
            stream=_knowledge(obj["stream"], f"{at}/stream", _as_record),
            at=_knowledge(obj["at"], f"{at}/at", timestamp_from_json),
            evidence=ev("evidence"),
            encoding=_knowledge(obj["encoding"], f"{at}/encoding", _as_str),
            frame=_knowledge(obj["frame"], f"{at}/frame", _frame),
        )
    if kind == "document_span":
        return DocumentSpanItem(
            **envelope,
            document=_upstream(f"{at}/document", lambda: _as_record(obj["document"])),
            evidence=ev("evidence"),
            text=_knowledge(obj["text"], f"{at}/text", _as_str),
        )
    if kind == "scene":
        return SceneItem(
            **envelope,
            frame=_upstream(f"{at}/frame", lambda: _frame(obj["frame"])),
            site=_knowledge(obj["site"], f"{at}/site", _node),
            nodes=_each(obj["nodes"], f"{at}/nodes", lambda v, a: _upstream(a, lambda: _node(v))),
            claims=_claim_ids(obj["claims"], f"{at}/claims"),
            records=_record_ids(obj["records"], f"{at}/records"),
        )
    return ConfigurationItem(
        **envelope,
        record=_upstream(f"{at}/record", lambda: _as_record(obj["record"])),
        record_kind=_str(obj["record_kind"], f"{at}/record_kind"),
        subject=_knowledge(obj["subject"], f"{at}/subject", _node),
        claims=_claim_ids(obj["claims"], f"{at}/claims"),
    )


def _optional_int(obj: Mapping[str, JsonValue], key: str, at: str) -> int | None:
    return _int(obj[key], f"{at}/{key}") if key in obj else None


def _budget(value: JsonValue, at: str) -> BudgetUse:
    obj = _exact(value, at, {"dropped", "exhausted", "limits", "tokenizer", "used"})
    lim = _exact(
        obj["limits"], f"{at}/limits", {"items"}, frozenset({"bytes", "latency_ms", "tokens"})
    )
    limits = _upstream(
        f"{at}/limits",
        lambda: Limits(
            items=_int(lim["items"], f"{at}/limits/items"),
            tokens=_optional_int(lim, "tokens", f"{at}/limits"),
            bytes=_optional_int(lim, "bytes", f"{at}/limits"),
            latency_ms=_optional_int(lim, "latency_ms", f"{at}/limits"),
        ),
    )
    used = _exact(obj["used"], f"{at}/used", {"bytes", "items", "tokens"})
    exhausted = _each(obj["exhausted"], f"{at}/exhausted", lambda v, a: _enum(v, a, Limit))
    return _upstream(
        at,
        lambda: BudgetUse(
            limits=limits,
            items=_int(used["items"], f"{at}/used/items"),
            bytes=_int(used["bytes"], f"{at}/used/bytes"),
            tokens=_int(used["tokens"], f"{at}/used/tokens"),
            dropped=_int(obj["dropped"], f"{at}/dropped"),
            exhausted=exhausted,
            tokenizer=_str(obj["tokenizer"], f"{at}/tokenizer"),
        ),
    )


def _during(value: JsonValue, at: str) -> During:
    obj = _exact(value, at, {"domain_id", "end", "start"})
    end = obj["end"]
    return _upstream(
        at,
        lambda: During(
            _as_record(obj["domain_id"]),
            _int(obj["start"], f"{at}/start"),
            None if end == "open" else _int(end, f"{at}/end"),
        ),
    )


def _superseded(value: JsonValue, at: str) -> Superseded:
    obj = _exact(value, at, {"by", "claim", "superseded_at"})
    (claim,) = _claim_ids([obj["claim"]], f"{at}/claim")
    by = _claim_ids(obj["by"], f"{at}/by")
    return _upstream(
        at,
        lambda: Superseded(claim, LedgerTx(_int(obj["superseded_at"], f"{at}/superseded_at")), by),
    )


def _gap(value: JsonValue, at: str) -> Gap:
    obj = _exact(value, at, {"at", "code", "detail", "refs"}, frozenset({"channel"}))
    channel = _enum(obj["channel"], f"{at}/channel", Channel) if "channel" in obj else None
    return _upstream(
        at,
        lambda: Gap(
            _enum(obj["code"], f"{at}/code", GapCode),
            _str(obj["at"], f"{at}/at"),
            channel,
            _each(obj["refs"], f"{at}/refs", _str),
            _str(obj["detail"], f"{at}/detail"),
        ),
    )


def _evidence_refs(value: JsonValue, at: str) -> tuple[Any, ...]:
    return _each(value, at, lambda v, a: _upstream(a, lambda: evidence_ref_from_json(v)))


def _why_step(value: JsonValue, at: str) -> WhyStep:
    obj = _exact(
        value,
        at,
        {"assertion_kind", "claim", "depth", "evidence", "relation", "repeat"},
        frozenset({"finding", "parent"}),
    )
    (claim,) = _claim_ids([obj["claim"]], f"{at}/claim")
    parent = _claim_ids([obj["parent"]], f"{at}/parent")[0] if "parent" in obj else None
    finding = (
        _upstream(f"{at}/finding", lambda: parse_finding_id(_str(obj["finding"], f"{at}/finding")))
        if "finding" in obj
        else None
    )
    return _upstream(
        at,
        lambda: WhyStep(
            claim,
            parent,
            _enum(obj["relation"], f"{at}/relation", Relation),
            _int(obj["depth"], f"{at}/depth"),
            _assertion_kind(obj["assertion_kind"], f"{at}/assertion_kind"),
            _evidence_refs(obj["evidence"], f"{at}/evidence"),
            finding,
            _bool(obj["repeat"], f"{at}/repeat"),
        ),
    )


def _point(value: JsonValue, at: str) -> DiffPoint:
    if isinstance(value, Mapping) and "tx" in value:
        obj = _exact(value, at, {"tx"})
        return _upstream(at, lambda: TxPoint(LedgerTx(_int(obj["tx"], f"{at}/tx"))))
    obj = _exact(value, at, {"clock", "ticks"})
    return _upstream(
        at,
        lambda: WorldPoint(
            _str(obj["clock"], f"{at}/clock"),  # type: ignore[arg-type]
            _int(obj["ticks"], f"{at}/ticks"),
        ),
    )


def _change(value: JsonValue, at: str) -> DiffChange:
    obj = _exact(value, at, {"after", "before", "change", "predicate"})
    predicate = _str(obj["predicate"], f"{at}/predicate")
    if predicate not in predicates():
        raise _Bad(
            Code.SHAPE,
            f"{at}/predicate",
            f"predicate {predicate!r} is not in the pinned graph-schema {GRAPH_SCHEMA_VERSION}",
        )
    return _upstream(
        at,
        lambda: DiffChange(
            predicate,
            _enum(obj["change"], f"{at}/change", Change),
            _claim_ids(obj["before"], f"{at}/before"),
            _claim_ids(obj["after"], f"{at}/after"),
        ),
    )


def _trail(value: JsonValue, at: str) -> Trail:
    kind = value.get("kind") if isinstance(value, Mapping) else None
    if kind == WhyTrail.kind:
        obj = _exact(value, at, {"at", "claim", "kind", "steps"})
        (claim,) = _claim_ids([obj["claim"]], f"{at}/claim")
        steps = _each(obj["steps"], f"{at}/steps", _why_step)
        return _upstream(at, lambda: WhyTrail(_str(obj["at"], f"{at}/at"), claim, steps))
    if kind == DiffTrail.kind:
        obj = _exact(value, at, {"after", "at", "before", "changes", "kind", "nodes", "subject"})
        subject = _upstream(f"{at}/subject", lambda: _node(obj["subject"]))
        nodes = _each(obj["nodes"], f"{at}/nodes", lambda v, a: _upstream(a, lambda: _node(v)))
        before = _point(obj["before"], f"{at}/before")
        after = _point(obj["after"], f"{at}/after")
        changes = _each(obj["changes"], f"{at}/changes", _change)
        return _upstream(
            at,
            lambda: DiffTrail(_str(obj["at"], f"{at}/at"), subject, nodes, before, after, changes),
        )
    raise _Bad(Code.SHAPE, f"{at}/kind", f"a trail's kind is 'why' or 'diff', got {kind!r}")


def _header(value: JsonValue, at: str) -> dict[str, Any]:
    obj = _exact(
        value,
        at,
        {
            "as_of",
            "budget",
            "head",
            "inference_included",
            "ledger_snapshot",
            "memory_snapshot",
            "produced_by",
            "query_id",
        },
        frozenset({"during"}),
    )
    mem = _exact(
        obj["memory_snapshot"],
        f"{at}/memory_snapshot",
        {"as_of", "generation", "graph_schema_version"},
    )
    led = _exact(obj["ledger_snapshot"], f"{at}/ledger_snapshot", {"catalog_api_version"})
    eng = _exact(
        obj["produced_by"], f"{at}/produced_by", {"config_hash", "engine_id", "engine_version"}
    )
    m = f"{at}/memory_snapshot"
    return {
        "query_id": _str(obj["query_id"], f"{at}/query_id"),
        "as_of": _int(obj["as_of"], f"{at}/as_of"),
        "head": _int(obj["head"], f"{at}/head"),
        "during": _during(obj["during"], f"{at}/during") if "during" in obj else None,
        "memory": _upstream(
            m,
            lambda: MemorySnapshot(
                _int(mem["graph_schema_version"], f"{m}/graph_schema_version"),
                _str(mem["generation"], f"{m}/generation"),  # type: ignore[arg-type]
                LedgerTx(_int(mem["as_of"], f"{m}/as_of")),
            ),
        ),
        "ledger": _upstream(
            f"{at}/ledger_snapshot",
            lambda: LedgerSnapshot(
                _str(led["catalog_api_version"], f"{at}/ledger_snapshot/catalog_api_version")
            ),
        ),
        "produced_by": _upstream(
            f"{at}/produced_by",
            lambda: Engine(
                _str(eng["engine_id"], f"{at}/produced_by/engine_id"),
                _str(eng["engine_version"], f"{at}/produced_by/engine_version"),
                _str(eng["config_hash"], f"{at}/produced_by/config_hash"),  # type: ignore[arg-type]
            ),
        ),
        "inference_included": _bool(obj["inference_included"], f"{at}/inference_included"),
        "budget": _budget(obj["budget"], f"{at}/budget"),
    }


def _packet(value: JsonValue) -> ContextPacket:
    if isinstance(value, Mapping) and "packet_version" in value:
        version = value["packet_version"]
        if type(version) is not int or version != PACKET_VERSION:
            raise _Bad(
                Code.UNSUPPORTED_VERSION,
                "/packet_version",
                f"this reader reads packet_version {PACKET_VERSION}, got {version!r}",
            )
    obj = _exact(
        value,
        "",
        {"findings", "gaps", "header", "id", "items", "kind", "packet_version", "superseded_since"},
        frozenset({"trails"}),
    )
    if obj["kind"] != PACKET_KIND:
        raise _Bad(Code.SHAPE, "/kind", f"kind must be {PACKET_KIND!r}")
    header = _header(obj["header"], "/header")
    items = _each(obj["items"], "/items", _item)
    superseded = _each(obj["superseded_since"], "/superseded_since", _superseded)
    findings = _each(obj["findings"], "/findings", lambda v, a: _upstream(a, lambda: _finding(v)))
    gaps = _each(obj["gaps"], "/gaps", _gap)
    if "trails" in obj and not _list(obj["trails"], "/trails"):
        raise _Bad(Code.SHAPE, "/trails", "trails is written only when there is one (ADR 0010)")
    trails = _each(obj["trails"], "/trails", _trail) if "trails" in obj else ()
    packet = _upstream(
        "",
        lambda: ContextPacket(
            **header,
            items=items,
            superseded_since=superseded,
            findings=findings,
            gaps=gaps,
            trails=trails,
        ),
    )
    stated, actual = _str(obj["id"], "/id"), _upstream("", lambda: packet.id)
    if stated != actual:
        raise _Bad(Code.ID_MISMATCH, "/id", f"packet id does not match its content ({actual})")
    return packet


def from_json(value: JsonValue) -> ContextPacket | PacketRefused:
    """A packet from an already-parsed JSON value, or every reason it is refused."""
    try:
        return _packet(value)
    except _Bad as bad:
        return PacketRefused((PacketFinding(bad.code, bad.at, bad.message),))
    except RecursionError:
        return PacketRefused((PacketFinding(Code.SHAPE, "", "document is nested too deeply"),))


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def _no_constants(token: str) -> NoReturn:
    raise ValueError(f"{token} is not JSON")


def decode(document: bytes | str) -> ContextPacket | PacketRefused:
    """Read a packet document (any JSON text of it; canonical form is not required)."""
    try:
        size = len(document.encode("utf-8") if isinstance(document, str) else document)
    except UnicodeEncodeError as exc:
        return PacketRefused((PacketFinding(Code.SYNTAX, "", f"not valid Unicode text: {exc}"),))
    if size > MAX_PACKET_BYTES:
        return PacketRefused(
            (PacketFinding(Code.TOO_LARGE, "", f"{size} bytes is over {MAX_PACKET_BYTES}"),)
        )
    try:
        text = document.decode("utf-8") if isinstance(document, bytes) else document
        value = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
    except RecursionError:
        return PacketRefused((PacketFinding(Code.SYNTAX, "", "document is nested too deeply"),))
    except ValueError as exc:  # UnicodeDecodeError, JSONDecodeError, our hooks
        return PacketRefused((PacketFinding(Code.SYNTAX, "", f"not a JSON document: {exc}"),))
    return from_json(value)


__all__ = ["canonical_bytes", "decode", "from_json", "packet_id"]
