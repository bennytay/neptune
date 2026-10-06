"""The Python SDK (ADR 0004): ``Client`` and ``AsyncClient``, identical types, one set of rules.

``query`` validates the query before any engine sees it and verifies the answer after
(``answer.answer_problems``): the packet must name this query by its id, answer the snapshot the
query pinned, agree on whether inference is included, echo the query's budget and window, and
keep every timed item on a clock the query asked for or bridged. ``why`` and ``diff`` are
conveniences that build the typed ``Query`` a caller could have written (ADR 0002 Q09 and Q04);
there is no second request type. ``hydrate`` resolves one evidence ref through the Ledger. Every
call is a read, so a transient failure (``unavailable``, ``timeout``) is retried under the
``RetryPolicy``; nothing else is.

With a ``Planner`` (ADR 0005, ADR 0009 §4), ``plan`` turns English into a ``PlannedQuery`` (shown,
never an answer), ``choose`` settles an ambiguous name, ``ask`` returns the plan beside the packet
of its query (run only when the plan is ``ready``), and ``entities`` / ``find`` list or match the
declared identities the planner's resolver holds. No planner means those calls are
``unavailable``; the query calls never depend on one.

``include_inferred`` has no default anywhere (ADR 0002 §5): the caller chooses on every call.
A local client is ``Client(engine)``; a remote one ``Client("https://...", token=...)``. Local
mode has an implicit tenant and takes no token.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from neptune.identity.canonical_json import dumps
from neptune.model.provenance import EvidenceRef
from neptune_context.answer import answer_problems
from neptune_context.packets.model import EvidenceItem
from neptune_context.query.decode import accept
from neptune_context.query.findings import Refused
from neptune_context.query.model import (
    HEAD,
    MAX_TEXT_CHARS,
    AsOf,
    Budget,
    Diff,
    Instant,
    Query,
    Subject,
    Why,
)
from neptune_context.sdk.engine import AsyncEngine, Engine, to_async
from neptune_context.sdk.errors import ErrorCode, SdkError
from neptune_context.sdk.http import DEFAULT_TIMEOUT_S, HttpEngine
from neptune_context.sdk.planning import Asked, Planner

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from neptune_ledger.api import Resolution

    from neptune_context.packets.model import ContextPacket
    from neptune_context.query.plan import Entity, Mention, PlannedQuery

T = TypeVar("T")

DEFAULT_EXPLAIN_ITEMS: Final = 10
MAX_TRANSACTION: Final = 2**63 - 1


@dataclass(frozen=True)
class RetryPolicy:
    """How many times an idempotent call is tried, and how long to wait before each retry.

    ``backoff_s[i]`` is the pause before retry ``i + 1``; the last value repeats. Deterministic: no
    jitter, so a test can assert the exact schedule.
    """

    attempts: int = 3
    backoff_s: tuple[float, ...] = (0.2, 0.8)

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not 1 <= self.attempts <= 10:
            raise SdkError(ErrorCode.INVALID_ARGUMENT, "attempts is between 1 and 10")
        if not self.backoff_s or any(not 0 <= b <= 60 for b in self.backoff_s):
            raise SdkError(ErrorCode.INVALID_ARGUMENT, "backoff_s holds pauses of 0 to 60 seconds")

    def pause(self, retry: int) -> float:
        return self.backoff_s[min(retry, len(self.backoff_s) - 1)]


NO_RETRY: Final = RetryPolicy(attempts=1)


# --- Pure steps shared by both clients ---------------------------------------------------------


def _checked_query(query: Query) -> Query:
    if not isinstance(query, Query):
        raise SdkError(ErrorCode.INVALID_ARGUMENT, f"query must be a Query, got {type(query)}")
    accepted = accept(query)
    if isinstance(accepted, Refused):
        first = accepted.findings[0]
        raise SdkError(
            ErrorCode.QUERY_REFUSED,
            f"{len(accepted.findings)} finding(s); first: {first.code} at {first.at}: "
            f"{first.message}",
            findings=accepted.findings,
        )
    return accepted


def _verified(query: Query, packet: object) -> ContextPacket:
    """The engine's answer, or ``invalid_response``: a wrong answer is a defect, never data."""
    from neptune_context.packets.model import ContextPacket

    if not isinstance(packet, ContextPacket):
        raise SdkError(ErrorCode.INVALID_RESPONSE, "the engine did not return a ContextPacket")
    problems = answer_problems(query, packet)
    if problems:
        extra = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        raise SdkError(ErrorCode.INVALID_RESPONSE, problems[0] + extra)
    return packet


