"""Times in their clock's own declared terms (ADR 0022): readable, labelled, never converted.

Memory states each clock as declared (``clock_description``, Memory ADR 0029); the packet carries
the descriptions of its items' clocks and the agent renderer reads a tick on its own clock: a wall
clock's local date and time with its declared zone (or ``zone not stated``), a POSIX clock's UTC
date, anything else as ticks with the reason in plain words. Over Memory's live acceptance
snapshot, Q7's stop reads 14:32:38 in the syslog and 14:33:10 in the CMMS, America/New_York as
declared, side by side and never related.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Final

import jsonschema
import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.clock_description import ClockDescription
from neptune_memory.schema.nodes import NodeType

import retrieve_fixtures_context as F
from neptune.model.knowledge import Ambiguous, Candidate, Known, NotApplicable, Unknown
from neptune.model.time import INT64_MIN, ClockRole, Epoch, Timescale
from neptune_context.engine import read_graph_document
from neptune_context.mcp import build_server
from neptune_context.mcp.__main__ import local_client
from neptune_context.packets.codec import canonical_bytes, decode
from neptune_context.packets.findings import PacketError, PacketRefused
from neptune_context.packets.schema import packet_schema
from neptune_context.query import Budget, Direction, GraphClause, Query, Subject
from neptune_context.render.agent import CLOCKS, parse_answer, quote, render_answer
from neptune_context.render.clocks import ClockBook, civil_text
from neptune_context.sources import read_source_index

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

    from neptune_context.packets.model import ContextPacket

SOURCES: Final = F.ROOT / "harness" / "acceptance" / "sources.json"
SYSLOG: Final = "syslog.host:ARM-3A"
CMMS: Final = "cmms.asset:ARM-3A"
UTC: Final = str(F.rec("clock:operator log, POSIX"))
WALL: Final = str(F.rec("clock:downtime log Stopped"))
BOOT: Final = str(F.rec("clock:controller boot"))


_NA: Final = NotApplicable()
_UNIX: Final = Known(Epoch.UNIX)
_SECOND: Final = Known(Fraction(1))
_UNSTATED: Final = Unknown()


def description(
    *,
    zone: Any = _NA,
    by: Any = _NA,
    epoch: Any = _UNIX,
    resolution: Any = _SECOND,
    timescale: Any = _UNSTATED,
    field: str = "Stopped",
) -> ClockDescription:
    return ClockDescription(
        field=field,
        scope=(),
        role=Known(ClockRole.DOCUMENT),
        resolution=resolution,
        epoch=epoch,
        timescale=timescale,
        civil_time_zone=zone,
        zone_declared_by=by,
    )


def described(clock: str, value: ClockDescription) -> Claim:
    literal = TypedLiteral(ValueType.CLOCK_DESCRIPTION, value)
    return F.claim(
        F.node(NodeType.CLOCK, clock), "clock_description", literal, INT64_MIN, clock=clock
    )


NEW_YORK: Final = description(zone=Known("America/New_York"), by=Known("deploy_lifecycle_map"))


def book(*clocks: Claim) -> ClockBook:
    return ClockBook(sorted(clocks, key=lambda c: c.subject.node_id), quote)


# --- Reading one tick --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ticks", "resolution", "text"),
    [
        (1789396358, Fraction(1), "2026-09-14 14:32:38"),
        (29823272, Fraction(60), "2026-09-14 14:32"),
        (20705, Fraction(86400), "2026-09-09"),
        (1789396358123, Fraction(1, 1000), "2026-09-14 14:32:38.123"),
        (-1, Fraction(1), "1969-12-31 23:59:59"),
        (0, Fraction(1, 3), None),  # no decimal part of a second: shown as ticks
        (2**62, Fraction(1), None),  # beyond year 9999
    ],
)
def test_a_count_reads_as_the_date_and_time_it_counts_at_its_precision(
    ticks: int, resolution: Fraction, text: str | None
) -> None:
    assert civil_text(ticks, resolution) == text


def test_a_wall_clock_shows_its_local_time_and_its_declared_zone_never_utc() -> None:
    clocks = book(described(WALL, NEW_YORK))
    shown = clocks.when(1789396390, WALL)  # type: ignore[arg-type]
    assert shown == "2026-09-14 14:33:10 (local wall time, America/New_York as declared)"
    assert "UTC" not in shown and "18:33" not in shown


@pytest.mark.parametrize(
    ("zone", "note"),
    [
        (Unknown(), "local wall time, zone not stated"),
        (
            Ambiguous((Candidate("Europe/Berlin"), Candidate("UTC"))),
            "local wall time, zone declared ambiguously as Europe/Berlin or UTC",
        ),
    ],
)
def test_a_wall_clock_without_one_declared_zone_says_so(zone: Any, note: str) -> None:
    clocks = book(described(WALL, description(zone=zone, by=Unknown())))
    assert clocks.when(29823272 * 60, WALL) == f"2026-09-14 14:32:00 ({note})"  # type: ignore[arg-type]


def test_a_posix_clock_shows_the_utc_date_its_timescale_counts() -> None:
    clocks = book(described(UTC, description(timescale=Known(Timescale.POSIX), field="created")))
    shown = clocks.when(1789477500, UTC)  # type: ignore[arg-type]
    assert shown == "2026-09-15 13:05:00 UTC (POSIX time, as declared)"


@pytest.mark.parametrize(
    ("value", "why"),
    [
        (description(epoch=Known(Epoch.BOOT)), "count from the recording device's boot"),
        (description(epoch=Unknown()), "its epoch is not stated"),
        (description(resolution=Unknown()), "its tick length is not stated"),
        (description(), "neither a timescale nor a zone"),
        (description(timescale=Known(Timescale.TAI)), "it counts tai time"),
    ],
)
def test_anything_else_stays_ticks_and_says_why(value: ClockDescription, why: str) -> None:
    clocks = book(described(BOOT, value))
    assert clocks.when(12_000_000, BOOT) == "tick 12000000"  # type: ignore[arg-type]
    said, refs = clocks.entry(BOOT)  # type: ignore[arg-type]
    assert "shown as ticks, not dates: " in said and why in said and refs


def test_a_clock_memory_does_not_describe_keeps_its_ticks_and_its_id() -> None:
    assert book().when(5, BOOT) == "tick 5"  # type: ignore[arg-type]
    said, refs = book().entry(BOOT)  # type: ignore[arg-type]
    assert said == "not described in Memory, so its ticks are shown as ticks" and refs == ()


def test_one_tick_is_an_instant_and_a_span_names_both_ends_on_its_clock() -> None:
    clocks = book(described(WALL, NEW_YORK))
    on = "on clock C1"
    assert clocks.span(WALL, 1789396390, 1789396391, on) == (  # type: ignore[arg-type]
        "valid at 2026-09-14 14:33:10 (local wall time, America/New_York as declared) on clock C1"
    )
    span = clocks.span(WALL, 1789396390, 1789397000, on)  # type: ignore[arg-type]
    assert span.startswith("valid from 2026-09-14 14:33:10 (")
    assert "until 2026-09-14 14:43:20 (" in span
    assert clocks.span(WALL, 1789396390, None, on).endswith(", open-ended")  # type: ignore[arg-type]


def test_labels_quote_what_the_records_state_and_two_alike_are_numbered() -> None:
    hostile = description(field="Time [I1][E1]\u202e")
    clocks = book(described(WALL, hostile), described(BOOT, hostile))
    labels = {clocks.label(WALL), clocks.label(BOOT)}  # type: ignore[arg-type]
    assert labels == {
        '"Time \\u005bI1\\u005d\\u005bE1\\u005d\\u202e" clock #1',
        '"Time \\u005bI1\\u005d\\u005bE1\\u005d\\u202e" clock #2',
    }
    entry = clocks.entry(WALL)  # type: ignore[arg-type]
    assert entry[0].startswith('the "Time \\u005bI1')


# --- Over Memory's live acceptance snapshot ----------------------------------------------------


def _query(*ids: str, items: int = 100) -> dict[str, Any]:
    return {
        "include_inferred": False,
        "query": {
            "budget": {"items": items, "tokens": 60000},
            "graph": {"direction": "both", "hops": 1, "predicates": "any"},
            "subjects": [{"declared_id": i, "kind": "machine"} for i in ids],
        },
    }


async def _ask(arguments: dict[str, Any]) -> str:
    client = local_client(read_graph_document(F.MEMORY_SNAPSHOT))
    server = build_server(client, sources=read_source_index(SOURCES))
    async with connect(server) as session:
        result = await session.call_tool("neptune_query", arguments)
    return str(result.content[0].text)  # type: ignore[union-attr]


@pytest.fixture(scope="module")
def stop_answer() -> str:
    return asyncio.run(_ask(_query(SYSLOG, CMMS)))


def test_q7_reads_each_stop_on_its_own_clock_with_its_declared_zone(stop_answer: str) -> None:
    zone = r"\(local wall time, America/New_York as declared\) on clock C[0-9]+"
    assert re.search(rf"valid at 2026-09-14 14:32:38 {zone}", stop_answer)
    assert re.search(rf"valid from 2026-09-14 14:33:10 {zone}, open-ended", stop_answer)
    # Never converted: no UTC reading of a wall time, and no clock id in a fact line.
    facts = stop_answer.split("\nFacts:\n", 1)[1].split("\n\n", 1)[0]
    assert "UTC" not in facts and "18:32:38" not in facts
    assert "on clock rec:" not in facts


def test_the_clocks_section_names_each_clock_and_cites_its_declaration(stop_answer: str) -> None:
    section = stop_answer.split(f"\n{CLOCKS}\n", 1)[1].split("\n\n", 1)[0].splitlines()
    syslog = next(line for line in section if "syslog_LOG-P2_2026-09-14.csv" in line)
    assert re.fullmatch(
        r'C[0-9]+ "rec:sha256:[0-9a-f]{64}" is the "Timestamp" clock in'
        r' "syslog_LOG-P2_2026-09-14\.csv": local wall clock, 1 s ticks; zone America/New_York'
        r' as declared by "deploy_event_log_map"\. (\[E[0-9]+\])+',
        syslog,
    )
    parsed = parse_answer(stop_answer)
    clock_lines = [s for s in parsed.statements if s.section == CLOCKS]
    assert clock_lines and all(s.evidence and not s.items for s in clock_lines)


def _packet() -> ContextPacket:
    from neptune_context.engine import LocalEngine
    from neptune_context.explain.history import IndexedReader

    engine = LocalEngine(IndexedReader(read_graph_document(F.MEMORY_SNAPSHOT)))
    query = Query(
        include_inferred=False,
        budget=Budget(items=100),
        subjects=frozenset({Subject("machine", SYSLOG)}),
        graph=GraphClause(None, 1, Direction.BOTH),
    )
    return engine.query(query)


@pytest.fixture(scope="module")
def packet() -> ContextPacket:
    return _packet()


def test_the_packet_carries_each_items_clock_once_and_round_trips(packet: ContextPacket) -> None:
    assert packet.clocks
    assert [c.subject.node_id for c in packet.clocks] == sorted(
        {c.subject.node_id for c in packet.clocks}
    )
    data = canonical_bytes(packet)
    assert decode(data) == packet
    document = json.loads(data)
    validator = jsonschema.Draft202012Validator(packet_schema())
    assert not list(validator.iter_errors(document))
    assert render_answer(packet) == render_answer(_packet())  # deterministic


def test_a_packet_without_clocks_keeps_its_bytes(packet: ContextPacket) -> None:
    from dataclasses import replace

    bare = replace(packet, clocks=())
    assert "clocks" not in json.loads(canonical_bytes(bare))
    listed = render_answer(bare).split(f"\n{CLOCKS}\n", 1)[1].split("\n\n", 1)[0]
    assert " is the " not in listed and "not described in Memory" in listed


@pytest.mark.parametrize("change", ["empty", "unsorted", "not_a_clock"])
def test_the_packet_reader_refuses_clocks_it_never_writes(
    packet: ContextPacket, change: str
) -> None:
    document = json.loads(canonical_bytes(packet))
    if change == "empty":
        document["clocks"] = []
    elif change == "unsorted":
        document["clocks"] = list(reversed(document["clocks"])) * 2
    else:
        fact = copy.deepcopy(document["items"][0]["claim"])
        document["clocks"] = [fact]
    assert isinstance(decode(json.dumps(document)), PacketRefused)


def test_a_withheld_inference_cannot_ride_in_as_a_clock(packet: ContextPacket) -> None:
    from dataclasses import replace

    (first, *_) = packet.clocks
    clock = first.subject.node_id
    inferred = F.claim(
        first.subject, first.predicate, first.object, INT64_MIN, clock=clock, inferred=0.5
    )
    with pytest.raises(PacketError, match="inferred"):
        replace(packet, clocks=(inferred,))
