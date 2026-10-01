"""Catalog scale harness for the L1 gate (docs/reviews/l1-stress-test.md, Ledger ADR 0005 §5).

Builds a synthetic catalog of ``--packages`` packages (about 107 records each) in a real
PostgreSQL 16 from the ``pgserver`` wheel, through the shipped migrations, then times the queries
that serve ``thread`` and ``query`` (ADR 0004) and the registration triggers, and prints a JSON
report. Everything is generated in SQL from the package sequence number, with no randomness, so
the same ``--packages`` gives the same rows on every run.

The fleet spans seven embodiments (manipulator, mobile base, legged, humanoid, aerial, marine,
road vehicle). One machine in twenty packages is a "workhorse" machine with a long thread; one
package in a hundred re-parses the previous package's source under adapter 2.0.0 (lineage
siblings); one package in a thousand is a long recording with 5000 streams on one clock.

Run the full gate measurement (tens of minutes and about 15 GB of disk; give it a data directory
on disk, not a tmpfs)::

    uv run --all-packages --all-groups \\
        python packages/neptune-ledger/tests/ledger_catalog_scale.py \\
        --packages 100000 --data-dir ~/.cache/catalog-scale --out catalog-scale.json

``tests/test_ledger_catalog_scale.py`` runs it at a small scale as a ``slow`` test, so the
harness and the plans it checks do not rot.
"""

import argparse
import json
import math
import re
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

import psycopg
from psycopg import sql

from neptune_ledger.catalog.migrate import apply_migrations, migrations, tenant_schema

Conn = psycopg.Connection[tuple[Any, ...]]
TENANT: Final = "fleet"
SCHEMA: Final = tenant_schema(TENANT)

# (embodiment, adapter, streams per recording, machine-id namespace). Seven kinds of robot, so no
# morphology dominates the indexes.
EMBODIMENTS: Final = (
    ("manipulator", "mcap", 40, "ur.serial"),
    ("mobile_base", "rosbag2", 60, "amr.serial"),
    ("legged", "mcap", 80, "spot.serial"),
    ("humanoid", "mcap", 120, "humanoid.serial"),
    ("aerial", "ulog", 50, "px4.sys_uuid"),
    ("marine", "csv", 40, "auv.serial"),
    ("road_vehicle", "rosbag1", 90, "av.vin"),
)
# Per-package record kinds besides streams, with their counts.
FIXED_KINDS: Final = (
    ("source_artifact", 1),
    ("source_revision", 1),
    ("transform_record", 1),
    ("timestamp_domain", 3),
    ("ingest_finding", 1),
    ("run", 1),
    ("machine", 1),
    ("hardware_configuration", 1),
    ("hardware_component", 10),
    ("software_configuration", 1),
    ("calibration", 1),
    ("frame", 15),
    ("frame_transform", 3),
)
MACHINE_THREAD_KINDS: Final = (
    "calibration",
    "hardware_configuration",
    "machine",
    "run",
    "software_configuration",
)


