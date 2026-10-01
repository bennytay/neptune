"""The four worked example packages, loaded into the catalog by the rules of ADR 0002 §5.

The rows each package produces are listed in ``docs/catalog-walkthrough.md``; this test loads the
packages and checks the document's tables against the database. The loader below is a test
harness that applies the ADR's column mapping, not the registration API (MVL-88).

The packages are the compiler's committed examples: ``manifest.json`` and ``receipt.json`` from
``tests/golden/packages/<name>/`` and the record tables from
``tests/fixtures/model/<name>/records/``, which the manifest pins by hash.
"""

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest

import neptune_ledger
from neptune.model.kinds import RECORD_KINDS
from neptune_ledger.catalog.migrate import apply_migrations

Conn = psycopg.Connection[tuple[object, ...]]
REPO: Final = Path(__file__).resolve().parents[3]
PACKAGES: Final = REPO / "tests" / "golden" / "packages"
RECORDS: Final = REPO / "tests" / "fixtures" / "model"
WALKTHROUGH: Final = Path(__file__).resolve().parents[1] / "docs" / "catalog-walkthrough.md"
EXAMPLES: Final = ("drone", "quadruped", "manipulator", "mobile_robot")

# The fields that state an entry's world time (ADR 0003 §3): start, end, and the fallback that
# stands in for both when neither is Known.
WORLD_TIME: Final[dict[str, tuple[str, str, str | None]]] = {
    "run": ("first", "last", None),
    "stream": ("first", "last", None),
    "calibration": ("valid_from", "valid_until", "performed"),
}


RECORD_COLUMNS: Final = (
    "tenant_id, kind, record_id, package_id, registration_key, line, schema_version,"
    " source_content_id, source_locator, transform_id, assertion_kind,"
    " world_clock, world_first, world_last, ambiguous_pointers, body_digest"
)


@dataclass(frozen=True)
class Package:
    name: str
    manifest_bytes: bytes
    manifest: dict[str, Any]
    receipt: dict[str, Any]
    tables: dict[str, list[dict[str, Any]]]  # kind -> records, in file order

    @property
    def package_id(self) -> str:
        return "sha256:" + hashlib.sha256(self.manifest_bytes).hexdigest()


