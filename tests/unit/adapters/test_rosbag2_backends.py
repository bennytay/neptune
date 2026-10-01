"""One recording, two rosbag2 storage backends, the same canonical run and streams (MVL-19).

``mobile_base_sqlite3`` and ``mobile_base_mcap`` hold the same 18 messages (``make_rosbag2.py``;
both validated with the official readers, see ``oracle.json``). The sqlite3 bag's ``.db3`` is read
by the rosbag2 adapter, the MCAP bag's ``.mcap`` by the MCAP adapter, untouched: that is how
MCAP-backed bags are handled (ADR 0045 §1). What must be equal is everything a consumer of the
canonical run and streams uses: the recording's extent, the first clock's meaning, and per topic
the type, encodings, QoS, count, definition bytes and every message's seq, time and payload.
Provenance, ids, assertion kinds and clocks the MCAP file adds (publish time) are not compared.
"""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.rosbag2 import Rosbag2Adapter
from neptune.discovery.reader import BytesReader
from neptune.model.knowledge import Known
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.reference import TimestampDomain
from neptune.model.run import Run, Stream
from neptune.model.series import SEQ
from neptune.model.world import StructuredRecord, StructuredTable

TESTS: Final = Path(__file__).parents[2]
FIXTURES: Final = TESTS / "fixtures"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


READING: Final = _load("mcap_reading", FIXTURES / "mcap" / "mcap_reading.py")
SQLITE: Final = FIXTURES / "rosbag2" / "mobile_base_sqlite3"
MCAP: Final = FIXTURES / "rosbag2" / "mobile_base_mcap"


def ingest(adapter: Any, path: Path) -> tuple[SourceOutput, bytes]:
    data = path.read_bytes()
    return ingest_source(adapter, BytesReader(data)), data


def v(known: object) -> Any:
    return getattr(known, "value", None)


def sqlite_payload(data: bytes, evidence: EvidenceRef, size: int) -> bytes:
    (step,) = evidence.locator
    assert isinstance(step, ByteRange)
    return data[step.offset : step.offset + step.length][-size:]


def view(out: SourceOutput, data: bytes, sqlite: bool) -> dict[str, Any]:
    """What a consumer of the canonical run and streams uses, backend-neutral."""
    (run,) = [r for r in out.records() if isinstance(r, Run)]
    domains = {r.id: r for r in out.records() if isinstance(r, TimestampDomain)}
    streams = {}
    for stream in (r for r in out.records() if isinstance(r, Stream)):
        clock = domains[stream.clocks[0]]
        rows = []
        for batch in out.series()[stream.id]:
            for row in batch.rows():
                evidence = stream.row_evidence(row)
                if sqlite:
                    payload = sqlite_payload(data, evidence, int(row["value/data_bytes"]))  # type: ignore[call-overload]
                else:
                    payload = READING.message(READING.record_at(data, evidence)[1]).payload
                rows.append((row[SEQ], row["time/0"], payload))
        definition = stream.schema_definition
        assert isinstance(definition, Known)
        (step, *inner) = definition.value.locator
        assert isinstance(step, ByteRange)
        cited = (
            data[step.offset : step.offset + step.length]
            if sqlite
            else READING.resolve(data, definition.value)
        )
        assert not inner or not sqlite
        streams[v(stream.topic)] = {
            "schema_name": v(stream.schema_name),
            "schema_encoding": v(stream.schema_encoding),
            "schema_definition": cited.decode(),
            "message_encoding": v(stream.message_encoding),
            "metadata": stream.metadata,
            "message_count": v(stream.message_count),
            "first_last": (stream.first, stream.last),
            "rows": sorted(rows),
            "clock0": _clock(clock),
        }
    return {
        "first": run.first.value.ticks,  # type: ignore[union-attr]
        "last": run.last.value.ticks,  # type: ignore[union-attr]
        "ids": (run.logical_id, run.machine),
        "streams": streams,
    }


