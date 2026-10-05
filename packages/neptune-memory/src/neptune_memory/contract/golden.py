"""The golden graph of graph-schema v1, from the compiler's four worked examples (ADR 0006 §10).

Inputs: the record lines of the drone, manipulator, mobile-robot and quadruped worked examples
and the fleet register (``tests/fixtures/model/``; the caller reads them), plus a *Ledger overlay*
derived from them: two operator logs whose bytes are in this module, and the two Ledger threads
the Ledger would declare for the drone's two ids (ADR 0003 §1's ``ledger_thread`` stand-in, until
Memory reads the catalog API). The operator's identity statement is the compiler's ``assertion``
kind (root ADR 0062) with its ``authored_at`` clock, as an assertions adapter would write it.

The Ledger grows over five transactions, and the same plan is rebuilt at each one:

- tx 1: drone and mobile robot. The fixture model guesses the mobile robot's recorder.
- tx 2: quadruped. The model guesses its recorder (``QUAD-07``) and an inferred
  ``same_as_candidate`` pair ``QUAD-07`` / ``QUAD-03``.
- tx 3: manipulator, the fleet register and operator log 1. The register's row co-declares the
  drone's asset tag and ``px4`` id (an ``IdentityLink``: ``same_as``), and the operator states the
  same identity (a ``same_identity`` assertion: a second ``same_as``, on civil time). The
  operator names the drone run's recorder by asset tag on civil time (a ``clock_mismatch``
  with the log's boot-clock claim) and by ``px4`` id on the run's own clock (corroborating the
  log), corrects the quadruped guess (superseded), and names the manipulator's recorder before
  the model's guess arrives (``overridden_on_arrival``).
- tx 4: operator log 2. Later statements narrow two earlier ones into split closures; the drone's
  ``clock_mismatch`` closes and the overridden guess's winner is superseded.
- tx 5: a package nothing consolidates: a transaction with no claims, so ``head`` is 5.

Plan, in order of priority (later arrives later within a transaction): ``golden.runs``
(deterministic), ``golden.operator`` (stated), ``memory.identity`` (the real identity policy),
``golden.fixture_model`` (inferred; ``_fixture_model``, golden only) and ``memory.time`` (the real
time-domain registry: the drone's clocks, and the quadruped's stated ``starting_time`` to
``log_time`` mapping). Same inputs give byte-identical canonical JSON. Every consolidation finding is a hard error: golden inputs are
clean by construction, so a finding means the inputs drifted.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.assertion import Assertion, AssertionType
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.knowledge import AssertionKind, Known, NotApplicable, NotCovered, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, evidence_ref_from_json
from neptune.model.reference import TimestampDomain
from neptune.model.time import Epoch, Timescale, Timestamp, timestamp_from_json
from neptune_memory.consolidate.base import (
    IDENTITY_CONSOLIDATOR_ID,
    ClaimDraft,
    ConsolidationFinding,
    ConsolidatorOutput,
    ModelRef,
    rebuild,
)
from neptune_memory.consolidate.identity import IdentityConsolidator
from neptune_memory.consolidate.time import TIME_CONSOLIDATOR_ID, TimeDomainConsolidator
from neptune_memory.contract._fixture_model import FIXTURE_MODEL, FIXTURE_MODEL_ID, _FixtureModel
from neptune_memory.contract.worked_examples import machine_node, run_node, runs
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import CivilClock, ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES
from neptune_memory.schema.supersede import resolve, resolver_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidator
    from neptune_memory.contract.worked_examples import RunRecord
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

WORKED_EXAMPLES: Final = ("drone", "fleet_register", "manipulator", "mobile_robot", "quadruped")
OPERATOR_LOG_1: Final = "operator-log-1"
OPERATOR_LOG_2: Final = "operator-log-2"
QUIET: Final = "inspection-notes"  # lands at tx 5 with no record any consolidator reads
# (transaction, packages that land in it); the Ledger is cumulative.
TRANSACTIONS: Final = (
    (1, ("drone", "mobile_robot")),
    (2, ("quadruped",)),
    (3, ("fleet_register", "manipulator", OPERATOR_LOG_1)),
    (4, (OPERATOR_LOG_2,)),
    (5, (QUIET,)),
)

RUNS_ID: Final = "golden.runs"
OPERATOR_ID: Final = "golden.operator"
PRIORITIES: Final[Mapping[str, int]] = {
    RUNS_ID: 0,
    OPERATOR_ID: 1,
    IDENTITY_CONSOLIDATOR_ID: 2,
    FIXTURE_MODEL_ID: 3,
    TIME_CONSOLIDATOR_ID: 4,
}
MODEL_CONFIG: Final[Mapping[str, JsonValue]] = {
    "candidates": {
        "quadruped": [
            {
                "confidence": 0.4,
                "left": {"namespace": "asset-tag", "value": "QUAD-03"},
                "right": {"namespace": "asset-tag", "value": "QUAD-07"},
            }
        ]
    },
    "guesses": {
        "manipulator": {"confidence": 0.7, "namespace": "asset-tag", "value": "ARM-09"},
        "mobile_robot": {"confidence": 0.6, "namespace": "asset-tag", "value": "AMR-12"},
        "quadruped": {"confidence": 0.82, "namespace": "asset-tag", "value": "QUAD-07"},
    },
    "model": FIXTURE_MODEL.to_json(),
}

# The operator logs are civil time the logs declare: POSIX seconds since the Unix epoch. A line
# about a run's recorder "from the run's start" is stated on that run's own clock.
CIVIL_SECONDS: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
OPERATOR: Final = "badge:4411"
UAV_TAG: Final = LogicalId("asset-tag", "UAV-0042")
UAV_TAG_2: Final = LogicalId("asset-tag", "UAV-0043")
QUAD_TAG: Final = LogicalId("asset-tag", "QUAD-03")
ARM_TAG: Final = LogicalId("asset-tag", "ARM-05")
ARM_TAG_2: Final = LogicalId("asset-tag", "ARM-06")
OPERATOR_LOG_TIME: Final = 1790762400
REPLACED_AT: Final = OPERATOR_LOG_TIME + 3600  # log 2: the drone's tag changed an hour later
ARM_SWAP_TICKS: Final = 10_000_000  # log 2: the manipulator's recorder changed 10 ms into the run
# One assertion per line: what the operator wrote, verbatim. Byte ranges cite each line.
LOG_LINES: Final[Mapping[str, tuple[str, ...]]] = {
    OPERATOR_LOG_1: (
        "1790762400,badge:4411,same_as,drone px4 sys_uuid,asset-tag UAV-0042",
        "1790762400,badge:4411,recorded_by,drone run,asset-tag UAV-0042",
        "run start,badge:4411,recorded_by,drone run,px4 sys_uuid",
        "run start,badge:4411,recorded_by,quadruped run,asset-tag QUAD-03",
        "run start,badge:4411,recorded_by,manipulator run,asset-tag ARM-05",
    ),
    OPERATOR_LOG_2: (
        "1790766000,badge:4411,recorded_by,drone run,asset-tag UAV-0043",
        "run start+10ms,badge:4411,recorded_by,manipulator run,asset-tag ARM-06",
    ),
}


def _log_bytes(log: str) -> bytes:
    return ("\n".join(LOG_LINES[log]) + "\n").encode("utf-8")


def _line(log: str, index: int) -> EvidenceRef:
    lines = LOG_LINES[log]
    offset = sum(len(line.encode("utf-8")) + 1 for line in lines[:index])
    length = len(lines[index].encode("utf-8"))
    return EvidenceRef(content_id(_log_bytes(log)), (ByteRange(offset, length),))


def _overlay_id(kind: str, inputs: Mapping[str, JsonValue]) -> RecordId:
    return record_id(f"memory.golden.{kind}", inputs)


def _assertion(
    log: str, line: int, predicate: str, subject: LogicalId, obj: LogicalId, valid_from: Timestamp
) -> dict[str, object]:
    """A ``recorded_by`` line, in the overlay's own shape for ``golden.operator``."""
    return {
        "evidence": [_line(log, line).to_json()],
        "id": _overlay_id("operator_assertion", {"line": LOG_LINES[log][line], "log": log}),
        "kind": "operator_assertion",
        "object": obj.to_json(),
        "operator": OPERATOR,
        "predicate": predicate,
        "subject": subject.to_json(),
        "valid_from": valid_from.to_json(),
    }


