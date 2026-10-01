"""PostgreSQL 16 + pgvector implementation of :class:`MemoryStore` (ADR 0004).

The store of record is two tables, ``claim`` and ``claim_embedding``, queried with SQL. Everything
else is derived and rebuilt by :meth:`PostgresStore.rebuild`: the bi-temporal GiST index, the edge
indexes and the HNSW index. An Apache AGE graph snapshot can optionally be built too
(``graph=...``). That snapshot is refreshed only by ``rebuild()``. It is not transactional with
writes, it goes stale at the first write, and nothing on the ``MemoryStore`` seam reads it.

The driver is not a dependency. Pass a DB-API 2.0 connection that uses ``%(name)s`` parameters
and is not in autocommit mode (psycopg 3's default), and that reports its transaction status as
``info.transaction_status`` (psycopg 3; psycopg2 >= 2.8). The store owns transaction boundaries:
every call needs an idle connection, runs in its own transaction, and commits (writes) or rolls
back (reads, errors) before it returns; a rollback that fails after an error never hides that
error. Every SQL text comes from a pure function, testable without a database.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final, Protocol

from neptune_memory.store.records import (
    CLAIM_COLUMNS,
    AsOf,
    ClaimEmbedding,
    ClaimRecord,
    Neighbour,
    VectorHit,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")
MAX_HOPS: Final = 6
#: Unfiltered HNSW search only (ADR 0007 §7: recall@10 0.93 on 200 queries at 10^6 embeddings);
#: pgvector's own default is 40. Graph-filtered search is exact and never uses the index.
DEFAULT_EF_SEARCH: Final = 400
#: Largest graph scope, in embeddings, searched exactly (ADR 0007 §7): 25k took 8 ms at 10^7
#: claims and cost is linear, so this bounds the exact branch near 160 ms, and the same 2-hop
#: scopes at 10^8 claims (about 250k) still take it.
DEFAULT_EXACT_SCOPE_LIMIT: Final = 500_000
VALID_RANGE: Final = "int8range(valid_from, valid_to, '[)')"
TX_RANGE: Final = "int8range(recorded_at, superseded_at, '[)')"


class Cursor(Protocol):
    def execute(self, query: str, params: Mapping[str, Any] | None = None) -> Any: ...
    def executemany(self, query: str, params_seq: Iterable[Mapping[str, Any]]) -> Any: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    @property
    def rowcount(self) -> int: ...


class ConnectionInfo(Protocol):
    @property
    def transaction_status(self) -> int: ...


class Connection(Protocol):
    """What the store needs: psycopg 3 (and psycopg2 >= 2.8) connections have all of it."""

    @property
    def info(self) -> ConnectionInfo: ...
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


def _ident(name: str) -> str:
    if not _IDENT.fullmatch(name):
        raise ValueError(f"not a safe SQL identifier: {name!r}")
    return name


# --- SQL text ------------------------------------------------------------------------------------


def ddl(schema: str, dimensions: int) -> list[str]:
    """Statements creating the source-of-truth tables (idempotent)."""
    s = _ident(schema)
    if not 1 <= dimensions <= 16_000:
        raise ValueError("dimensions must be in [1, 16000]")
    return [
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS btree_gist",
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"""CREATE TABLE IF NOT EXISTS {s}.claim (
  claim_id bigint PRIMARY KEY,
  subject text NOT NULL,
  predicate text NOT NULL,
  object_entity text,
  object_value text,
  valid_clock text NOT NULL,
  valid_from bigint NOT NULL,
  valid_to bigint,
  recorded_at bigint NOT NULL,
  superseded_at bigint,
  assertion_kind text NOT NULL CHECK (assertion_kind IN ('observed', 'stated', 'inferred')),
  source_id text NOT NULL,
  transform_id text NOT NULL,
  supersedes bigint,
  CHECK ((object_entity IS NULL) <> (object_value IS NULL)),
  CHECK (valid_to IS NULL OR valid_to > valid_from),
  CHECK (superseded_at IS NULL OR superseded_at >= recorded_at)
)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.claim_embedding (
  claim_id bigint PRIMARY KEY,
  subject text NOT NULL,
  embedding vector({dimensions}) NOT NULL
)""",
    ]


def index_ddl(schema: str) -> list[str]:
    """Derived structures over the tables; ``rebuild`` drops and recreates exactly these."""
    s = _ident(schema)
    return [
        # As-of thread: subject plus both intervals; containment finds every visible claim.
        f"CREATE INDEX IF NOT EXISTS claim_bitemporal_ix ON {s}.claim USING gist "
        f"(subject, {VALID_RANGE}, {TX_RANGE})",
        # Traversal: entity-valued claims are edges, walked in both directions.
        f"CREATE INDEX IF NOT EXISTS claim_edge_out_ix ON {s}.claim (subject) "
        "WHERE object_entity IS NOT NULL",
        f"CREATE INDEX IF NOT EXISTS claim_edge_in_ix ON {s}.claim (object_entity) "
        "WHERE object_entity IS NOT NULL",
        f"CREATE INDEX IF NOT EXISTS claim_embedding_subject_ix ON {s}.claim_embedding (subject)",
        f"CREATE INDEX IF NOT EXISTS claim_embedding_hnsw_ix ON {s}.claim_embedding "
        "USING hnsw (embedding vector_l2_ops)",
        f"ANALYZE {s}.claim",
        f"ANALYZE {s}.claim_embedding",
    ]


def drop_index_ddl(schema: str) -> list[str]:
    s = _ident(schema)
    names = (
        "claim_thread_ix",  # pre-review B-tree; dropped so old schemas rebuild cleanly
        "claim_bitemporal_ix",
        "claim_edge_out_ix",
        "claim_edge_in_ix",
        "claim_embedding_subject_ix",
        "claim_embedding_hnsw_ix",
    )
    return [f"DROP INDEX IF EXISTS {s}.{n}" for n in names]


def age_projection_sql(schema: str, graph: str) -> list[str]:
    """(Re)build the AGE graph from the claim table: ``Entity`` vertices, one ``CLAIM`` edge per
    entity-valued claim carrying its id and both time intervals. Requires ``age`` in
    ``shared_preload_libraries``. ``{schema}.age_entity`` maps entity ids to graph ids."""
    s, g = _ident(schema), _ident(graph)
    if len(g) < 3:
        raise ValueError("AGE graph names need at least three characters")
    label = (
        "(SELECT l.id FROM ag_catalog.ag_label l JOIN ag_catalog.ag_graph gr "
        f"ON l.graph = gr.graphid WHERE gr.name = '{g}' AND l.name = '{{}}')"
    )
    edges = f"""INSERT INTO {g}."CLAIM" (id, start_id, end_id, properties)