def load_package(name: str) -> Package:
    manifest_bytes = (PACKAGES / name / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    pinned = {f["path"]: f["sha256"] for f in manifest["files"]}
    tables: dict[str, list[dict[str, Any]]] = {}
    for kind in RECORD_KINDS:
        path = RECORDS / name / "records" / f"{kind}.jsonl"
        data = path.read_bytes() if path.exists() else b""
        assert pinned[f"records/{kind}.jsonl"] == "sha256:" + hashlib.sha256(data).hexdigest()
        tables[kind] = [json.loads(line) for line in data.splitlines()]
    receipt = json.loads((PACKAGES / name / "receipt.json").read_bytes())
    return Package(name, manifest_bytes, manifest, receipt, tables)


def canonical(value: Any) -> str:
    """Canonical JSON text (root ADR 0002): sorted keys, no whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def body_digest(record: dict[str, Any]) -> str:
    """sha256 of the record's canonical line, without the newline (ADR 0005 §2)."""
    return "sha256:" + hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _walk(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    """Every JSON object in ``value`` with its pointer, outermost first."""
    if isinstance(value, dict):
        yield pointer, value
        for key in sorted(value):
            yield from _walk(value[key], f"{pointer}/{_escape(key)}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{pointer}/{index}")


def ambiguous_pointers(record: dict[str, Any]) -> list[str]:
    found = [p for p, obj in _walk(record) if obj.get("knowledge") == "ambiguous"]
    # An Ambiguous value's candidates are not themselves fields of the record.
    return sorted(p for p in found if not any(p.startswith(q + "/") for q in found))


def logical_ids(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (p, obj["value"]["namespace"], obj["value"]["value"])
        for p, obj in _walk(record)
        if obj.get("knowledge") == "known"
        and isinstance(obj.get("value"), dict)
        and obj["value"].keys() == {"namespace", "value"}
    ]


def _known_time(record: dict[str, Any], field: str | None) -> tuple[str, int] | None:
    node = record.get(field) if field else None
    if isinstance(node, dict) and node.get("knowledge") == "known":
        return node["value"]["domain_id"], node["value"]["ticks"]
    return None


def world_time(kind: str, record: dict[str, Any]) -> tuple[str | None, int | None, int | None]:
    """(clock, s, e) by ADR 0003 §3: e is NULL (open) if not Known or on another clock than s."""
    if kind not in WORLD_TIME:
        return None, None, None
    start_field, end_field, fallback = WORLD_TIME[kind]
    start, end = _known_time(record, start_field), _known_time(record, end_field)
    if start is None and (performed := _known_time(record, fallback)) is not None:
        start = performed
        end = end or performed
    if start is None:
        start = end  # a point at the end
    if start is None:
        return None, None, None
    open_end = end is None or end[0] != start[0]
    return start[0], start[1], None if open_end else end[1]  # type: ignore[index]


def provenance_summary(kind: str, record: dict[str, Any]) -> tuple[str | None, ...]:
    """(source content id, locator JSON, transform id, assertion kind), record-level provenance."""
    if kind == "ingest_finding":
        subject = record["subject"]
        if subject["kind"] != "evidence":
            return None, None, record["transform"], None
        ref = subject["ref"]
        return ref["source"], canonical(ref["locator"]), record["transform"], None
    provenance = record.get("provenance")
    if provenance is None:  # source_artifact, source_revision, source_absence, transform_record
        return None, None, None, None
    evidence = provenance["evidence"]
    return (
        evidence["source"],
        canonical(evidence["locator"]),
        provenance["transform"],
        provenance["assertion_kind"],
    )


class CatalogConflict(Exception):
    """An id already in the catalog arrived with different fields (ADR 0002 §6)."""


def _insert_once(conn: Conn, table: str, keys: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Insert a row keyed by ``keys`` once; True if new. A stored row must agree on ``fields``."""
    columns = {**keys, **fields}
    inserted = conn.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
        " ON CONFLICT DO NOTHING RETURNING 1",
        tuple(columns.values()),
    ).fetchone()
    if inserted is not None:
        return True
    where = " AND ".join(f"{key} = %s" for key in keys)
    stored = conn.execute(
        f"SELECT {', '.join(fields)} FROM {table} WHERE {where}", tuple(keys.values())
    ).fetchone()
    if stored != tuple(fields.values()):
        raise CatalogConflict(f"{table} {keys} is stored as {stored}, arrived as {fields}")
    return False


def register(
    conn: Conn, schema: str, package: Package, logged: tuple[Any, ...] | None = None
) -> tuple[str, int, bool]:
    """Register ``package``; return (package id, tx_seq, newly registered). ADR 0002 §4, §6.

    Live, the tick comes from ``next_tx()``. On a rebuild, ``logged`` is the package's
    registration-log row ``(tx_seq, tx_time, package_id, root_locator, ledger_version)``, replayed
    through ``replay_tx`` so every column comes out as it was.
    """
    tenant = schema.removeprefix("tenant_")
    pid = package.package_id
    with conn.transaction():
        conn.execute(f"SET LOCAL search_path TO {schema}")
        conn.execute("SELECT 1 FROM tx_clock FOR UPDATE")  # serialise registrations
        existing = conn.execute("SELECT tx_seq FROM package WHERE package_id = %s", (pid,))
        row = existing.fetchone()
        if row is not None:
            return pid, int(str(row[0])), False
        if logged is None:
            tick = conn.execute("SELECT tx_seq, tx_time FROM next_tx()").fetchone()
            assert tick is not None
            tx_seq, tx_time = tick
            root, version = f"tests/golden/packages/{package.name}", neptune_ledger.__version__
        else:
            tx_seq, tx_time, logged_pid, root, version = logged
            assert logged_pid == pid
            conn.execute("SELECT replay_tx(%s, %s)", (tx_seq, tx_time))
        conn.execute(
            "INSERT INTO registration_log VALUES (%s, %s, %s, %s, %s, %s)",
            (tenant, tx_seq, tx_time, pid, root, version),
        )
        conn.execute(
            "INSERT INTO package (tenant_id, package_id, schema_version, receipt_id)"
            " VALUES (%s, %s, %s, %s)",
            (tenant, pid, package.manifest["schema_version"], package.manifest["receipt"]),
        )
        for source in package.manifest["sources"]:
            _insert_once(
                conn,
                "source",
                {"tenant_id": tenant, "content_id": source["content_id"]},
                {"size": source["size"]},
            )
            conn.execute(
                "INSERT INTO package_source VALUES (%s, %s, %s, %s)",
                (tenant, pid, source["content_id"], source["storage"]),
            )
        for revision in package.tables["source_revision"]:
            conn.execute(
                "INSERT INTO source_location VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    tenant,
                    revision["id"],
                    pid,
                    revision["content_id"],
                    canonical(revision["location"]),
                    revision["supersedes"],
                ),
            )
        for absence in package.tables["source_absence"]:
            conn.execute(
                "INSERT INTO location_absence VALUES (%s, %s, %s, %s, %s)",
                (tenant, absence["id"], pid, canonical(absence["location"]), absence["supersedes"]),
            )
        for transform in package.tables["transform_record"]:
            fields = {
                "adapter_id": transform["adapter_id"],
                "adapter_version": transform["adapter_version"],
                "config_hash": transform["config_hash"],
                "libraries": canonical(transform["libraries"]),
            }
            keys = {"tenant_id": tenant, "transform_id": transform["id"]}
            if _insert_once(conn, "transform", keys, fields):
                for upstream in transform["upstream"]:
                    conn.execute(
                        "INSERT INTO transform_upstream VALUES (%s, %s, %s)",
                        (tenant, transform["id"], upstream),
                    )
                continue
            stored = conn.execute(
                "SELECT upstream_id FROM transform_upstream WHERE transform_id = %s",
                (transform["id"],),
            ).fetchall()
            if sorted(str(edge[0]) for edge in stored) != sorted(transform["upstream"]):
                raise CatalogConflict(f"transform {transform['id']} arrived with other upstream")
        for domain in package.tables["timestamp_domain"]:
            conn.execute(
                "INSERT INTO clock VALUES (%s, %s, %s, %s, %s)",
                (tenant, domain["id"], pid, domain["field"], domain["scope"]),
            )
        for kind, records in package.tables.items():
            for line, record in enumerate(records, start=1):
                key = record["content_id"] if kind == "source_artifact" else record["id"]
                conn.execute(
                    f"INSERT INTO record ({RECORD_COLUMNS}) VALUES ({', '.join(['%s'] * 16)})",
                    (
                        tenant,
                        kind,
                        key,
                        pid,
                        tx_seq,
                        line,
                        record["schema_version"],
                        *provenance_summary(kind, record),
                        *world_time(kind, record),
                        ambiguous_pointers(record),
                        body_digest(record),
                    ),
                )
                for pointer, namespace, value in logical_ids(record):
                    conn.execute(
                        "INSERT INTO record_logical_id VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (tenant, kind, key, pid, pointer, namespace, value),
                    )
    return pid, int(str(tx_seq)), True


@pytest.fixture
def catalog(pg: Conn) -> Conn:
    apply_migrations(pg, "acme")
    return pg


def _count(conn: Conn, query: str, *params: object) -> int:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return int(str(row[0]))


def _cell(value: object) -> str:
    return "NULL" if value is None else str(value)


def _doc_table(heading: str) -> dict[str, list[str]]:
    """A markdown table under ``heading`` in the walkthrough, as {first cell: other cells}."""
    text = WALKTHROUGH.read_text(encoding="utf-8")
    section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    cells = [[c.strip().strip("`") for c in row.strip("|").split("|")] for row in rows[1:]]
    return {row[0]: row[1:] for row in cells}


def test_the_example_records_are_the_packages_tables() -> None:
    for name in EXAMPLES:
        package = load_package(name)
        counts = {kind: len(records) for kind, records in package.tables.items()}
        assert counts == package.manifest["tables"]
        assert package.manifest["receipt"] == package.receipt["id"]


def test_ambiguous_pointers_agree_with_the_receipt() -> None:
    for name in EXAMPLES:
        package = load_package(name)
        found = sorted(
            (record["id"], pointer)
            for records in package.tables.values()
            for record in records
            for pointer in ambiguous_pointers(record)
        )
        stated = sorted((a["record"], a["pointer"]) for a in package.receipt["ambiguous"])
        assert found == stated


def test_the_walkthrough_package_table_matches_the_catalog(catalog: Conn) -> None:
    expected = _doc_table("Packages")
    for seq, name in enumerate(EXAMPLES, start=1):
        package = load_package(name)
        pid, tx_seq, created = register(catalog, "tenant_acme", package)
        assert (tx_seq, created) == (seq, True)
        counts = ", ".join(
            f"(SELECT count(*) FROM tenant_acme.{table} x WHERE x.package_id = p.package_id)"
            for table in ("package_source", "source_location", "clock", "record")
        )
        row = catalog.execute(
            f"SELECT tx_seq, {counts} FROM tenant_acme.package p WHERE package_id = %s", (pid,)
        ).fetchone()
        assert row is not None
        assert [str(cell) for cell in row] == [expected[name][0], *expected[name][2:]]
        assert expected[name][1] == pid[:19] + "…"


def test_the_walkthrough_partition_table_matches_the_catalog(catalog: Conn) -> None:
    for name in EXAMPLES:
        register(catalog, "tenant_acme", load_package(name))
    expected = _doc_table("Record rows by partition")
    rows = catalog.execute(
        "SELECT p.tx_seq, r.kind, count(*) FROM tenant_acme.record r"
        " JOIN tenant_acme.package p USING (tenant_id, package_id) GROUP BY 1, 2"
    ).fetchall()
    actual: Counter[tuple[str, int]] = Counter()
    for seq, kind, count in rows:
        actual[(str(kind), int(str(seq)))] = int(str(count))
    for kind in RECORD_KINDS:
        cells = [str(actual[(kind, seq)]) for seq in range(1, len(EXAMPLES) + 1)]
        assert cells == expected.get(f"record_{kind}", ["0"] * len(EXAMPLES)), kind
    assert sum(actual.values()) == sum(int(c) for cells in expected.values() for c in cells)


def test_the_walkthrough_world_time_rows_match_the_catalog(catalog: Conn) -> None:
    for name in EXAMPLES:
        register(catalog, "tenant_acme", load_package(name))
    expected = _doc_table("World-time index")
    rows = catalog.execute(
        "SELECT p.tx_seq, r.kind, r.record_id, c.field, r.world_first, r.world_last"
        " FROM tenant_acme.record r JOIN tenant_acme.package p USING (tenant_id, package_id)"
        " LEFT JOIN tenant_acme.clock c"
        "   ON c.clock_id = r.world_clock AND c.package_id = r.package_id"
        " WHERE r.world_clock IS NOT NULL ORDER BY 1, 2, 3"
    ).fetchall()
    actual = {
        f"{seq}·{kind}·{str(rid)[11:19]}": [str(field), _cell(first), _cell(last)]
        for seq, kind, rid, field, first, last in rows
    }
    assert actual == expected


def test_the_walkthrough_logical_ids_match_the_catalog(catalog: Conn) -> None:
    for name in EXAMPLES:
        register(catalog, "tenant_acme", load_package(name))
    expected = _doc_table("Logical-id index")
    rows = catalog.execute(
        "SELECT p.tx_seq, l.kind, l.pointer, l.namespace, l.value"
        " FROM tenant_acme.record_logical_id l"
        " JOIN tenant_acme.package p USING (tenant_id, package_id) ORDER BY 1, 2, 3, 4, 5"
    ).fetchall()
    actual: Counter[str] = Counter()
    for seq, kind, pointer, namespace, value in rows:
        actual[f"{seq}·{kind}·{pointer}·{namespace}·{value}"] += 1
    assert {key: [str(n)] for key, n in actual.items()} == expected


def test_reregistering_an_identical_package_is_a_no_op(catalog: Conn) -> None:
    first = [register(catalog, "tenant_acme", load_package(name)) for name in EXAMPLES]
    snapshot = _snapshot(catalog, "tenant_acme")
    again = [register(catalog, "tenant_acme", load_package(name)) for name in EXAMPLES]
    assert [(pid, seq) for pid, seq, _ in again] == [(pid, seq) for pid, seq, _ in first]
    assert not any(created for _, _, created in again)
    assert _snapshot(catalog, "tenant_acme") == snapshot


def test_the_catalog_is_a_deterministic_function_of_the_packages(catalog: Conn) -> None:
    """Two tenants registering the same packages live in the same order hold identical rows,
    apart from the host times their clocks read."""
    apply_migrations(catalog, "replica")
    for name in EXAMPLES:
        register(catalog, "tenant_acme", load_package(name))
        register(catalog, "tenant_replica", load_package(name))
    assert _snapshot(catalog, "tenant_acme", tx=False) == _snapshot(
        catalog, "tenant_replica", tx=False
    )


def test_replaying_the_registration_log_rebuilds_the_catalog_byte_for_byte(catalog: Conn) -> None:
    """ADR 0002 §4: same Ledger version + packages + registration log = the same catalog,
    transaction times included."""
    for name in EXAMPLES:
        register(catalog, "tenant_acme", load_package(name))
    log = catalog.execute(
        "SELECT tx_seq, tx_time, package_id, root_locator, ledger_version"
        " FROM tenant_acme.registration_log ORDER BY tx_seq"
    ).fetchall()
    packages = {load_package(name).package_id: load_package(name) for name in EXAMPLES}
    apply_migrations(catalog, "rebuilt")
    for entry in log:
        register(catalog, "tenant_rebuilt", packages[str(entry[2])], logged=entry)
    original, rebuilt = _snapshot(catalog, "tenant_acme"), _snapshot(catalog, "tenant_rebuilt")
    assert set(original) == set(rebuilt)
    assert {"registration_log", "package", "record", "tx_clock"} <= set(original)
    for table, rows in original.items():
        assert rows == rebuilt[table], table
    assert sum(len(rows) for rows in original.values()) > 100


def _altered(package: Package, manifest: dict[str, Any], tables: Any = None) -> Package:
    """``package`` with another manifest (so another package id) and optionally other tables."""
    data = canonical(manifest).encode()
    return Package(package.name, data, manifest, package.receipt, tables or package.tables)


def test_an_existing_source_with_another_size_is_refused(catalog: Conn) -> None:
    original = load_package("manipulator")
    register(catalog, "tenant_acme", original)
    manifest = json.loads(original.manifest_bytes)
    manifest["sources"][0]["size"] = 1
    conflicting = _altered(original, manifest)
    assert conflicting.package_id != original.package_id
    with pytest.raises(CatalogConflict, match="source"):
        register(catalog, "tenant_acme", conflicting)
    assert _count(catalog, "SELECT count(*) FROM tenant_acme.package") == 1
    assert _count(catalog, "SELECT last_seq FROM tenant_acme.tx_clock") == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("adapter_version", "9.9.9"),
        ("libraries", {"zstd": "1.0"}),
        ("upstream", ["rec:sha256:" + "f" * 64]),
    ],
)
def test_an_existing_transform_with_other_fields_is_refused(
    catalog: Conn, field: str, value: Any
) -> None:
    original = load_package("manipulator")
    register(catalog, "tenant_acme", original)
    tables = {kind: [dict(r) for r in records] for kind, records in original.tables.items()}
    tables["transform_record"][0][field] = value
    manifest = json.loads(original.manifest_bytes)
    for entry in manifest["files"]:  # the altered table would hash differently
        if entry["path"] == "records/transform_record.jsonl":
            entry["sha256"] = "sha256:" + "0" * 64
    conflicting = _altered(original, manifest, tables)
    assert conflicting.package_id != original.package_id
    with pytest.raises(CatalogConflict, match="transform"):
        register(catalog, "tenant_acme", conflicting)
    assert _count(catalog, "SELECT count(*) FROM tenant_acme.package") == 1


def _snapshot(conn: Conn, schema: str, *, tx: bool = True) -> dict[str, list[str]]:
    """Every row of every table in ``schema`` as PostgreSQL's text rendering, tenant_id aside."""
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND NOT c.relispartition",
            (schema,),
        ).fetchall()
    ]
    out: dict[str, list[str]] = {}
    for table in tables:
        columns = [
            str(row[0])
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s"
                " AND column_name <> 'tenant_id' ORDER BY ordinal_position",
                (schema, table),
            ).fetchall()
        ]
        if not tx:
            columns = [c for c in columns if c not in ("tx_time", "last_time")]
        rows = conn.execute(
            f"SELECT ROW({', '.join(columns)})::text FROM {schema}.{table} ORDER BY 1"
        ).fetchall()
        out[table] = [str(row[0]) for row in rows]
    return out