# What an assertions adapter would record the operator log as (root ADR 0062): its transform, and
# per line a clock for the line's own time column, POSIX seconds as the log declares.
OPERATOR_TRANSFORM: Final = transform_record(
    adapter_id="memory.golden.operator_log", adapter_version="1", config={}
)


def _same_identity(log: str, line: int, ids: tuple[LogicalId, ...]) -> list[dict[str, object]]:
    """The operator's ``same_identity`` line as the compiler's ``assertion`` and its clock."""
    cited = _line(log, line)
    stated = Provenance(cited, OPERATOR_TRANSFORM.id, AssertionKind.STATED)
    clock = TimestampDomain(
        id=evidence_record_id("timestamp_domain", cited, OPERATOR_TRANSFORM),
        provenance=Provenance(cited, OPERATOR_TRANSFORM.id, AssertionKind.OBSERVED),
        field="authored_at",
        scope=(),
        role=Unknown(),
        resolution=Known(Fraction(1)),
        epoch=Known(Epoch.UNIX),
        timescale=Known(Timescale.POSIX),
        declared_monotonic=Unknown(),
    )
    said = Assertion(
        id=evidence_record_id("assertion", cited, OPERATOR_TRANSFORM),
        provenance=stated,
        identifier=Known(LogicalId("operator-log", f"{log}:{line + 1}")),
        assertion_type=Known(AssertionType.SAME_IDENTITY),
        author=Known(LogicalId("badge", OPERATOR.removeprefix("badge:"))),
        authored_at=Known(Timestamp(OPERATOR_LOG_TIME, clock.id)),
        authored_zone=NotCovered(),
        scope=Known(ids),
        retracts=NotApplicable(),
        payload=NotCovered(),
        rationale=NotCovered(),
        signature=NotCovered(),
        ticket=NotCovered(),
    )
    return [dict(clock.to_json()), dict(said.to_json())]


