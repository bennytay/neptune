"""The acceptance corpus's incident bag (MVL-181, ``harness/acceptance``): its ``/diagnostics``
statuses become records (ADR 0071) whose evidence resolves to the exact bytes of each status inside
its message, on every clock the bag carries and none converted."""

import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType
from typing import Final

import pytest

from neptune.adapters.harness import ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.provenance import ByteRange, Provenance
from neptune.model.status import StatusConvention, StatusReport, StatusValue

pytestmark = pytest.mark.integration

REPO: Final = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CORPUS: Final = _load("status_corpus_generate", REPO / "harness" / "acceptance" / "generate.py")
STATUS: Final = _load(
    "status_corpus_fixtures", REPO / "tests" / "fixtures" / "status" / "make_status_fixtures.py"
)
FILES: Final = CORPUS.cell_runs()
BAG: Final = FILES[f"{CORPUS.CELL}/bags/{CORPUS.INCIDENT_RUN}/{CORPUS.INCIDENT_RUN}_0.mcap"]
ESTOP: Final = (
    2,
    "cell3/safety",
    "emergency stop pressed at operator panel OP-2",
    "PLC-C3",
    (("input", "OP-2.ES1"),),
)
COLLISION: Final = (
    2,
    "cell3/arm/joint_5",
    "external torque 41.7 Nm exceeds collision limit 35.0 Nm",
    "ARM-3A",
    (("torque_nm", "41.7"), ("limit_nm", "35.0"), ("station", "P1")),
)


@pytest.fixture(scope="module")
def records() -> list[StatusReport]:
    output = ingest_source(McapAdapter(), BytesReader(BAG))
    return [r for r in output.records() if isinstance(r, StatusReport)]


def _said(record: StatusReport) -> tuple[object, ...]:
    pairs = record.values.value if isinstance(record.values, Known) else ()
    return (
        record.level.value if isinstance(record.level, Known) else None,
        record.name.value if isinstance(record.name, Known) else None,
        record.message.value if isinstance(record.message, Known) else None,
        record.hardware_id.value if isinstance(record.hardware_id, Known) else None,
        tuple((pair.key, pair.value) for pair in pairs),
    )


def test_the_incidents_warnings_collision_and_estop_are_records(
    records: list[StatusReport],
) -> None:
    said = [_said(r) for r in records]
    assert said.count(ESTOP) == 1 and said.count(COLLISION) == 1
    warnings = [s for s in said if s[0] == 1]
    assert warnings and all(s[1] == "cell3/vision" for s in warnings)  # residual over 2.0 mm
    assert all(s[0] != 0 for s in said)  # OK statuses stay rows (nominal_status_records off)
    assert {r.convention for r in records} == {StatusConvention.ROS_DIAGNOSTIC_STATUS}
    (estop,) = [r for r in records if _said(r) == ESTOP]
    assert estop.level_names == Known(("ERROR",), estop.level_names.provenance)  # type: ignore[union-attr]
    assert estop.values == Known((StatusValue("input", "OP-2.ES1"),))


def test_times_are_the_bags_own_clocks_never_converted(records: list[StatusReport]) -> None:
    """log_time and publish_time are the cell PC's; the header stamp is the controller's, 96.7 s
    behind: three clocks, three readings, nothing reconciled."""
    (estop,) = [r for r in records if _said(r) == ESTOP]
    log_time, publish_time, stamp = (t.value for t in estop.times)  # type: ignore[union-attr]
    assert log_time.ticks == publish_time.ticks
    assert log_time.ticks - stamp.ticks == CORPUS.IPC_AHEAD_2026_09_14
    assert len({log_time.domain_id, publish_time.domain_id, stamp.domain_id}) == 3


def _records_of_chunk(record: bytes) -> bytes:
    """An MCAP Chunk record's records; the corpus writes them uncompressed."""
    at = 1 + 8 + 8 + 8 + 8 + 4
    (name_length,) = struct.unpack_from("<I", record, at)
    assert name_length == 0  # no compression
    return record[at + 4 + 8 :]


def test_evidence_resolves_to_the_exact_bytes_of_each_status(records: list[StatusReport]) -> None:
    for record in (r for r in records if _said(r) in (ESTOP, COLLISION)):
        chunk_step, message_step, status_step = record.provenance.evidence.locator
        assert isinstance(chunk_step, ByteRange) and isinstance(message_step, ByteRange)
        assert isinstance(status_step, ByteRange)
        chunk = BAG[chunk_step.offset : chunk_step.offset + chunk_step.length]
        inner = _records_of_chunk(chunk)
        message = inner[message_step.offset : message_step.offset + message_step.length]
        assert message[0] == 0x05  # a Message record
        log_time = struct.unpack_from("<Q", message, 1 + 8 + 2 + 4)[0]
        assert log_time == record.times[0].value.ticks  # type: ignore[union-attr]
        item = message[status_step.offset : status_step.offset + status_step.length]
        # The status as CDR would write it at that place in the payload (alignment counted
        # after the 4-byte encapsulation header that starts the payload, 31 bytes in).
        at = status_step.offset - (1 + 8 + 2 + 4 + 8 + 8) - 4
        cdr = STATUS.Cdr()
        cdr.out = bytearray(at)
        STATUS.cdr_status(cdr, _said(record))
        assert item == bytes(cdr.out[at:])
        first = record.times[0]
        assert isinstance(first, Known) and isinstance(first.provenance, Provenance)
        cited = first.provenance
        assert cited.evidence.locator == (chunk_step, message_step)  # the times are the message's
