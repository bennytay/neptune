"""``lineage(record_id)`` and the transform DAG at one catalog point (ADR 0003 §4, ADR 0010 §4).

A transform is registered at a point when a ``transform_record`` row for it has a registration
key at or before the point; only then are its fields and its declared upstream edges known. An
upstream a registered transform names but no package holds by then is a node with ``NotCovered``
fields: the edge is kept, never dropped (ADR 0002 §5).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import psycopg

from neptune.identity import canonical_json
from neptune.model.knowledge import Knowledge, Known, NotApplicable, NotCovered
from neptune_ledger.api.types import (
    CatalogFinding,
    LineageEdge,
    LineageGraph,
    LineageNode,
    RecordRef,
    TransactionKey,
    TransformInfo,
)
from neptune_ledger.threads.order import Chain, chain_of

Conn = psycopg.Connection[tuple[Any, ...]]


@dataclass(frozen=True)
class TransformGraph:
    """The registered transforms reachable upstream of some start set, at one point."""

    info: dict[str, TransformInfo]
    upstream: dict[str, tuple[str, ...]]  # in consumed order (``position``)

    def chain(self, transform_id: str) -> Chain | None:
        adapters = {tid: (i.adapter_id, i.adapter_version) for tid, i in self.info.items()}
        return chain_of(transform_id, adapters, self.upstream)


def transform_graph(conn: Conn, tenant: str, start: Iterable[str], limit: int) -> TransformGraph:
    """Every transform registered by ``limit`` that is in ``start`` or upstream of one."""
    info: dict[str, TransformInfo] = {}
    upstream: dict[str, tuple[str, ...]] = {}
    seen: set[str] = set()
    frontier = sorted(set(start))
    while frontier:
        seen.update(frontier)
        rows = conn.execute(
            "SELECT t.transform_id, t.adapter_id, t.adapter_version, t.config_hash, t.libraries"
            " FROM transform t"
            " WHERE t.tenant_id = %s AND t.transform_id = ANY(%s) AND EXISTS ("
            "   SELECT 1 FROM record r WHERE r.tenant_id = t.tenant_id"
            "   AND r.kind = 'transform_record' AND r.record_id = t.transform_id"
            "   AND r.registration_key <= %s)",
            (tenant, frontier, limit),
        ).fetchall()
        found = []
        for tid, adapter, version, config, libraries in rows:
            loaded = canonical_json.loads(str(libraries).encode("utf-8"))
            assert isinstance(loaded, dict)
            info[str(tid)] = TransformInfo(str(adapter), str(version), str(config), loaded)
            found.append(str(tid))
        edges: dict[str, list[str]] = {tid: [] for tid in found}
        for tid, up in conn.execute(
            "SELECT transform_id, upstream_id FROM transform_upstream"
            " WHERE tenant_id = %s AND transform_id = ANY(%s) ORDER BY transform_id, position",
            (tenant, found),
        ).fetchall():
            edges[str(tid)].append(str(up))
        upstream.update({tid: tuple(ups) for tid, ups in edges.items()})
        frontier = sorted({u for ups in edges.values() for u in ups} - seen)
    return TransformGraph(info, upstream)


def read_lineage(
    conn: Conn, tenant: str, record_id: str, limit: int, point: Knowledge[TransactionKey]
) -> LineageGraph:
    rows = conn.execute(
        "SELECT kind, package_id, line, transform_id, source_content_id, source_locator"
        " FROM record WHERE tenant_id = %s AND record_id = %s AND registration_key <= %s"
        " ORDER BY registration_key, package_id",
        (tenant, record_id, limit),
    ).fetchall()
    if not rows:
        return unknown_record(record_id, point)
    kind, transform, source, locator = str(rows[0][0]), rows[0][3], rows[0][4], rows[0][5]
    registered_by = tuple(
        RecordRef(str(package), str(k), record_id, int(line)) for k, package, line, *_ in rows
    )
    nodes: tuple[LineageNode, ...] = ()
    edges: tuple[LineageEdge, ...] = ()
    siblings: tuple[RecordRef, ...] = ()
    if transform is not None:
        graph = transform_graph(conn, tenant, [str(transform)], limit)
        named = {str(transform)} | {u for ups in graph.upstream.values() for u in ups}
        nodes = tuple(
            LineageNode(tid, Known(graph.info[tid]) if tid in graph.info else NotCovered())
            for tid in sorted(named, key=str.encode)
        )
        edges = tuple(
            LineageEdge(tid, up, position)
            for tid in sorted(graph.upstream, key=str.encode)
            for position, up in enumerate(graph.upstream[tid])
        )
    if transform is not None and source is not None:
        siblings = tuple(
            RecordRef(str(package), str(k), str(rid), int(line))
            for k, rid, package, line in conn.execute(
                "SELECT kind, record_id, package_id, line FROM record"
                " WHERE tenant_id = %s AND source_content_id = %s AND kind = %s"
                "   AND md5(source_locator) = md5(%s) AND source_locator = %s"
                "   AND transform_id <> %s AND registration_key <= %s"
                " ORDER BY kind, record_id, package_id",
                (tenant, source, kind, locator, locator, transform, limit),
            ).fetchall()
        )
    return LineageGraph(
        record_id=record_id,
        status="found",
        kind=Known(kind),
        transform_id=Known(str(transform)) if transform is not None else NotApplicable(),
        registered_by=registered_by,
        nodes=nodes,
        edges=edges,
        siblings=siblings,
        as_of=point,
        findings=(),
    )


def unknown_record(
    record_id: str, point: Knowledge[TransactionKey], finding: CatalogFinding | None = None
) -> LineageGraph:
    """A rejected or unknown-record ``lineage``: every collection empty (ADR 0004 §2)."""
    found = finding or CatalogFinding(
        "unknown_record", record_id, "no registered package holds this record id"
    )
    return LineageGraph(
        record_id=record_id,
        status="unknown_record",
        kind=NotCovered(),
        transform_id=NotCovered(),
        registered_by=(),
        nodes=(),
        edges=(),
        siblings=(),
        as_of=point,
        findings=(found,),
    )
