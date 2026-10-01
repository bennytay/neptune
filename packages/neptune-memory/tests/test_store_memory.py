"""MemoryStore seam: records, Protocol conformance, SQL text, the Neo4j stub, a live Postgres run.

Nothing here needs a database except ``test_postgres_store_end_to_end`` (``@slow``), which runs only
when ``NEPTUNE_MEMORY_PG_DSN`` names a PostgreSQL with pgvector + Apache AGE and psycopg imports.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from neptune_memory.store import (
    AsOf,
    ClaimEmbedding,
    ClaimRecord,
    MemoryStore,
    Neo4jStore,
    PostgresStore,
)
from neptune_memory.store import postgres as pg
from neptune_memory.store.bench.generator import DeploymentSpec, generate

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

AT = AsOf("fleet_utc", valid_at=100, known_at=200)


def claim(**kw: Any) -> ClaimRecord:
    base: dict[str, Any] = {
        "claim_id": 1,
        "subject": "robot:arm-001",
        "predicate": "operating_mode",
        "object_entity": None,
        "object_value": "idle",
        "valid_clock": "fleet_utc",
        "valid_from": 50,
        "valid_to": 150,
        "recorded_at": 60,
        "superseded_at": None,
        "assertion_kind": "observed",
        "source_id": "ev:log:arm-001:0000",
        "transform_id": "memory.consolidate.log@1.0.0",
    }
    return ClaimRecord(**(base | kw))


# --- records -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"object_entity": "site:01"},  # both objects
        {"object_value": None},  # neither
        {"valid_to": 50},  # empty interval
        {"superseded_at": 10},  # superseded before recorded
        {"assertion_kind": "guessed"},
    ],
)
def test_claim_record_rejects_malformed(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="claim 1"):
        claim(**bad)


@pytest.mark.parametrize(
    ("kw", "at", "visible"),
    [
        ({}, AT, True),
        ({"valid_from": 100}, AT, True),  # valid interval is closed at the start
        ({"valid_to": 100}, AT, False),  # ... and open at the end
        ({"valid_to": None}, AT, True),  # open-ended
        ({"recorded_at": 200}, AT, True),
        ({"recorded_at": 201}, AT, False),  # not yet known
        ({"superseded_at": 200}, AT, False),  # superseded exactly then
        ({"superseded_at": 201}, AT, True),
        ({}, AsOf("gps_time", 100, 200), False),  # another clock is never converted
    ],
)
def test_visible_is_half_open_on_both_axes(kw: dict[str, Any], at: AsOf, visible: bool) -> None:
    assert (
        claim(**kw).visible(valid_clock=at.valid_clock, valid_at=at.valid_at, known_at=at.known_at)
        is visible
    )


# --- Protocol conformance and the stub ------------------------------------------------------------


class RecordingCursor:
    def __init__(self, conn: RecordingConnection) -> None:
        self.conn = conn
        self.rowcount = conn.rowcount

    def execute(self, query: str, params: Mapping[str, Any] | None = None) -> None:
        self.conn.log.append((query, dict(params or {})))

    def executemany(self, query: str, params_seq: Iterable[Mapping[str, Any]]) -> None:
        for params in params_seq:
            self.execute(query, params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []

    def fetchone(self) -> tuple[Any, ...] | None:
        return None


class RecordingConnection:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_both_implementations_have_the_memory_store_shape() -> None:
    assert isinstance(PostgresStore(RecordingConnection()), MemoryStore)
    assert isinstance(Neo4jStore(), MemoryStore)


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("write_claims", ([],)),
        ("supersede", (1, claim())),
        ("as_of_thread", ("robot:arm-001", AT)),
        ("neighbours", ("site:01", 3, AT)),
        ("write_embeddings", ([],)),
        ("vector_top_k", ([0.0], 10)),
        ("rebuild", ()),
    ],
)
def test_neo4j_stub_refuses_every_operation_naming_the_adr(
    method: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(NotImplementedError, match="adr/0004"):
        getattr(Neo4jStore(), method)(*args)


# --- SQL text ------------------------------------------------------------------------------------


def _placeholders(sql: str) -> set[str]:
    return set(re.findall(r"%\((\w+)\)s", sql))


@pytest.mark.parametrize("schema", ["memory; DROP TABLE x", "Memory", "1abc", "", "a" * 64, 'a"b'])
def test_identifiers_are_validated_not_quoted(schema: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        pg.as_of_thread_sql(schema)


def test_age_graph_name_needs_three_characters() -> None:
    with pytest.raises(ValueError, match="three"):
        pg.age_projection_sql("memory", "gr")


def test_query_parameters_are_exactly_what_the_adapter_binds() -> None:
    at = set(pg.as_of_params(AT))
    assert _placeholders(pg.as_of_thread_sql("memory")) == {"subject"} | at
    assert _placeholders(pg.neighbours_sql("memory")) == {"start", "hops"} | at
    assert _placeholders(pg.vector_top_k_sql("memory", filtered=False)) == {"query", "k"}
    assert (
        _placeholders(pg.vector_top_k_sql("memory", filtered=True))
        == {"query", "k", "start", "hops"} | at
    )
    assert _placeholders(pg.insert_claim_sql("memory")) == set(pg.CLAIM_COLUMNS)
    assert _placeholders(pg.close_claim_sql("memory")) == {"old", "at"}


def test_thread_sql_selects_columns_in_record_order() -> None:
    sql = pg.as_of_thread_sql("memory")
    select = sql.split("SELECT x.", 1)[1].split("\nFROM", 1)[0]
    assert [c.strip().removeprefix("x.") for c in select.split(",")] == list(pg.CLAIM_COLUMNS)
    assert tuple(f.name for f in ClaimRecord.__dataclass_fields__.values()) == pg.CLAIM_COLUMNS


def test_ddl_is_schema_qualified_and_rejects_bad_dimensions() -> None:
    for stmt in [*pg.ddl("mem_x", 4), *pg.index_ddl("mem_x")]:
        assert (
            "EXTENSION" in stmt or "mem_x." in stmt or "mem_x\n" in stmt or stmt.endswith("mem_x")
        )
    with pytest.raises(ValueError, match="dimensions"):
        pg.ddl("memory", 0)


@pytest.mark.parametrize(("vector", "text"), [([1.0, -0.5], "[1.0,-0.5]"), ([0.1], "[0.1]")])
def test_vector_literal_round_trips(vector: list[float], text: str) -> None:
    assert pg.vector_literal(vector) == text
    assert [float(x) for x in text.strip("[]").split(",")] == vector


def test_vector_literal_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        pg.vector_literal([])


# --- adapter behaviour against a recording connection ---------------------------------------------


def test_supersede_is_one_transaction_close_then_append() -> None:
    conn = RecordingConnection(rowcount=1)
    new = claim(claim_id=2, recorded_at=300, supersedes=1)
    PostgresStore(conn).supersede(1, new)
    assert [q.split()[0] for q, _ in conn.log] == ["UPDATE", "INSERT"]
    assert conn.log[0][1] == {"old": 1, "at": 300}
    assert (conn.commits, conn.rollbacks) == (1, 0)


def test_supersede_rejects_a_claim_that_names_another_and_a_missing_old_claim() -> None:
    conn = RecordingConnection(rowcount=0)
    store = PostgresStore(conn)
    with pytest.raises(ValueError, match="does not name"):
        store.supersede(1, claim(claim_id=2, supersedes=9))
    with pytest.raises(LookupError, match="already superseded"):
        store.supersede(1, claim(claim_id=2, supersedes=1))
    assert (conn.commits, conn.rollbacks) == (0, 1)
    assert not any(q.startswith("INSERT") for q, _ in conn.log)


def test_adapter_bounds_are_checked_before_any_query() -> None:
    conn = RecordingConnection()
    store = PostgresStore(conn, dimensions=2)
    with pytest.raises(ValueError, match="hops"):
        store.neighbours("site:01", 0, AT)
    with pytest.raises(ValueError, match="hops"):
        store.vector_top_k([0.0, 1.0], 5, within=("site:01", pg.MAX_HOPS + 1, AT))
    with pytest.raises(ValueError, match="k must"):
        store.vector_top_k([0.0, 1.0], 0)
    with pytest.raises(ValueError, match="dimensions"):
        store.write_embeddings([ClaimEmbedding(1, "robot:arm-001", (0.0,))])
    assert conn.log == []


def test_rebuild_drops_and_recreates_indexes_and_the_graph() -> None:
    conn = RecordingConnection()
    PostgresStore(conn, graph="claimgraph").rebuild()
    text = [q for q, _ in conn.log]
    assert text[: len(pg.drop_index_ddl("memory"))] == pg.drop_index_ddl("memory")
    assert any("create_graph('claimgraph')" in q for q in text)
    conn = RecordingConnection()
    PostgresStore(conn, graph=None).rebuild()
    assert not any("ag_catalog" in q for q, _ in conn.log)


# --- live engine ---------------------------------------------------------------------------------


@pytest.mark.slow
def test_postgres_store_end_to_end() -> None:
    dsn = os.environ.get("NEPTUNE_MEMORY_PG_DSN")
    if not dsn:
        pytest.skip("NEPTUNE_MEMORY_PG_DSN not set (no PostgreSQL with pgvector + AGE)")
    psycopg = pytest.importorskip("psycopg")
    claims = list(generate(DeploymentSpec(claims=3_000, robots=12, sites=3)))
    with psycopg.connect(dsn) as conn:
        conn.execute("DROP SCHEMA IF EXISTS memory_test CASCADE")
        conn.commit()
        store = PostgresStore(conn, schema="memory_test", dimensions=3, graph="memory_test_graph")
        store.create()
        assert store.write_claims(claims) == len(claims)
        store.write_embeddings(
            ClaimEmbedding(c.claim_id, c.subject, (c.claim_id % 7, 1.0, 0.5)) for c in claims[:500]
        )
        store.rebuild()
        robot = claims[0].subject
        span = DeploymentSpec(claims=1).span
        for valid_at in range(span // 7, span, span // 7):
            at = AsOf("fleet_utc", valid_at, valid_at + 10**15)
            want = sorted(
                c.claim_id
                for c in claims
                if c.subject == robot
                and c.visible(
                    valid_clock=at.valid_clock, valid_at=at.valid_at, known_at=at.known_at
                )
            )
            assert sorted(c.claim_id for c in store.as_of_thread(robot, at)) == want
        at = AsOf("fleet_utc", span // 2, span * 2)
        hood = store.neighbours("site:00", 3, at)
        assert hood and all(1 <= n.depth <= 3 and len(n.via) == n.depth for n in hood)
        current = next(
            c
            for c in claims
            if c.subject == robot and c.superseded_at is None and c.object_value is not None
        )
        fix = replace(
            current,
            claim_id=10**9,
            recorded_at=span * 3,
            object_value="corrected",
            supersedes=current.claim_id,
        )
        store.supersede(current.claim_id, fix)
        with pytest.raises(LookupError):
            store.supersede(current.claim_id, replace(fix, claim_id=10**9 + 1))
        hits = store.vector_top_k([0.0, 1.0, 0.5], 5)
        assert len(hits) == 5 and hits[0].distance == 0.0
        conn.execute("DROP SCHEMA memory_test CASCADE")
        conn.execute("SELECT ag_catalog.drop_graph('memory_test_graph', true)")
        conn.commit()


def test_failed_bulk_write_rolls_back_so_the_connection_stays_usable() -> None:
    class Failing(RecordingConnection):
        def cursor(self) -> RecordingCursor:
            cur = RecordingCursor(self)

            def boom(query: str, params_seq: Iterable[Mapping[str, Any]]) -> None:
                raise RuntimeError("duplicate key")

            cur.executemany = boom  # type: ignore[method-assign]
            return cur

    conn = Failing()
    with pytest.raises(RuntimeError, match="duplicate"):
        PostgresStore(conn).write_claims([claim()])
    assert (conn.commits, conn.rollbacks) == (0, 1)
