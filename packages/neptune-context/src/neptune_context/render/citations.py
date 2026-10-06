"""The citation contract every text renderer keeps (ADR 0003 §7), and a reference renderer.

``render_text`` writes a packet as plain text for a language model: a header, one line per item
that ends with the citation keys of its evidence (``[E1]``, ``[E2]`` ...), the claims superseded
since ``as_of``, the resolver findings, the gaps, and an ``Evidence:`` footer that maps each key to
the evidence ref's canonical JSON. ``parse_citations`` reads the footer back (a line's citations
are the run of keys that ends it). The property every
renderer must keep: ``parse_citations(render(packet)) == packet.evidence_refs()``, and every key a
line cites is in the footer. A renderer only formats what the packet holds; it adds no fact, no
score and no reading of its own, and it marks every inferred item as inferred.

Every value taken from evidence is written as a JSON string literal, so text in a document span
cannot start a line, forge a citation footer or pose as an item. The LLM-agent renderer (MVL-147)
builds on this module; it may change the layout, not this contract.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import TypedLiteral
from neptune_memory.schema.nodes import NodeRef

from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import Known, to_json
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune_context.packets.model import (
    ClaimItem,
    ContextPacket,
    DocumentSpanItem,
    EvidenceItem,
    FrameItem,
    Item,
    SceneItem,
    SeriesWindowItem,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue

FOOTER: Final = "Evidence:"
CITE: Final = re.compile(r"\[E([1-9][0-9]*)\]")
# A line's citations are the run of keys that ends it; a key inside a quoted value is text.
_TRAILING: Final = re.compile(r"(?:\[E[1-9][0-9]*\])+$")
_FOOTER_LINE: Final = re.compile(r"\[E([1-9][0-9]*)\] (\{.*\})")


class CitationError(ValueError):
    """Text whose citations do not follow the contract: a key without a footer entry, a footer
    entry out of order, or a footer line that is not an evidence ref."""


# Characters some readers treat as line breaks that canonical JSON leaves raw: escaped here too.
_SEPARATORS: Final = {"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}


def _lit(value: JsonValue) -> str:
    """A value from evidence, as one JSON literal on one line: no character in it breaks a line."""
    text = dumps(value).decode("utf-8")
    for raw, escaped in _SEPARATORS.items():
        text = text.replace(raw, escaped)
    return text


def _node(node: NodeRef) -> str:
    return f"{node.node_type}:{_lit(node.node_id)}"


def _object(item: ClaimItem) -> str:
    obj = item.claim.object
    if isinstance(obj, NodeRef):
        return _node(obj)
    if isinstance(obj, TypedLiteral):
        return _lit(obj.to_json())
    return f"record {obj.record_id}"


def _epistemics(item: Item) -> str:
    if not item.is_inferred:
        return str(item.assertion_kind)
    model = item.provenance.model
    confidence = (
        f"confidence {item.confidence.value!r}"
        if isinstance(item.confidence, Known)
        else f"confidence {item.confidence.state}"
    )
    named = f"{model.model_id} {model.model_version}" if model is not None else "no model"
    return f"INFERRED by {_lit(named)}, {confidence}"


def _summary(item: Item) -> str:
    if isinstance(item, ClaimItem):
        claim = item.claim
        valid = _lit(claim.valid.to_json())
        subject = _node(claim.subject)
        return f"{claim.id}: {subject} {claim.predicate} {_object(item)} valid {valid}"
    if isinstance(item, EvidenceItem):
        return f"source bytes, {item.status}, size {_lit(to_json(item.size))}"
    if isinstance(item, SeriesWindowItem):
        return (
            f"stream {item.stream} on clock {item.clock}, ticks [{item.start}, {item.end}),"
            f" rows at {item.arrow.package_id}/{item.arrow.path}"
        )
    if isinstance(item, FrameItem):
        stream = _lit(to_json(item.stream))
        at = _lit(to_json(item.at, lambda t: t.to_json()))
        frame = _lit(to_json(item.frame, lambda f: f.to_json()))
        encoding = _lit(to_json(item.encoding))
        return f"sensor sample, stream {stream}, at {at}, frame {frame}, encoding {encoding}"
    if isinstance(item, DocumentSpanItem):
        return f"span of {item.document}: {_lit(to_json(item.text))}"
    if isinstance(item, SceneItem):
        nodes = ", ".join(_node(n) for n in item.nodes) or "no nodes"
        site = _lit(to_json(item.site, lambda n: n.to_json()))
        return (
            f"scene in frame {_lit(item.frame.to_json())}, site {site}: {nodes};"
            f" claims {list(item.claims)}; records {list(item.records)}"
        )
    subject = _lit(to_json(item.subject, lambda n: n.to_json()))
    return f"{item.record_kind} {item.record} configures {subject}; claims {list(item.claims)}"


def _snapshot_lines(packet: ContextPacket) -> list[str]:
    """What a reader needs to scope the answer: Memory's snapshot when it trails the Ledger's,
    and the world-time window on its named clock (C1 gate, ADR 0006 §4)."""
    lines = []
    if packet.memory.as_of < packet.as_of:
        lines.append(
            f"Claims as Memory knew them at transaction {packet.memory.as_of}"
            f" (it trails the Ledger's {packet.as_of})."
        )
    if packet.during is not None:
        end = "open" if packet.during.end is None else str(packet.during.end)
        lines.append(
            f"World time: ticks [{packet.during.start}, {end}) on clock {packet.during.domain_id}."
        )
    return lines


def render_text(packet: ContextPacket) -> str:
    """The packet as cited plain text. Deterministic: the same packet, the same text."""
    keys = {ref: index for index, ref in enumerate(packet.evidence_refs(), start=1)}
    budget = packet.budget
    lines = [
        f"Context packet {packet.id}",
        f"Query {packet.query_id}, as of transaction {packet.as_of} (head {packet.head}).",
        *_snapshot_lines(packet),
        "Inferred items: "
        + ("included, each marked INFERRED." if packet.inference_included else "excluded."),
        f"Items: {budget.items} of {budget.items + budget.dropped} found"
        + (
            f"; cut by the {', '.join(map(str, budget.exhausted))} budget."
            if budget.dropped
            else "."
        ),
        "",
    ]
    for number, item in enumerate(packet.items, start=1):
        cites = "".join(f"[E{keys[ref]}]" for ref in dict.fromkeys(item.evidence_refs()))
        lines.append(
            f"{number}. {item.kind} {item.id} ({_epistemics(item)}): {_summary(item)} {cites}"
        )
    if packet.superseded_since:
        lines += ["", f"Changed since transaction {packet.memory.as_of}:"]
        lines += [
            f"- {s.claim} superseded at transaction {s.superseded_at} by {', '.join(s.by)}"
            for s in packet.superseded_since
        ]
    if packet.findings:
        lines += ["", "Resolver findings:"]
        lines += [f"- {f.code}: {f.claim} with {', '.join(f.others)}" for f in packet.findings]
    if packet.gaps:
        lines += ["", "Not answered:"]
        lines += [
            f"- {g.code} at {_lit(g.at)}"
            + (f" ({g.channel})" if g.channel is not None else "")
            + f": {_lit(g.detail)} {list(g.refs)}"
            for g in packet.gaps
        ]
    lines += ["", FOOTER]
    lines += [f"[E{index}] {_lit(ref.to_json())}" for ref, index in keys.items()]
    return "\n".join(lines) + "\n"


def parse_citations(text: str) -> tuple[EvidenceRef, ...]:
    """The evidence refs a rendered text cites, in key order, from its last ``Evidence:`` footer.

    Raises ``CitationError`` when a body line cites a key the footer does not define, when the
    footer's keys are not ``1..n`` in order, or when a footer line is not an evidence ref.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    try:
        start = len(lines) - 1 - lines[::-1].index(FOOTER)
    except ValueError:
        raise CitationError("no Evidence: footer") from None
    refs: list[EvidenceRef] = []
    for offset, line in enumerate(lines[start + 1 :], start=1):
        match = _FOOTER_LINE.fullmatch(line)
        if match is None or int(match.group(1)) != offset:
            raise CitationError(f"footer line {offset} is not [E{offset}] <evidence ref>: {line!r}")
        try:
            refs.append(evidence_ref_from_json(json.loads(match.group(2))))
        except ValueError as exc:
            raise CitationError(f"[E{offset}] is not an evidence ref: {exc}") from exc
    cited = {
        int(key)
        for line in lines[:start]
        if (run := _TRAILING.search(line)) is not None
        for key in CITE.findall(run.group(0))
    }
    undefined = sorted(cited - set(range(1, len(refs) + 1)))
    if undefined:
        raise CitationError(f"cited keys with no footer entry: {undefined}")
    return tuple(refs)
