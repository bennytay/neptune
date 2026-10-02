"""``thread`` and ``threads_of`` from the derived thread index at one catalog point (ADR 0003).

One indexed range scan reads a thread's members (``THREAD_MEMBERS``); ordering, lineage-set
resolution and any merge are the pure functions of ``order`` and ``merge``. Threads are returned
whole (ADR 0006 §7).
"""

from collections.abc import Sequence
from functools import lru_cache
from typing import Any, Final, cast

import psycopg

from neptune.identity import canonical_json
from neptune.model.knowledge import Knowledge
from neptune_ledger.api import codec
from neptune_ledger.api.types import (
    CatalogFinding,
    ClockMerge,
    History,
    Membership,
    Order,
    RevisionEdge,
    Thread,
    ThreadKey,
    ThreadPreference,
    ThreadsOf,
    TransactionKey,
    UnresolvedMember,
    UnresolvedMembership,
    WorldTime,
)
from neptune_ledger.lineage.graph import transform_graph
from neptune_ledger.threads import merge as merging
from neptune_ledger.threads.order import (
    Member,
    SetKey,
    collapse,
    history_entries,
    history_sets,
    lineage_sets,
    ordered,
    resolve,
)

Conn = psycopg.Connection[tuple[Any, ...]]

# A thread's members at a catalog point: the index range scan ADR 0005 §5's budget applies to.
# The scale harness (tests/ledger_catalog_scale.py) measures this exact statement.
THREAD_MEMBERS: Final = """
SELECT m.package_id, m.record_id, m.kind, m.roles, m.registration_key, p.tx_time,
       m.transform_id, m.source_content_id, m.world
FROM thread_member m
JOIN package p ON p.tenant_id = m.tenant_id AND p.package_id = m.package_id
WHERE m.tenant_id = %(tenant)s AND m.thread_id = %(thread_id)s
  AND m.registration_key <= %(as_of)s
"""

_WORLD: Final[Any] = cast("Any", Knowledge)[WorldTime]


@lru_cache(maxsize=65536)
def _world(text: str) -> Knowledge[WorldTime]:
    return codec.decode_as(_WORLD, canonical_json.loads(text.encode("utf-8")))  # type: ignore[no-any-return]


@lru_cache(maxsize=65536)
def _key(text: str) -> ThreadKey:
    return codec.loads(ThreadKey, text.encode("utf-8"))


def members(conn: Conn, tenant: str, thread_id: str, limit: int) -> list[Member]:
    rows = conn.execute(
        THREAD_MEMBERS, {"tenant": tenant, "thread_id": thread_id, "as_of": limit}
    ).fetchall()
    return [
        Member(
            package_id=str(package),
            record_id=str(record),
            kind=str(kind),
            roles=tuple(roles),
            registration=TransactionKey(int(seq), str(at)),
            transform_id=str(transform),
            source=str(source),
            world=_world(str(world)),
        )
        for package, record, kind, roles, seq, at, transform, source, world in rows
    ]


def read_thread(
    conn: Conn,
    tenant: str,
    key: ThreadKey,
    order: Order,
    preference: ThreadPreference,
    merge: ClockMerge | None,
    mappings: Sequence[merging.ClockMapping],
    limit: int,
    point: Knowledge[TransactionKey],
) -> Thread:
    """The thread at ``limit``. The request has been validated, and the merge's mapping ids
    resolved to ``mappings`` (empty when no merge was asked for)."""
    thread_id = key.thread_id
    found = members(conn, tenant, thread_id, limit)
    sets = lineage_sets(found)
    if isinstance(preference, History):
        entries = history_entries(found)
        resolved = history_sets(sets)
    else:
        start = {m.transform_id for m in found}
        graph = transform_graph(conn, tenant, start, limit)
        chains = {tid: graph.chain(tid) for tid in start}
        resolved, selected = resolve(sets, preference, chains)
        entries = collapse(selected)
    partitions = ordered(entries, order)
    findings: list[CatalogFinding] = []
    if merge is not None:
        partitions, findings = merging.merge(partitions, merge.reference_clock, mappings)
    return Thread(
        thread_id=thread_id,
        key=key,
        order=order,
        as_of=point,
        partitions=partitions,
        lineage_sets=resolved,
        revisions=revisions(conn, tenant, set(sets), limit),
        unresolved=unresolved(conn, tenant, thread_id, limit),
        links=(),
        findings=tuple(findings),
        preference=preference,
        merge=merge,
    )


