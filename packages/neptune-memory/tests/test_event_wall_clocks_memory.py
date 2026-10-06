"""INC-C3-0011's two stops as Deploy writes them (Deploy ADR 0016, 0017): a syslog export mapped to
a typed ``syslog events`` table and a CMMS downtime row mapped to an ``intervention``, each on its
own civil *wall* clock. Their ticks count seconds from 1970-01-01T00:00:00 of that wall clock
(``epoch: unix``, ``timescale`` Unknown): they look like POSIX seconds and are not. The event index
keeps every placement on the record's own domain, never on a UTC ``CivilClock``. The two stops
(32 s apart as written) are compared only through a stated mapping between the two clocks;
without one they are ``events.clocks_unrelated``, whatever the window.

The declaration is the acceptance snapshot's own
(``fixtures/acceptance_corpus.memory_config.json``), passed to ``memory rebuild --config``, whose
resolved config hash is in every claim's provenance and in the ``MemorySnapshot``.
"""

from __future__ import annotations

import io
import json
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pytest

from memory_event_records import intervention, table
from memory_identity_records import Record, cite, ledger, provenance, rid
from memory_run_records import mapping
from neptune.identity import canonical_json
from neptune.identity.ids import config_hash
from neptune.model.knowledge import Known, NotCovered, Unknown
from neptune.model.reference import TimestampDomain
from neptune.model.time import ClockRole, Epoch, Timestamp
from neptune_memory.cli import OK, USAGE, main, registrations
from neptune_memory.consolidate.base import Consolidation, run_consolidator
from neptune_memory.consolidate.event_records import resolve_config
from neptune_memory.consolidate.events import EVENTS_CONSOLIDATOR_ID, EventConsolidator, event_node
from neptune_memory.ledger import ExportedPackage, LedgerExport
from neptune_memory.schema.claim import TypedLiteral, ValueType
from neptune_memory.schema.codec import graph_from_json
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES

if TYPE_CHECKING:
    from neptune.model.ids import RecordId
    from neptune.model.jsonvalue import JsonValue

CONFIG_FILE: Final = (
    Path(__file__).resolve().parent / "fixtures" / "acceptance_corpus.memory_config.json"
)
SNAPSHOT_CONFIG: Final[dict[str, Any]] = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
EVENTS_CONFIG: Final[dict[str, Any]] = SNAPSHOT_CONFIG[EVENTS_CONSOLIDATOR_ID]
TX = ledger_tx(2)
SECOND: Final = Fraction(1)
# 2026-09-14 on PLANT-2's wall clock, in seconds from 1970-01-01T00:00:00 of that clock.
PSTOP_AT: Final = 1_789_396_358  # 14:32:38, syslog 4182
ESTOP_AT: Final = 1_789_396_361  # 14:32:41, syslog 4183
CMMS_AT: Final = 1_789_396_390  # 14:33:10, DT-26-0914-01


def wall_clock(name: str) -> tuple[Record, RecordId]:
    """A ``timestamp_domain`` as Deploy's time reader writes one for a zone-less column: a document
    clock to the second, counted from the Unix epoch's date and time of day, timescale Unknown."""
    declared = provenance(cite(f"wall clock {name}"))
    record = TimestampDomain(
        id=rid("timestamp_domain", declared.evidence),
        provenance=declared,
        field=name,
        scope=(),
        role=Known(ClockRole.DOCUMENT),
        resolution=Known(SECOND),
        epoch=Known(Epoch.UNIX),
        timescale=Unknown(),
        declared_monotonic=NotCovered(),
    )
    return record.to_json(), record.id  # type: ignore[return-value]


SYSLOG_HEADER: Final = (
    "Seq",
    "Host",
    "Facility",
    "Severity",
    "Tag",
    "MsgID",
    "Message",
    "Timestamp",
    "Timestamp.sec",
    "Timestamp.nanosec",
    "@clock:Timestamp",
    "@id:syslog",
)


