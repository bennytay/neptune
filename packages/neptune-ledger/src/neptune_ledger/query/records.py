"""The record stage: keyset batches of the record statement, the lineage filter, the deadline.

Rows come back in ``(kind, record_id, package_id)`` order. The stage stops between batches when
it has the rows it needs or the deadline has passed, and each statement runs under a
``statement_timeout`` of the time left, so whatever it returns is a prefix of the full answer
(ADR 0016 §6). A lineage preference is resolved over each lineage set the rows touch, whole
(ADR 0016 §3), with the resolver ``thread`` uses.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Final

import psycopg

from neptune.model.knowledge import Ambiguous, Knowledge, Known, NotApplicable, NotCovered
from neptune_ledger.api.types import (
    CatalogFinding,
    LatestTransform,
    Preference,
    TransactionKey,
)
from neptune_ledger.lineage.graph import transform_graph
from neptune_ledger.query.budget import Budget
from neptune_ledger.query.plan import RecordPlan
from neptune_ledger.threads.order import Member, SetKey, lineage_sets, resolve

Conn = psycopg.Connection[tuple[Any, ...]]
Row = tuple[Any, ...]  # the QueryRow columns, in order
KIND, RECORD, PACKAGE, TRANSFORM, SOURCE = 0, 1, 2, 5, 6

_SET_MEMBERS: Final = """
SELECT r.kind, r.record_id, r.package_id, r.registration_key, r.transform_id, r.source_content_id
  FROM record r
 WHERE r.tenant_id = %(tenant)s AND r.source_content_id IS NOT NULL
   AND r.transform_id IS NOT NULL AND r.registration_key <= %(as_of)s
   AND (r.source_content_id, r.kind) IN (
       SELECT * FROM unnest(%(sources)s::text[], %(kinds)s::text[]))
"""
_THREAD_SET_MEMBERS: Final = """
SELECT m.kind, m.record_id, m.package_id, m.registration_key, m.transform_id, m.source_content_id
  FROM thread_member m
 WHERE m.tenant_id = %(tenant)s AND m.thread_id = %(thread_id)s
   AND m.registration_key <= %(as_of)s
   AND (m.source_content_id, m.kind) IN (
       SELECT * FROM unnest(%(sources)s::text[], %(kinds)s::text[]))
"""


class Lineage:
    """A lineage preference applied to record rows (ADR 0016 §3).

    Each set is resolved once per call, over all its members at the catalog point: the thread's
    members of the set when the spec names a thread, else every record of that kind and source.
    """

    def __init__(
        self, conn: Conn, tenant: str, preference: Preference, thread_id: str | None, as_of: int
    ) -> None:
        self._conn = conn
        self._tenant = tenant
        self._preference = preference
        self._thread_id = thread_id
        self._as_of = as_of
        self._resolved: dict[SetKey, Knowledge[str]] = {}
        self._reported: set[SetKey] = set()
        self.findings: list[CatalogFinding] = []

    def resolve(self, rows: Sequence[Row]) -> None:
        """Resolve every set ``rows`` touch that is not resolved yet."""
        keys = sorted({k for row in rows if (k := _set_key(row)) is not None} - set(self._resolved))
        if not keys:
            return
        params = {
            "tenant": self._tenant,
            "thread_id": self._thread_id,
            "as_of": self._as_of,
            "sources": [source for _, source in keys],
            "kinds": [kind for kind, _ in keys],
        }
        statement = _SET_MEMBERS if self._thread_id is None else _THREAD_SET_MEMBERS
        members = [
            Member(
                package_id=str(package),
                record_id=str(record),
                kind=str(kind),
                roles=("subject",),
                registration=TransactionKey(int(seq), ""),
                transform_id=str(transform),
                source=str(source),
                world=NotApplicable(),
            )
            for kind, record, package, seq, transform, source in self._conn.execute(
                statement, params
            ).fetchall()
        ]
        sets = lineage_sets(members)
        chains = {}
        if isinstance(self._preference, LatestTransform):
            start = {m.transform_id for m in members}
            chains = transform_graph(self._conn, self._tenant, start, self._as_of).chains(start)
        resolved, _ = resolve(sets, self._preference, chains)
        for found in resolved:
            self._resolved[(found.kind, found.source)] = found.resolution
        for key in keys:
            self._resolved.setdefault(key, NotCovered())

    def keeps(self, row: Row) -> bool:
        """Whether the row is of its set's resolved transform; a row without a lineage set
        passes. An ``Ambiguous`` set's first dropped row reports the set once."""
        key = _set_key(row)
        if key is None:
            return True
        resolution = self._resolved[key]
        if isinstance(resolution, Known):
            return bool(resolution.value == row[TRANSFORM])
        if isinstance(resolution, Ambiguous) and key not in self._reported:
            self._reported.add(key)
            candidates = ", ".join(str(c.value) for c in resolution.candidates)
            detail = (
                f"{key[0]} records of this source have no single {_name(self._preference)}"
                f" transform (candidates: {candidates}); none of them is returned"
            )
            self.findings.append(CatalogFinding("ambiguous_lineage", key[1], detail))
        return False


