"""MemoryStore seam: records, Protocol conformance, SQL text, the Neo4j stub, a live Postgres run.

Nothing here needs a database except the ``@slow`` live tests, which run only when
``NEPTUNE_MEMORY_PG_DSN`` names a PostgreSQL with pgvector and psycopg imports. Apache AGE is
optional (ADR 0004): only the snapshot check needs it, and it skips without it.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from memory_pg_live import has_age, open_live
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
from neptune_memory.store.records import CLAIM_COLUMNS

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

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


class _Info:
    def __init__(self) -> None:
        self.transaction_status = 0  # psycopg TransactionStatus.IDLE


class RecordingConnection:
    autocommit = False

    def __init__(self, rowcount: int = 1) -> None:
        self.info = _Info()
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
        == {"query", "k", "start", "hops", "exact_limit"} | at
    )
    assert _placeholders(pg.insert_claim_sql("memory")) == set(CLAIM_COLUMNS)
    assert _placeholders(pg.close_claim_sql("memory")) == {"old", "at"}


def test_thread_sql_selects_columns_in_record_order() -> None:
    sql = pg.as_of_thread_sql("memory")
    select = sql.split("SELECT ", 1)[1].split("\nFROM", 1)[0]
    assert [c.strip().removeprefix("c.") for c in select.split(",")] == list(CLAIM_COLUMNS)
    assert tuple(f.name for f in ClaimRecord.__dataclass_fields__.values()) == CLAIM_COLUMNS


def test_thread_sql_returns_every_visible_claim_not_one_per_predicate() -> None:
    """Corroborating claims (same object, different evidence) are current together (ADR 0002);
    the thread is a containment query on both intervals with no per-predicate limit."""
    sql = pg.as_of_thread_sql("memory")
    assert "LIMIT" not in sql.upper()
    assert "int8range(c.valid_from, c.valid_to, '[)') @> %(valid_at)s" in sql
    assert "int8range(c.recorded_at, c.superseded_at, '[)')\n      @> %(known_at)s" in sql
    # ...and it matches the GiST index expression exactly, so the index is usable.
    gist = next(x for x in pg.index_ddl("memory") if "gist" in x)
    assert pg.VALID_RANGE in gist and pg.TX_RANGE in gist
    a = claim(claim_id=1, valid_from=50, valid_to=150, source_id="ev:log:a")
    b = claim(claim_id=2, valid_from=80, valid_to=None, source_id="ev:log:b")
    assert all(c.visible(valid_clock="fleet_utc", valid_at=100, known_at=200) for c in (a, b))


def test_walk_expands_each_entity_once() -> None:
    """A visited set bounds the walk by entities, not by paths through hubs or cycles."""
    sql = pg.neighbours_sql("memory")
    assert "NOT jsonb_exists(b.seen, e.other)" in sql  # keyed lookup, not a scan (MVL-106)
    assert "b.seen || n.added" in sql
    assert "DISTINCT ON (e.other)" in sql
    assert "<> ALL" not in sql


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
    with pytest.raises(ValueError, match="ef_search"):
        PostgresStore(conn, ef_search=0)
    with pytest.raises(ValueError, match="exact_scope_limit"):
        PostgresStore(conn, exact_scope_limit=-1)
    assert conn.log == []


def test_autocommit_connections_are_refused() -> None:
    conn = RecordingConnection()
    conn.autocommit = True  # SET LOCAL and the store's transactions would be no-ops
    with pytest.raises(ValueError, match="autocommit"):
        PostgresStore(conn)


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.as_of_thread("robot:arm-001", AT),
        lambda s: s.neighbours("site:01", 2, AT),
        lambda s: s.vector_top_k([0.0, 1.0], 3),
        lambda s: s.write_claims([claim()]),
        lambda s: s.supersede(1, claim(claim_id=2, supersedes=1)),
    ],
    ids=["thread", "neighbours", "vector", "write", "supersede"],
)
def test_every_call_refuses_a_transaction_the_caller_left_open(call: Any) -> None:
    conn = RecordingConnection()
    conn.info.transaction_status = 2  # INTRANS: the caller has uncommitted work
    with pytest.raises(RuntimeError, match="idle connection"):
        call(PostgresStore(conn, dimensions=2))
    assert (conn.log, conn.commits, conn.rollbacks) == ([], 0, 0)


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.as_of_thread("robot:arm-001", AT),
        lambda s: s.neighbours("site:01", 2, AT),
        lambda s: s.vector_top_k([0.0, 1.0], 3, within=("site:01", 2, AT)),
    ],
    ids=["thread", "neighbours", "vector"],
)
def test_reads_always_end_their_transaction_and_never_commit(call: Any) -> None:
    conn = RecordingConnection()
    call(PostgresStore(conn, dimensions=2))
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_failed_read_rolls_back() -> None:
    class Failing(RecordingConnection):
        def cursor(self) -> RecordingCursor:
            cur = RecordingCursor(self)

            def boom(query: str, params: Mapping[str, Any] | None = None) -> None:
                raise RuntimeError("canceling statement")

            cur.execute = boom  # type: ignore[method-assign]
            return cur

    conn = Failing()
    with pytest.raises(RuntimeError, match="canceling"):
        PostgresStore(conn).as_of_thread("robot:arm-001", AT)
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_graph_filtered_search_is_exact_for_a_small_scope_and_hnsw_for_a_wide_one() -> None:
    """A filtered HNSW scan misses most of a small scope (ADR 0007 §7), so a scope of at most
    ``exact_limit`` embeddings is scanned exactly, in a materialized CTE the vector index cannot
    serve; a wider one uses the index. Both branches are gated on one count, so one runs."""
    sql = pg.vector_top_k_sql("memory", filtered=True)
    assert "size AS MATERIALIZED" in sql and "candidates AS MATERIALIZED" in sql
    assert "WHERE (SELECT n FROM size) <= %(exact_limit)s" in sql
    assert "WHERE (SELECT n FROM size) > %(exact_limit)s" in sql
    exact = sql.split("candidates AS MATERIALIZED", 1)[1].split("UNION ALL", 1)[0]
    assert "ORDER BY e.embedding" not in exact  # only the wide branch orders by the index
    assert "CROSS JOIN LATERAL" in exact and "WHERE e.subject = scope.entity OFFSET 0" in exact
    assert sql.rstrip().endswith(") hits ORDER BY distance, claim_id")
    conn = RecordingConnection()
    PostgresStore(conn, dimensions=2, exact_scope_limit=7).vector_top_k(
        [0.0, 1.0], 3, within=("site:01", 2, AT)
    )
    (params,) = [p for q, p in conn.log if not q.startswith("SET")]
    assert params["exact_limit"] == 7


@pytest.mark.parametrize("filtered", [False, True])
def test_vector_search_forces_custom_plans_in_its_own_transaction(filtered: bool) -> None:
    """A generic plan after psycopg's fifth execution would not be the plan G1 measured."""
    conn = RecordingConnection()
    within = ("site:01", 2, AT) if filtered else None
    PostgresStore(conn, dimensions=2).vector_top_k([0.0, 1.0], 3, within=within)
    statements = [q for q, _ in conn.log]
    assert "SET LOCAL plan_cache_mode = force_custom_plan" in statements
    assert statements.index("SET LOCAL plan_cache_mode = force_custom_plan") < len(statements) - 1
    assert (conn.commits, conn.rollbacks) == (0, 1)  # SET LOCAL ends with the read