SELECT ag_catalog._graphid({label.format("CLAIM")}, c.claim_id), a.gid, b.gid,
  ag_catalog.agtype_build_map('claim_id', c.claim_id, 'predicate', c.predicate,
    'valid_clock', c.valid_clock, 'valid_from', c.valid_from, 'valid_to', c.valid_to,
    'recorded_at', c.recorded_at, 'superseded_at', c.superseded_at)
FROM {s}.claim c JOIN {s}.age_entity a ON a.eid = c.subject
JOIN {s}.age_entity b ON b.eid = c.object_entity
WHERE c.object_entity IS NOT NULL"""
    return [
        "CREATE EXTENSION IF NOT EXISTS age",
        f"SELECT ag_catalog.drop_graph(name, true) FROM ag_catalog.ag_graph WHERE name = '{g}'",
        f"SELECT ag_catalog.create_graph('{g}')",
        f"SELECT ag_catalog.create_vlabel('{g}', 'Entity')",
        f"SELECT ag_catalog.create_elabel('{g}', 'CLAIM')",
        f"DROP TABLE IF EXISTS {s}.age_entity",
        f"""CREATE TABLE {s}.age_entity AS
SELECT x.eid,
  ag_catalog._graphid({label.format("Entity")}, row_number() OVER (ORDER BY x.eid)) AS gid