def _state(knowledge: object) -> tuple[str, object]:
    """A value's state and value, without its provenance (the backends cite different bytes)."""
    return type(knowledge).__name__, getattr(knowledge, "value", None)


def _clock(clock: TimestampDomain) -> tuple[object, ...]:
    return (
        clock.scope,
        _state(clock.role),
        _state(clock.resolution),
        _state(clock.epoch),
        _state(clock.timescale),
        _state(clock.declared_monotonic),
    )


def test_both_backends_give_equivalent_runs_and_streams() -> None:
    sqlite_out, sqlite_data = ingest(Rosbag2Adapter(), SQLITE / "mobile_base_sqlite3_0.db3")
    mcap_out, mcap_data = ingest(McapAdapter(), MCAP / "mobile_base_mcap_0.mcap")
    left, right = view(sqlite_out, sqlite_data, True), view(mcap_out, mcap_data, False)
    assert (
        left["streams"].keys()
        == right["streams"].keys()
        == {
            "/cmd_vel",
            "/battery_voltage",
            "/status",
        }
    )
    assert left == right
    # And the substance is there: all 18 messages, the QoS text rosbag2 offered, no assumptions.
    assert sum(len(s["rows"]) for s in left["streams"].values()) == 18
    cmd = left["streams"]["/cmd_vel"]
    assert (
        cmd["metadata"][0][0] == "offered_qos_profiles"
        and "reliability: 1" in cmd["metadata"][0][1]
    )
    assert (cmd["clock0"][3], cmd["clock0"][4]) == (("Unknown", None), ("Unknown", None))


def test_both_bags_state_the_same_run_and_topics_in_their_metadata() -> None:
    sqlite_out, _ = ingest(Rosbag2Adapter(), SQLITE / "metadata.yaml")
    mcap_out, _ = ingest(Rosbag2Adapter(), MCAP / "metadata.yaml")

    def stated(out: SourceOutput) -> dict[str, Any]:
        (run,) = [r for r in out.records() if isinstance(r, Run)]
        tables = {t.id: v(t.name) for t in out.records() if isinstance(t, StructuredTable)}
        rows: dict[str, list[list[Any]]] = {}
        for row in sorted(
            (r for r in out.records() if isinstance(r, StructuredRecord)),
            key=lambda r: (r.table, r.row),
        ):
            rows.setdefault(tables[row.table], []).append(
                [getattr(c, "value", None) for c in row.cells]
            )
        return {
            "first": run.first.value.ticks,  # type: ignore[union-attr]
            "last": run.last.value.ticks,  # type: ignore[union-attr]
            "topics": rows["topics_with_message_count"],
            "counts": [f[1:] for f in rows["files"]],
            "message_count": dict(rows["rosbag2_bagfile_information"])["message_count"],
        }

    assert stated(sqlite_out) == stated(mcap_out)
    # The metadata and the data agree: the stated extent is the observed extent.
    data_view = view(*ingest(Rosbag2Adapter(), SQLITE / "mobile_base_sqlite3_0.db3"), True)
    assert (stated(sqlite_out)["first"], stated(sqlite_out)["last"]) == (
        data_view["first"],
        data_view["last"],
    )


def test_the_backends_differ_only_where_the_storage_does() -> None:
    """The one place the two bags' metadata differ is the declared storage and the part's name."""
    sqlite_out, _ = ingest(Rosbag2Adapter(), SQLITE / "metadata.yaml")
    mcap_out, _ = ingest(Rosbag2Adapter(), MCAP / "metadata.yaml")

    def scalars(out: SourceOutput) -> dict[str, Any]:
        tables = {t.id: v(t.name) for t in out.records() if isinstance(t, StructuredTable)}
        return {
            v(r.cells[0]): v(r.cells[1])
            for r in out.records()
            if isinstance(r, StructuredRecord) and tables[r.table] == "rosbag2_bagfile_information"
        }

    left, right = scalars(sqlite_out), scalars(mcap_out)
    assert {k for k in left if left[k] != right.get(k)} == {"storage_identifier"}
