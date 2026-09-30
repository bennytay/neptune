"""MVL-3 acceptance, end to end: every value traces to exact source bytes and a transform chain,
and the lineage survives normalisation and an adapter upgrade without mutating anything.

The test plays a tiny CSV adapter and an SI normaliser, emits records the way they would, then
resolves every citation against the real fixture bytes and walks every transform chain.
"""

import csv
import gzip
import io
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.provenance import (
    check_transform_record,
    evidence_record_id,
    transform_record,
)
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import AssertionKind, Known, to_json
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    RowCell,
    TransformRecord,
    provenance_from_json,
    transform_record_from_json,
)
from neptune.model.units import CATALOGUE_VERSION, to_si, unit_from_text

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parent.parent / "fixtures/provenance/arm_limits.csv"
UNITS = {"neptune.units-catalogue": str(CATALOGUE_VERSION)}


# --- A resolver: what the locator conventions promise a consumer can do --------------------


def resolve(data: bytes, steps: Sequence[Locator], decode: Callable[[bytes], bytes]) -> JsonValue:
    """Follow a locator path into ``data``. ``decode`` is what the transform does between steps."""
    step, rest = steps[0], steps[1:]
    match step:
        case ByteRange(offset=offset, length=length):
            assert offset + length <= len(data), "byte range runs past the end of its scope"
            scoped = data[offset : offset + length]
            return resolve(decode(scoped), rest, decode) if rest else scoped.decode()
        case RowCell(row=row, column=column, column_name=name):
            assert not rest
            table = list(csv.reader(io.StringIO(data.decode(), newline="")))
            assert table[0][column] == name, "column moved: the declared name no longer matches"
            return table[row][column]
        case JsonPointer(pointer=pointer):
            assert not rest
            node: JsonValue = json.loads(data)
            for token in pointer.split("/")[1:]:
                token = token.replace("~1", "/").replace("~0", "~")
                if isinstance(node, list):
                    node = node[int(token)]
                else:
                    assert isinstance(node, dict)
                    node = node[token]
            return node
    raise AssertionError(f"this test's resolver does not handle {step!r}")


def chain(transform: RecordId, transforms: Mapping[RecordId, TransformRecord]) -> list[str]:
    """The transform chain from the source bytes to this value, as ``adapter@version`` names."""
    record = check_transform_record(transforms[transform])
    upstream = [name for parent in record.upstream for name in chain(parent, transforms)]
    return [*upstream, f"{record.adapter_id}@{record.adapter_version}"]


# --- A tiny adapter and normaliser ---------------------------------------------------------


def ingest(data: bytes, adapter_version: str) -> tuple[list[JsonObject], list[TransformRecord]]:
    """Emit one joint-limit record per row, and an SI-normalised record per limit."""
    source = content_id(data)
    adapter = transform_record(
        adapter_id="csv-limits",
        adapter_version=adapter_version,
        config={"delimiter": ",", "header_rows": 1},
        libraries=UNITS,
    )
    normaliser = transform_record(
        adapter_id="neptune.si", adapter_version="1", config={}, upstream=[adapter.id]
    )
    header, *rows = list(csv.reader(io.StringIO(data.decode(), newline="")))
    records: list[JsonObject] = []
    for index, (joint, velocity, unit_text) in enumerate(rows, start=1):
        cell = EvidenceRef(source, (RowCell(index, 1, header[1]),))
        unit_cell = Provenance(
            EvidenceRef(source, (RowCell(index, 2, header[2]),)),
            adapter.id,
            AssertionKind.OBSERVED,
        )
        value = Known(int(velocity), Provenance(cell, adapter.id, AssertionKind.OBSERVED))
        unit = unit_from_text(unit_text, provenance=unit_cell)
        records.append(
            {
                "id": evidence_record_id("joint_limit", cell, adapter),
                "joint": joint,
                "max_velocity": {
                    "unit": to_json(unit, lambda u: u.to_json()),
                    "value": to_json(value),
                },
            }
        )
        si = to_si(value.value, unit.known_or_raise())
        records.append(
            {
                "id": evidence_record_id("si_value", cell, normaliser),
                "provenance": Provenance(cell, normaliser.id, AssertionKind.OBSERVED).to_json(),
                "unit": si.unit.to_json(),
                "value": {"pi_power": si.value.pi_power, "rational": str(si.value.rational)},
            }
        )
    return records, [adapter, normaliser]