FROM (SELECT subject AS eid FROM {s}.claim WHERE object_entity IS NOT NULL
      UNION SELECT object_entity FROM {s}.claim WHERE object_entity IS NOT NULL) x""",
        f"ALTER TABLE {s}.age_entity ADD PRIMARY KEY (eid)",
        f"""INSERT INTO {g}."Entity" (id, properties)
SELECT gid, ag_catalog.agtype_build_map('eid', eid) FROM {s}.age_entity""",
        edges,
        f'CREATE INDEX ON {g}."CLAIM" (start_id)',
        f'CREATE INDEX ON {g}."CLAIM" (end_id)',
        f'CREATE UNIQUE INDEX ON {g}."Entity" (id)',
        f'CREATE INDEX ON {g}."Entity" USING gin (properties)',
        f'ANALYZE {g}."CLAIM"',
        f'ANALYZE {g}."Entity"',
    ]


def _visible(alias: str) -> str:
    a = alias
    return (
        f"{a}.valid_clock = %(clock)s AND {a}.valid_from <= %(valid_at)s "
        f"AND ({a}.valid_to IS NULL OR {a}.valid_to > %(valid_at)s) "
        f"AND {a}.recorded_at <= %(known_at)s "
        f"AND ({a}.superseded_at IS NULL OR {a}.superseded_at > %(known_at)s)"
    )


def as_of_thread_sql(schema: str) -> str:
    """Every claim about one subject that is visible at (valid_at, known_at).

    Both intervals are int8 ranges (a NULL end is unbounded), and one GiST index over
    ``(subject, valid range, transaction range)`` answers containment directly. Nothing is assumed
    about overlap: corroborating claims and ``many``-cardinality predicates all come back.
    """
    s = _ident(schema)
    cols = ", ".join(f"c.{c}" for c in CLAIM_COLUMNS)
    return f"""SELECT {cols}
FROM {s}.claim c
WHERE c.subject = %(subject)s AND c.valid_clock = %(clock)s
  AND {VALID_RANGE.replace("valid_", "c.valid_")} @> %(valid_at)s::bigint
  AND {TX_RANGE.replace("recorded_at", "c.recorded_at").replace("superseded_at", "c.superseded_at")}
      @> %(known_at)s::bigint
ORDER BY c.predicate, c.valid_from, c.claim_id"""


def _walk_cte(schema: str) -> str:
    """Breadth-first walk that expands each entity once, so cost is bounded by the entities and
    edges reached, not by the number of paths through hubs or cycles.

    The visited set is a ``jsonb`` object keyed by entity (MVL-106): ``jsonb_exists`` is a keyed
    lookup, logarithmic in the entities seen, where the earlier ``text[]`` with ``<> ALL`` scanned
    every seen entity for every edge (quadratic around a hub). A level adds its entities with one
    ``||``. The lookup is a function call, not the ``?`` operator, so no driver mistakes it for a
    placeholder."""
    s = _ident(schema)
    return f"""WITH RECURSIVE bfs(depth, ents, paths, seen) AS (
  SELECT 0, ARRAY[%(start)s::text], ARRAY[''::text], jsonb_build_object(%(start)s::text, 0)
  UNION ALL
  SELECT b.depth + 1, n.ents, n.paths, b.seen || n.added
  FROM bfs b CROSS JOIN LATERAL (
    SELECT array_agg(x.other ORDER BY x.other) AS ents, array_agg(x.path ORDER BY x.other) AS paths,
      jsonb_object_agg(x.other, b.depth + 1) AS added
    FROM (
      SELECT DISTINCT ON (e.other) e.other,
        concat_ws(',', NULLIF(f.path, ''), e.claim_id::text) AS path
      FROM unnest(b.ents, b.paths) AS f(entity, path)
      CROSS JOIN LATERAL (
        SELECT c.object_entity AS other, c.claim_id FROM {s}.claim c
        WHERE c.subject = f.entity AND c.object_entity IS NOT NULL AND {_visible("c")}
        UNION ALL
        SELECT c.subject, c.claim_id FROM {s}.claim c
        WHERE c.object_entity = f.entity AND {_visible("c")}
      ) e
      WHERE NOT jsonb_exists(b.seen, e.other)
      ORDER BY e.other, e.claim_id
    ) x
  ) n
  WHERE b.depth < %(hops)s AND n.ents IS NOT NULL
)"""


def neighbours_sql(schema: str) -> str:
    """Entities within ``hops`` edges of ``start`` over edges visible at (valid_at, known_at), each
    once, at its shortest depth, with the claim ids of one shortest path (comma-separated)."""
    return (
        _walk_cte(schema)
        + """
