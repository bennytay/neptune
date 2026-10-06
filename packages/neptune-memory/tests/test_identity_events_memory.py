"""Identity joins events an operator states are one (ADR 0019 §1).

An arm cell's protective stop is stated twice: by the controller's syslog (a row of an event table)
and by the CMMS downtime log, entered by hand 32 s later (an ``intervention`` declaring
``cmms.downtime:DT-0914-01``). ``memory.events`` keys each by its record and never relates them.
A person's ``same_identity`` assertion naming both, by declared id or by record id, is the one
ground on which ``memory.identity`` joins them: ``same_as``, stated, citing the assertion and
listing both event records. Without the assertion nothing joins them.

Retraction and ambiguity are ADR 0008's: a retracted assertion grounds nothing, an ``Ambiguous``
identifier, retraction or scope entry (an id several event records declare, or one may) gives
candidates and never ``same_as``. A record id that names no event names evidence and is not read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pytest

from memory_event_records import SECOND, incident, intervention, table
from memory_identity_records import Record, ambiguous, assertion, ledger, thread
from memory_run_records import NS, domain, mapping
from neptune.identity import canonical_json
from neptune.model.assertion import AssertionType
from neptune.model.ids import LogicalId, RecordId
from neptune.model.knowledge import AssertionKind, Known
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune.model.time import Epoch, Timescale, Timestamp
from neptune_memory.consolidate.base import (
    EVENTS_CONSOLIDATOR_ID,
    Consolidation,
    rebuild,
)
from neptune_memory.consolidate.event_records import resolve_config
from neptune_memory.consolidate.events import RECORD_NAMESPACE, EventConsolidator, event_node
from neptune_memory.consolidate.identity import (
    EVIDENCED_BY,
    SAME_AS,
    SAME_AS_CANDIDATE,
    IdentityConsolidator,
    node_ref,
)
from neptune_memory.consolidate.runs import EVIDENCED_BY as RUNS_EVIDENCED_BY
from neptune_memory.schema.interval import OPEN, CivilClock, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.knowledge import Knowledge

SAME, DISTINCT, RETRACT = (
    AssertionType.SAME_IDENTITY,
    AssertionType.DISTINCT_IDENTITY,
    AssertionType.RETRACT,
)
TX: Final = ledger_tx(3)
CIVIL: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, NS)
T0: Final = 1_789_396_358 * SECOND  # the protective stop, POSIX nanoseconds
ARM: Final = LogicalId("cmms.asset", "ARM-3A")
DOWNTIME: Final = LogicalId("cmms.downtime", "DT-0914-01")
SYSLOG_SEQ: Final = LogicalId("plant.syslog", "4182")  # declared by nothing: no event table id


def _config() -> dict[str, Any]:
    return {
        "vendors": {"syslog": {"text": {"err": "fault", "notice": "not_an_event"}}},
        "tables": [
            {
                "name": "syslog",
                "vendor": "syslog",
                "kind": "severity",
                "at": {"seconds": "sec", "nanoseconds": "nsec"},
                "clock": {"column": "@clock:ts"},
                "machine": {"column": "host", "namespace": "syslog.host"},
                "description": "msg",
            }
        ],
    }


def cell(
    *extra: Record,
    downtime_ids: Sequence[LogicalId] | Knowledge[tuple[Knowledge[LogicalId], ...]] = (DOWNTIME,),
    second_downtime: bool = False,
) -> tuple[dict[str, list[Record]], dict[str, Any]]:
    """The two statements of one stop, on two civil clocks, in two packages."""
    cmms_record, cmms = domain("cmms export", civil=True)
    sys_record, sys_clock = domain("syslog collector", civil=True)
    stop, stop_id = intervention(
        "downtime_log.csv row 7",
        start=Timestamp(T0 + 32 * SECOND, cmms),
        mode="Protective stop",
        machines=[ARM],
        identifiers=downtime_ids,
    )
    rows, table_id, row_ids = table(
        "syslog",
        ("sec", "nsec", "@clock:ts", "host", "severity", "msg"),
        [
            (T0 // SECOND - 2, 0, sys_clock, "ARM-3A", "notice", "program started"),
            (T0 // SECOND, 0, sys_clock, "ARM-3A", "err", "PSTOP: collision detection joint 5"),
        ],
    )
    cmms_package: list[Record] = [cmms_record, stop]
    built: dict[str, Any] = {"stop": stop_id, "table": table_id, "pstop": row_ids[1]}
    if second_downtime:  # a second log re-uses the id for another stop
        other, other_id = intervention(
            "downtime_log_b.csv row 2",
            start=Timestamp(T0 + 90 * SECOND, cmms),
            mode="Protective stop",
            machines=[ARM],
            identifiers=(DOWNTIME,),
        )
        cmms_package.append(other)
        built["other"] = other_id
    return {
        "cmms": cmms_package,
        "syslog": [sys_record, *rows],
        "assertions": list(extra),
    }, built


def run(packages: Mapping[str, Sequence[Record]]) -> tuple[Consolidation, Consolidation]:
    """Events, then identity reading events' claims (ADR 0019 §2)."""
    events, identity = rebuild(
        ledger(packages),
        [(EventConsolidator(), resolve_config(_config())), (IdentityConsolidator(), {})],
        recorded_at=TX,
    )
    return events, identity


