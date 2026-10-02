"""G1 scenario 3: a calibration file dated before the robot existed (aerial, arm).

Expected (ADR 0002 §1-§3, ADR 0007 §3): valid time is stored exactly as declared and never clamped
to a robot's commissioning. Memory models no node lifetime, so it has nothing to compare an early
date against, and judging plausibility is interpretation (``derived/``), not consolidation. A
sensor really is often calibrated at the factory before it is mounted; the robot's view of it is
the overlap of ``mounted_on`` and ``has_calibration``, which the reader returns unaltered. A date
from an unset real-time clock (1970) on a clock whose epoch is not declared stays on that clock:
never ordered against civil time, flagged ``clock_mismatch`` while it competes. Verdict: HOLDS.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from memory_g1_harness import (
    JAN_10_2025,
    JUN_01_2025,
    MAR_02_2026,
    Fixed,
    cite,
    civil,
    draft,
    own_clock,
    reader,
    rid,
    source,
    thread,
)
from neptune.model.knowledge import Known
from neptune.model.time import DomainMismatchError, Timestamp
from neptune_memory.consolidate.base import rebuild
from neptune_memory.ledger import StubLedger
from neptune_memory.schema.interval import OPEN, Interval, ledger_tx
from neptune_memory.schema.nodes import NodeRef, NodeType
from neptune_memory.schema.supersede import FindingCode

if TYPE_CHECKING:
    from neptune_memory.schema.claim import Claim

DRONE = NodeRef(NodeType.MACHINE, "px4-uuid:000600000000000044d7")
CAMERA = NodeRef(NodeType.SENSOR, "camera-serial:FLIR-22416")
FACTORY_CAL = NodeRef(NodeType.CONFIGURATION, "cal:flir-22416-factory")
FIELD_CAL = NodeRef(NodeType.CONFIGURATION, "cal:flir-22416-field-2026")
RTC_CAL = NodeRef(NodeType.CONFIGURATION, "cal:flir-22416-rtc-unset")
CAMERA_RTC = own_clock("FLIR-22416 real-time clock, epoch not declared")
TX = ledger_tx(3)


def _claims() -> tuple[Claim, ...]:
    calibration = [rid("calibration_file", "factory")]
    built = rebuild(
        StubLedger(
            {
                "pkg": (
                    1,
                    [
                        thread(
                            "px4-uuid",
                            "000600000000000044d7",
                            "machine",
                            cite(source("flight-001.ulg")),
                            valid_from=civil(JUN_01_2025),
                        )
                    ],
                )
            }
        ),
        [
            (
                Fixed(
                    "test.calibration",
                    (
                        # Calibrated at the factory in January, five months before the drone
                        # was commissioned (its thread starts 1 June).
                        draft(
                            CAMERA,
                            "has_calibration",
                            FACTORY_CAL,
                            civil(JAN_10_2025),
                            records=calibration,
                            evidence=(cite(source("factory.yaml")),),
                        ),
                        draft(
                            CAMERA,
                            "mounted_on",
                            DRONE,
                            civil(JUN_01_2025),
                            records=(rid("build_sheet", "drone"),),
                            evidence=(cite(source("build-sheet.pdf")),),
                        ),
                        # A recalibration whose file carries 1970-01-01 from an unset RTC: the
                        # camera's own clock, with no declared epoch.
                        draft(
                            CAMERA,
                            "has_calibration",
                            RTC_CAL,
                            Timestamp(0, CAMERA_RTC),
                            records=(rid("calibration_file", "rtc"),),
                            evidence=(cite(source("rtc.yaml")),),
                        ),
                        draft(
                            CAMERA,
                            "has_calibration",
                            FIELD_CAL,
                            civil(MAR_02_2026),
                            records=(rid("calibration_file", "field"),),
                            evidence=(cite(source("field.yaml")),),
                        ),
                    ),
                ),
                {},
            )
        ],
        recorded_at=TX,
    )
    return built[0].claims


def test_a_pre_commissioning_calibration_is_stored_as_declared_never_clamped() -> None:
    graph = reader(_claims(), {"test.calibration": 0}, head=3)
    civil_cals = graph.claims(CAMERA, "has_calibration", TX, during=Interval(civil(0), OPEN))
    by_object = {c.object: c for c in civil_cals.claims}
    factory = by_object[FACTORY_CAL]
    # Valid from January, as the file says, cut only by the 2026 field recalibration.
    assert (factory.valid_from, factory.valid_to) == (civil(JAN_10_2025), civil(MAR_02_2026))
    # Before the drone existed the camera already had its calibration; the reader answers that.
    before_drone = graph.claims(
        CAMERA, "has_calibration", TX, during=Interval(civil(JAN_10_2025), civil(JUN_01_2025))
    )
    assert [c.object for c in before_drone.claims] == [FACTORY_CAL]
    assert graph.claims(CAMERA, "mounted_on", TX).claims[0].valid_from == civil(JUN_01_2025)
    # Nothing about the early date is a finding: it is evidence, not an error.
    assert not [f for f in civil_cals.findings if f.code is not FindingCode.CLOCK_MISMATCH]


def test_an_unset_rtc_date_stays_on_its_own_clock_and_is_never_ordered_against_civil_time() -> None:
    graph = reader(_claims(), {"test.calibration": 0}, head=3)
    result = graph.claims(CAMERA, "has_calibration", TX, during=Interval(civil(0), OPEN))
    (rtc,) = result.other_clocks  # returned, never dropped, never coerced to 1970 UTC
    assert rtc.object == RTC_CAL and rtc.valid_from == Timestamp(0, CAMERA_RTC)
    assert {f.code for f in result.findings} == {FindingCode.CLOCK_MISMATCH}
    assert all(rtc.id in (f.claim, *f.others) for f in result.findings)
    with pytest.raises(DomainMismatchError):
        rtc.valid.overlaps(Interval(civil(JAN_10_2025), OPEN))
    node = graph.node(CAMERA, TX)
    assert isinstance(node, Known)