SELECT u.entity, b.depth, u.path
FROM bfs b CROSS JOIN LATERAL unnest(b.ents, b.paths) AS u(entity, path)
WHERE b.depth > 0
ORDER BY u.entity"""
    )


def vector_top_k_sql(schema: str, *, filtered: bool) -> str:
    """Nearest neighbours by L2 distance, ties broken by claim id.

    Unfiltered: the HNSW index, re-sorted, since an iterative ``relaxed_order`` scan may return
    hits slightly out of order. Filtered (only subjects the walk reaches, the anchor included):
    an **exact** scan of the scope's embeddings through the subject index when the scope holds
    at most ``exact_limit`` of them, else the HNSW index filtered by the scope. A 2-hop scope is
    a tiny fraction of all embeddings, and a filtered HNSW scan exhausts its tuple budget before
    it finds them (recall@10 0.88, some queries 0), while the exact scan costs about the same
    (8 ms against 6). A scope above the fixed cutoff (many hops, a hub) keeps the filtered HNSW
    scan so that cost stays bounded; its recall is below budget wherever measured, a risk owned
    by MVL-132 (ADR 0007 §7). Both branches are gated on the scope
    size, computed once, so only one runs. The exact distances live in a materialized CTE the
    vector index cannot serve, and a ``LATERAL`` lookup per scope entity, fenced with
    ``OFFSET 0`` so it is not flattened into a join, keeps the subject index in the plan (a join
    was planned as a hash join over every embedding: 75 ms instead of 8).
    """
    s = _ident(schema)
    if not filtered:
        return f"""SELECT claim_id, distance FROM (
  SELECT claim_id, embedding <-> %(query)s::vector AS distance
  FROM {s}.claim_embedding ORDER BY embedding <-> %(query)s::vector LIMIT %(k)s
) hits ORDER BY distance, claim_id"""
    return (
        _walk_cte(schema)
        + f""", scope AS (
  SELECT u.entity FROM bfs b CROSS JOIN LATERAL unnest(b.ents) AS u(entity)
), size AS MATERIALIZED (
  SELECT count(*) AS n FROM scope CROSS JOIN LATERAL (
    SELECT 1 FROM {s}.claim_embedding e WHERE e.subject = scope.entity
  ) x
), candidates AS MATERIALIZED (
  SELECT x.claim_id, x.distance FROM scope CROSS JOIN LATERAL (
    SELECT e.claim_id, e.embedding <-> %(query)s::vector AS distance
    FROM {s}.claim_embedding e WHERE e.subject = scope.entity OFFSET 0
  ) x
  WHERE (SELECT n FROM size) <= %(exact_limit)s
)
SELECT claim_id, distance FROM (
  (SELECT claim_id, distance FROM candidates ORDER BY distance, claim_id LIMIT %(k)s)
  UNION ALL
  (SELECT e.claim_id, e.embedding <-> %(query)s::vector AS distance
   FROM {s}.claim_embedding e
   WHERE (SELECT n FROM size) > %(exact_limit)s AND e.subject IN (SELECT entity FROM scope)
   ORDER BY e.embedding <-> %(query)s::vector LIMIT %(k)s)
) hits ORDER BY distance, claim_id"""
    )


def insert_claim_sql(schema: str) -> str:
    s = _ident(schema)
    names = ", ".join(CLAIM_COLUMNS)
    values = ", ".join(f"%({c})s" for c in CLAIM_COLUMNS)
    return f"INSERT INTO {s}.claim ({names}) VALUES ({values})"


def close_claim_sql(schema: str) -> str:
    """Close the old claim's transaction interval; matches nothing if it is already closed."""
    s = _ident(schema)
    return (
        f"UPDATE {s}.claim SET superseded_at = %(at)s "
        "WHERE claim_id = %(old)s AND superseded_at IS NULL AND recorded_at <= %(at)s"
    )