def of(result: Consolidation, predicate: str) -> list[Any]:
    return [c for c in result.claims if c.predicate == predicate]


def codes(result: Consolidation) -> list[str]:
    return sorted(f.code for f in result.findings)


def authored(seconds: int = 0) -> Timestamp:
    return CIVIL.at(T0 + (86_400 + seconds) * SECOND)


def stop_nodes(built: Mapping[str, Any]) -> tuple[NodeRef, NodeRef]:
    return event_node(built["stop"]), event_node(built["pstop"])


# --- Without and with the assertion --------------------------------------------------------------


def test_without_an_assertion_two_statements_of_one_stop_stay_two_events() -> None:
    packages, built = cell()
    events, identity = run(packages)
    stop, pstop = stop_nodes(built)
    subjects = {c.subject for c in events.claims}
    assert {stop, pstop} <= subjects
    assert not identity.claims
    assert not identity.findings


def test_an_operator_assertion_joins_the_two_events_with_a_stated_same_as() -> None:
    packages, built = cell()
    said = assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    packages["assertions"] = [said]
    _, identity = run(packages)
    (claim,) = of(identity, SAME_AS)
    stop, pstop = stop_nodes(built)
    assert {claim.subject, claim.object} == {stop, pstop}
    assert claim.subject.node_id < claim.object.node_id  # the lower key is the subject
    assert claim.assertion_kind is AssertionKind.STATED
    assert claim.valid_from == authored()
    assert claim.valid_to == OPEN
    # Cites the assertion, and lists both event records it joins.
    assert [ref.source for ref in claim.provenance.evidence] == [
        ref.source for ref in _evidence(said)
    ]
    assert set(claim.provenance.records) == {said["id"], built["stop"], built["pstop"]}
    assert not identity.findings


def test_record_ids_and_declared_ids_name_the_same_events() -> None:
    packages, built = cell()
    by_record = assertion("ASR-REC", SAME, (built["pstop"], built["stop"]), authored_at=authored())
    packages["assertions"] = [by_record]
    _, identity = run(packages)
    (claim,) = of(identity, SAME_AS)
    assert {claim.subject, claim.object} == set(stop_nodes(built))


def test_identity_runs_on_event_claims_only_from_memory_events() -> None:
    packages, built = cell()
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    ]
    events, _ = run(packages)
    assert {c.provenance.consolidator_id for c in events.claims} == {EVENTS_CONSOLIDATOR_ID}
    # Without events' claims, the record ids name evidence (ADR 0008 §3) and nothing is joined.
    alone = rebuild(ledger(packages), [(IdentityConsolidator(), {})], recorded_at=TX)[0]
    assert not alone.claims
    assert codes(alone) == ["identity.assertion_scope"]


def test_the_vocabulary_and_node_keys_are_events_own() -> None:
    assert EVIDENCED_BY == RUNS_EVIDENCED_BY
    assert RECORD_NAMESPACE == "record"
    record: RecordId = "rec:sha256:" + "0" * 64  # type: ignore[assignment]
    assert node_ref(NodeType.EVENT, LogicalId(RECORD_NAMESPACE, record)) == event_node(record)


# --- Retraction and ambiguity, as for every same_as ---------------------------------------------


def test_a_retracted_assertion_joins_nothing_and_a_retracted_retraction_restores_it() -> None:
    packages, built = cell()
    said = assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    withdrawn = assertion(
        "ASR-WD",
        RETRACT,
        (),
        retracts=LogicalId("ops-console", "ASR-STOP"),
        authored_at=authored(9),
    )
    packages["assertions"] = [said, withdrawn]
    _, identity = run(packages)
    assert not identity.claims

    restored = assertion(
        "ASR-RS", RETRACT, (), retracts=LogicalId("ops-console", "ASR-WD"), authored_at=authored(20)
    )
    packages["assertions"] = [said, withdrawn, restored]
    _, again = run(packages)
    (claim,) = of(again, SAME_AS)
    assert said["id"] in claim.provenance.records


