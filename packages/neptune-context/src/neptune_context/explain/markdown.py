"""The human renderer (ADR 0010 §6): a packet as Markdown, trails as trees and change lists.

``render_markdown(packet)`` writes, for a person in Deploy's console:

- a header (packet, query, snapshot, Memory's snapshot when it trails, the world-time window,
  whether inference is included, how many items the budget kept);
- each ``why`` trail as a nested list: the root claim, then the claims that corroborate,
  conflict with or offer an alternative to it, each with its evidence keys, records, transform
  and, when inferred, **INFERRED** with model and confidence;
- each ``diff`` trail grouped by predicate: what opened, closed and was superseded;
- the items no trail names, the claims superseded since the snapshot, resolver findings, the
  gaps, and an evidence table mapping each key to the ref and its resolution.

Claims link to ``neptune://claim/...`` and evidence to ``neptune://evidence/...`` (``links``),
which the console resolves with ``why`` and ``hydrate``. A renderer formats; it adds no fact.
Every value that comes from evidence is written as a JSON literal inside a code span whose fence
is longer than any backtick run in it, with every line break escaped, so source text cannot
start a heading, forge a link or pose as a claim. Deterministic: the same packet, the same text.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from neptune_memory.schema.claim import LedgerRecordRef, TypedLiteral
from neptune_memory.schema.interval import Open
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.supersede import is_closure

from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import Known
from neptune_context.explain.links import claim_link, evidence_link
from neptune_context.packets.model import (
    ClaimItem,
    ContextPacket,
    EvidenceItem,
    EvidenceStatus,
    Item,
)
from neptune_context.packets.trails import (
    Change,
    DiffChange,
    DiffTrail,
    TxPoint,
    WhyStep,
    WhyTrail,
)
from neptune_context.pinned import older_graph_notice

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune.model.jsonvalue import JsonValue
    from neptune.model.provenance import EvidenceRef
    from neptune_context.packets.trails import DiffPoint

_SEPARATORS: Final = {"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"}
_MARKDOWN: Final = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~])")
_TICKS: Final = re.compile(r"`+")
_IDENT: Final = re.compile(r"[A-Za-z0-9:_./-]+")
# A URI (any scheme) or a Neptune identifier in prose; it ends at whitespace or closing punctuation.
_BARE: Final = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.|(?:claim|rec|sha256|finding|item|packet|query):)"
    r"[^\s)\]>\"',;]+"
)
_CHANGE_ORDER: Final = (Change.OPENED, Change.CLOSED, Change.SUPERSEDED, Change.BETWEEN)


def code(value: JsonValue) -> str:
    """``value`` as one JSON literal in a code span no content can close or break."""
    text = dumps(value).decode("utf-8")
    for raw, escaped in _SEPARATORS.items():
        text = text.replace(raw, escaped)
    fence = "`" * (max((len(m) for m in _TICKS.findall(text)), default=0) + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def ident(value: str) -> str:
    """An identifier (claim, packet, record, finding id; a JSON pointer) in a plain code span
    when it is made only of safe characters, else as ``code`` writes any value."""
    if _IDENT.fullmatch(value):
        return f"`{value}`"
    return code(value)


def text(value: str) -> str:
    """Engine prose (gap details may quote upstream text): one line, Markdown escaped, and
    every bare URI or identifier in a code span, so no reader turns it into a live link."""
    line = " ".join(value.split())
    out: list[str] = []
    last = 0
    for match in _BARE.finditer(line):
        bare = match.group(0).rstrip(".")  # a sentence's full stop is prose
        out.append(_MARKDOWN.sub(r"\\\1", line[last : match.start()]))
        out.append(ident(bare))
        last = match.start() + len(bare)
    out.append(_MARKDOWN.sub(r"\\\1", line[last:]))
    return "".join(out)


def _node(node: NodeRef) -> str:
    return f"{node.node_type} {code(node.node_id)}"


def _object(claim: Claim) -> str:
    obj = claim.object
    if isinstance(obj, NodeRef):
        return _node(obj)
    if isinstance(obj, TypedLiteral):
        # The unit as the literal declares it (Memory's own encoding; a bare ``Unit`` is not JSON).
        encoded = obj.to_json()
        unit = (
            f" ({code(encoded['unit'])})"
            if str(obj.datatype) in {"quantity", "delta", "declared_value"}
            else ""
        )
        return code(encoded["value"]) + unit
    assert isinstance(obj, LedgerRecordRef)
    return f"record {ident(obj.record_id)}"


def _valid(claim: Claim) -> str:
    end = claim.valid_to
    stop = "open" if isinstance(end, Open) else str(end.ticks)
    return f"valid [{claim.valid_from.ticks}, {stop}) on clock {ident(claim.valid.domain_id)}"


class _Writer:
    def __init__(self, packet: ContextPacket) -> None:
        self.packet = packet
        self.claims: dict[str, ClaimItem] = {
            i.claim.id: i for i in packet.items if isinstance(i, ClaimItem)
        }
        self.evidence: dict[EvidenceRef, EvidenceItem] = {
            i.evidence: i for i in packet.items if isinstance(i, EvidenceItem)
        }
        refs = dict.fromkeys(packet.evidence_refs())
        for trail in packet.trails:
            if isinstance(trail, WhyTrail):
                for step in trail.steps:
                    refs.update(dict.fromkeys(step.evidence))
        self.keys = {ref: n for n, ref in enumerate(refs, start=1)}
        self.lines: list[str] = []

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)

    def cites(self, refs: tuple[EvidenceRef, ...]) -> str:
        return " ".join(f"[E{self.keys[r]}]" for r in dict.fromkeys(refs) if r in self.keys)

    def epistemics(self, item: Item) -> str:
        if not item.is_inferred:
            return str(item.assertion_kind)
        model = item.provenance.model
        named = f"{code(model.model_id)} {code(model.model_version)}" if model else "no model"
        confidence = (
            f"confidence {item.confidence.value!r}"
            if isinstance(item.confidence, Known)
            else f"confidence {item.confidence.state}"
        )
        return f"**INFERRED** by {named}, {confidence}"

    def claim(self, claim_id: str, *, as_of: int | None = None) -> str:
        """A claim in one line: its content when carried, else its id and a ``why`` link."""
        carried = claim_id in self.claims
        at = self.packet.as_of if as_of is None or carried else as_of
        link = f"[{ident(claim_id)}]({claim_link(claim_id, at)})"
        item = self.claims.get(claim_id)
        if item is None:
            return f"claim {link} (not carried in this packet)"
        c = item.claim
        return (
            f"{_node(c.subject)} *{text(c.predicate)}* {_object(c)}, {_valid(c)}"
            f" · {self.epistemics(item)} · {link} {self.cites(c.provenance.evidence)}".rstrip()
        )

    # --- Sections ------------------------------------------------------------------------------

    def header(self) -> None:
        p, budget = self.packet, self.packet.budget
        self.add(
            "# Context packet",
            "",
            f"- Packet {ident(p.id)}, answering query {ident(p.query_id)}",
            f"- As of transaction {p.as_of} (head {p.head})",
        )
        if p.memory.as_of < p.as_of:
            self.add(
                f"- Claims as Memory knew them at transaction {p.memory.as_of} (it trails the"
                f" Ledger's {p.as_of})"
            )
        notice = older_graph_notice(p.memory.graph_schema_version)
        if notice is not None:
            self.add(f"- {notice.removesuffix('.')}")
        if p.during is not None:
            end = "open" if p.during.end is None else str(p.during.end)
            self.add(
                f"- World time: ticks [{p.during.start}, {end}) on clock"
                f" {ident(p.during.domain_id)}"
            )
        self.add(
            "- Inferred items: "
            + ("included, each marked **INFERRED**" if p.inference_included else "excluded"),
            f"- Items: {budget.items} of {budget.items + budget.dropped} found"
            + (
                f"; cut by the {', '.join(map(str, budget.exhausted))} budget"
                if budget.dropped
                else ""
            ),
        )

    def why(self, trail: WhyTrail) -> None:
        self.add("", f"## Why do we believe {ident(trail.claim)}?", "")
        for step in trail.steps:
            self.step(step)

    def step(self, step: WhyStep) -> None:
        pad = "  " * step.depth
        label = str(step.relation)
        if step.finding is not None:
            label += f" (finding {ident(step.finding)})"
        if step.repeat:
            self.add(f"{pad}- **{label}**: {ident(step.claim)}, shown above")
            return
        item = self.claims.get(step.claim)
        line = f"{pad}- **{label}**: {self.claim(step.claim)}"
        if item is None:
            kind = "**INFERRED**" if step.is_inferred else str(step.assertion_kind)
            line += f" · {kind} {self.cites(step.evidence)}".rstrip()
        self.add(line)
        for ref in step.evidence:
            self.add(f"{pad}  - evidence {self.evidence_line(ref)}")
        if item is not None:
            prov = item.provenance
            transform = prov.transform
            records = ", ".join(ident(r) for r in prov.records) or "none"
            self.add(
                f"{pad}  - records {records}; transform {code(transform.producer_id)}"
                f" {code(transform.producer_version)} {ident(transform.config_hash)}"
            )

    def evidence_line(self, ref: EvidenceRef) -> str:
        key = self.keys[ref]
        held = self.evidence.get(ref)
        status = "not resolved in this packet"
        if held is not None:
            status = str(held.status)
            if held.status is EvidenceStatus.RESOLVED and isinstance(held.size, Known):
                status += f", {held.size.value} bytes"
        return f"[E{key}]({evidence_link(ref, self.packet.as_of)}) {code(ref.to_json())}: {status}"

    def diff(self, trail: DiffTrail) -> None:
        self.add(
            "",
            f"## What changed: {_node(trail.subject)}, {_point(trail.before)} to"
            f" {_point(trail.after)}",
            "",
        )
        if len(trail.nodes) > 1:
            others = ", ".join(_node(n) for n in trail.nodes if n != trail.subject)
            self.add(f"With its declared identities: {others}.", "")
        if not trail.changes:
            self.add("No claim about it changed.")
            return
        by_predicate: dict[str, list[DiffChange]] = {}
        for change in trail.changes:
            by_predicate.setdefault(change.predicate, []).append(change)
        old = trail.before.tx if isinstance(trail.before, TxPoint) else None
        new = trail.after.tx if isinstance(trail.after, TxPoint) else None
        for predicate, changes in by_predicate.items():
            self.add(f"### {text(predicate)}", "")
            for kind in _CHANGE_ORDER:
                for change in (c for c in changes if c.change is kind):
                    self.change(change, old, new, world=old is None)
            self.add("")
        while self.lines and self.lines[-1] == "":
            self.lines.pop()

    def change(self, change: DiffChange, old: int | None, new: int | None, *, world: bool) -> None:
        """One change; a claim no longer carried links to ``why`` where it was held."""
        if change.change is Change.OPENED:
            for claim_id in change.after:
                self.add(f"- **opened**: {self.claim(claim_id, as_of=new)}")
            return
        if change.change is Change.BETWEEN:
            (claim_id,) = change.after
            self.add(f"- **between**: {self.claim(claim_id, as_of=new)}")
            self.add("  - held at neither point: opened and closed in between")
            return
        (before,) = change.before
        self.add(f"- **{change.change}**: {self.claim(before, as_of=old)}")
        if not change.after:
            self.add(
                "  - no longer valid at the later instant"
                if world
                else "  - no longer held by Memory at the later transaction"
            )
        for claim_id in change.after:
            item = self.claims.get(claim_id)
            narrowed = change.change is Change.CLOSED or (
                item is not None and is_closure(item.claim)
            )
            word = "narrowed to" if narrowed else "replaced by"
            self.add(f"  - {word} {self.claim(claim_id, as_of=new)}")

    def rest(self) -> None:
        named = {i for t in self.packet.trails for i in t.claims}
        cited = {
            r
            for t in self.packet.trails
            if isinstance(t, WhyTrail)
            for s in t.steps
            for r in s.evidence
        }
        others = [
            item
            for item in self.packet.items
            if not (isinstance(item, ClaimItem) and item.claim.id in named)
            and not (isinstance(item, EvidenceItem) and item.evidence in cited)
        ]
        if others:
            self.add("", "## Other items", "")
            for number, item in enumerate(others, start=1):
                self.add(f"{number}. {self.item(item)}")
        p = self.packet
        if p.superseded_since:
            self.add("", f"## Changed since transaction {p.memory.as_of}", "")
            for s in p.superseded_since:
                by = ", ".join(ident(b) for b in s.by)
                self.add(f"- {ident(s.claim)} superseded at transaction {s.superseded_at} by {by}")
        if p.findings:
            self.add("", "## Resolver findings", "")
            for f in p.findings:
                others_ = ", ".join(ident(o) for o in f.others)
                self.add(f"- {text(str(f.code))}: {ident(f.claim)} with {others_}")
        if p.gaps:
            self.add("", "## Not answered", "")
            for g in p.gaps:
                where = f" ({g.channel})" if g.channel is not None else ""
                refs = f" [{', '.join(ident(r) for r in g.refs)}]" if g.refs else ""
                at = "the whole query" if g.at == "" else ident(g.at)
                self.add(f"- {g.code} at {at}{where}: {text(g.detail)}{refs}")
        if self.keys:
            self.add("", "## Evidence", "")
            for ref in self.keys:
                self.add(f"- {self.evidence_line(ref)}")

    def item(self, item: Item) -> str:
        if isinstance(item, ClaimItem):
            return self.claim(item.claim.id)
        if isinstance(item, EvidenceItem):
            return f"evidence {self.evidence_line(item.evidence)}"
        return (
            f"{item.kind} {ident(item.id)} · {self.epistemics(item)}"
            f" {self.cites(item.evidence_refs())}".rstrip()
        )


def _point(point: DiffPoint) -> str:
    if isinstance(point, TxPoint):
        return f"transaction {point.tx}"
    return f"tick {point.ticks} on clock {ident(point.clock)}"


def render_markdown(packet: ContextPacket) -> str:
    """``packet`` as Markdown for a person; trails first, then everything else it holds."""
    if not isinstance(packet, ContextPacket):
        raise TypeError(f"render_markdown takes a ContextPacket, got {type(packet).__name__}")
    writer = _Writer(packet)
    writer.header()
    for trail in packet.trails:
        if isinstance(trail, WhyTrail):
            writer.why(trail)
        else:
            writer.diff(trail)
    writer.rest()
    return "\n".join(writer.lines) + "\n"


__all__ = ["code", "render_markdown", "text"]
