"""Renderers: canonical JSON and a deterministic PDF of an ``EvidencePack`` (ADR 0013 §9).

The JSON is the pack: canonical JSON (``neptune.identity.canonical_json``), so the same pack is the
same bytes. The PDF is a reading of the JSON, laid out by a fixed line-flow engine: monospaced
Courier for everything a reader may copy (ids, values), Helvetica-Bold for headings, wrapping by
exact glyph widths (Courier is 0.6 em per glyph; a heading wraps as if every glyph were 1 em, which
no Helvetica-Bold glyph exceeds). Its metadata is fixed: ``/CreationDate`` and ``/ModDate`` are
``D:19700101000000Z`` (a pack has no wall-clock time; the snapshot's head says when), and ``/ID``
is the first 16 bytes of the pack id.
"""

import textwrap
from collections.abc import Callable, Mapping, Sequence
from typing import Final

from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune_deploy.packs import text as t
from neptune_deploy.packs.compile import (
    COMPILER_VERSION,
    PACK_PREFIX,
    SECTION_NOT_COVERED,
    Entry,
    EvidencePack,
    Section,
    Statement,
)
from neptune_deploy.packs.pdf import PAGE_HEIGHT, PAGE_WIDTH, PlacedLine, write_pdf
from neptune_deploy.packs.snapshot import Claim

FIXED_DATE: Final = "D:19700101000000Z"
MARGIN: Final = 50
TOP: Final = PAGE_HEIGHT - 50
BOTTOM: Final = 56
FOOTER_Y: Final = 30
BODY: Final = 8.0
SMALL: Final = 7.0
COURIER_EM: Final = 0.6
HEADING_EM: Final = 1.0

STATE_CAPTIONS: Final[Mapping[str, str]] = {
    "known": "KNOWN",
    "ambiguous": "AMBIGUOUS - every reading below; none is chosen",
    "unknown": "UNKNOWN - stated as not known; the cited claim names the record leaving it open",
    "conflict": "CONFLICT - these statements disagree; none is chosen",
}
CHANGE_CAPTIONS: Final[Mapping[str, str]] = {
    "known": "CHANGE - one decided configuration ends where another begins, on this node only",
    "ambiguous": (
        "AMBIGUOUS BOUNDARY - a candidate span meets it; every reading below, none is chosen, and"
        " no change is read across it"
    ),
    "unknown": (
        "UNKNOWN BOUNDARY - an unknown span meets it; the cited claim names the record leaving it"
        " open, and no change is read across it"
    ),
}
TIME_CONFLICT: Final = (
    "CONFLICT - this event is placed at different times on the pack clock; every time is shown,"
    " none is chosen"
)


def render_json(pack: EvidencePack) -> bytes:
    """The pack as canonical JSON bytes."""
    return canonical_json.dumps(pack.to_json())


def render_claims(pack: EvidencePack) -> bytes:
    """The pack's claim set for external tools: a graph-schema ``ClaimsResult`` (canonical JSON)
    as of the snapshot's head, so current versions only (a superseded version the pack cites, from
    a resolver finding, stays in ``pack.json``). ``claims`` are those on the pack clock,
    ``other_clocks`` those on any other (never compared with it, as Memory's own ``claims`` query
    returns them), and ``findings`` the resolver findings the pack lists, each exactly as the
    snapshot holds it."""
    clock = pack.spec.clock
    current = [c for c in pack.claims if c.current]
    noted = {note.id for section in pack.sections for note in section.findings}
    document: JsonObject = {
        "as_of": pack.snapshot.head,
        "claims": [c.raw for c in current if c.valid.clock == clock],
        "findings": [f.raw for f in pack.snapshot.current_findings if f.id in noted],
        "other_clocks": [c.raw for c in current if c.valid.clock != clock],
    }
    return canonical_json.dumps(document)


