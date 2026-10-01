"""The golden graph of graph-schema v1, from the compiler's four worked examples (ADR 0006 §10).

Inputs: the record lines of the drone, manipulator, mobile-robot and quadruped worked examples
(``tests/fixtures/model/``; the caller reads them), plus a small *Ledger overlay* derived from
them: an operator log whose bytes are in this module, and the two Ledger threads the Ledger would
declare for its identity assertion (ADR 0003 §1's record kinds, until MVL-85 defines them).

The Ledger grows over three transactions, and the same plan is rebuilt at each one:

- tx 1: drone and manipulator; tx 2: + mobile robot and quadruped; tx 3: + the overlay.
- Plan, in order: ``golden.runs`` (deterministic: ``evidenced_by`` and a declared
  ``recorded_by`` per run), ``golden.recorder_guess`` (inferred, ``derived/golden.py``),
  ``memory.identity`` (the real identity policy) and ``golden.operator`` (stated claims from
  operator assertions).

So the graph has deterministic and inferred claims, a superseded inferred guess (the operator
corrects the quadruped's recorder at tx 3), a ``same_as`` and a ``clock_mismatch`` finding (the
operator names the drone by asset tag on civil time while the log names it on its boot clock).
Same inputs give byte-identical canonical JSON. Every consolidation finding is a hard error:
golden inputs are clean by construction, so a finding means the inputs drifted.
"""

from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Final

from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.ids import LogicalId, RecordId, logical_id_from_json, parse_record_id
from neptune.model.knowledge import AssertionKind
from neptune.model.provenance import ByteRange, EvidenceRef, evidence_ref_from_json
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
from neptune_memory.contract.worked_examples import machine_node, run_node, runs
from neptune_memory.derived.golden import GUESS_ID, GUESS_MODEL, RecorderGuess
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.claim import LedgerRecordRef
from neptune_memory.schema.codec import GraphDocument
from neptune_memory.schema.interval import CivilClock, ledger_tx
from neptune_memory.schema.predicates import CORE_PREDICATES, SAME_AS
from neptune_memory.schema.supersede import resolve, resolver_config

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from neptune.model.jsonvalue import JsonValue
    from neptune_memory.consolidate.base import Consolidator
    from neptune_memory.ledger import LedgerReader
    from neptune_memory.schema.claim import Claim

WORKED_EXAMPLES: Final = ("drone", "manipulator", "mobile_robot", "quadruped")
OVERLAY: Final = "ledger-overlay"
# (transaction, packages that land in it); the Ledger is cumulative.
TRANSACTIONS: Final = (
    (1, ("drone", "manipulator")),
    (2, ("mobile_robot", "quadruped")),
    (3, (OVERLAY,)),
)

RUNS_ID: Final = "golden.runs"
OPERATOR_ID: Final = "golden.operator"
PRIORITIES: Final[Mapping[str, int]] = {
    RUNS_ID: 0,
    GUESS_ID: 1,
    IDENTITY_CONSOLIDATOR_ID: 2,
    OPERATOR_ID: 3,
}
GUESS_CONFIG: Final[Mapping[str, JsonValue]] = {
    "guesses": {
        "mobile_robot": {"confidence": 0.6, "namespace": "asset-tag", "value": "AMR-12"},
        "quadruped": {"confidence": 0.82, "namespace": "asset-tag", "value": "QUAD-07"},
    },
    "model": GUESS_MODEL.to_json(),
}

# The operator log is civil time the log declares: POSIX seconds since the Unix epoch.
CIVIL_SECONDS: Final = CivilClock(Timescale.POSIX, Epoch.UNIX, Fraction(1))
OPERATOR: Final = "badge:4411"
UAV_TAG: Final = LogicalId("asset-tag", "UAV-0042")
QUAD_TAG: Final = LogicalId("asset-tag", "QUAD-03")
# One assertion per line: what the operator wrote, verbatim. Byte ranges below cite each line.
OPERATOR_LOG_LINES: Final = (
    "1790762400,badge:4411,same_as,drone px4 sys_uuid,asset-tag UAV-0042",
    "1790762400,badge:4411,recorded_by,drone run,asset-tag UAV-0042",
    "1790762400,badge:4411,recorded_by,quadruped run,asset-tag QUAD-03",
)
OPERATOR_LOG: Final = ("\n".join(OPERATOR_LOG_LINES) + "\n").encode("utf-8")
OPERATOR_LOG_TIME: Final = 1790762400