def as_lines(records: Sequence[JsonObject]) -> bytes:
    return b"".join(canonical_json.dumps(record) + b"\n" for record in records)


def provenance_of(record: JsonObject) -> Provenance:
    if "provenance" in record:
        return provenance_from_json(record["provenance"])
    field = record["max_velocity"]
    assert isinstance(field, Mapping)
    value = field["value"]
    assert isinstance(value, Mapping)
    return provenance_from_json(value["provenance"])


# --- Acceptance ----------------------------------------------------------------------------


def test_every_normalised_value_traces_to_exact_bytes_and_a_transform_chain() -> None:
    data = FIXTURE.read_bytes()
    records, transforms = ingest(data, "1.0.0")
    # Read everything back from its stored form, as a consumer of the package would.
    stored = [canonical_json.loads(line) for line in as_lines(records).splitlines()]
    transform_lines = as_lines([t.to_json() for t in transforms]).splitlines()
    by_id = {
        t.id: t
        for t in (
            transform_record_from_json(canonical_json.loads(line)) for line in transform_lines
        )
    }

    traced = []
    for record in stored:
        assert isinstance(record, Mapping)
        provenance = provenance_of(record)
        assert provenance.evidence.source == content_id(data)
        cited = resolve(data, provenance.evidence.locator, decode=lambda b: b)
        traced.append((cited, chain(provenance.transform, by_id)))

    assert traced == [
        ("90", ["csv-limits@1.0.0"]),
        ("90", ["csv-limits@1.0.0", "neptune.si@1"]),
        ("120", ["csv-limits@1.0.0"]),
        ("120", ["csv-limits@1.0.0", "neptune.si@1"]),
    ]
    si = records[1]
    assert si["unit"] == "rad.s^-1"
    assert si["value"] == {"pi_power": 1, "rational": "1/2"}  # 90 deg/s = pi/2 rad/s


def test_byte_ranges_and_nested_locators_resolve_from_the_outermost_source_inward() -> None:
    data = FIXTURE.read_bytes()
    offset = data.index(b"elbow,120") + len(b"elbow,")
    assert resolve(data, (ByteRange(offset, 3),), decode=lambda b: b) == "120"

    urdf_like = json.dumps({"joints": [{"limit/max": {"velocity": 1.5708}}]}).encode()
    archive = gzip.compress(urdf_like, mtime=0)
    ref = EvidenceRef(
        content_id(archive),
        (ByteRange(0, len(archive)), JsonPointer("/joints/0/limit~1max/velocity")),
    )
    assert resolve(archive, ref.locator, decode=gzip.decompress) == 1.5708


def test_an_adapter_upgrade_adds_a_lineage_beside_the_old_one() -> None:
    data = FIXTURE.read_bytes()
    v1_records, v1_transforms = ingest(data, "1.0.0")
    v1_bytes = as_lines(v1_records)
    v2_records, v2_transforms = ingest(data, "1.1.0")

    # Durable citations are identical across lineages ...
    assert [provenance_of(r).evidence for r in v1_records] == [
        provenance_of(r).evidence for r in v2_records
    ]
    # ... while every tier-2 id is new, including the normalised records downstream of the upgrade.
    assert not {r["id"] for r in v1_records} & {r["id"] for r in v2_records}
    assert not {t.id for t in v1_transforms} & {t.id for t in v2_transforms}
    # Nothing about the first lineage changed.
    assert as_lines(ingest(data, "1.0.0")[0]) == v1_bytes


def test_ingest_is_byte_deterministic() -> None:
    data = FIXTURE.read_bytes()
    first, first_transforms = ingest(data, "1.0.0")
    second, second_transforms = ingest(data, "1.0.0")
    assert as_lines(first) == as_lines(second)
    assert as_lines([t.to_json() for t in first_transforms]) == as_lines(
        [t.to_json() for t in second_transforms]
    )
