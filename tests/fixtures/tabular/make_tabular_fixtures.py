"""Build the tabular adapter's fixtures: real small tables across embodiments, and broken ones.

Run ``uv run python tests/fixtures/tabular/make_tabular_fixtures.py`` to rewrite every file under
``tests/fixtures/tabular/``. Text files are byte-for-byte deterministic. The Parquet files are
written by pyarrow, whose ``created_by`` string carries its version, so the tests compare them to
``build()`` by content, not by bytes.

``--check`` validates the Parquet files with the official reader and nothing of Neptune's::

    uv run --no-project --with pyarrow python \
        tests/fixtures/tabular/make_tabular_fixtures.py --check
"""

import datetime
import decimal
import io
import sys
from pathlib import Path
from typing import Final

HERE: Final = Path(__file__).parent

# --- Text tables ---------------------------------------------------------------------------------

# A warehouse mobile base: comma-delimited, a quoted comma and a doubled quote, a blank, CRLF.
TELEMETRY_CSV: Final = (
    b"t_ms,battery_pct,note,x_m,y_m\r\n"
    b'0,97.5,"docked, charging",0.0,0.0\r\n'
    b'1000,97.1,"said ""go""",0.25,0.0\r\n'
    b"2000,,,0.5,0.01\r\n"
    b"3000,96.8,n/a,0.75,0.02\r\n"
)

# A legged platform's inspection register: tab-delimited, UTF-8, a non-ASCII name, blank cells.
INSPECTION_TSV: Final = (
    "asset_id\tasset\tstatus\tinspected\n"
    "A-001\tHip actuator FL\tOK\t2026-03-02\n"
    "A-002\tFoot sensor ÅR\tWORN\t2026-03-02\n"
    "A-003\tBattery bay\t\t\n"
).encode()

# A marine vehicle's event log: JSON Lines, nested objects, null, a number no double holds.
EVENTS_JSONL: Final = (
    b'{"t":0,"kind":"dive","depth_m":0.0,"gps":null}\n'
    b'{"t":5,"kind":"dive","depth_m":12.5,"sensors":{"ctd":{"temp_c":9.5,"sal":35.1}}}\n'
    b"\n"
    b'{"t":9,"kind":"fault","code":18446744073709551615,"detail":"","legs":[1,2.5,true]}\n'
    b'{"t":12,"kind":"surface","big":123456789012345678901234567890}\n'
)

# A manipulator's joint states: a JSON array of objects with a heterogeneous row.
JOINT_STATES_JSON: Final = (
    b'[{"stamp":{"sec":10,"nsec":500},"name":["j1","j2"],"position":[0.1,-0.2]},\n'
    b' {"stamp":{"sec":10,"nsec":600},"name":["j1","j2"],"position":[0.15,-0.25],"effort":[]},\n'
    b' {"stamp":{"sec":10,"nsec":700},"name":["j1"],"position":[0.2]}]\n'
)

# Broken: a CSV with ragged rows, a stray text after a quote and a Latin-1 byte. Its records
# disagree in the head, so nothing sniffs a delimiter: it is read with ``csv_delimiter`` declared.
RAGGED_CSV: Final = (
    b'id,site,reading\n1,north,4.5\n2,south\n3,east,7.0,extra\n4,"west"x,1.0\n5,caf\xe9,2.0\n'
)

# Broken: a quoted field that never closes (the file was cut inside it).
UNCLOSED_CSV: Final = b'id,comment,by\n1,fine,ana\n2,"the operator wrote that the arm\n'

# Broken: JSON Lines with one syntax error, one bad UTF-8 row, one duplicated key.
DAMAGED_JSONL: Final = (
    b'{"i":1,"ok":true}\n'
    b'{"i":2,"ok":tru}\n'
    b'{"i":3,"name":"\xff\xfe"}\n'
    b'{"i":4,"i":5}\n'
    b'{"i":6,"ok":false}\n'
)

# Broken: a JSON array cut in its third element.
TRUNCATED_JSON: Final = b'[{"a":1},{"a":2},{"a":'