def test_an_ambiguous_retraction_leaves_candidates_citing_the_retract() -> None:
    packages, built = cell()
    said = assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    maybe = assertion(
        "ASR-MAYBE",
        RETRACT,
        (),
        retracts=ambiguous(
            "ops console",
            LogicalId("ops-console", "ASR-STOP"),
            LogicalId("ops-console", "ASR-OTHER"),
        ),
        authored_at=authored(9),
    )
    packages["assertions"] = [said, maybe]
    _, identity = run(packages)
    assert not of(identity, SAME_AS)
    candidates = of(identity, SAME_AS_CANDIDATE)
    assert {(c.subject, c.object) for c in candidates} == {
        stop_nodes(built),
        stop_nodes(built)[::-1],
    }
    assert all(maybe["id"] in c.provenance.records for c in candidates)
    assert "identity.retraction_ambiguous" in codes(identity)


def test_an_ambiguous_assertion_identifier_is_candidates_never_same_as() -> None:
    packages, built = cell()
    packages["assertions"] = [
        assertion(
            "ASR-STOP",
            SAME,
            (DOWNTIME, built["pstop"]),
            identifier=ambiguous("ops console", LogicalId("ops", "a"), LogicalId("ops", "b")),
            authored_at=authored(),
        )
    ]
    _, identity = run(packages)
    assert not of(identity, SAME_AS)
    assert len(of(identity, SAME_AS_CANDIDATE)) == 2


def test_an_id_two_event_records_declare_is_candidates_for_each() -> None:
    packages, built = cell(second_downtime=True)
    said = assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    packages["assertions"] = [said]
    _, identity = run(packages)
    assert not of(identity, SAME_AS)
    pstop = event_node(built["pstop"])
    pairs = {(c.subject, c.object) for c in of(identity, SAME_AS_CANDIDATE)}
    expected = {event_node(built["stop"]), event_node(built["other"])}
    assert pairs == {(e, pstop) for e in expected} | {(pstop, e) for e in expected}
    (finding,) = [f for f in identity.findings if f.code == "identity.scope_ambiguous"]
    assert set(finding.records) == {said["id"], built["stop"], built["other"], built["pstop"]}
    assert finding.details == {"events": sorted((built["stop"], built["other"]))}


def test_an_id_an_event_record_only_possibly_declares_is_a_candidate() -> None:
    possible = Known((ambiguous("downtime_log.csv", DOWNTIME, LogicalId("cmms.downtime", "X")),))
    packages, built = cell(downtime_ids=possible)
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    ]
    _, identity = run(packages)
    assert not of(identity, SAME_AS)
    assert {(c.subject, c.object) for c in of(identity, SAME_AS_CANDIDATE)} == {
        stop_nodes(built),
        stop_nodes(built)[::-1],
    }
    assert "identity.scope_ambiguous" in codes(identity)


def test_a_record_and_the_id_it_declares_are_one_entry_and_one_event_record_is_certain() -> None:
    packages, built = cell(second_downtime=True)
    # The record id settles which DT-0914-01 is meant only for itself: the id still names two.
    packages["assertions"] = [
        assertion("ASR-ONE", SAME, (built["stop"], built["pstop"]), authored_at=authored())
    ]
    _, identity = run(packages)
    (claim,) = of(identity, SAME_AS)
    assert {claim.subject, claim.object} == set(stop_nodes(built))


def test_distinct_identity_between_events_suppresses_their_candidates() -> None:
    packages, built = cell(second_downtime=True)
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored()),
        assertion("ASR-APART", DISTINCT, (built["other"], built["pstop"]), authored_at=authored(5)),
    ]
    _, identity = run(packages)
    pairs = {(c.subject, c.object) for c in of(identity, SAME_AS_CANDIDATE)}
    assert pairs == {stop_nodes(built), stop_nodes(built)[::-1]}


def test_a_same_as_across_a_declared_distinctness_between_events_is_contested() -> None:
    packages, built = cell()
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored()),
        assertion("ASR-APART", DISTINCT, (built["stop"], built["pstop"]), authored_at=authored(5)),
    ]
    _, identity = run(packages)
    assert len(of(identity, SAME_AS)) == 1
    assert "identity.contested" in codes(identity)


# --- Malformed and boundary scopes -------------------------------------------------------------


def test_a_record_id_naming_no_event_is_evidence_and_leaves_too_few_entries() -> None:
    packages, built = cell()
    packages["assertions"] = [
        assertion("ASR-TABLE", SAME, (built["table"], built["pstop"]), authored_at=authored())
    ]
    _, identity = run(packages)
    assert not identity.claims
    (finding,) = identity.findings
    assert finding.code == "identity.assertion_scope"
    assert finding.details == {"unread_records": 1}


def test_an_id_no_thread_and_no_event_declares_dangles() -> None:
    packages, _ = cell()
    packages["assertions"] = [
        assertion("ASR-SEQ", SAME, (DOWNTIME, SYSLOG_SEQ), authored_at=authored())
    ]
    _, identity = run(packages)
    assert not identity.claims
    assert codes(identity) == ["identity.dangling_link"]