def _flag(include_inferred: object) -> bool:
    if not isinstance(include_inferred, bool):
        raise SdkError(ErrorCode.INVALID_ARGUMENT, "include_inferred is a bool you choose")
    return include_inferred


def why_query(
    claim_id: str, *, include_inferred: bool, as_of: AsOf = HEAD, budget: Budget | None = None
) -> Query:
    """The query ``why`` sends: explain one claim (ADR 0002 Q09)."""
    return Query(
        include_inferred=_flag(include_inferred),
        budget=budget or Budget(items=DEFAULT_EXPLAIN_ITEMS),
        as_of=as_of,
        explain=(Why(claim_id),),
    )


def diff_query(
    subject: Subject,
    before: int | Instant,
    after: int | Instant,
    *,
    include_inferred: bool,
    as_of: AsOf = HEAD,
    budget: Budget | None = None,
) -> Query:
    """The query ``diff`` sends: what changed about one subject between two points (Q04, Q10)."""
    return Query(
        include_inferred=_flag(include_inferred),
        budget=budget or Budget(items=DEFAULT_EXPLAIN_ITEMS),
        subjects=frozenset({subject}),
        as_of=as_of,
        explain=(Diff(subject, before, after),),
    )


def _evidence(evidence: EvidenceRef | EvidenceItem) -> EvidenceRef:
    if isinstance(evidence, EvidenceItem):
        return evidence.evidence
    if isinstance(evidence, EvidenceRef):
        return evidence
    raise SdkError(
        ErrorCode.INVALID_ARGUMENT,
        f"hydrate takes an EvidenceRef or EvidenceItem, got {type(evidence)}",
    )


def _transaction(as_of: int | None) -> int | None:
    if as_of is None:
        return None
    if isinstance(as_of, bool) or not isinstance(as_of, int) or not 0 <= as_of <= MAX_TRANSACTION:
        raise SdkError(ErrorCode.INVALID_ARGUMENT, "as_of is a Ledger transaction or None")
    return as_of


def _resolved(ref: EvidenceRef, resolution: object) -> Resolution:
    """The Ledger's answer for exactly ``ref`` (source and locator); else ``invalid_response``."""
    from neptune_ledger.api import Resolution

    if not isinstance(resolution, Resolution):
        raise SdkError(ErrorCode.INVALID_RESPONSE, "the engine did not return a Resolution")
    if isinstance(ref.source, str) and resolution.evidence_ref.source != ref.source:
        raise SdkError(ErrorCode.INVALID_RESPONSE, "the resolution is for a different source")
    locator = [step.to_json() for step in ref.locator]
    if dumps(list(resolution.evidence_ref.locator)) != dumps(locator):
        raise SdkError(ErrorCode.INVALID_RESPONSE, "the resolution is for a different locator")
    return resolution


def _engine_error(error: Exception) -> SdkError:
    return SdkError(ErrorCode.ENGINE_ERROR, f"{type(error).__name__}: {error}")


def _split_target(
    target: str | Engine | AsyncEngine, token: str | None
) -> str | Engine | AsyncEngine:
    if isinstance(target, str):
        return target
    if token is not None:
        raise SdkError(
            ErrorCode.INVALID_ARGUMENT, "a local engine has an implicit tenant: no token"
        )
    if not (
        callable(getattr(target, "query", None)) and callable(getattr(target, "hydrate", None))
    ):
        raise SdkError(ErrorCode.INVALID_ARGUMENT, "target is a URL or an engine")
    return target