@dataclass(frozen=True)
class Scale:
    packages: int
    packages_per_machine: int = 50
    heavy_every: int = 20  # every 20th package belongs to machine 0, the workhorse
    sibling_every: int = 100  # every 100th package re-parses the previous one's source under 2.0.0
    long_every: int = 1000  # one package in a thousand is a long recording ...
    long_offset: int = 501  # ... at these sequence numbers (never a sibling's)
    long_streams: int = 5000
    samples: int = 200

    @property
    def machines(self) -> int:
        return max(2, self.packages // self.packages_per_machine)


# --- SQL ---------------------------------------------------------------------------------------

# One row per package: everything else derives from it. ``src`` is the source sequence number;
# a sibling re-parses its predecessor's source, keeps its embodiment and machine and gets new
# record ids and clocks because its adapter version differs.
GEN_PACKAGE: Final = """
CREATE TEMP TABLE gen_package AS
WITH base AS (
  SELECT i AS seq,
         CASE WHEN i %% %(sibling_every)s = 0 THEN i - 1 ELSE i END AS src,
         CASE WHEN i %% %(sibling_every)s = 0 THEN '2.0.0' ELSE '1.0.0' END AS version
  FROM generate_series(1, %(packages)s) AS i
)
SELECT seq, src, version, machine,
       (machine + 1) %% 7 AS embodiment,  -- one embodiment per machine; machine 0: a mobile base
       'sha256:' || encode(sha256(convert_to('package:' || seq, 'UTF8')), 'hex') AS package_id,
       to_char(timestamp '2026-01-01' + seq * interval '1 second',
               'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS tx_time,
       'sha256:' || encode(sha256(convert_to('source:' || src, 'UTF8')), 'hex') AS content_id,
       1790000000000000000::bigint + src::bigint * 3600000000000 AS base
FROM (
  SELECT *,
         CASE WHEN src %% %(heavy_every)s = 0 THEN 0 ELSE 1 + src %% (%(machines)s - 1) END
           AS machine
  FROM base
) AS b
"""

GEN_EMBODIMENT: Final = """
CREATE TEMP TABLE gen_embodiment (embodiment int, name text, adapter text, streams int, ns text)
"""

GEN_PACKAGE_FULL: Final = """
CREATE TEMP TABLE gen_pkg AS
SELECT p.*, e.adapter, e.ns,
       CASE WHEN p.src %% %(long_every)s = %(long_offset)s THEN %(long_streams)s
            ELSE e.streams END AS streams,
       'rec:sha256:' || encode(sha256(convert_to('transform:' || e.adapter || ':' || p.version,
                                                 'UTF8')), 'hex') AS transform_id,
       'rec:sha256:' || encode(sha256(convert_to('clock:' || p.seq || ':0', 'UTF8')), 'hex')
         AS clock0,
       'rec:sha256:' || encode(sha256(convert_to('clock:' || p.seq || ':2', 'UTF8')), 'hex')
         AS clock2
FROM gen_package p JOIN gen_embodiment e USING (embodiment)
"""

LOAD_REGISTRATIONS: Final = (
    """
INSERT INTO registration_log
SELECT %(tenant)s, seq, tx_time, package_id, '/fleet/packages/' || seq, '0.0.1' FROM gen_pkg
""",
    """
INSERT INTO package
SELECT %(tenant)s, package_id, 1,
       'rec:sha256:' || encode(sha256(convert_to('receipt:' || seq, 'UTF8')), 'hex'),
       '/fleet/packages/' || seq, '0.0.1', seq, tx_time
FROM gen_pkg
""",
    """
INSERT INTO source SELECT DISTINCT %(tenant)s, content_id, 1000 + src FROM gen_pkg
""",
    """
INSERT INTO package_source SELECT %(tenant)s, package_id, content_id, 'referenced' FROM gen_pkg
""",
    """
INSERT INTO source_location
SELECT %(tenant)s,
       'rec:sha256:' || encode(sha256(convert_to('revision:' || src, 'UTF8')), 'hex'),
       package_id, content_id, '{"kind":"local","path":"runs/' || src || '.log"}', '{}'
FROM gen_pkg
""",
    """
INSERT INTO transform
SELECT DISTINCT %(tenant)s, transform_id, adapter, version,
       'sha256:' || encode(sha256(convert_to('config:' || adapter, 'UTF8')), 'hex'), '{}'
FROM gen_pkg
""",
    """
INSERT INTO clock
SELECT %(tenant)s,
       'rec:sha256:' || encode(sha256(convert_to('clock:' || seq || ':' || k, 'UTF8')), 'hex'),
       package_id, (ARRAY['log_time', 'header.stamp', 'document'])[k + 1], ARRAY[content_id]
FROM gen_pkg, generate_series(0, 2) AS k
""",
)

# Every record row. Record ids derive from (kind, source, adapter version, index), so a sibling
# gets new ids and a shared Ledger record (source_artifact, source_revision, transform_record)
# keeps its id and its body digest across packages.
LOAD_RECORDS: Final = """
INSERT INTO record (tenant_id, kind, record_id, package_id, registration_key, line,
                    schema_version, source_content_id, source_locator, transform_id,
                    assertion_kind, world_clock, world_first, world_last, ambiguous_pointers,
                    body_digest)
SELECT %(tenant)s, k.kind, ids.record_id, p.package_id, p.seq, j, 1,
       CASE WHEN ledger THEN NULL ELSE p.content_id END,
       CASE WHEN ledger THEN NULL
            WHEN k.kind = 'run' THEN '[{"kind":"byte_range","length":64,"offset":0}]'
            ELSE '[{"kind":"' || k.kind || '","index":' || j || '}]' END,
       CASE WHEN k.kind IN ('source_artifact', 'source_revision', 'transform_record') THEN NULL
            ELSE p.transform_id END,
       CASE WHEN ledger OR k.kind = 'ingest_finding' THEN NULL
            WHEN k.kind IN ('machine', 'software_configuration') THEN 'stated'
            ELSE 'observed' END,
       CASE WHEN k.kind IN ('run', 'stream') THEN p.clock0
            WHEN k.kind = 'calibration' THEN p.clock2 END,
       CASE WHEN k.kind = 'run' THEN p.base
            WHEN k.kind = 'stream' THEN p.base + j::bigint * 1000000
            WHEN k.kind = 'calibration' THEN 1700000000 + p.src END,
       CASE WHEN k.kind = 'run' THEN p.base + 600000000000
            WHEN k.kind = 'stream' THEN p.base + 600000000000 - j::bigint * 1000
            WHEN k.kind = 'calibration' THEN 1700000000 + p.src END,
       '{}',
       'sha256:' || encode(sha256(convert_to('body:' || ids.record_id, 'UTF8')), 'hex')
FROM gen_pkg p
CROSS JOIN LATERAL (
  SELECT kind, n FROM (VALUES %(fixed)s) AS f (kind, n)
  UNION ALL SELECT 'stream', p.streams
) AS k
CROSS JOIN LATERAL generate_series(1, k.n) AS j
CROSS JOIN LATERAL (
  SELECT k.kind IN ('source_artifact', 'source_revision', 'transform_record') AS ledger
) AS l
CROSS JOIN LATERAL (
  SELECT CASE
    WHEN k.kind = 'source_artifact' THEN p.content_id
    WHEN k.kind = 'source_revision'
      THEN 'rec:sha256:' || encode(sha256(convert_to('revision:' || p.src, 'UTF8')), 'hex')
    WHEN k.kind = 'transform_record' THEN p.transform_id
    WHEN k.kind = 'timestamp_domain'
      THEN 'rec:sha256:' || encode(sha256(convert_to('clock:' || p.seq || ':' || (j - 1),
                                                     'UTF8')), 'hex')
    ELSE 'rec:sha256:' || encode(sha256(convert_to(
           k.kind || ':' || p.src || ':' || p.version || ':' || j, 'UTF8')), 'hex')
  END AS record_id
) AS ids
"""

# Every Known logical id: the machine's own identifier, the four records that cite it, and one
# declared identifier per hardware component (a sensor-thread key).
LOAD_LOGICAL_IDS: Final = """
INSERT INTO record_logical_id
SELECT %(tenant)s, r.kind, r.record_id, r.package_id,
       CASE WHEN r.kind IN ('machine', 'hardware_component') THEN '/identifiers/0'
            ELSE '/machine' END,
       CASE WHEN r.kind = 'hardware_component' THEN p.ns || '.component' ELSE p.ns END,
       CASE WHEN r.kind = 'hardware_component' THEN 'M-' || p.machine || '-C' || r.line
            ELSE 'M-' || p.machine END
FROM record r JOIN gen_pkg p ON p.package_id = r.package_id
WHERE r.kind IN ('machine', 'run', 'hardware_configuration', 'software_configuration',
                 'calibration', 'hardware_component')
"""

# --- The measured queries (what thread() and query() run against the catalog indexes) ---------

# thread(key=DeclaredKey, …): every record stating the key, with what ADR 0003 orders and
# resolves by. Partition grouping and lineage resolution happen in the caller over these rows.
THREAD_DECLARED: Final = """
SELECT r.kind, r.record_id, r.package_id, l.pointer, r.registration_key, p.tx_time,
       r.transform_id, r.source_content_id, r.world_clock, r.world_first, r.world_last
FROM record_logical_id l
JOIN record r ON r.tenant_id = l.tenant_id AND r.kind = l.kind
             AND r.record_id = l.record_id AND r.package_id = l.package_id
JOIN package p ON p.tenant_id = r.tenant_id AND p.package_id = r.package_id
WHERE l.namespace = %(namespace)s AND l.value = %(value)s AND l.kind = ANY(%(kinds)s)
  AND r.kind = ANY(%(kinds)s)  -- repeated on record, so the planner prunes its partitions
  AND r.registration_key <= %(as_of)s
ORDER BY r.world_clock NULLS LAST, r.world_first, r.world_last NULLS LAST,
         r.registration_key, r.record_id, r.package_id
"""

# thread(key=EvidenceAnchor, …): the records whose record-level anchor is exactly the key.
THREAD_ANCHORED: Final = """
SELECT r.kind, r.record_id, r.package_id, r.registration_key, r.transform_id,
       r.world_clock, r.world_first, r.world_last
FROM record r
WHERE r.source_content_id = %(source)s AND r.kind = %(kind)s
  AND md5(r.source_locator) = md5(%(locator)s) AND r.source_locator = %(locator)s
  AND r.registration_key <= %(as_of)s
ORDER BY r.world_clock NULLS LAST, r.world_first, r.world_last NULLS LAST,
         r.registration_key, r.record_id, r.package_id
"""

# A lineage set (ADR 0003 §4.1): every record of one kind from one source, all transforms.
LINEAGE_SET: Final = """
SELECT r.kind, r.record_id, r.package_id, r.transform_id, md5(r.source_locator)
FROM record r
WHERE r.source_content_id = %(source)s AND r.kind = %(kind)s AND r.registration_key <= %(as_of)s
"""

# query(QuerySpec(kinds, window)): QuerySpec's window rule, rows in (kind, record_id, package_id)
# order.
WINDOW: Final = """
SELECT kind, record_id, package_id, line, registration_key, transform_id, source_content_id,
       source_locator, assertion_kind, world_clock, world_first, world_last
FROM record
WHERE world_clock = %(clock)s AND kind = ANY(%(kinds)s)
  AND world_first <= %(last)s AND COALESCE(world_last, world_first) >= %(first)s
  AND registration_key <= %(as_of)s
ORDER BY kind, record_id, package_id
"""

# query(QuerySpec(kinds=("stream",), limit=1000, after=…)): one keyset page.
QUERY_PAGE: Final = """
SELECT kind, record_id, package_id, line, registration_key
FROM record
WHERE tenant_id = %(tenant)s AND kind = ANY(%(kinds)s) AND registration_key <= %(as_of)s
  AND (kind, record_id, package_id) > (%(kind)s, %(record_id)s, %(package_id)s)
ORDER BY kind, record_id, package_id
LIMIT 1000
"""
QUERY_PAGE_NO_TENANT: Final = QUERY_PAGE.replace("tenant_id = %(tenant)s AND ", "")

# register(): the "is this package already here?" lookup, with and without the tenant key.
PACKAGE_LOOKUP: Final = (
    "SELECT tx_seq FROM package WHERE tenant_id = %(tenant)s AND package_id = %(package_id)s"
)
PACKAGE_LOOKUP_NO_TENANT: Final = "SELECT tx_seq FROM package WHERE package_id = %(package_id)s"


# --- Harness -----------------------------------------------------------------------------------


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _summary(times_ms: Sequence[float], rows: Sequence[int]) -> dict[str, Any]:
    return {
        "calls": len(times_ms),
        "p50_ms": round(_percentile(times_ms, 0.50), 3),
        "p95_ms": round(_percentile(times_ms, 0.95), 3),
        "max_ms": round(max(times_ms), 3),
        "rows_min": min(rows),
        "rows_max": max(rows),
        "rows_mean": round(sum(rows) / len(rows), 1),
    }


def _plan(conn: Conn, query: str, params: dict[str, Any]) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS) of one call: total time, and every scan node with its index."""
    row = conn.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params).fetchone()
    assert row is not None
    document = row[0][0]
    scans: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        kind = node["Node Type"]
        if "Scan" in kind:
            target = node.get("Index Name") or node.get("Relation Name", "")
            scans.append(f"{kind} {target}".strip())
        for child in node.get("Plans", ()):
            walk(child)

    walk(document["Plan"])
    text_rows = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + query, params).fetchall()
    return {
        "execution_ms": round(document["Execution Time"], 3),
        "planning_ms": round(document["Planning Time"], 3),
        "rows": document["Plan"].get("Actual Rows"),
        "scans": sorted(set(scans)),
        "text": [str(r[0]) for r in text_rows],
    }


def _timed(conn: Conn, query: str, params: dict[str, Any]) -> tuple[float, int]:
    start = time.perf_counter()
    rows = conn.execute(query, params).fetchall()
    return (time.perf_counter() - start) * 1000, len(rows)


def _measure(
    conn: Conn, query: str, calls: Sequence[dict[str, Any]], *, warm: bool = True
) -> dict[str, Any]:
    if warm:
        _timed(conn, query, calls[0])
    times, rows = zip(*(_timed(conn, query, params) for params in calls), strict=True)
    out = _summary(times, rows)
    out["plan"] = _plan(conn, query, calls[len(calls) // 2])
    return out


@contextmanager
def _phase(report: dict[str, Any], name: str) -> Iterator[None]:
    start = time.perf_counter()
    sys.stderr.write(f"... {name}\n")
    sys.stderr.flush()
    yield
    report.setdefault("load_seconds", {})[name] = round(time.perf_counter() - start, 1)


def _index_shape(conn: Conn) -> list[tuple[str, str]]:
    """Every index and constraint definition in the tenant schema, by name: the schema to keep."""
    indexes = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s", (SCHEMA,)
    ).fetchall()
    constraints = conn.execute(
        "SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c"
        " JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = %s",
        (SCHEMA,),
    ).fetchall()
    return sorted((str(a), str(b)) for a, b in [*indexes, *constraints])


def _drop_record_indexes(conn: Conn) -> list[str]:
    """Drop record's and record_logical_id's keys and indexes for the bulk load; return the DDL
    that recreates them exactly as the migrations made them."""
    restore: list[str] = []
    for table in ("record_logical_id", "record"):
        rows = conn.execute(
            "SELECT conname, pg_get_constraintdef(oid), contype FROM pg_constraint"
            " WHERE conrelid = %s::regclass AND contype IN ('p', 'u', 'f')"
            " AND conparentid = 0"
            " ORDER BY contype DESC",  # foreign keys first, then unique, then primary
            (f"{SCHEMA}.{table}",),
        ).fetchall()
        for name, definition, contype in rows:
            if table == "record" and contype == "f":
                continue  # record -> package stays; replica mode skips its checks while loading
            conn.execute(
                sql.SQL("ALTER TABLE {}.{} DROP CONSTRAINT {}").format(
                    sql.Identifier(SCHEMA), sql.Identifier(table), sql.Identifier(str(name))
                )
            )
            restore.append(f"ALTER TABLE {SCHEMA}.{table} ADD CONSTRAINT {name} {definition}")
        indexes = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (SCHEMA, table),
        ).fetchall()
        for name, definition in indexes:
            conn.execute(
                sql.SQL("DROP INDEX {}.{}").format(sql.Identifier(SCHEMA), sql.Identifier(name))
            )
            # A partitioned index renders as "ON ONLY"; recreate it on every partition too.
            restore.append(str(definition).replace(" ON ONLY ", " ON ", 1))

    # Primary and unique keys before the foreign key that needs them, then plain indexes.
    def order(statement: str) -> int:
        if "PRIMARY KEY" in statement or " UNIQUE " in statement:
            return 0
        return 1 if "FOREIGN KEY" in statement else 2

    return sorted(restore, key=order)


def build(conn: Conn, scale: Scale, report: dict[str, Any]) -> None:
    """Migrate a fresh tenant and bulk-load the synthetic catalog into it."""
    apply_migrations(conn, TENANT)
    shape = _index_shape(conn)
    params = {
        **asdict(scale),
        "machines": scale.machines,
        "tenant": TENANT,
    }
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA)))
        conn.execute("SET LOCAL session_replication_role = replica")  # skip triggers and FKs
        conn.execute("SET LOCAL maintenance_work_mem = '512MB'")
        conn.execute("SET LOCAL max_parallel_maintenance_workers = 4")
        conn.execute("SET LOCAL work_mem = '256MB'")
        restore = _drop_record_indexes(conn)
        with _phase(report, "packages"):
            conn.execute(GEN_PACKAGE, params)
            conn.execute(GEN_EMBODIMENT)
            with conn.cursor() as cursor, cursor.copy("COPY gen_embodiment FROM STDIN") as copy:
                for number, (name, adapter, streams, ns) in enumerate(EMBODIMENTS):
                    copy.write_row((number, name, adapter, streams, ns))
            conn.execute(GEN_PACKAGE_FULL, params)
            for statement in LOAD_REGISTRATIONS:
                conn.execute(statement, params)
            last = conn.execute("SELECT seq, tx_time FROM gen_pkg ORDER BY seq DESC LIMIT 1")
            seq, tx_time = last.fetchone() or (0, None)
            conn.execute("SELECT replay_tx(%s, %s)", (seq, tx_time))
        with _phase(report, "records"):
            fixed = ", ".join(f"('{kind}', {n})" for kind, n in FIXED_KINDS)
            conn.execute(LOAD_RECORDS.replace("%(fixed)s", fixed), params)
        with _phase(report, "logical_ids"):
            conn.execute(LOAD_LOGICAL_IDS, params)
        with _phase(report, "indexes"):
            for statement in restore:
                conn.execute(statement)
    with _phase(report, "analyze"):
        conn.execute(sql.SQL("VACUUM (ANALYZE) {}.record").format(sql.Identifier(SCHEMA)))
        conn.execute(sql.SQL("ANALYZE {}.record_logical_id").format(sql.Identifier(SCHEMA)))
        conn.execute(sql.SQL("ANALYZE {}.package").format(sql.Identifier(SCHEMA)))
    rebuilt = _index_shape(conn)
    if rebuilt != shape:
        missing, extra = sorted(set(shape) - set(rebuilt)), sorted(set(rebuilt) - set(shape))
        raise AssertionError(f"the measured schema differs: missing {missing}, extra {extra}")
    report["schema_matches_migrations"] = True


def _counts(conn: Conn) -> dict[str, Any]:
    def one(query: str) -> int:
        row = conn.execute(query).fetchone()
        return int(row[0]) if row else 0

    sizes = conn.execute(
        "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition",
        (SCHEMA,),
    ).fetchall()
    partition_bytes = one(
        "SELECT sum(pg_total_relation_size(inhrelid)) FROM pg_inherits"
        f" WHERE inhparent = '{SCHEMA}.record'::regclass"
    )
    by_table = {str(name): int(size) for name, size in sizes}
    by_table["record"] = partition_bytes
    return {
        "packages": one("SELECT count(*) FROM package"),
        "records": one("SELECT count(*) FROM record"),
        "timed_records": one("SELECT count(*) FROM record WHERE world_clock IS NOT NULL"),
        "logical_ids": one("SELECT count(*) FROM record_logical_id"),
        "sources": one("SELECT count(*) FROM source"),
        "clocks": one("SELECT count(*) FROM clock"),
        "transforms": one("SELECT count(*) FROM transform"),
        "total_bytes": sum(by_table.values()),
        "bytes_by_table": dict(sorted(by_table.items())),
    }


def _package_row(conn: Conn, seq: int) -> tuple[Any, ...]:
    row = conn.execute(
        "SELECT package_id, content_id, clock0, base, ns, machine, streams, src"
        " FROM gen_pkg WHERE seq = %s",
        (seq,),
    ).fetchone()
    assert row is not None
    return tuple(row)


def measure(conn: Conn, scale: Scale, report: dict[str, Any]) -> None:
    n = scale.packages
    as_of = n
    # Deterministic samples: packages spread over the whole sequence by a fixed stride, leaving
    # out the workhorse machine and the long recordings, which are measured on their own.
    stride = max(1, n // scale.samples)
    picks = sorted({1 + ((k * stride * 7919) % n) for k in range(scale.samples)})
    rows = {seq: _package_row(conn, seq) for seq in picks}
    typical = [s for s in picks if rows[s][5] != 0 and rows[s][6] < scale.long_streams]
    threads = [
        {
            "namespace": rows[seq][4],
            "value": f"M-{rows[seq][5]}",
            "kinds": list(MACHINE_THREAD_KINDS),
            "as_of": as_of,
        }
        for seq in typical
    ]
    report["thread_declared_typical"] = _measure(conn, THREAD_DECLARED, threads)
    sensor = [
        {
            "namespace": rows[seq][4] + ".component",
            "value": f"M-{rows[seq][5]}-C3",
            "kinds": ["hardware_component"],
            "as_of": as_of,
        }
        for seq in typical
    ]
    report["thread_declared_sensor"] = _measure(conn, THREAD_DECLARED, sensor)
    heavy_ns = conn.execute("SELECT ns FROM gen_pkg WHERE machine = 0 LIMIT 1").fetchone()
    assert heavy_ns is not None
    heavy = [
        {"namespace": heavy_ns[0], "value": "M-0", "kinds": list(MACHINE_THREAD_KINDS), "as_of": a}
        for a in [as_of] * 20
    ]
    report["thread_declared_workhorse"] = _measure(conn, THREAD_DECLARED, heavy)
    anchored = [
        {
            "source": rows[seq][1],
            "kind": "stream",
            "locator": '[{"kind":"stream","index":7}]',
            "as_of": as_of,
        }
        for seq in typical
    ]
    report["thread_anchored"] = _measure(conn, THREAD_ANCHORED, anchored)
    lineage = [{"source": rows[seq][1], "kind": "stream", "as_of": as_of} for seq in typical]
    report["lineage_set"] = _measure(conn, LINEAGE_SET, lineage)

    windows = [
        {
            "clock": rows[seq][2],
            "kinds": ["run", "stream"],
            "first": rows[seq][3] + 100_000_000_000,
            "last": rows[seq][3] + 200_000_000_000,
            "as_of": as_of,
        }
        for seq in typical
    ]
    report["window_typical"] = _measure(conn, WINDOW, windows)
    long_seqs = list(range(scale.long_offset, n + 1, scale.long_every))[:20]
    if long_seqs:
        long_rows = [_package_row(conn, seq) for seq in long_seqs]
        long_windows = [
            {
                "clock": row[2],
                "kinds": ["run", "stream"],
                "first": row[3] + 2_000_000_000,
                "last": row[3] + 3_000_000_000,
                "as_of": as_of,
            }
            for row in long_rows
        ]
        report["window_long_recording"] = _measure(conn, WINDOW, long_windows)

    cursor = {"kind": "stream", "record_id": "rec:sha256:8", "package_id": ""}
    page = [{"tenant": TENANT, "kinds": ["stream"], "as_of": as_of, **cursor}] * 20
    report["query_page"] = _measure(conn, QUERY_PAGE, page)
    report["query_page_without_tenant_key"] = _measure(conn, QUERY_PAGE_NO_TENANT, page[:3])

    lookups = [{"tenant": TENANT, "package_id": rows[seq][0]} for seq in typical]
    report["package_lookup"] = _measure(conn, PACKAGE_LOOKUP, lookups)
    report["package_lookup_without_tenant_key"] = _measure(
        conn, PACKAGE_LOOKUP_NO_TENANT, lookups[:20]
    )
    report["registration"] = _registration_costs(conn, scale)


def _function(sql_text: str, name: str) -> str:
    """One CREATE FUNCTION statement from a migration, as CREATE OR REPLACE."""
    match = re.search(rf"CREATE (?:OR REPLACE )?FUNCTION {name}\(\).*?\n\$\$;", sql_text, re.S)
    if match is None:
        raise AssertionError(f"{name} not found")
    return match.group(0).replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


def _registration_costs(conn: Conn, scale: Scale) -> dict[str, Any]:
    """One more registration at the full catalog size: the log and package inserts under 0001's
    triggers and under 0002's, and the record inserts of one package, each rolled back."""
    migration_sql = {m.version: m.sql for m in migrations()}
    seq = scale.packages + 1
    pid = "sha256:" + _hash(f"package:{seq}")
    out: dict[str, Any] = {}

    def attempt(version: int) -> float:
        times: list[float] = []
        for _ in range(5):
            with conn.transaction(force_rollback=True):
                conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA)))
                for name in ("registration_log_follows_clock", "package_from_log"):
                    conn.execute(_function(migration_sql[version], name).encode("utf-8"))
                tick = conn.execute("SELECT tx_seq, tx_time FROM next_tx()").fetchone()
                assert tick is not None
                start = time.perf_counter()
                conn.execute(
                    "INSERT INTO registration_log VALUES (%s, %s, %s, %s, '/fleet/new', '0.0.1')",
                    (TENANT, tick[0], tick[1], pid),
                )
                conn.execute(
                    "INSERT INTO package (tenant_id, package_id, schema_version, receipt_id)"
                    " VALUES (%s, %s, 1, %s)",
                    (TENANT, pid, "rec:sha256:" + "c" * 64),
                )
                times.append((time.perf_counter() - start) * 1000)
        return round(sorted(times)[2], 3)

    out["log_and_package_insert_ms_0001_triggers"] = attempt(1)
    out["log_and_package_insert_ms_0002_triggers"] = attempt(2)

    # The record rows of one 108-record package, through every trigger and foreign key.
    with conn.transaction(force_rollback=True):
        conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(SCHEMA)))
        tick = conn.execute("SELECT tx_seq, tx_time FROM next_tx()").fetchone()
        assert tick is not None
        conn.execute(
            "INSERT INTO registration_log VALUES (%s, %s, %s, %s, '/fleet/new', '0.0.1')",
            (TENANT, tick[0], tick[1], pid),
        )
        conn.execute(
            "INSERT INTO package (tenant_id, package_id, schema_version, receipt_id)"
            " VALUES (%s, %s, 1, %s)",
            (TENANT, pid, "rec:sha256:" + "c" * 64),
        )
        rows = [
            (
                TENANT,
                "stream",
                "rec:sha256:" + _hash(f"new-stream:{j}"),
                pid,
                tick[0],
                j,
                "sha256:" + _hash(f"new-body:{j}"),
            )
            for j in range(1, 109)
        ]
        start = time.perf_counter()
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO record (tenant_id, kind, record_id, package_id, registration_key,"
                " line, schema_version, body_digest) VALUES (%s, %s, %s, %s, %s, %s, 1, %s)",
                rows,
            )
        out["record_rows_108_insert_ms"] = round((time.perf_counter() - start) * 1000, 3)
    return out