def test_vector_search_sets_ef_search_locally_from_the_constructor() -> None:
    conn = RecordingConnection()
    PostgresStore(conn, dimensions=2, ef_search=250).vector_top_k([0.0, 1.0], 3)
    setup = [q for q, _ in conn.log if q.startswith("SET")]
    assert setup == [
        "SET LOCAL hnsw.ef_search = 250",
        "SET LOCAL hnsw.iterative_scan = relaxed_order",
        "SET LOCAL plan_cache_mode = force_custom_plan",
    ]
    conn = RecordingConnection()
    PostgresStore(conn, dimensions=2).vector_top_k([0.0, 1.0], 3)
    assert f"SET LOCAL hnsw.ef_search = {pg.DEFAULT_EF_SEARCH}" in [q for q, _ in conn.log]


def test_rebuild_drops_and_recreates_indexes_and_the_optional_graph() -> None:
    conn = RecordingConnection()
    PostgresStore(conn).rebuild()  # default: no AGE snapshot
    text = [q for q, _ in conn.log]
    assert text[: len(pg.drop_index_ddl("memory"))] == pg.drop_index_ddl("memory")
    assert not any("ag_catalog" in q for q in text)
    conn = RecordingConnection()
    PostgresStore(conn, graph="claimgraph").rebuild()
    assert any("create_graph('claimgraph')" in q for q, _ in conn.log)


def test_a_connection_that_cannot_report_its_transaction_status_is_refused() -> None:
    """Without a status the idle check would pass anything, and the store's commit could end the
    caller's transaction (MVL-106)."""

    class Silent(RecordingConnection):
        def __init__(self) -> None:
            super().__init__()
            del self.info

    with pytest.raises(TypeError, match="transaction status"):
        PostgresStore(Silent())
    conn = RecordingConnection()
    conn.info.transaction_status = None  # type: ignore[assignment]
    with pytest.raises(TypeError, match="transaction status"):
        PostgresStore(conn)