def _thread(node: LogicalId, evidence: EvidenceRef) -> dict[str, object]:
    return {
        "evidence": [evidence.to_json()],
        "id": _overlay_id("ledger_thread", node.to_json()),
        "kind": "ledger_thread",
        "logical_id": node.to_json(),
        "node_type": "machine",
        "valid_from": CIVIL_SECONDS.at(OPERATOR_LOG_TIME).to_json(),
    }


def _run_id(run: RunRecord) -> LogicalId:
    return LogicalId("record", run.record)


def ledger_overlay(
    packages: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    """The overlay packages: operator logs 1 and 2 (with two Ledger threads) and a quiet one."""
    ledger = StubLedger({name: (1, records) for name, records in packages.items()})
    by_package = {run.package_id: run for run in runs(ledger)}
    drone, quadruped, arm = by_package["drone"], by_package["quadruped"], by_package["manipulator"]
    if drone.machine is None or drone.machine_evidence is None:
        raise ValueError("the drone worked example no longer declares its machine")
    civil = CIVIL_SECONDS.at(OPERATOR_LOG_TIME)
    swap = Timestamp(arm.first.ticks + ARM_SWAP_TICKS, arm.first.domain_id)
    log_1, log_2 = OPERATOR_LOG_1, OPERATOR_LOG_2
    return {
        log_1: [
            _thread(drone.machine, drone.machine_evidence),
            _thread(UAV_TAG, _line(log_1, 0)),
            *_same_identity(log_1, 0, (drone.machine, UAV_TAG)),
            _assertion(log_1, 1, "recorded_by", _run_id(drone), UAV_TAG, civil),
            _assertion(log_1, 2, "recorded_by", _run_id(drone), drone.machine, drone.first),
            _assertion(log_1, 3, "recorded_by", _run_id(quadruped), QUAD_TAG, quadruped.first),
            _assertion(log_1, 4, "recorded_by", _run_id(arm), ARM_TAG, arm.first),
        ],
        log_2: [
            _assertion(
                log_2, 0, "recorded_by", _run_id(drone), UAV_TAG_2, CIVIL_SECONDS.at(REPLACED_AT)
            ),
            _assertion(log_2, 1, "recorded_by", _run_id(arm), ARM_TAG_2, swap),
        ],
        QUIET: [],
    }


class RunEvidence:
    """Golden consolidator: per run, ``evidenced_by`` its record, and ``recorded_by`` its machine
    when the record declares one. Valid over the run's ``[first, last)``, open if no end."""

    consolidator_id: Final = RUNS_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        drafts: list[ClaimDraft] = []
        for run in runs(ledger):
            drafts.append(
                ClaimDraft(
                    subject=run.node,
                    predicate="evidenced_by",
                    object=LedgerRecordRef(run.record),
                    valid_from=run.first,
                    valid_to=run.last,
                    assertion_kind=run.assertion_kind,
                    evidence=(run.evidence,),
                    records=(run.record,),
                )
            )
            if run.machine is not None and run.machine_evidence is not None:
                drafts.append(
                    ClaimDraft(
                        subject=run.node,
                        predicate="recorded_by",
                        object=machine_node(run.machine),
                        valid_from=run.first,
                        valid_to=run.last,
                        assertion_kind=run.assertion_kind,
                        evidence=(run.machine_evidence,),
                        records=(run.record,),
                    )
                )
        return ConsolidatorOutput(tuple(drafts))


class OperatorAssertions:
    """Golden consolidator: a stated claim per operator assertion about a run's recorder.

    ``same_as`` assertions are the identity policy's (``memory.identity``); this reads only
    ``recorded_by``, whose subject is a run (``record:<id>``) and object a machine.
    """

    consolidator_id: Final = OPERATOR_ID
    version: Final = "1"
    model: Final[ModelRef | None] = None

    def consolidate(
        self,
        ledger: LedgerReader,
        previous: Sequence[Claim],
        config: Mapping[str, JsonValue],
    ) -> ConsolidatorOutput:
        drafts: list[ClaimDraft] = []
        for ref in ledger.list_packages():
            for record in ledger.read_records(ref.package_id, "operator_assertion") or ():
                if record.get("predicate") != "recorded_by":
                    continue
                subject = logical_id_from_json(record["subject"])  # type: ignore[arg-type]
                if subject.namespace != "record":
                    raise ValueError(f"recorded_by names a run by record id: {subject!r}")
                evidence = record["evidence"]
                if not isinstance(evidence, list):
                    raise TypeError("evidence must be a list")
                rid = parse_record_id(str(record["id"]))
                drafts.append(
                    ClaimDraft(
                        subject=run_node(parse_record_id(subject.value)),
                        predicate="recorded_by",
                        object=machine_node(logical_id_from_json(record["object"])),  # type: ignore[arg-type]
                        valid_from=timestamp_from_json(record["valid_from"]),  # type: ignore[arg-type]
                        assertion_kind=AssertionKind.STATED,
                        evidence=tuple(evidence_ref_from_json(e) for e in evidence),
                        records=(rid,),
                    )
                )
        return ConsolidatorOutput(tuple(drafts))


def plan() -> list[tuple[Consolidator, Mapping[str, JsonValue]]]:
    return [
        (RunEvidence(), {}),
        (OperatorAssertions(), {}),
        (IdentityConsolidator(), {}),
        (_FixtureModel(), MODEL_CONFIG),
        (TimeDomainConsolidator(), {}),
    ]


def build_golden(packages: Mapping[str, Sequence[Mapping[str, object]]]) -> GraphDocument:
    """The golden graph from the four worked examples' records (``{name: record lines}``)."""
    missing = sorted(set(WORKED_EXAMPLES) - packages.keys())
    if missing:
        raise ValueError(f"worked examples missing: {missing}")
    inputs: dict[str, Sequence[Mapping[str, object]]] = {
        name: list(packages[name]) for name in WORKED_EXAMPLES
    }
    inputs.update(ledger_overlay(inputs))
    landed: dict[str, tuple[int, Sequence[Mapping[str, object]]]] = {}
    claims: list[Claim] = []
    for tx, names in TRANSACTIONS:
        landed.update({name: (1, inputs[name]) for name in names})
        for result in rebuild(StubLedger(landed), plan(), recorded_at=ledger_tx(tx)):
            if result.findings:
                raise ValueError(_findings(result.findings))
            claims.extend(result.claims)
    resolution = resolve(claims, CORE_PREDICATES, PRIORITIES)
    head = ledger_tx(TRANSACTIONS[-1][0])
    return GraphDocument(resolution, resolver_config(CORE_PREDICATES, PRIORITIES), head)


def _findings(findings: Sequence[ConsolidationFinding]) -> str:
    return "golden inputs produced findings: " + "; ".join(
        f"{f.code}: {f.message}" for f in findings
    )