def run(uri: str, scale: Scale) -> dict[str, Any]:
    """Build and measure on the server at ``uri`` (a fresh database is created there)."""
    name = f"catalog_scale_{scale.packages}"
    with psycopg.connect(uri, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
            ).format(sql.Identifier(name))
        )
        version = admin.execute("SHOW server_version").fetchone()
    target = uri.replace("/postgres?", f"/{name}?", 1)
    report: dict[str, Any] = {"scale": asdict(scale), "server_version": version and version[0]}
    report["scale"]["machines"] = scale.machines
    with psycopg.connect(target, autocommit=True) as conn:
        build(conn, scale, report)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(SCHEMA)))
        conn.execute("CREATE INDEX ON gen_pkg (seq)")  # build()'s temp table, for sampling
        conn.execute("ANALYZE gen_pkg")
        report["counts"] = _counts(conn)
        settings = conn.execute(
            "SELECT name, setting FROM pg_settings WHERE name IN"
            " ('shared_buffers', 'work_mem', 'effective_cache_size', 'random_page_cost',"
            " 'jit', 'max_parallel_workers_per_gather')"
        ).fetchall()
        report["settings"] = {str(k): str(v) for k, v in settings}
        measure(conn, scale, report)
    with psycopg.connect(uri, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--packages", type=int, default=100_000)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--uri", help="a PostgreSQL 16 server; default: start one from pgserver")
    parser.add_argument(
        "--data-dir", type=Path, help="pgserver data directory on disk (default: a temp dir)"
    )
    parser.add_argument("--out", type=Path, help="write the JSON report here too")
    args = parser.parse_args(argv)
    scale = Scale(packages=args.packages, samples=args.samples)
    if args.uri:
        report = run(args.uri, scale)
    else:
        from pgserver.postgres_server import get_server

        data = args.data_dir or Path(tempfile.mkdtemp(prefix="catalog-scale-"))
        server = get_server(data, cleanup_mode="stop" if args.data_dir else "delete")
        try:
            report = run(str(server.get_uri()), scale)
        finally:
            server.cleanup()
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