def _name(preference: Preference) -> str:
    return str(getattr(preference, "tag", "preferred"))


def _set_key(row: Row) -> SetKey | None:
    if row[SOURCE] is None or row[TRANSFORM] is None:
        return None
    return str(row[KIND]), str(row[SOURCE])


@dataclass
class RecordRead:
    """The rows the stage returns, a prefix of the answer in key order."""

    rows: list[Row]
    findings: list[CatalogFinding]


def read_records(
    conn: Conn,
    plan: RecordPlan,
    *,
    want: int,
    after: tuple[str, str, str] | None,
    budget: Budget,
    batch_rows: int,
    lineage: Lineage | None,
) -> RecordRead:
    """Up to ``want`` rows after ``after``, in key order, under the budget's deadline."""
    rows: list[Row] = []
    cursor = after or ("", "", "")
    # Batches only where the stage may stop early; otherwise one statement asks for everything.
    stoppable = lineage is not None or budget.remaining() is not None
    while len(rows) < want:
        if budget.out_of_time():
            break
        # A lineage filter drops rows, so it reads whole batches; otherwise no more than needed.
        if lineage is not None:
            size = batch_rows
        else:
            size = min(batch_rows, want - len(rows)) if stoppable else want - len(rows)
        fetched = _fetch(conn, plan, cursor, size, budget)
        if fetched is None:
            break
        if lineage is not None:
            if not _run_or_stop(conn, budget, partial(lineage.resolve, fetched)):
                break
            for row in fetched:
                if lineage.keeps(row):
                    rows.append(row)
                    if len(rows) == want:
                        break
        else:
            rows += fetched
        if len(fetched) < size:
            break
        last = fetched[-1]
        cursor = (str(last[KIND]), str(last[RECORD]), str(last[PACKAGE]))
    return RecordRead(rows, list(lineage.findings) if lineage is not None else [])


def _timeout(conn: Conn, budget: Budget) -> None:
    left = budget.remaining()
    if left is not None:
        # At least 1 ms: 0 would mean no timeout at all.
        millis = max(1, int(left * 1000))
        conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{millis}ms",))


def _fetch(
    conn: Conn, plan: RecordPlan, cursor: tuple[str, str, str], size: int, budget: Budget
) -> list[Row] | None:
    params = {
        **plan.params,
        "after_kind": cursor[0],
        "after_record": cursor[1],
        "after_package": cursor[2],
        "batch": size,
    }
    out: list[Row] = []

    def run() -> None:
        out.extend(tuple(row) for row in conn.execute(plan.statement, params).fetchall())

    return out if _run_or_stop(conn, budget, run) else None


def _run_or_stop(conn: Conn, budget: Budget, body: Any) -> bool:
    """Run ``body`` in a savepoint under the time left; False when the deadline cancelled it."""
    try:
        with conn.transaction():
            _timeout(conn, budget)
            body()
    except psycopg.errors.QueryCanceled:
        budget.time_ran_out()
        return False
    return True


def keys(rows: Iterable[Row]) -> list[tuple[str, str]]:
    """The ``(package_id, record_id)`` pairs of stream rows, in row order."""
    return [(str(row[PACKAGE]), str(row[RECORD])) for row in rows]
