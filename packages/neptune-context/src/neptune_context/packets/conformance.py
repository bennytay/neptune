"""Consumer contract checks (ADR 0003 §9): what Deploy, Learn and SDK users may rely on.

``check(document)`` runs every check in ``CHECKS`` over one packet document and returns the
failures, empty when the document conforms. A consumer runs it over the golden packets in its
own tests (and over packets it receives, if it wants to), so a producer change that would break
a consumer's reading fails there, not in production. The checks are the guarantees, not the
implementation: a packet decodes; its canonical bytes and ids are stable; every evidence ref
survives rendering as cited text; every inferred item stays marked as inferred.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Final

from neptune.identity.canonical_json import dumps
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.findings import PacketRefused
from neptune_context.packets.model import ContextPacket
from neptune_context.render.citations import CitationError, parse_citations, render_text

Check = Callable[[ContextPacket, bytes], str | None]


def _canonical(packet: ContextPacket, document: bytes) -> str | None:
    if canonical_bytes(packet) != dumps(json.loads(document)):
        return "re-encoding the decoded packet does not reproduce the document's canonical JSON"
    return None


def _rebuild(packet: ContextPacket, _: bytes) -> str | None:
    again = decode(canonical_bytes(packet))
    if not isinstance(again, ContextPacket) or again != packet or again.id != packet.id:
        return "decoding the canonical bytes again gives a different packet"
    return None


def _citations(packet: ContextPacket, _: bytes) -> str | None:
    try:
        cited = parse_citations(render_text(packet))
    except CitationError as exc:
        return f"rendered citations do not parse: {exc}"
    if cited != packet.evidence_refs():
        return "rendering then parsing citations does not recover every evidence ref"
    return None


def _inference_marked(packet: ContextPacket, _: bytes) -> str | None:
    lines = render_text(packet).split("\n")
    for number, item in enumerate(packet.items, start=1):
        head = f"{number}. {item.kind} {item.id} ("
        found = [line for line in lines if line.startswith(head)]
        if len(found) != 1:
            return f"{item.id}: rendered {len(found)} times, not once"
        if item.is_inferred != found[0].startswith(head + "INFERRED"):
            return f"{item.id}: the rendered text does not mark inference as the packet does"
    return None


CHECKS: Final[tuple[tuple[str, Check], ...]] = (
    ("canonical_round_trip", _canonical),
    ("stable_rebuild", _rebuild),
    ("citations_recover_evidence", _citations),
    ("inference_marked", _inference_marked),
)


def check(document: bytes) -> tuple[str, ...]:
    """Every way ``document`` fails the packet contract; ``()`` when it conforms."""
    packet = decode(document)
    if isinstance(packet, PacketRefused):
        return tuple(f"refused: {f.code} at {f.at!r}: {f.message}" for f in packet.findings)
    failures = []
    for name, run in CHECKS:
        failure = run(packet, document)
        if failure is not None:
            failures.append(f"{name}: {failure}")
    return tuple(failures)