def plant(*, mapped: bool = False, bound: int | None = 0) -> tuple[list[Record], dict[str, Any]]:
    """The two stops on their own wall clocks; ``mapped`` adds a stated identity mapping
    syslog -> CMMS whose residual bound is ``bound`` seconds (``None``: it states none)."""
    sys_clock, syslog = wall_clock("syslog Timestamp")
    cmms_clock, cmms = wall_clock("downtime Stopped")
    rows, _, row_ids = table(
        "syslog events",
        SYSLOG_HEADER,
        [
            ("4170", "ARM-3A", "user", "notice", "PALLET_C3", "PGM_START",
             "program PALLET_C3 1.4.0 started from the HMI", "2026-09-14 14:28:00",
             PSTOP_AT - 278, 0, syslog, "4170"),
            ("4182", "ARM-3A", "local0", "err", "SAFETY", "PSTOP",
             "PSTOP: collision detection joint 5", "2026-09-14 14:32:38",
             PSTOP_AT, 0, syslog, "4182"),
            ("4183", "PLC-C3", "local0", "crit", "SAFETY", "ESTOP", "ESTOP: OP-2.ES1 pressed",
             "2026-09-14 14:32:41", ESTOP_AT, 0, syslog, "4183"),
        ],
        file="syslog_LOG-P2_2026-09-14.csv",
    )  # fmt: skip
    stop, stop_id = intervention(
        "downtime_log.csv DT-26-0914-01",
        start=Timestamp(CMMS_AT, cmms),
        mode="Protective stop",
        reason="Collision at pick P1; E-stop at OP-2",
    )
    records = [sys_clock, cmms_clock, *rows, stop]
    if mapped:
        records.append(mapping("site survey", syslog, cmms, anchor=(0, 0), bound=bound))
    return records, {
        "syslog": syslog,
        "cmms": cmms,
        "pstop": event_node(row_ids[1]),
        "estop": event_node(row_ids[2]),
        "start": event_node(row_ids[0]),
        "stop": event_node(stop_id),
    }


def consolidate(records: list[Record], window: str = "5") -> Consolidation:
    config = {**EVENTS_CONFIG, "co_occurrence": {"window_seconds": window}}
    return run_consolidator(
        EventConsolidator(),
        ledger({"plant-2": records}),
        (),
        resolve_config(config),
        recorded_at=TX,
        registry=CORE_PREDICATES,
    )


def text(value: str) -> TypedLiteral:
    return TypedLiteral(ValueType.TEXT, value)


def kinds(result: Consolidation) -> dict[object, set[object]]:
    out: dict[object, set[object]] = {}
    for claim in result.claims:
        if claim.predicate == "event_kind":
            out.setdefault(claim.subject, set()).add(claim.object)
    return out


def test_the_declared_syslog_table_becomes_typed_events_on_its_own_wall_clock() -> None:
    records, at = plant()
    result = consolidate(records)
    assert not [f for f in result.findings if f.code == "events.invalid_config"]
    assert kinds(result) == {
        at["pstop"]: {text("protective_stop")},
        at["estop"]: {text("emergency_stop")},
        at["stop"]: {text("protective_stop")},  # the CMMS Stop Type, through the declared map
    }
    # PGM_START maps to no registered kind: an event with its declared kind, never a guessed one.
    declared = {c.subject: c.object for c in result.claims if c.predicate == "declared_kind"}
    assert declared[at["start"]] == text("PGM_START")
    assert "events.kind_unmapped" in {f.code for f in result.findings}
    # Every placement is on the record's own wall clock, at the ticks it states.
    placed = {c.subject: c.valid_from for c in result.claims if c.predicate == "evidenced_by"}
    assert placed[at["pstop"]] == Timestamp(PSTOP_AT, at["syslog"])
    assert placed[at["stop"]] == Timestamp(CMMS_AT, at["cmms"])
    domains = {
        bound.domain_id
        for c in result.claims
        for bound in (c.valid_from, c.valid_to)
        if isinstance(bound, Timestamp)
    }
    assert domains == {at["syslog"], at["cmms"]}  # no UTC CivilClock: the ticks are not instants


@pytest.mark.parametrize("window", ["5", "60", "86400"])
def test_without_a_stated_mapping_the_two_stops_are_unrelated_whatever_the_window(
    window: str,
) -> None:
    """32 s apart as written, and both look like epoch seconds: comparing them would be the
    silent assumption that two wall clocks agree."""
    records, at = plant()
    result = consolidate(records, window)
    assert not [c for c in result.claims if c.predicate == "co_occurs_within"]
    (unrelated,) = [f for f in result.findings if f.code == "events.clocks_unrelated"]
    assert sorted(unrelated.details["clocks"]) == sorted([at["syslog"], at["cmms"]])  # type: ignore[arg-type,type-var]


def test_a_stated_bounded_mapping_compares_them_on_the_cmms_clock() -> None:
    records, at = plant(mapped=True)
    result = consolidate(records, "60")
    pairs = {(c.subject, c.object) for c in result.claims if c.predicate == "co_occurs_within"}
    assert (at["pstop"], at["stop"]) in pairs and (at["stop"], at["pstop"]) in pairs
    for claim in result.claims:
        if claim.predicate == "co_occurs_within" and claim.subject == at["pstop"]:
            assert isinstance(claim.valid_from, Timestamp)
            assert claim.valid_from.domain_id == at["cmms"]
    # The default 5 s window: 32 s apart is not "at the same time".
    assert not [
        c
        for c in consolidate(records).claims
        if c.predicate == "co_occurs_within" and at["stop"] in (c.subject, c.object)
    ]


