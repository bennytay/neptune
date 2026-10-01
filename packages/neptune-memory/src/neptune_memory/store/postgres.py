"""PostgreSQL 16 + Apache AGE + pgvector implementation of :class:`MemoryStore` (ADR 0004).

Layout: the ``claim`` and ``claim_embedding`` tables are the only source of truth. Everything else
is derived and rebuildable: the bi-temporal indexes, the HNSW vector index, and the AGE graph
projection (``Entity`` vertices, one ``CLAIM`` edge per entity-valued claim) used for ad-hoc Cypher.
The hot paths (as-of thread, traversal, filtered vector search) run as SQL over the tables because
that is what measured fastest (ADR 0004); AGE compiles Cypher to similar joins over agtype.

The driver is not a dependency: pass any DB-API 2.0 connection that uses ``%(name)s`` parameters
(psycopg 3 does). Every SQL text comes from a pure function, testable without a database.
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
    from collections.abc import Iterable, Mapping, Sequence

_IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}")
MAX_HOPS: Final = 6


class Cursor(Protocol):
    def execute(self, query: str, params: Mapping[str, Any] | None = None) -> Any: ...
    def executemany(self, query: str, params_seq: Iterable[Mapping[str, Any]]) -> Any: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    @property
    def rowcount(self) -> int: ...


class Connection(Protocol):
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
        # As-of thread: walk one (subject, predicate) newest-first from valid_at.
        f"CREATE INDEX IF NOT EXISTS claim_thread_ix ON {s}.claim "
        "(subject, predicate, valid_clock, valid_from DESC)",
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
        "claim_thread_ix",
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
    """Claims about one subject visible at (valid_at, known_at).

    A loose index scan enumerates the subject's predicates; for each, the newest claim with
    ``valid_from <= valid_at`` that is current at ``known_at`` is the only candidate (predicates are
    functional, so visible intervals never overlap), and it is kept if its interval still covers
    ``valid_at``. Cost is O(predicates x log n), independent of history depth.
    """
    s = _ident(schema)
    cols = ", ".join(f"x.{c}" for c in CLAIM_COLUMNS)
    tx = (
        "c.recorded_at <= %(known_at)s "
        "AND (c.superseded_at IS NULL OR c.superseded_at > %(known_at)s)"
    )
    return f"""WITH RECURSIVE preds(p) AS (
  SELECT min(predicate) FROM {s}.claim WHERE subject = %(subject)s
  UNION ALL
  SELECT (SELECT min(predicate) FROM {s}.claim WHERE subject = %(subject)s AND predicate > preds.p)
  FROM preds WHERE preds.p IS NOT NULL
)
SELECT {cols}
FROM preds CROSS JOIN LATERAL (
  SELECT c.* FROM {s}.claim c
  WHERE c.subject = %(subject)s AND c.predicate = preds.p AND c.valid_clock = %(clock)s
    AND c.valid_from <= %(valid_at)s AND {tx}
  ORDER BY c.valid_from DESC
  LIMIT 1
) x
WHERE preds.p IS NOT NULL AND (x.valid_to IS NULL OR x.valid_to > %(valid_at)s)
ORDER BY x.predicate"""


def _walk_cte(schema: str) -> str:
    s = _ident(schema)
    return f"""WITH RECURSIVE walk(entity, depth, via) AS (
  SELECT %(start)s::text, 0, ARRAY[]::bigint[]
  UNION ALL
  SELECT e.other, w.depth + 1, w.via || e.claim_id
  FROM walk w CROSS JOIN LATERAL (
    SELECT c.object_entity AS other, c.claim_id FROM {s}.claim c
    WHERE c.subject = w.entity AND c.object_entity IS NOT NULL AND {_visible("c")}
    UNION ALL
    SELECT c.subject, c.claim_id FROM {s}.claim c
    WHERE c.object_entity = w.entity AND {_visible("c")}
  ) e
  WHERE w.depth < %(hops)s
)"""


def neighbours_sql(schema: str) -> str:
    """Entities within ``hops`` edges of ``start`` over edges visible at (valid_at, known_at)."""
    return (
        _walk_cte(schema)
        + """
SELECT DISTINCT ON (entity) entity, depth, via FROM walk
WHERE depth > 0 AND entity <> %(start)s
ORDER BY entity, depth, via"""
    )


def vector_top_k_sql(schema: str, *, filtered: bool) -> str:
    """Nearest neighbours by L2 distance; with ``filtered``, only subjects within the walk."""
    s = _ident(schema)
    if not filtered:
        return f"""SELECT claim_id, embedding <-> %(query)s::vector AS distance
FROM {s}.claim_embedding ORDER BY embedding <-> %(query)s::vector LIMIT %(k)s"""
    return (
        _walk_cte(schema)
        + f"""
SELECT e.claim_id, e.embedding <-> %(query)s::vector AS distance
FROM {s}.claim_embedding e
WHERE e.subject IN (SELECT entity FROM walk)
ORDER BY e.embedding <-> %(query)s::vector LIMIT %(k)s"""
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


class PostgresStore:
    """:class:`MemoryStore` over one PostgreSQL schema. Call :meth:`create` once per schema."""

    def __init__(
        self,
        conn: Connection,
        *,
        schema: str = "memory",
        dimensions: int = 128,
        graph: str | None = "claimgraph",
    ) -> None:
        """``graph=None`` skips the AGE projection (for servers without the ``age`` extension)."""
        self.conn = conn
        self.schema = _ident(schema)
        self.dimensions = dimensions
        self.graph = None if graph is None else _ident(graph)

    def _run(self, statements: Iterable[str]) -> None:
        try:
            cur = self.conn.cursor()
            for statement in statements:
                cur.execute(statement)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def _many(self, sql: str, rows: list[dict[str, Any]]) -> None:
        try:
            if rows:
                self.conn.cursor().executemany(sql, rows)
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

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
        cur = self.conn.cursor()
        try:
            cur.execute(close_claim_sql(self.schema), {"old": old_claim_id, "at": new.recorded_at})
            if cur.rowcount != 1:
                raise LookupError(f"claim {old_claim_id} is missing or already superseded")
            cur.execute(insert_claim_sql(self.schema), _row_params(new))
        except BaseException:
            self.conn.rollback()
            raise
        self.conn.commit()

    def as_of_thread(self, subject: str, at: AsOf) -> list[ClaimRecord]:
        cur = self.conn.cursor()
        cur.execute(as_of_thread_sql(self.schema), {"subject": subject, **as_of_params(at)})
        return [ClaimRecord(*row) for row in cur.fetchall()]

    def neighbours(self, start: str, hops: int, at: AsOf) -> list[Neighbour]:
        _check_hops(hops)
        cur = self.conn.cursor()
        cur.execute(neighbours_sql(self.schema), {"start": start, "hops": hops, **as_of_params(at)})
        return [Neighbour(e, d, tuple(via)) for e, d, via in cur.fetchall()]

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
            params |= {"start": anchor, "hops": hops, **as_of_params(at)}
        cur = self.conn.cursor()
        # Filtered HNSW scans keep going until k rows pass the filter (pgvector >= 0.8).
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur.execute(vector_top_k_sql(self.schema, filtered=within is not None), params)
        hits = [VectorHit(int(cid), float(d)) for cid, d in cur.fetchall()]
        self.conn.commit()
        return hits

    def rebuild(self) -> None:
        graph = [] if self.graph is None else age_projection_sql(self.schema, self.graph)
        self._run([*drop_index_ddl(self.schema), *index_ddl(self.schema), *graph])