@pytest.mark.parametrize("kind", ["read", "write"])
def test_a_failing_rollback_never_hides_the_original_error(kind: str) -> None:
    class Broken(RecordingConnection):
        def cursor(self) -> RecordingCursor:
            cur = RecordingCursor(self)

            def boom(query: str, params: Any = None) -> None:
                raise RuntimeError("canceling statement due to statement timeout")

            cur.execute = boom  # type: ignore[method-assign]
            cur.executemany = boom  # type: ignore[method-assign,assignment]
            return cur

        def rollback(self) -> None:
            super().rollback()
            raise ConnectionError("server closed the connection unexpectedly")

    store = PostgresStore(Broken())
    with pytest.raises(RuntimeError, match="statement timeout") as caught:
        if kind == "read":
            store.as_of_thread("robot:arm-001", AT)
        else:
            store.write_claims([claim()])
    assert any("rollback also failed: ConnectionError" in n for n in caught.value.__notes__)


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


# --- live engine ---------------------------------------------------------------------------------

SPAN = DeploymentSpec(claims=1).span


def _visible(c: ClaimRecord, at: AsOf) -> bool:
    return c.visible(valid_clock=at.valid_clock, valid_at=at.valid_at, known_at=at.known_at)


def _bfs(claims: list[ClaimRecord], start: str, hops: int, at: AsOf) -> dict[str, int]:
    """Reference walk: shortest depth of every entity within ``hops`` over visible edges."""
    adj: dict[str, set[str]] = {}
    for c in claims:
        if c.object_entity is not None and _visible(c, at):
            adj.setdefault(c.subject, set()).add(c.object_entity)
            adj.setdefault(c.object_entity, set()).add(c.subject)
    depth = {start: 0}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if depth[node] == hops:
            continue
        for nxt in sorted(adj.get(node, ())):
            if nxt not in depth:
                depth[nxt] = depth[node] + 1
                queue.append(nxt)
    del depth[start]
    return depth


def _hub(first_id: int) -> list[ClaimRecord]:
    """A dense hub with cycles: 40 robots at one site, each mounting 3 components that are all
    calibrated by one shared rig, which is itself located at the site."""
    out: list[ClaimRecord] = []
    ids = iter(range(first_id, first_id + 10_000))

    def edge(subject: str, predicate: str, obj: str) -> None:
        out.append(
            claim(
                claim_id=next(ids),
                subject=subject,
                predicate=predicate,
                object_entity=obj,
                object_value=None,
                valid_from=0,
                valid_to=None,
                recorded_at=0,
                assertion_kind="stated",
                source_id="ev:site:hub",
            )
        )

    edge("calibration:rig", "located_at", "site:hub")
    for r in range(40):
        robot = f"robot:hub-{r:02d}"
        edge(robot, "located_at", "site:hub")
        for slot in range(3):
            component = f"component:hub-{r:02d}/s{slot}"
            edge(robot, f"mounts/s{slot}", component)
            edge(component, "calibrated_by", "calibration:rig")
    return out


@pytest.fixture
def live() -> Iterator[tuple[Any, PostgresStore]]:
    yield from open_live()