def _planner(planner: Planner | None) -> Planner:
    if planner is None:
        raise SdkError(
            ErrorCode.UNAVAILABLE,
            "this client has no planner; write the typed query and call query",
        )
    return planner


def _checked_planner(planner: object) -> Planner | None:
    if planner is not None and not isinstance(planner, Planner):
        raise SdkError(
            ErrorCode.INVALID_ARGUMENT, f"planner must be a Planner, got {type(planner)}"
        )
    return planner


def _question(question: object) -> str:
    """A question or text to plan or match: a string of at most ``MAX_TEXT_CHARS`` characters.
    An oversized one is refused without being echoed back."""
    if not isinstance(question, str):
        raise SdkError(ErrorCode.INVALID_ARGUMENT, "the question is a string")
    if len(question) > MAX_TEXT_CHARS:
        raise SdkError(
            ErrorCode.INVALID_ARGUMENT,
            f"the text is {len(question)} characters; at most {MAX_TEXT_CHARS}",
        )
    return question


def _planned(call: Callable[[], T]) -> T:
    """A planner call: ``SdkError`` passes, anything else is the planner's defect."""
    try:
        return call()
    except SdkError:
        raise
    except Exception as error:
        raise _engine_error(error) from error


# --- Sync ---------------------------------------------------------------------------------------