def test_a_mapping_that_states_no_bound_decides_nothing() -> None:
    records, at = plant(mapped=True, bound=None)
    result = consolidate(records, "60")
    assert not [
        c
        for c in result.claims
        if c.predicate == "co_occurs_within" and at["stop"] in (c.subject, c.object)
    ]
    assert "events.co_occurrence_unbounded" in {f.code for f in result.findings}


# --- memory rebuild --config ---------------------------------------------------------------------


def export(tmp_path: Path) -> Path:
    records, _ = plant()
    path = tmp_path / "ledger.json"
    exported = LedgerExport(1, "stub", (ExportedPackage("plant-2", 8, 1, tuple(records)),))
    path.write_bytes(canonical_json.dumps(exported.to_json()))  # type: ignore[arg-type]
    return path


def memory(tmp_path: Path, *argv: str) -> tuple[int, str]:
    err = io.StringIO()
    base = ["--graphs", str(tmp_path / "graphs"), "--tenant", "t"]
    status = main([*base, *argv], stdout=io.StringIO(), stderr=err)
    return status, err.getvalue()


def rebuild(tmp_path: Path, *extra: str) -> Any:
    argv = ("rebuild", "--ledger", str(export(tmp_path)), "--snapshot", "1", *extra)
    assert memory(tmp_path, *argv) == (OK, "")
    return graph_from_json(json.loads((tmp_path / "graphs" / "t" / "graph.json").read_text()))


def events_hash(document: Any) -> str:
    (build,) = [b for b in document.builds if b.consolidator_id == EVENTS_CONSOLIDATOR_ID]
    return str(build.config_hash)


def test_without_config_the_syslog_table_is_not_read(tmp_path: Path) -> None:
    document = rebuild(tmp_path)
    assert events_hash(document) == config_hash(resolve_config({}))
    declared = {c.object for c in document.resolution.claims if c.predicate == "declared_kind"}
    assert declared == {text("Protective stop")}  # the CMMS stop's mode; no syslog row is read


def test_the_config_file_is_resolved_and_hashed_into_every_claim(tmp_path: Path) -> None:
    document = rebuild(tmp_path, "--config", str(CONFIG_FILE))
    expected = config_hash(resolve_config(EVENTS_CONFIG))
    assert events_hash(document) == expected
    claims = [
        c
        for c in document.resolution.claims
        if c.provenance.consolidator_id == EVENTS_CONSOLIDATOR_ID
    ]
    assert claims and {str(c.provenance.config_hash) for c in claims} == {expected}
    snapshot = json.loads((tmp_path / "graphs" / "t" / "snapshots" / "1.json").read_text())
    (recorded,) = [
        c for c in snapshot["snapshot"]["consolidators"] if c["consolidator_id"] == "memory.events"
    ]
    assert recorded["config_hash"] == expected


def test_a_config_spelled_otherwise_hashes_the_same(tmp_path: Path) -> None:
    explicit = {**EVENTS_CONFIG, "co_occurrence": {"window_seconds": "5.0", "max_partners": 64}}
    path = tmp_path / "explicit.json"
    path.write_text(json.dumps({EVENTS_CONSOLIDATOR_ID: explicit}))
    assert events_hash(rebuild(tmp_path, "--config", str(path))) == config_hash(
        resolve_config(EVENTS_CONFIG)
    )


def test_registrations_keep_the_estimates_model_and_refuse_unknown_ids() -> None:
    given: dict[str, dict[str, JsonValue]] = {"memory.time_estimates": {}}
    (estimates,) = [
        r
        for r in registrations(with_estimates=True, configs=given)
        if r.consolidator_id == "memory.time_estimates"
    ]
    assert "model" in estimates.config
    with pytest.raises(ValueError, match="not registered"):
        registrations(with_estimates=False, configs=given)


@pytest.mark.parametrize(
    "content",
    [
        '{"memory.nothing": {}}',  # not a registered consolidator
        '{"memory.events": []}',  # a config is an object
        "[]",
        '{"memory.events": {}, "memory.events": {}}',  # a repeated key
        "{not json",
    ],
)
def test_an_unusable_config_file_is_a_usage_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(content)
    argv = ("rebuild", "--ledger", str(export(tmp_path)), "--snapshot", "1", "--config", str(path))
    status, err = memory(tmp_path, *argv)
    assert status == USAGE
    assert err.startswith("memory: ")
    assert not (tmp_path / "graphs" / "t" / "graph.json").exists()
