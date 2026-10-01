"""The catalog API as a typed protocol, a stub, and a dispatcher for request records.

Guarantees and refusals per call are in ``docs/catalog-api.md``; the error model is ADR 0004:
evidence and request problems come back as ``CatalogFinding``s inside the typed response, and the
only exception a conforming implementation raises is ``CatalogUnavailable``.
"""

import os
from typing import Any, Protocol, runtime_checkable

from neptune_ledger.api.types import (
    ClockMerge,
    EvidenceAnchor,
    LineageGraph,
    LineageRequest,
    Order,
    QueryRequest,
    QuerySpec,
    RegisterRequest,
    Registration,
    Resolution,
    ResolveRequest,
    Thread,
    ThreadKey,
    ThreadPreference,
    ThreadRequest,
    ThreadsOf,
    ThreadsOfRequest,
    VerifyReport,
    VerifyRequest,
)


class CatalogUnavailable(RuntimeError):
    """The catalog store could not answer (unreachable, or a transaction kept failing).

    Infrastructure, never evidence: nothing about a package, a record or a request is reported
    this way. A call that raised it wrote nothing (ADR 0004 §2).
    """


@runtime_checkable
class CatalogApi(Protocol):
    """The Ledger catalog API, version ``CATALOG_API_VERSION``. Synchronous; one tenant.

    Every read call is evaluated at a catalog point: ``as_of`` (a ``tx_seq``) or, when omitted,
    the latest committed point, which the response returns so the call can be replayed exactly.
    """

    def register(self, package_root: str | os.PathLike[str]) -> Registration:
        """Catalogue the package at ``package_root`` (ADR 0002 §4, §6).

        One database transaction writes the registration-log row, the package row and every
        index row together, or nothing. It locks the ``tx_clock`` row before looking the package
        up and holds it to commit, so READ COMMITTED suffices; at REPEATABLE READ or SERIALIZABLE
        the implementation retries the whole transaction on SQLSTATE 40001 / 40P01, which is safe
        because a retry either registers or finds the package registered. A known package id is a
        no-op returning the stored ids (``already_registered``); a package bringing an existing
        source, transform or clock id with different fields is ``refused`` with a
        ``conflicting_id`` finding, and a corrupt or tampered package is ``refused`` too.
        """
        ...

    def verify(self, package_id: str, *, as_of: int | None = None) -> VerifyReport:
        """Re-hash a registered package at its stored root locator; report, never repair."""
        ...

    def resolve(self, evidence_ref: EvidenceAnchor, *, as_of: int | None = None) -> Resolution:
        """Where the cited source lives, how to fetch it, and which records cite this anchor."""
        ...

    def thread(
        self,
        key: ThreadKey,
        order: Order,
        preference: ThreadPreference,
        *,
        merge: ClockMerge | None = None,
        as_of: int | None = None,
    ) -> Thread:
        """One thread (ADR 0003): ``History()`` is ``history()``; a ``Preference`` is ``current()``.

        ``preference`` has no default; a dynamic caller passing ``None`` gets an empty ``Thread``
        with a ``preference_required`` finding.
        """
        ...

    def threads_of(self, record_id: str, *, as_of: int | None = None) -> ThreadsOf:
        """Every thread a record id belongs to, per registering package (ADR 0003 §1.4)."""
        ...

    def lineage(self, record_id: str, *, as_of: int | None = None) -> LineageGraph:
        """The transform DAG behind a record id, its registering packages and lineage siblings."""
        ...

    def query(self, spec: QuerySpec) -> Any:
        """A ``pyarrow.Table`` with ``QUERY_RESULT_SCHEMA`` columns and ``QueryMeta`` metadata.

        Typed ``Any`` because pyarrow has no type information; build it with ``query_table``.
        """
        ...


class StubCatalog:
    """The contract with no implementation: every call raises ``NotImplementedError``.

    The contract tests run against it as an expected failure (ADR 0004 §6), so the suite is
    exercised on every CI run before the real catalog (MVL-90, MVL-92, MVL-98) exists.
    """

    def _missing(self, call: str) -> NotImplementedError:
        return NotImplementedError(f"catalog API {call}() has no implementation yet")

    def register(self, package_root: str | os.PathLike[str]) -> Registration:
        raise self._missing("register")

    def verify(self, package_id: str, *, as_of: int | None = None) -> VerifyReport:
        raise self._missing("verify")

    def resolve(self, evidence_ref: EvidenceAnchor, *, as_of: int | None = None) -> Resolution:
        raise self._missing("resolve")

    def thread(
        self,
        key: ThreadKey,
        order: Order,
        preference: ThreadPreference,
        *,
        merge: ClockMerge | None = None,
        as_of: int | None = None,
    ) -> Thread:
        raise self._missing("thread")

    def threads_of(self, record_id: str, *, as_of: int | None = None) -> ThreadsOf:
        raise self._missing("threads_of")

    def lineage(self, record_id: str, *, as_of: int | None = None) -> LineageGraph:
        raise self._missing("lineage")

    def query(self, spec: QuerySpec) -> Any:
        raise self._missing("query")


Request = (
    RegisterRequest
    | VerifyRequest
    | ResolveRequest
    | ThreadRequest
    | ThreadsOfRequest
    | LineageRequest
    | QueryRequest
)


def call(api: CatalogApi, request: Request) -> Any:
    """Dispatch a request record to its method: the wire form of every call is its request."""
    match request:
        case RegisterRequest(package_root=root):
            return api.register(root)
        case VerifyRequest(package_id=package_id, as_of=as_of):
            return api.verify(package_id, as_of=as_of)
        case ResolveRequest(evidence_ref=ref, as_of=as_of):
            return api.resolve(ref, as_of=as_of)
        case ThreadRequest(key=key, order=order, preference=preference, merge=merge, as_of=as_of):
            return api.thread(key, order, preference, merge=merge, as_of=as_of)
        case ThreadsOfRequest(record_id=record_id, as_of=as_of):
            return api.threads_of(record_id, as_of=as_of)
        case LineageRequest(record_id=record_id, as_of=as_of):
            return api.lineage(record_id, as_of=as_of)
        case QueryRequest(spec=spec):
            return api.query(spec)
    raise TypeError(f"not a catalog API request: {type(request).__name__}")