class Client:
    """Synchronous client. ``target`` is an engine URL (retries by default) or an in-process
    ``Engine`` (no retries unless you pass a ``RetryPolicy``)."""

    def __init__(
        self,
        target: str | Engine,
        *,
        token: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        sleep: Callable[[float], None] = time.sleep,
        planner: Planner | None = None,
    ) -> None:
        resolved = _split_target(target, token)
        self._planner = _checked_planner(planner)
        self._engine: Engine = (
            HttpEngine(resolved, token=token, timeout=timeout)
            if isinstance(resolved, str)
            else _sync(resolved)
        )
        self._retry = retry or (RetryPolicy() if isinstance(resolved, str) else NO_RETRY)
        self._sleep = sleep

    @classmethod
    def local(cls, engine: Engine, *, retry: RetryPolicy | None = None) -> Client:
        """A client over an in-process engine; no network, so no retries unless you ask."""
        return cls(engine, retry=retry)

    def _call(self, call: Callable[[], T], *, idempotent: bool = True) -> T:
        tries = self._retry.attempts if idempotent else 1
        for attempt in range(tries):
            try:
                return call()
            except SdkError as error:
                if not error.retryable or attempt == tries - 1:
                    raise
                self._sleep(self._retry.pause(attempt))
            except Exception as error:
                raise _engine_error(error) from error
        raise AssertionError("unreachable")  # pragma: no cover

    def query(self, query: Query) -> ContextPacket:
        """The packet answering ``query``; refused queries never reach the engine."""
        checked = _checked_query(query)
        return self._call(lambda: _verified(checked, self._engine.query(checked)))

    def why(
        self,
        claim_id: str,
        *,
        include_inferred: bool,
        as_of: AsOf = HEAD,
        budget: Budget | None = None,
    ) -> ContextPacket:
        """Why memory holds one claim: its evidence, transform, supersession and findings."""
        return self.query(
            why_query(claim_id, include_inferred=include_inferred, as_of=as_of, budget=budget)
        )

    def diff(
        self,
        subject: Subject,
        before: int | Instant,
        after: int | Instant,
        *,
        include_inferred: bool,
        as_of: AsOf = HEAD,
        budget: Budget | None = None,
    ) -> ContextPacket:
        """What changed about ``subject`` between two Ledger transactions or two world instants."""
        return self.query(
            diff_query(
                subject,
                before,
                after,
                include_inferred=include_inferred,
                as_of=as_of,
                budget=budget,
            )
        )

    def hydrate(
        self, evidence: EvidenceRef | EvidenceItem, *, as_of: int | None = None
    ) -> Resolution:
        """What the Ledger knows about a cited source at ``as_of`` (``None``: head)."""
        ref, tx = _evidence(evidence), _transaction(as_of)
        return self._call(lambda: _resolved(ref, self._engine.hydrate(ref, as_of=tx)))

    def plan(self, question: str, *, as_of: AsOf = HEAD) -> PlannedQuery:
        """The planner's typed query for ``question``: inference, shown, never an answer."""
        planner, text = _planner(self._planner), _question(question)
        return _planned(lambda: planner.plan(text, as_of))

    def choose(self, planned: PlannedQuery, mention: str, declared_id: str) -> PlannedQuery:
        """``planned`` with one ambiguous name settled by the caller's choice (no model call)."""
        planner = _planner(self._planner)
        return _planned(lambda: planner.choose(planned, mention, declared_id))

    def ask(self, question: str, *, as_of: AsOf = HEAD) -> Asked:
        """Plan ``question``; when the plan is ready, run its query. The plan comes back
        either way, beside the packet (``None`` when the plan needs a choice or input)."""
        planned = self.plan(question, as_of=as_of)
        if not planned.executable or planned.query is None:
            return Asked(planned, None)
        return Asked(planned, self.query(planned.query))

    def entities(
        self, kind: str | None = None, *, as_of: int | None = None, include_inferred: bool
    ) -> tuple[Entity, ...]:
        """The declared identities current at ``as_of`` (``None``: head), optionally of one
        kind; names only inferred claims mention need ``include_inferred``."""
        planner, tx, flag = _planner(self._planner), _transaction(as_of), _flag(include_inferred)
        return _planned(lambda: planner.entities(kind, as_of=tx, include_inferred=flag))

    def find(
        self, text: str, *, as_of: int | None = None, include_inferred: bool
    ) -> tuple[Mention, ...]:
        """The declared names in ``text`` with every candidate (never a choice)."""
        planner, words = _planner(self._planner), _question(text)
        tx, flag = _transaction(as_of), _flag(include_inferred)
        return _planned(lambda: planner.find(words, as_of=tx, include_inferred=flag))

    def conflicts(
        self, *, as_of: int | None = None, include_inferred: bool = False
    ) -> tuple[str, ...]:
        """Identifiers the planner's resolver declines to offer as names."""
        if self._planner is None:
            return ()
        planner = self._planner
        return _planned(lambda: planner.conflicts(as_of=as_of, include_inferred=include_inferred))

    def __repr__(self) -> str:
        return f"Client({self._engine!r})"


def _sync(engine: Engine | AsyncEngine) -> Engine:
    if any(inspect.iscoroutinefunction(getattr(engine, m, None)) for m in ("query", "hydrate")):
        raise SdkError(ErrorCode.INVALID_ARGUMENT, "Client needs a sync engine; use AsyncClient")
    return engine  # type: ignore[return-value]


# --- Async --------------------------------------------------------------------------------------