class _Layout:
    """Lines flowing down A4 pages; a page breaks before a line that would cross the bottom."""

    def __init__(self) -> None:
        self.pages: list[list[PlacedLine]] = [[]]
        self.y = float(TOP)

    def space(self, points: float) -> None:
        self.y -= points

    def line(self, text: str, *, font: str = "F2", size: float = BODY, indent: int = 0) -> None:
        heading = font == "F1"
        em = HEADING_EM if heading else COURIER_EM
        x = MARGIN + indent * COURIER_EM * BODY
        width = max(8, int((PAGE_WIDTH - MARGIN - x) // (em * size)))
        display = t.winansi(text)
        lines = textwrap.wrap(
            display,
            width=width,
            subsequent_indent="" if heading else "    ",
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        leading = size * 1.3
        for part in lines:
            if self.y - leading < BOTTOM:
                self.pages.append([])
                self.y = float(TOP)
            self.y -= leading
            self.pages[-1].append(PlacedLine(font, size, x, self.y, part))


def _claim_text(claim: Claim) -> str:
    return f"{claim.subject.node_type} {claim.subject.node_id} {claim.predicate} " + t.claim_object(
        claim.object
    )


def _inferred_mark(statement: Statement) -> str:
    if not statement.claim.inferred:
        return ""
    model = statement.claim.raw["provenance"]
    assert isinstance(model, Mapping)
    ref = model["model"]
    assert isinstance(ref, Mapping)
    confidence = statement.claim.raw["confidence"]
    level = (
        t.claim_object({"kind": "literal", "datatype": "real", "value": confidence["value"]})
        if isinstance(confidence, Mapping) and confidence.get("knowledge") == "known"
        else "unknown"
    )
    return f"[INFERRED by {ref['model_id']} {ref['model_version']}, confidence {level}] "


def _change(out: _Layout, entry: Entry, indent: int) -> None:
    change = entry.change
    assert change is not None
    caption = CHANGE_CAPTIONS.get(entry.knowledge, entry.knowledge.upper())
    out.line(f"[{caption}] {entry.node.node_type} {entry.node.node_id}", font="F3", indent=indent)
    out.line(f"at {t.stamp(change.at)}", indent=indent + 2)
    for statement in entry.statements:
        claim = statement.claim
        side = "before" if claim.id in change.before else "after"
        role = "" if statement.role == "known" else f" ({statement.role} reading)"
        out.line(
            f"- {side}: {_inferred_mark(statement)}{claim.predicate}:"
            f" {t.claim_object(claim.object)} [{claim.assertion_kind}]{role}, valid"
            f" {t.interval(claim.valid)}",
            indent=indent + 2,
        )
        out.line(f"cites {claim.id}", font="F4", size=SMALL, indent=indent + 4)
    out.space(2)


def _entry(out: _Layout, entry: Entry, indent: int) -> None:
    if entry.change is not None:
        _change(out, entry, indent)
        return
    caption = STATE_CAPTIONS.get(entry.knowledge, entry.knowledge.upper())
    if entry.differences:
        caption = TIME_CONFLICT
    out.line(f"[{caption}] {entry.node.node_type} {entry.node.node_id}", font="F3", indent=indent)
    out.line(f"valid {t.interval(entry.valid)}", indent=indent + 2)
    for statement in entry.statements:
        claim = statement.claim
        role = "" if statement.role == "known" else f" ({statement.role} reading)"
        out.line(
            f"- {_inferred_mark(statement)}{claim.predicate}: {t.claim_object(claim.object)}"
            f" [{claim.assertion_kind}]{role}",
            indent=indent + 2,
        )
        out.line(f"cites {claim.id}", font="F4", size=SMALL, indent=indent + 4)
    if entry.placement_records:
        out.line(
            "placed through (cited by this placement only): " + ", ".join(entry.placement_records),
            size=SMALL,
            indent=indent + 2,
        )
    if entry.identity:
        out.line(
            "same event as "
            + ", ".join(f"{n.node_type} {n.node_id}" for n in entry.identity)
            + " (by "
            + ", ".join(entry.identity_claims)
            + ")",
            size=SMALL,
            indent=indent + 2,
        )
    for difference in entry.differences:
        ticks = difference.start_difference_ticks
        out.line(
            f"other placement: {difference.node.node_type} {difference.node.node_id} valid"
            f" {t.interval(difference.valid)}, starting {ticks:+d} ticks from this one",
            font="F3",
            size=SMALL,
            indent=indent + 2,
        )
    out.space(2)


def _marker(pack: EvidencePack) -> Callable[[str], str]:
    """A claim id as cited in prose: marked when inferred, or when the pack leaves it out."""
    inferred = {c.id: c.inferred for c in pack.claims}

    def mark(claim_id: str) -> str:
        if claim_id not in inferred:
            held = pack.snapshot.versions.get(claim_id)
            return f"{claim_id} [{'INFERRED:excluded' if held else 'not-in-snapshot'}]"
        return f"{claim_id} [INFERRED]" if inferred[claim_id] else claim_id

    return mark


def _section(out: _Layout, number: int, section: Section, mark: Callable[[str], str]) -> None:
    out.space(8)
    out.line(f"{number}. {section.template.title}", font="F1", size=13)
    out.line(section.template.description, font="F4", size=SMALL)
    out.space(3)
    if section.knowledge == "not_applicable":
        out.line(
            f"NOT APPLICABLE - this section covers {', '.join(section.template.subject_types)}"
            " subjects.",
            font="F3",
        )
        return
    if section.scope:
        out.line("In scope:", font="F3")
        for node in section.scope:
            via = (
                "the pack subject"
                if not node.via
                else "via " + ", ".join(mark(i) for i in node.via)
            )
            out.line(f"{node.node.node_type} {node.node.node_id} - {via}", indent=2)
    reason = section.reason or {}
    if reason.get("code") == SECTION_NOT_COVERED:
        out.line(f"NOT COVERED ({SECTION_NOT_COVERED}) - {reason['reason']}.", font="F3")
        return
    if section.knowledge == "not_covered":
        missing = reason.get("missing_from_vocabulary")
        what = (
            "no boundary between spans of "
            if section.template.kind == "changes"
            else "no current claim of "
        )
        out.line(
            "NOT COVERED - the snapshot holds "
            + what
            + ", ".join(section.template.predicates)
            + " about the nodes in scope within the interval.",
            font="F3",
        )
        if isinstance(missing, list | tuple) and missing:
            out.line(
                "The snapshot's vocabulary has no predicate "
                + ", ".join(str(m) for m in missing)
                + ".",
                indent=2,
            )
    for entry in section.entries:
        _entry(out, entry, 0)
    if section.other_clocks and section.template.kind == "timeline":
        out.line(
            "NOT PLACED - Memory states no placement of these on the pack clock; each is listed"
            " on its own clock and never compared:",
            font="F3",
        )
        for entry in section.other_clocks:
            _entry(out, entry, 2)
    elif section.other_clocks:
        out.line("On other clocks (never compared with the pack interval):", font="F3")
        for entry in section.other_clocks:
            _entry(out, entry, 2)
    if section.findings:
        out.line("Resolver findings:", font="F3")
        for finding in section.findings:
            others = f"; others {', '.join(map(mark, finding.others))}" if finding.others else ""
            out.line(f"- {finding.code} {finding.id} on {mark(finding.claim)}{others}", indent=2)
    left = []
    if section.outside_interval:
        left.append(f"{section.outside_interval} claims on this clock outside the interval")
    if section.other_clock_restated:
        left.append(
            f"{section.other_clock_restated} claims on other clocks that a placement on the pack"
            " clock restates (other_clock_restated)"
        )
    if section.excluded_inferred:
        left.append(f"{len(section.excluded_inferred)} inferred claims (inference excluded)")
    if left:
        out.line("Left out: " + "; ".join(left) + ".", font="F4", size=SMALL)


def _compact(value: JsonValue) -> str:
    return canonical_json.dumps(value).decode("utf-8")


def _appendix(out: _Layout, pack: EvidencePack) -> None:
    out.space(10)
    out.line("Claims index", font="F1", size=13)
    out.line(
        "Every claim the pack cites, as the snapshot holds it (the JSON pack holds each in full).",
        font="F4",
        size=SMALL,
    )
    for claim in pack.claims:
        out.line(claim.id, font="F3", size=SMALL)
        out.line(_claim_text(claim), indent=2)
        state = "current" if claim.current else "superseded"
        out.line(
            f"{claim.assertion_kind}; valid {t.interval(claim.valid)}; recorded at Ledger tx"
            f" {claim.recorded_at} ({state}); {len(claim.evidence)} evidence refs;"
            f" records {', '.join(claim.records) or 'none'}",
            size=SMALL,
            indent=2,
        )
    out.space(10)
    out.line("Appendix A - evidence references", font="F1", size=13)
    out.line(
        "Each evidence ref the cited claims rest on, and how to resolve it through the Ledger's"
        " catalog API at the snapshot's head.",
        font="F4",
        size=SMALL,
    )
    for i, item in enumerate(pack.appendix.evidence, start=1):
        out.line(f"E{i}. {_compact(item['ref'])}", font="F3", size=SMALL)
        _resolution(out, item)
    out.space(10)
    out.line("Appendix B - records", font="F1", size=13)
    for i, item in enumerate(pack.appendix.records, start=1):
        out.line(f"R{i}. {item['record_id']}", font="F3", size=SMALL)
        _resolution(out, item)


def _resolution(out: _Layout, item: JsonObject) -> None:
    claims = item["claims"]
    assert isinstance(claims, list | tuple)
    out.line(f"cited by {', '.join(str(c) for c in claims)}", size=SMALL, indent=2)
    resolve = item["resolve"]
    assert isinstance(resolve, Mapping)
    if resolve["via"] == "ledger":
        calls = resolve["calls"]
        assert isinstance(calls, list | tuple)
        for call in calls:
            assert isinstance(call, Mapping)
            out.line(
                f"Ledger {resolve['api']} {resolve['api_version']} {call['call']}:"
                f" {_compact(call['request'])}",
                size=SMALL,
                indent=2,
            )
    else:
        detail = {k: v for k, v in resolve.items() if k not in ("reason", "via")}
        out.line(f"not through the catalog: {resolve['reason']}", size=SMALL, indent=2)
        out.line(_compact(detail), size=SMALL, indent=2)


def _header(out: _Layout, pack: EvidencePack) -> None:
    spec = pack.spec
    out.line(pack.template.title, font="F1", size=18)
    out.line("Neptune evidence pack", font="F3", size=9)
    out.space(4)
    rows: Sequence[tuple[str, str]] = (
        ("Pack", pack.id),
        ("Template", f"{pack.template.id} v{pack.template.version} ({pack.template.sha256})"),
        ("Subject", f"{spec.subject.node_type} {spec.subject.node_id}"),
        ("Interval", t.interval(spec.interval)),
        ("Snapshot", pack.snapshot.id),
        (
            "Memory",
            f"graph-schema {pack.snapshot.release or pack.snapshot.major}, Ledger head tx"
            f" {pack.snapshot.head}, generation"
            f" {pack.snapshot.generation}, vocabulary {pack.snapshot.vocabulary_version}",
        ),
        (
            "Inference",
            f"excluded ({pack.excluded_inferred} inferred claims left out)"
            if spec.inference == "exclude"
            else f"INCLUDED - {pack.included_inferred} inferred claims, each marked [INFERRED]",
        ),
    )
    for key, value in rows:
        out.line(f"{key + ':':<10} {value}")
    for unread in pack.snapshot.unread:
        out.line(
            f"Not read: {unread.key_path} ({unread.occurrences} at {unread.pointer}), a key of"
            f" graph-schema {pack.snapshot.declared_schema_version} this compiler does not read;"
            " its content is not shown"
        )
    for number, section in enumerate(pack.sections, start=1):
        reason = section.reason or {}
        if reason.get("code") == SECTION_NOT_COVERED:
            out.line(f"Not covered: section {number} ({section.template.id}), {reason['reason']}")
    out.space(4)
    out.line(pack.template.description, font="F4", size=SMALL)
    out.line(
        "Every statement cites the claim ids it rests on. The claims index lists them; the"
        " appendices say how to resolve each evidence ref and record through the Ledger.",
        font="F4",
        size=SMALL,
    )


def render_pdf(pack: EvidencePack) -> bytes:
    """The pack as a deterministic PDF."""
    out = _Layout()
    _header(out, pack)
    mark = _marker(pack)
    for number, section in enumerate(pack.sections, start=1):
        _section(out, number, section, mark)
    _appendix(out, pack)
    total = len(out.pages)
    pages = [
        [
            *lines,
            PlacedLine("F2", SMALL, MARGIN, FOOTER_Y, f"{pack.id}  -  page {n} of {total}"),
        ]
        for n, lines in enumerate(out.pages, start=1)
    ]
    info = {
        "Title": t.ascii_only(f"{pack.template.title}: {pack.spec.subject.node_id}"),
        "Subject": pack.id,
        "Keywords": f"{pack.snapshot.id} {pack.template.id}@{pack.template.version}",
        "Creator": "neptune-deploy packs",
        "Producer": f"neptune-deploy packs {COMPILER_VERSION}",
        "CreationDate": FIXED_DATE,
        "ModDate": FIXED_DATE,
    }
    digest = bytes.fromhex(pack.id.removeprefix(PACK_PREFIX).removeprefix("sha256:"))
    return write_pdf(pages, info, digest[:16])