@pytest.mark.slow
def test_live_thread_neighbours_and_vectors_match_the_reference(
    live: tuple[Any, PostgresStore],
) -> None:
    conn, store = live
    claims = list(generate(DeploymentSpec(claims=3_000, robots=12, sites=3)))
    claims += _hub(len(claims) + 1)
    assert store.write_claims(claims) == len(claims)
    vectors = [
        ClaimEmbedding(c.claim_id, c.subject, (float(c.claim_id % 97), float(c.claim_id % 13), 0.5))
        for c in claims[::3]
    ]
    store.write_embeddings(vectors)
    store.rebuild()
    robots = sorted({c.subject for c in claims if c.subject.startswith("robot:")})
    for i, valid_at in enumerate(range(SPAN // 7, SPAN, SPAN // 7)):
        at = AsOf("fleet_utc", valid_at, valid_at + (10**15 if i % 2 else 2 * SPAN))
        for robot in robots[:: max(1, len(robots) // 5)]:
            want = sorted(c.claim_id for c in claims if c.subject == robot and _visible(c, at))
            assert sorted(c.claim_id for c in store.as_of_thread(robot, at)) == want
        for start in ("site:00", "site:hub"):
            got = store.neighbours(start, 3, at)
            assert {n.entity: n.depth for n in got} == _bfs(claims, start, 3, at)
            assert len({n.entity for n in got}) == len(got)  # each entity once
            assert all(len(n.via) == n.depth for n in got)
    # The hub: 40 robots, 120 components and the rig are each reached once, by depth 2.
    hub = {n.entity: n.depth for n in store.neighbours("site:hub", 6, AsOf("fleet_utc", 1, 1))}
    assert len(hub) == 1 + 40 + 120 and max(hub.values()) == 2
    # Filtered top-k equals an exact search over the walk's scope.
    at = AsOf("fleet_utc", SPAN // 2, 2 * SPAN)
    scope = {"site:00", *_bfs(claims, "site:00", 2, at)}
    query = (5.0, 3.0, 0.5)

    def dist(e: ClaimEmbedding) -> float:
        return float(sum((a - b) ** 2 for a, b in zip(e.vector, query, strict=True)) ** 0.5)

    exact = sorted((e for e in vectors if e.subject in scope), key=lambda e: (dist(e), e.claim_id))
    nearest = [round(dist(e), 4) for e in exact[:5]]
    hits = store.vector_top_k(query, 5, within=("site:00", 2, at))
    assert [round(h.distance, 4) for h in hits] == nearest
    # The wide-scope branch (the HNSW index, filtered) on the same scope: a small index is exact.
    wide = PostgresStore(conn, schema="memory_test", dimensions=3, exact_scope_limit=0)
    assert [
        round(h.distance, 4) for h in wide.vector_top_k(query, 5, within=("site:00", 2, at))
    ] == nearest
    assert conn.info.transaction_status == 0  # no read left a transaction open


@pytest.mark.slow
def test_live_corroboration_and_supersede_across_known_at(live: tuple[Any, PostgresStore]) -> None:
    conn, store = live
    a = claim(claim_id=1, valid_from=50, valid_to=150, recorded_at=60, source_id="ev:log:a")
    b = claim(claim_id=2, valid_from=80, valid_to=None, recorded_at=70, source_id="ev:log:b")
    other = claim(claim_id=3, predicate="health_status", object_value="ok", valid_to=None)
    store.write_claims([a, b, other])
    # Two corroborating claims on one predicate are both current.
    assert [c.claim_id for c in store.as_of_thread(a.subject, AsOf("fleet_utc", 100, 200))] == [
        3,
        1,
        2,
    ]
    fix = replace(other, claim_id=4, object_value="degraded", recorded_at=300, supersedes=3)
    store.supersede(3, fix)
    before = AsOf("fleet_utc", 100, 299)
    after = AsOf("fleet_utc", 100, 300)
    assert {c.claim_id for c in store.as_of_thread(a.subject, before)} == {1, 2, 3}
    assert {c.claim_id for c in store.as_of_thread(a.subject, after)} == {1, 2, 4}
    with pytest.raises(LookupError):
        store.supersede(3, replace(fix, claim_id=5))
    assert conn.info.transaction_status == 0


@pytest.mark.slow
def test_live_repeated_filtered_search_keeps_its_answer_past_auto_prepare(
    live: tuple[Any, PostgresStore],
) -> None:
    """psycopg prepares a statement after five executions; ten identical filtered searches on one
    connection must return the same hits, with custom plans, and leave the connection idle."""
    conn, store = live
    claims = list(generate(DeploymentSpec(claims=2_000, robots=8, sites=2)))
    store.write_claims(claims)
    store.write_embeddings(
        ClaimEmbedding(c.claim_id, c.subject, (float(c.claim_id % 31), float(c.claim_id % 7), 1.0))
        for c in claims[::2]
    )
    store.rebuild()
    at = AsOf("fleet_utc", SPAN // 2, 2 * SPAN)
    answers = [store.vector_top_k((3.0, 2.0, 1.0), 5, within=("site:00", 2, at)) for _ in range(10)]
    assert answers[0] and all(answer == answers[0] for answer in answers)
    assert conn.info.transaction_status == 0
    mode = conn.execute("SHOW plan_cache_mode").fetchone()[0]
    conn.rollback()
    assert mode == "auto"  # SET LOCAL did not leak out of the read's transaction


@pytest.mark.slow
def test_live_age_snapshot_is_a_rebuild_time_copy_of_the_edges(
    live: tuple[Any, PostgresStore],
) -> None:
    conn, store = live
    if not has_age(conn):
        pytest.skip("Apache AGE not installed: the optional snapshot is not checked")
    snap = PostgresStore(conn, schema="memory_test", dimensions=3, graph="memory_test_graph")
    store.write_claims([claim(claim_id=6, object_value=None, object_entity="site:01")])
    snap.rebuild()
    edges = conn.execute('SELECT count(*) FROM memory_test_graph."CLAIM"').fetchone()[0]
    conn.rollback()
    assert edges == 1