class AsyncClient:
    """The same calls, awaitable. ``target`` is an engine URL, an ``AsyncEngine`` or an ``Engine``
    (a sync engine runs in a worker thread)."""

    def __init__(
        self,
        target: str | Engine | AsyncEngine,
        *,
        token: str | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        planner: Planner | None = None,
    ) -> None:
        resolved = _split_target(target, token)
        self._planner = _checked_planner(planner)
        if isinstance(resolved, str):
            self._engine: AsyncEngine = to_async(HttpEngine(resolved, token=token, timeout=timeout))
        elif inspect.iscoroutinefunction(getattr(resolved, "query", None)):
            self._engine = resolved  # type: ignore[assignment]
        else:
            self._engine = to_async(resolved)  # type: ignore[arg-type]
        self._retry = retry or (RetryPolicy() if isinstance(resolved, str) else NO_RETRY)
        self._sleep = sleep

    @classmethod
    def local(
        cls, engine: Engine | AsyncEngine, *, retry: RetryPolicy | None = None
    ) -> AsyncClient:
        return cls(engine, retry=retry)

    async def _call(self, call: Callable[[], Awaitable[T]], *, idempotent: bool = True) -> T:
        tries = self._retry.attempts if idempotent else 1
        for attempt in range(tries):
            try:
                return await call()
            except SdkError as error:
                if not error.retryable or attempt == tries - 1:
                    raise
                await self._sleep(self._retry.pause(attempt))
            except Exception as error:
                raise _engine_error(error) from error
        raise AssertionError("unreachable")  # pragma: no cover

    async def query(self, query: Query) -> ContextPacket:
        checked = _checked_query(query)

        async def ask() -> ContextPacket:
            return _verified(checked, await self._engine.query(checked))

        return await self._call(ask)

    async def why(
        self,
        claim_id: str,
        *,
        include_inferred: bool,
        as_of: AsOf = HEAD,
        budget: Budget | None = None,
    ) -> ContextPacket:
        return await self.query(
            why_query(claim_id, include_inferred=include_inferred, as_of=as_of, budget=budget)
        )

    async def diff(
        self,
        subject: Subject,
        before: int | Instant,
        after: int | Instant,
        *,
        include_inferred: bool,
        as_of: AsOf = HEAD,
        budget: Budget | None = None,
    ) -> ContextPacket:
        return await self.query(
            diff_query(
                subject,
                before,
                after,
                include_inferred=include_inferred,
                as_of=as_of,
                budget=budget,
            )
        )

    async def hydrate(
        self, evidence: EvidenceRef | EvidenceItem, *, as_of: int | None = None
    ) -> Resolution:
        ref, tx = _evidence(evidence), _transaction(as_of)

        async def resolve() -> Resolution:
            return _resolved(ref, await self._engine.hydrate(ref, as_of=tx))

        return await self._call(resolve)

    async def plan(self, question: str, *, as_of: AsOf = HEAD) -> PlannedQuery:
        """The planner's typed query; the model call runs in a worker thread."""
        planner, text = _planner(self._planner), _question(question)
        return await asyncio.to_thread(_planned, lambda: planner.plan(text, as_of))

    async def choose(self, planned: PlannedQuery, mention: str, declared_id: str) -> PlannedQuery:
        planner = _planner(self._planner)
        return _planned(lambda: planner.choose(planned, mention, declared_id))

    async def ask(self, question: str, *, as_of: AsOf = HEAD) -> Asked:
        planned = await self.plan(question, as_of=as_of)
        if not planned.executable or planned.query is None:
            return Asked(planned, None)
        return Asked(planned, await self.query(planned.query))

    async def entities(
        self, kind: str | None = None, *, as_of: int | None = None, include_inferred: bool
    ) -> tuple[Entity, ...]:
        planner, tx, flag = _planner(self._planner), _transaction(as_of), _flag(include_inferred)
        return await asyncio.to_thread(
            _planned, lambda: planner.entities(kind, as_of=tx, include_inferred=flag)
        )

    async def find(
        self, text: str, *, as_of: int | None = None, include_inferred: bool
    ) -> tuple[Mention, ...]:
        planner, words = _planner(self._planner), _question(text)
        tx, flag = _transaction(as_of), _flag(include_inferred)
        return await asyncio.to_thread(
            _planned, lambda: planner.find(words, as_of=tx, include_inferred=flag)
        )

    async def conflicts(
        self, *, as_of: int | None = None, include_inferred: bool = False
    ) -> tuple[str, ...]:
        """Identifiers the planner's resolver declines to offer as names."""
        if self._planner is None:
            return ()
        planner = self._planner
        return await asyncio.to_thread(
            _planned, lambda: planner.conflicts(as_of=as_of, include_inferred=include_inferred)
        )

    def __repr__(self) -> str:
        return f"AsyncClient({self._engine!r})"