def _line(index: int) -> EvidenceRef:
    offset = sum(len(line.encode("utf-8")) + 1 for line in OPERATOR_LOG_LINES[:index])
    length = len(OPERATOR_LOG_LINES[index].encode("utf-8"))
    return EvidenceRef(content_id(OPERATOR_LOG), (ByteRange(offset, length),))


def _overlay_id(kind: str, inputs: Mapping[str, JsonValue]) -> RecordId:
    return record_id(f"memory.golden.{kind}", inputs)


def ledger_overlay(
    packages: Mapping[str, Sequence[Mapping[str, object]]],
) -> list[dict[str, object]]:
    """The overlay records: two Ledger threads and three operator assertions (see module doc)."""
    ledger = StubLedger({name: (1, records) for name, records in packages.items()})
    by_package = {run.package_id: run for run in runs(ledger)}
    drone, quadruped = by_package["drone"], by_package["quadruped"]
    if drone.machine is None or drone.machine_evidence is None:
        raise ValueError("the drone worked example no longer declares its machine")
    civil = CIVIL_SECONDS.at(OPERATOR_LOG_TIME).to_json()

    def thread(node: LogicalId, evidence: EvidenceRef) -> dict[str, object]:
        return {
            "evidence": [evidence.to_json()],
            "id": _overlay_id("ledger_thread", node.to_json()),
            "kind": "ledger_thread",
            "logical_id": node.to_json(),
            "node_type": "machine",
            "valid_from": civil,
        }

    def assertion(
        line: int, predicate: str, subject: LogicalId, obj: LogicalId, valid_from: Timestamp
    ) -> dict[str, object]:
        return {
            "evidence": [_line(line).to_json()],
            "id": _overlay_id("operator_assertion", {"line": OPERATOR_LOG_LINES[line]}),
            "kind": "operator_assertion",
            "object": obj.to_json(),
            "operator": OPERATOR,
            "predicate": predicate,
            "subject": subject.to_json(),
            "valid_from": valid_from.to_json(),
        }

    drone_run = LogicalId("record", drone.record)
    quadruped_run = LogicalId("record", quadruped.record)
    return [
        thread(drone.machine, drone.machine_evidence),
        thread(UAV_TAG, _line(0)),
        assertion(0, SAME_AS, drone.machine, UAV_TAG, CIVIL_SECONDS.at(OPERATOR_LOG_TIME)),
        assertion(1, "recorded_by", drone_run, UAV_TAG, CIVIL_SECONDS.at(OPERATOR_LOG_TIME)),
        # The operator corrects the quadruped run from its own first instant, on its own clock.
        assertion(2, "recorded_by", quadruped_run, QUAD_TAG, quadruped.first),
    ]


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
        (RecorderGuess(), GUESS_CONFIG),
        (IdentityConsolidator(), {}),
        (OperatorAssertions(), {}),
    ]


def build_golden(packages: Mapping[str, Sequence[Mapping[str, object]]]) -> GraphDocument:
    """The golden graph from the four worked examples' records (``{name: record lines}``)."""
    missing = sorted(set(WORKED_EXAMPLES) - packages.keys())
    if missing:
        raise ValueError(f"worked examples missing: {missing}")
    inputs: dict[str, Sequence[Mapping[str, object]]] = {
        name: list(packages[name]) for name in WORKED_EXAMPLES
    }
    inputs[OVERLAY] = ledger_overlay(inputs)
    landed: dict[str, tuple[int, Sequence[Mapping[str, object]]]] = {}
    claims: list[Claim] = []
    for tx, names in TRANSACTIONS:
        landed.update({name: (1, inputs[name]) for name in names})
        for result in rebuild(StubLedger(landed), plan(), recorded_at=ledger_tx(tx)):
            if result.findings:
                raise ValueError(_findings(result.findings))
            claims.extend(result.claims)
    resolution = resolve(claims, CORE_PREDICATES, PRIORITIES)
    return GraphDocument(resolution, resolver_config(CORE_PREDICATES, PRIORITIES))


def _findings(findings: Sequence[ConsolidationFinding]) -> str:
    return "golden inputs produced findings: " + "; ".join(
        f"{f.code}: {f.message}" for f in findings
    )