def revisions(conn: Conn, tenant: str, sets: set[SetKey], limit: int) -> tuple[RevisionEdge, ...]:
    """``(kind, c2) revises (kind, c1)`` for every pair of this thread's lineage sets where a
    source revision of ``c2`` supersedes one of ``c1`` (ADR 0003 §5), both registered by
    ``limit``. One edge per revision pair; entries are never paired."""
    sources = sorted({source for _, source in sets})
    if len(sources) < 2:
        return ()
    rows = conn.execute(
        "SELECT DISTINCT s.content_id, s.revision_id, prev.content_id, prev.revision_id"
        " FROM source_location s"
        " JOIN package ps ON ps.tenant_id = s.tenant_id AND ps.package_id = s.package_id"
        " CROSS JOIN LATERAL unnest(s.supersedes) AS sup(revision_id)"
        " JOIN source_location prev"
        "   ON prev.tenant_id = s.tenant_id AND prev.revision_id = sup.revision_id"
        " JOIN package pp ON pp.tenant_id = prev.tenant_id AND pp.package_id = prev.package_id"
        " WHERE s.tenant_id = %s AND s.content_id = ANY(%s) AND prev.content_id = ANY(%s)"
        "   AND s.content_id <> prev.content_id AND ps.tx_seq <= %s AND pp.tx_seq <= %s",
        (tenant, sources, sources, limit, limit),
    ).fetchall()
    kinds = sorted({kind for kind, _ in sets})
    edges = {
        RevisionEdge(kind, str(c2), str(c1), str(r2), str(r1))
        for c2, r2, c1, r1 in rows
        for kind in kinds
        if (kind, str(c2)) in sets and (kind, str(c1)) in sets
    }
    return tuple(
        sorted(
            edges,
            key=lambda e: tuple(
                part.encode("utf-8")
                for part in (e.kind, e.source, e.revises, e.source_revision, e.revised_revision)
            ),
        )
    )


def unresolved(conn: Conn, tenant: str, thread_id: str, limit: int) -> tuple[UnresolvedMember, ...]:
    rows = conn.execute(
        "SELECT package_id, record_id, kind, pointer, registration_key FROM thread_unresolved"
        " WHERE tenant_id = %s AND thread_id = %s AND registration_key <= %s",
        (tenant, thread_id, limit),
    ).fetchall()
    rows.sort(key=lambda r: (int(r[4]), str(r[1]).encode(), str(r[0]).encode(), str(r[3]).encode()))
    return tuple(UnresolvedMember(str(p), str(r), str(k), str(ptr)) for p, r, k, ptr, _ in rows)


def empty_thread(
    thread_id: str,
    key: ThreadKey,
    order: Order,
    point: Knowledge[TransactionKey],
    findings: tuple[CatalogFinding, ...],
    preference: ThreadPreference | None,
    merge: ClockMerge | None,
) -> Thread:
    """A rejected ``thread`` call: the payload empty, the findings say why (ADR 0004 §2)."""
    return Thread(
        thread_id=thread_id,
        key=key,
        order=order,
        as_of=point,
        partitions=(),
        lineage_sets=(),
        revisions=(),
        unresolved=(),
        links=(),
        findings=findings,
        preference=preference,
        merge=merge,
    )


def read_threads_of(
    conn: Conn, tenant: str, record_id: str, limit: int, point: Knowledge[TransactionKey]
) -> ThreadsOf:
    held = conn.execute(
        "SELECT 1 FROM record WHERE tenant_id = %s AND record_id = %s AND registration_key <= %s"
        " LIMIT 1",
        (tenant, record_id, limit),
    ).fetchone()
    if held is None:
        finding = CatalogFinding(
            "unknown_record", record_id, "no registered package holds this record id"
        )
        return unknown_threads_of(record_id, point, finding)
    memberships = sorted(
        (
            Membership(str(tid), _key(str(key)), str(package), tuple(roles))
            for tid, key, package, roles in conn.execute(
                "SELECT m.thread_id, t.key, m.package_id, m.roles FROM thread_member m"
                " JOIN thread t ON t.tenant_id = m.tenant_id AND t.thread_id = m.thread_id"
                " WHERE m.tenant_id = %s AND m.record_id = %s AND m.registration_key <= %s",
                (tenant, record_id, limit),
            ).fetchall()
        ),
        key=lambda m: (m.thread_id.encode(), m.package_id.encode()),
    )
    named = sorted(
        (
            UnresolvedMembership(str(tid), _key(str(key)), str(package), str(pointer))
            for tid, key, package, pointer in conn.execute(
                "SELECT u.thread_id, t.key, u.package_id, u.pointer FROM thread_unresolved u"
                " JOIN thread t ON t.tenant_id = u.tenant_id AND t.thread_id = u.thread_id"
                " WHERE u.tenant_id = %s AND u.record_id = %s AND u.registration_key <= %s",
                (tenant, record_id, limit),
            ).fetchall()
        ),
        key=lambda u: (u.thread_id.encode(), u.package_id.encode(), u.pointer.encode()),
    )
    return ThreadsOf(
        record_id=record_id,
        status="found",
        memberships=tuple(memberships),
        unresolved=tuple(named),
        as_of=point,
        findings=(),
    )


def unknown_threads_of(
    record_id: str, point: Knowledge[TransactionKey], finding: CatalogFinding
) -> ThreadsOf:
    return ThreadsOf(
        record_id=record_id,
        status="unknown_record",
        memberships=(),
        unresolved=(),
        as_of=point,
        findings=(finding,),
    )