def insert_embedding_sql(schema: str) -> str:
    s = _ident(schema)
    return (
        f"INSERT INTO {s}.claim_embedding (claim_id, subject, embedding) "
        "VALUES (%(claim_id)s, %(subject)s, %(vector)s::vector)"
    )


def as_of_params(at: AsOf) -> dict[str, Any]:
    return {"clock": at.valid_clock, "valid_at": at.valid_at, "known_at": at.known_at}


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form; ``repr`` round-trips every float exactly."""
    if not vector:
        raise ValueError("empty vector")
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _row_params(c: ClaimRecord) -> dict[str, Any]:
    return {col: getattr(c, col) for col in CLAIM_COLUMNS}


def _check_hops(hops: int) -> None:
    if not 1 <= hops <= MAX_HOPS:
        raise ValueError(f"hops must be in [1, {MAX_HOPS}]")


# --- adapter -------------------------------------------------------------------------------------


def _transaction_status(conn: object) -> int | None:
    """The driver's transaction status (0 = idle), or ``None`` if the connection reports none."""
    status = getattr(getattr(conn, "info", None), "transaction_status", None)
    if status is None or isinstance(status, bool):
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


class PostgresStore:
    """:class:`MemoryStore` over one PostgreSQL schema. Call :meth:`create` once per schema.

    ``graph`` names an optional AGE snapshot that only :meth:`rebuild` refreshes (default: none).
    ``ef_search`` is set per query with ``SET LOCAL`` for HNSW search; ``exact_scope_limit`` is the
    largest graph scope, in embeddings, that filtered search scans exactly. Both defaults are
    ADR 0007 §7's.
    """

    def __init__(
        self,
        conn: Connection,
        *,
        schema: str = "memory",
        dimensions: int = 128,
        graph: str | None = None,
        ef_search: int = DEFAULT_EF_SEARCH,
        exact_scope_limit: int = DEFAULT_EXACT_SCOPE_LIMIT,
    ) -> None:
        if getattr(conn, "autocommit", False):
            raise ValueError("PostgresStore needs a connection with autocommit off")
        if _transaction_status(conn) is None:
            raise TypeError(
                "PostgresStore needs a connection that reports its transaction status"
                " (``info.transaction_status``, as psycopg 3 and psycopg2 >= 2.8 do): without it"
                " the store cannot tell whether committing would end the caller's work"
            )
        if not 1 <= ef_search <= 1000:
            raise ValueError("ef_search must be in [1, 1000] (pgvector's range)")
        if exact_scope_limit < 0:
            raise ValueError("exact_scope_limit must be >= 0")
        self.conn = conn
        self.schema = _ident(schema)
        self.dimensions = dimensions
        self.graph = None if graph is None else _ident(graph)
        self.ef_search = ef_search
        self.exact_scope_limit = exact_scope_limit

    def _require_idle(self) -> None:
        """Refuse to run inside a transaction the caller opened: committing or rolling back here
        would end the caller's work. The constructor guarantees the status is readable."""
        if _transaction_status(self.conn) != 0:
            raise RuntimeError("PostgresStore needs an idle connection; end the open transaction")

    def _abort(self, error: BaseException) -> None:
        """Roll back after ``error`` without hiding it: a rollback that fails too (a dropped
        connection) is attached to ``error`` as a note, and ``error`` is what propagates."""
        try:
            self.conn.rollback()
        except Exception as rollback_error:
            error.add_note(
                f"rollback also failed: {type(rollback_error).__name__}: {rollback_error}"
            )

    def _write(self, work: Callable[[Cursor], None]) -> None:
        self._require_idle()
        try:
            work(self.conn.cursor())
        except BaseException as error:
            self._abort(error)
            raise
        self.conn.commit()

    def _read(self, sql: str, params: Mapping[str, Any], setup: Sequence[str] = ()) -> list[Any]:
        """Run one read in its own transaction and always end it (rollback: nothing to keep)."""
        self._require_idle()
        try:
            cur = self.conn.cursor()
            for statement in setup:
                cur.execute(statement)
            cur.execute(sql, params)
            rows = cur.fetchall()
        except BaseException as error:
            self._abort(error)
            raise
        self.conn.rollback()
        return rows

    def _run(self, statements: Iterable[str]) -> None:
        def work(cur: Cursor) -> None:
            for statement in statements:
                cur.execute(statement)

        self._write(work)

    def _many(self, sql: str, rows: list[dict[str, Any]]) -> None:
        def work(cur: Cursor) -> None:
            if rows:
                cur.executemany(sql, rows)

        self._write(work)

    def create(self) -> None:
        """Create the tables, then every derived structure (see :meth:`rebuild`)."""
        self._run(ddl(self.schema, self.dimensions))
        self.rebuild()

    def write_claims(self, claims: Iterable[ClaimRecord]) -> int:
        rows = [_row_params(c) for c in claims]
        self._many(insert_claim_sql(self.schema), rows)
        return len(rows)

    def supersede(self, old_claim_id: int, new: ClaimRecord) -> None:
        if new.supersedes != old_claim_id:
            raise ValueError(f"claim {new.claim_id} does not name {old_claim_id} as superseded")

        def work(cur: Cursor) -> None:
            cur.execute(close_claim_sql(self.schema), {"old": old_claim_id, "at": new.recorded_at})
            if cur.rowcount != 1:
                raise LookupError(f"claim {old_claim_id} is missing or already superseded")
            cur.execute(insert_claim_sql(self.schema), _row_params(new))

        self._write(work)

    def as_of_thread(self, subject: str, at: AsOf) -> list[ClaimRecord]:
        rows = self._read(as_of_thread_sql(self.schema), {"subject": subject, **as_of_params(at)})
        return [ClaimRecord(*row) for row in rows]

    def neighbours(self, start: str, hops: int, at: AsOf) -> list[Neighbour]:
        _check_hops(hops)
        params = {"start": start, "hops": hops, **as_of_params(at)}
        rows = self._read(neighbours_sql(self.schema), params)
        return [Neighbour(e, d, tuple(int(x) for x in path.split(","))) for e, d, path in rows]

    def write_embeddings(self, embeddings: Iterable[ClaimEmbedding]) -> int:
        rows = []
        for e in embeddings:
            if len(e.vector) != self.dimensions:
                raise ValueError(f"claim {e.claim_id}: expected {self.dimensions} dimensions")
            rows.append(
                {"claim_id": e.claim_id, "subject": e.subject, "vector": vector_literal(e.vector)}
            )
        self._many(insert_embedding_sql(self.schema), rows)
        return len(rows)

    def vector_top_k(
        self,
        query: Sequence[float],
        k: int,
        *,
        within: tuple[str, int, AsOf] | None = None,
    ) -> list[VectorHit]:
        if k < 1:
            raise ValueError("k must be positive")
        params: dict[str, Any] = {"query": vector_literal(query), "k": k}
        if within is not None:
            anchor, hops, at = within
            _check_hops(hops)
            params |= {
                "start": anchor,
                "hops": hops,
                "exact_limit": self.exact_scope_limit,
                **as_of_params(at),
            }
        # SET LOCAL lasts until the read's rollback. It steers unfiltered search and the wide-
        # scope branch of filtered search; iterative scans (pgvector >= 0.8) keep a filtered HNSW
        # scan going until k rows pass the filter. A small scope is searched exactly. Custom
        # plans only: psycopg prepares a statement after five executions and the server may then
        # choose a generic plan, while every measured latency (ADR 0007 §7) is a custom plan's.
        setup = (
            f"SET LOCAL hnsw.ef_search = {int(self.ef_search)}",
            "SET LOCAL hnsw.iterative_scan = relaxed_order",
            "SET LOCAL plan_cache_mode = force_custom_plan",
        )
        rows = self._read(vector_top_k_sql(self.schema, filtered=within is not None), params, setup)
        return [VectorHit(int(cid), float(d)) for cid, d in rows]

    def rebuild(self) -> None:
        graph = [] if self.graph is None else age_projection_sql(self.schema, self.graph)
        self._run([*drop_index_ddl(self.schema), *index_ddl(self.schema), *graph])