def test_a_body_digest_is_the_sha256_of_the_records_line() -> None:
    for name in EXAMPLES:
        for kind in RECORD_KINDS:
            path = RECORDS / name / "records" / f"{kind}.jsonl"
            lines = path.read_bytes().splitlines() if path.exists() else []
            records = load_package(name).tables[kind]
            assert [canonical(r).encode("utf-8") for r in records] == lines
            assert [body_digest(r) for r in records] == [
                "sha256:" + hashlib.sha256(line).hexdigest() for line in lines
            ]


def test_the_same_records_in_another_package_are_accepted(catalog: Conn) -> None:
    """A re-ingest after a referenced source moved: another package, the same record bodies."""
    original = load_package("mobile_robot")
    register(catalog, "tenant_acme", original)
    manifest = json.loads(original.manifest_bytes)
    manifest["store"] = {**manifest["store"], "moved": True}  # any change gives a new package id
    again = _altered(original, manifest)
    _, seq, created = register(catalog, "tenant_acme", again)
    assert created and seq == 2
    shared = _count(
        catalog,
        "SELECT count(*) FROM (SELECT kind, record_id FROM tenant_acme.record"
        " GROUP BY kind, record_id HAVING count(DISTINCT package_id) = 2) AS twice",
    )
    assert shared == sum(len(records) for records in original.tables.values())


def test_an_existing_record_id_with_another_body_is_refused(catalog: Conn) -> None:
    """ADR 0005 §2: a tier-2 id does not cover the body, so the catalog compares digests."""
    original = load_package("mobile_robot")
    register(catalog, "tenant_acme", original)
    tables = {kind: [dict(r) for r in records] for kind, records in original.tables.items()}
    run = tables["run"][0]
    run["last"] = {**run["last"], "value": {**run["last"]["value"], "ticks": 1}}
    manifest = json.loads(original.manifest_bytes)
    manifest["store"] = {**manifest["store"], "tampered": True}
    hostile = _altered(original, manifest, tables)
    with pytest.raises(psycopg.errors.RaiseException, match="catalogued with body"):
        register(catalog, "tenant_acme", hostile)
    assert _count(catalog, "SELECT count(*) FROM tenant_acme.package") == 1