TEXT_FILES: Final[dict[str, bytes]] = {
    "telemetry_amr.csv": TELEMETRY_CSV,
    "inspection_quadruped.tsv": INSPECTION_TSV,
    "events_auv.jsonl": EVENTS_JSONL,
    "joint_states_arm.json": JOINT_STATES_JSON,
    "ragged.csv": RAGGED_CSV,
    "unclosed_quote.csv": UNCLOSED_CSV,
    "damaged.jsonl": DAMAGED_JSONL,
    "truncated.json": TRUNCATED_JSON,
}

# --- Parquet -------------------------------------------------------------------------------------

JOINT_NAMES: Final = ("hip_l", "hip_r", "knee_l", "knee_r")
ROW_GROUPS: Final = (4, 4, 2)


def humanoid_rows() -> dict[str, list[object]]:
    """Ten rows of a humanoid's joint log: nested state, nulls, decimals, a list and bytes."""
    count = sum(ROW_GROUPS)
    start = datetime.datetime(2026, 3, 2, 8, 0, 0, tzinfo=datetime.UTC)
    return {
        "stamp": [start + datetime.timedelta(milliseconds=20 * i) for i in range(count)],
        "joint": [JOINT_NAMES[i % 4] for i in range(count)],
        "position_rad": [None if i == 3 else 0.5 * i - 1 for i in range(count)],
        "effort_nm": [1.5 * i for i in range(count)],
        "torque": [decimal.Decimal(i) / decimal.Decimal(8) for i in range(count)],
        "state": [{"mode": "hold" if i % 2 else "walk", "fault": i == 7} for i in range(count)],
        "tags": [["gait", f"step{i}"] for i in range(count)],
        "raw": [bytes([i, 255]) for i in range(count)],
        "day": [datetime.date(2026, 3, 2 + i // 5) for i in range(count)],
    }


def humanoid_parquet() -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    columns = humanoid_rows()
    schema = pa.schema(
        [
            ("stamp", pa.timestamp("us", tz="UTC")),
            ("joint", pa.string()),
            ("position_rad", pa.float64()),
            ("effort_nm", pa.float32()),
            ("torque", pa.decimal128(10, 3)),
            ("state", pa.struct([("mode", pa.string()), ("fault", pa.bool_())])),
            ("tags", pa.list_(pa.string())),
            ("raw", pa.binary()),
            ("day", pa.date32()),
        ],
        metadata={"robot": "humanoid-h1", "frame": "base_link"},
    )
    table = pa.table(columns, schema=schema)
    sink = io.BytesIO()
    with pq.ParquetWriter(sink, schema, compression="snappy", use_dictionary=["joint"]) as writer:
        offset = 0
        for height in ROW_GROUPS:
            writer.write_table(table.slice(offset, height))
            offset += height
    return sink.getvalue()


def build() -> dict[str, bytes]:
    parquet = humanoid_parquet()
    broken = bytearray(parquet)
    broken[-4:] = b"PAR2"
    return {
        **TEXT_FILES,
        "humanoid_joints.parquet": parquet,
        "truncated.parquet": parquet[: len(parquet) * 6 // 10],
        "bad_tail.parquet": bytes(broken),
    }


def write() -> None:
    for name, data in build().items():
        (HERE / name).write_bytes(data)


def check() -> None:
    """Read the committed Parquet files with pyarrow alone and compare them to the generator."""
    import pyarrow.parquet as pq

    committed = pq.ParquetFile(HERE / "humanoid_joints.parquet")
    assert committed.metadata.num_row_groups == len(ROW_GROUPS)
    assert [committed.metadata.row_group(i).num_rows for i in range(3)] == list(ROW_GROUPS)
    table = committed.read()
    expected = humanoid_rows()
    assert table.column_names == list(expected)
    for name, values in expected.items():
        assert table.column(name).to_pylist() == values, name
    assert committed.schema_arrow.metadata[b"robot"] == b"humanoid-h1"
    for name in ("truncated.parquet", "bad_tail.parquet"):
        try:
            pq.ParquetFile(HERE / name)
        except Exception:  # pyarrow raises ArrowInvalid, OSError or others
            continue
        raise AssertionError(f"pyarrow reads {name}, which must be broken")
    sys.stdout.write("ok: the Parquet fixtures read with pyarrow as the generator describes them\n")


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        check()
    else:
        write()