def test_one_event_named_twice_is_one_entry() -> None:
    packages, built = cell()
    packages["assertions"] = [
        assertion("ASR-SELF", SAME, (DOWNTIME, built["stop"]), authored_at=authored())
    ]
    _, identity = run(packages)
    assert not identity.claims
    assert codes(identity) == ["identity.assertion_scope"]


def test_an_event_and_a_machine_are_never_joined() -> None:
    packages, built = cell()
    packages["threads"] = [thread(ARM, "asset register")]
    packages["assertions"] = [
        assertion("ASR-TYPE", SAME, (ARM, built["pstop"]), authored_at=authored())
    ]
    _, identity = run(packages)
    assert not identity.claims
    assert codes(identity) == ["identity.type_mismatch"]


def test_a_ledger_thread_keeps_its_node_for_an_id_an_event_also_declares() -> None:
    packages, built = cell()
    packages["threads"] = [thread(DOWNTIME, "downtime register")]
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    ]
    _, identity = run(packages)
    assert not identity.claims  # a thread node is never an event: no join across types
    assert codes(identity) == ["identity.type_mismatch"]


def test_a_malformed_event_record_is_events_finding_and_joins_nothing() -> None:
    packages, built = cell()
    broken = dict(packages["cmms"][1])
    broken["identifiers"] = {"state": "known", "value": "not a list"}
    packages["cmms"][1] = broken
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    ]
    events, identity = run(packages)
    assert any(f.code == "events.malformed_record" for f in events.findings)
    assert not identity.claims
    assert codes(identity) == ["identity.dangling_link"]


def test_a_record_names_its_own_event_never_its_timeline_entries() -> None:
    packages, built = cell()
    clock_record, clock = domain("hmi", civil=True)
    report, report_id = incident(
        "INC-C3 report",
        occurred=Timestamp(T0, clock),
        timeline=[(Timestamp(T0 + SECOND, clock), "E-stop at OP-2")],
    )
    packages["hmi"] = [clock_record, report]
    packages["assertions"] = [
        assertion("ASR-TL", SAME, (report_id, built["pstop"]), authored_at=authored())
    ]
    events, identity = run(packages)
    assert event_node(report_id, "timeline", 0) in {c.subject for c in events.claims}
    (claim,) = of(identity, SAME_AS)
    assert {claim.subject, claim.object} == {event_node(report_id), event_node(built["pstop"])}


def test_an_unstated_authored_time_holds_from_the_subject_events_own_start() -> None:
    boot_record, boot = domain("controller boot", civil=False)
    cmms_record, cmms = domain("cmms export", civil=True)
    rows, _, (pstop,) = table(
        "syslog",
        ("sec", "nsec", "@clock:ts", "host", "severity", "msg"),
        [(5_000, 0, boot, "ARM-3A", "err", "PSTOP")],
    )
    stop, stop_id = intervention("downtime row", start=Timestamp(T0, cmms), identifiers=(DOWNTIME,))
    ntp = mapping("controller ntp", boot, cmms, anchor=(5_000 * SECOND, T0), bound=SECOND)
    packages: dict[str, list[Record]] = {
        "cell": [boot_record, cmms_record, *rows, stop, ntp],
        "assertions": [assertion("ASR-NT", SAME, (pstop, stop_id), authored_at=None)],
    }
    events, identity = run(packages)
    (claim,) = of(identity, SAME_AS)
    subject_record = stop_id if claim.subject == event_node(stop_id) else pstop
    own = [c for c in events.claims if c.subject == claim.subject and c.predicate == EVIDENCED_BY]
    primary = min(own, key=lambda c: len(c.provenance.records))
    assert len(own) >= 1
    assert claim.valid_from == primary.valid_from  # its own clock, never a mapping's projection
    assert subject_record in claim.provenance.records


# --- Determinism -----------------------------------------------------------------------------


def _document(packages: Mapping[str, Sequence[Record]]) -> bytes:
    return b"\n".join(canonical_json.dumps(result.to_json()) for result in run(packages))


@pytest.mark.parametrize("second", [False, True])
def test_the_build_is_byte_identical_and_independent_of_package_order(second: bool) -> None:
    packages, built = cell(second_downtime=second)
    packages["assertions"] = [
        assertion("ASR-STOP", SAME, (DOWNTIME, built["pstop"]), authored_at=authored())
    ]
    first = _document(packages)
    assert _document(packages) == first
    assert _document(dict(reversed(list(packages.items())))) == first


def _evidence(record: Record) -> list[EvidenceRef]:
    return [evidence_ref_from_json(record["provenance"]["evidence"])]  # type: ignore[index]
