"""The ingest job: nine phases, resume from the workspace, quarantine, cancellation (ADR 0028).

::

    discover     walk the root: regular files, symlinks, entries that could not be read
    fingerprint  hash every file into the root's ledger; reconcile absences; save the ledger
    inspect      probe each distinct source's head; select and configure an adapter
    plan         plan each selected source, or reuse the plan the workspace holds; save it
    parse        run the adapter over one chunk the workspace has not committed
    normalize    check that chunk's output against the contract; commit it, whole or not at all
    assemble     admit each source whose chunks all committed and pass the cross-chunk laws;
                 build the package beside its destination
    validate     read the staged package back and verify it
    commit       write the envelope into it and rename it into place

Parse and normalize alternate per chunk; every other phase runs once. The workspace is the
checkpoint: a job killed at any point leaves a saved ledger, saved plans and committed chunks,
each written whole or not at all, and the next job over the same root and workspace reuses them
and skips committed chunk ids. There is no job file to repair.

A source's problems are findings, never a failed job: an adapter that raises on a chunk is
retried and then quarantined with the source it was reading, a file that changes under the job
is reported and left alone, and every other source still reaches the package. The job itself
fails (``JobError``) only when it cannot proceed at all: an unreadable root, a destination that
exists, a config naming an option no adapter has, a workspace or disk that will not write, a
host that cannot run the sandbox.

Every adapter call (each probe, each plan, each chunk's ``ingest``) goes through a runner
(``neptune.runtime.sandbox``, ADR 0030): by default a confined child process per call, bounded
in CPU time, wall time and memory, with no network and nowhere to write, whose crash, hang or
exhaustion becomes a finding as a raise does. Only this process writes the workspace, after
checking what the child returned, so a killed child leaves nothing behind.

Cancellation is checked between units of work (sources and chunks) and between phases from
inspect on; the walk and its saved ledger always finish. A chunk in progress finishes and commits;
nothing in the workspace is left half-written.

The workspace is also the cache (ADR 0031): a plan, a chunk's output and a derivative (a source's
verdict on the cross-chunk laws, a stream's series file) are each reused whenever the workspace
keeps them under the key the job needs, so an unchanged source costs a hash and no adapter call.
Each miss names the rule that caused it, and the job leaves a ``CacheReport`` beside the envelope.
"""

import errno
import platform
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Final, TypeVar

from neptune.adapters.check import check_chunk_output, check_plan
from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    Adapter,
    AdapterConfig,
    Chunk,
    ChunkOutput,
    ConfigError,
    ContractError,
    Plan,
    ProbeHints,
    ProbeResult,
    chunk_from_json,
    configure,
)
from neptune.adapters.registry import AdapterRegistry, SelectionStatus
from neptune.discovery.policy import DISCOVERY_TRANSFORM, SHORT_READ
from neptune.discovery.probe import PROBE_ID, ProbeEngine, SourceProbe
from neptune.discovery.reader import LocalReader, SourceChangedError
from neptune.discovery.scan import fingerprint
from neptune.discovery.scratch import ScratchError, clear_scratch, scratch_space
from neptune.discovery.source import (
    LocalSource,
    SkippedEntry,
    SkipReason,
    SourceAccessError,
    SourceEntry,
    SymlinkEntry,
    WalkEntry,
)
from neptune.discovery.verify import short_read_finding, verify_artifact
from neptune.identity import canonical_json
from neptune.identity.revisions import Observation, SourceLedger
from neptune.model.finding import IngestFinding
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.package import ReceiptEnvelope
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.run import Stream
from neptune.model.series import SEQ, SeriesBatch
from neptune.model.source import (
    LocalPath,
    RawLocalPath,
    SourceArtifact,
    local_location,
)
from neptune.runtime import events, lineage, sandbox, wire
from neptune.runtime.cache import (
    VERDICT_FILE,
    CacheReport,
    Calls,
    ChunkCache,
    DerivativeCache,
    PlanCache,
    Rule,
    SourceCache,
    admission_key,
    chunk_laws_key,
    explain_plan,
)
from neptune.runtime.events import PHASES, EventSink, JobEvent, JobState, Phase
from neptune.runtime.lineage import Failure, Law, Step, failure_from_json, type_name
from neptune.runtime.sandbox import (
    DEFAULT_LIMITS,
    Crashed,
    Exceeded,
    Isolation,
    Limits,
    Raised,
    Returned,
    SandboxError,
)
from neptune.store.assemble import StagedPackage, publish, stage
from neptune.store.package import (
    PackageError,
    read_package,
    write_cache_report,
    write_envelope,
)
from neptune.store.series import RunCheck, SeriesError, SeriesReadError, check_run, read_run
from neptune.store.workspace import (
    Collected,
    Derivative,
    DerivativeKey,
    Held,
    Workspace,
    WorkspaceError,
)

DEFAULT_ATTEMPTS: Final = 2
ADAPTER_FAILED: Final = f"{PROBE_ID}.adapter_failed"
# What the runtime's own reads of a source raise: opening it, its size, its head. An adapter's
# reads are the adapter's: anything but ``SourceChangedError`` from ``plan`` or ``ingest`` is its
# failure (``plan_failed``, ``chunk_failed``).
_UNREADABLE: Final = (SourceChangedError, SourceAccessError, OSError)
_T = TypeVar("_T")

T = TypeVar("T")


class JobError(Exception):
    """The job cannot proceed. Never about one source: a source's problems are findings."""


class _Cancelled(Exception):
    """Raised at a checkpoint when cancellation was requested; caught by ``run``."""


class _Unopened(Exception):
    """A source could not be opened to judge a kept chunk; ``cause`` says why.

    Neither an ``OSError`` nor a ``ValueError``, so it passes through the workspace's handling of
    a derivative being built to the job, which quarantines the source.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause


@dataclass(frozen=True)
class JobOptions:
    """What a job may be told besides its root, destination, workspace and adapters.

    ``attempts`` is how many times a chunk is tried before it fails. ``isolation`` is where
    adapter code runs: a confined child process per call (the default), or this process, which
    must be asked for. ``limits`` bound each sandboxed call. ``allow_degraded_sandbox`` lets a
    job run where the host's Landlock ABI is below the floor the source-immutability guarantee
    needs (ADR 0030): off by default, so such a host fails the job; on, the job runs and the
    receipt and ``sandbox_ready`` event record exactly which guarantees were lost. These are the
    runtime transform's config, so a package's runtime findings name the policy they were made
    under. ``config`` gives each adapter, by id, the option values to configure it with. ``job``
    names the job in its envelope; by default a fresh random token.
    """

    attempts: int = DEFAULT_ATTEMPTS
    config: Mapping[str, Mapping[str, JsonValue]] = field(default_factory=dict)
    job: str | None = None
    isolation: Isolation = Isolation.SUBPROCESS
    limits: Limits = DEFAULT_LIMITS
    allow_degraded_sandbox: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise JobError(f"attempts must be an integer, got {self.attempts!r}")
        if self.attempts < 1:
            raise JobError(f"attempts must be at least 1, got {self.attempts}")
        if self.job is not None and (not isinstance(self.job, str) or not self.job):
            raise JobError(f"a job name is non-empty text, got {self.job!r}")
        if not isinstance(self.isolation, Isolation):
            raise JobError(f"isolation must be an Isolation, got {self.isolation!r}")
        if not isinstance(self.limits, Limits):
            raise JobError(f"limits must be Limits, got {self.limits!r}")
        if not isinstance(self.allow_degraded_sandbox, bool):
            raise JobError(
                f"allow_degraded_sandbox must be a bool, got {self.allow_degraded_sandbox!r}"
            )
        if self.isolation is Isolation.IN_PROCESS and self.limits != DEFAULT_LIMITS:
            raise JobError("limits bound sandboxed calls; in-process calls have none to set")
        if self.isolation is Isolation.IN_PROCESS and self.allow_degraded_sandbox:
            raise JobError("allow_degraded_sandbox is a sandbox policy; in-process calls have none")


@dataclass(frozen=True)
class JobOutcome:
    """How a job ended: committed with a package, or cancelled at a checkpoint without one.

    ``cache`` says what the job reused and recomputed, and why (ADR 0031 §5).
    """

    state: JobState
    job: str
    destination: Path
    package: ContentId | None
    ingested: tuple[tuple[ContentId, RecordId], ...]
    findings: tuple[IngestFinding, ...]
    durations: tuple[tuple[str, float], ...]
    cache: CacheReport = field(default_factory=CacheReport)


@dataclass
class _Source:
    """One distinct artifact seen by this job, and what became of it."""

    artifact: SourceArtifact
    location: LocalPath | RawLocalPath  # the first location holding it, in walk order
    adapter: Adapter | None = None
    config: AdapterConfig | None = None
    chunks: tuple[Chunk, ...] = ()
    planned: bool = False
    quarantined: list[str] = field(default_factory=list)  # the codes of its runtime findings
    replaced: ContentId | None = None  # bytes a location of it held before, if any
    plan_cache: PlanCache | None = None  # set once the job decides to plan or reuse
    hits: set[str] = field(default_factory=set)  # chunks the workspace had committed

    @property
    def content_id(self) -> ContentId:
        return self.artifact.content_id

    @property
    def key(self) -> tuple[ContentId, RecordId]:
        assert self.config is not None  # only selected sources have a key
        return (self.content_id, self.config.transform.id)


class _Opener:
    """One source's reader, opened the first time a chunk needs the source; then ``close``."""

    def __init__(self, source: LocalSource, item: _Source) -> None:
        self._source, self._item = source, item
        self._reader: LocalReader | None = None

    def open(self) -> LocalReader:
        """The reader. The first call opens it and raises whatever opening raises."""
        if self._reader is None:
            self._reader = LocalReader(self._source, self._item.location, self._item.artifact)
        return self._reader

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None


def _now() -> str:
    moment = datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond // 1000:03d}Z"


def _failure(raised: Raised, call: Step, result: Step, check: Step) -> Failure:
    """What an adapter call that raised failed as: at ``call`` with the exception's class; at
    ``result`` naming the type, if it returned the wrong one; at ``check`` if what it returned
    could not cross the sandbox, as the contract check that would have refused it."""
    if raised.returned is not None:
        return Failure(result, raised.error, {"returned": raised.returned})
    if raised.unencodable:
        return Failure(check, raised.error)
    return Failure(call, raised.error)


def _hint_name(location: LocalPath | RawLocalPath) -> str:
    if isinstance(location, LocalPath):
        return location.parts[-1]
    return location.raw.rsplit(b"/", 1)[-1].decode("utf-8", "replace")


def _errno_name(exc: BaseException) -> str | None:
    """The symbolic errno (``EACCES``) of the ``OSError`` behind ``exc``, if there is one."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, OSError) and cause.errno is not None:
            return errno.errorcode.get(cause.errno)
        cause = cause.__cause__
    return None


def _chunk_series_failure(output: ChunkOutput) -> Failure | None:
    """A chunk's batches of one stream agree on their columns, and ``seq`` is unique among them.

    Both are laws ``write_run`` relies on; across chunks, ``_run_problems`` checks the rest.
    The failure, and every fact it names, is a function of the chunk's content alone, never of
    the order or batching the adapter emitted it in, which the workspace does not keep (ADR 0033
    §5): streams are judged in id order, the first stream that breaks a law is the one named,
    and within it the laws are tried in a fixed order and name the least offending value (the
    least repeated ``seq``, the least type name). So a chunk judged again from its committed form
    (``_judge``) fails exactly as it failed, or would fail, fresh. Memory is one entry per row of
    this chunk, which the chunk's output already holds.
    """

    def broken(law: Law, stream: RecordId, **facts: JsonValue) -> Failure:
        details: dict[str, JsonValue] = {"law": str(law), "stream": stream, **facts}
        return Failure(Step.CHUNK_SERIES, ContractError.__name__, details)

    by_stream: dict[RecordId, list[SeriesBatch]] = defaultdict(list)
    for batch in output.series:
        by_stream[batch.stream].append(batch)
    for stream, batches in sorted(by_stream.items()):
        if len({batch.schema() for batch in batches}) > 1:
            return broken(Law.BATCH_COLUMNS_DISAGREE, stream)
        seen: set[int] = set()
        repeated: int | None = None
        strange: str | None = None  # the least type name of a seq cell that is no integer
        for batch in batches:
            for column in batch.columns:
                if column.name != SEQ:
                    continue
                for value in column.values:
                    if isinstance(value, bool) or not isinstance(value, int):
                        name = type_name(value)
                        strange = name if strange is None else min(strange, name)
                    elif value in seen:
                        repeated = value if repeated is None else min(repeated, value)
                    else:
                        seen.add(value)
        if strange is not None:
            return broken(Law.SEQ_NOT_INTEGER, stream, type=strange)
        if repeated is not None:
            return broken(Law.SEQ_REPEATED, stream, seq=repeated)
    return None


def _replaced(
    ledger: SourceLedger, observations: Iterable[Observation]
) -> dict[ContentId, ContentId]:
    """For each source a location now holds in place of other bytes, those bytes (the least).

    What a ``source_changed`` miss names (ADR 0031 §3). Bytes returning to a location that was
    seen empty replace nothing.
    """
    held: dict[RecordId, ContentId] = {r.id: r.content_id for r in ledger.revisions()}
    replaced: dict[ContentId, ContentId] = {}
    for observation in observations:
        revision = observation.revision
        if not observation.new_revision:
            continue
        for previous in revision.supersedes:
            before = held.get(previous)  # None for an absence
            if before is not None and before != revision.content_id:
                replaced[revision.content_id] = min(
                    before, replaced.get(revision.content_id, before)
                )
    return replaced


def _run_problems(stream: Stream, runs: list[tuple[str, Path]]) -> list[JsonObject]:
    """What breaks the series laws among one stream's runs, each paired with its chunk's id.

    Each run keeps the stream's contract, all agree on their columns, and their ``seq`` ranges
    are disjoint. A run the workspace cannot read is the workspace's fault, not the source's:
    ``JobError``.
    """
    problems: list[JsonObject] = []
    checked: list[tuple[str, RunCheck]] = []
    for chunk_id, run in runs:
        try:
            checked.append((chunk_id, check_run(stream, run)))
        except (SeriesReadError, OSError) as exc:
            raise JobError(f"the run of committed chunk {chunk_id} cannot be read: {exc}") from exc
        except Exception as exc:
            problems.append(
                {
                    "chunk": chunk_id,
                    "error": type(exc).__name__,
                    "law": str(Law.RUN_BREAKS_STREAM),
                    "stream": stream.id,
                }
            )
    if checked:
        first, expected = checked[0]
        for chunk_id, result in checked[1:]:
            if result.columns != expected.columns:
                problems.append(
                    {
                        "chunks": [first, chunk_id],
                        "law": str(Law.RUN_COLUMNS_DISAGREE),
                        "stream": stream.id,
                    }
                )
    ranges = sorted((r.seq[0], r.seq[1], chunk_id) for chunk_id, r in checked if r.seq is not None)
    for (_, high, first), (low, _, second) in pairwise(ranges):
        if low <= high:
            problems.append(
                {
                    "chunks": [first, second],
                    "law": str(Law.SEQ_RANGES_OVERLAP),
                    "seq": low,
                    "stream": stream.id,
                }
            )
    return problems


class IngestJob:
    """One ingest of ``root`` into a package at ``destination``, through ``workspace``.

    Build it, then ``run`` it once. ``on_event`` receives every ``JobEvent`` as it happens;
    ``cancel`` is checked at every checkpoint. Problems with one source become findings in the
    package; problems with the job raise ``JobError``.
    """

    def __init__(
        self,
        root: Path,
        destination: Path,
        workspace: Workspace,
        registry: AdapterRegistry,
        options: JobOptions | None = None,
        *,
        on_event: EventSink | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.root = Path(root)
        self.destination = Path(destination)
        if not self.root.is_dir():
            raise JobError(f"{self.root} is not a directory")
        if self.destination.exists():
            raise JobError(f"{self.destination} exists; a package is written once")
        self.workspace = workspace
        self.registry = registry
        self.options = options if options is not None else JobOptions()
        self._configs = self._configure(registry, self.options.config)
        self._on_event: EventSink = on_event if on_event is not None else (lambda event: None)
        self._cancel = cancel
        self.job = self.options.job if self.options.job is not None else uuid.uuid4().hex
        try:
            self._runner = sandbox.runner(
                self.options.isolation,
                self.options.limits,
                allow_degraded=self.options.allow_degraded_sandbox,
            )
        except SandboxError as exc:
            raise JobError(
                f"{exc}; ingest with isolation in_process to run adapters unconfined"
            ) from exc
        # Built after the runner, so a degraded host's lost guarantees enter the lineage: a job
        # run under weaker isolation never shares a transform id with a fully sandboxed one.
        self._lost_guarantees = self._runner.lost_guarantees()
        self.transform = lineage.runtime_transform(
            self.options.attempts,
            self.options.isolation,
            self.options.limits,
            self._lost_guarantees,
        )
        self.state = JobState.PENDING
        self._phase = Phase.DISCOVER
        self._started: set[Phase] = set()
        self._entered_at: float | None = None
        self._durations: dict[Phase, float] = dict.fromkeys(PHASES, 0.0)
        self._findings: dict[RecordId, IngestFinding] = {}
        # Every producer whose findings the job records, by transform id: the runtime, and the
        # discovery and probe transforms whose findings it records for them (ADR 0033 §1, §3).
        self._producers: dict[RecordId, TransformRecord] = {self.transform.id: self.transform}
        self._local: LocalSource | None = None
        self._sources: list[_Source] = []
        self._ingested: list[tuple[ContentId, RecordId]] = []
        self._staged: StagedPackage | None = None
        self._calls: dict[str, int] = {"ingest": 0, "plan": 0, "probe": 0}
        self._engine = ProbeEngine(registry)
        self._derivatives: dict[str, DerivativeCache] = {}
        self._receipt: RecordId | None = None

    @staticmethod
    def _configure(
        registry: AdapterRegistry, config: Mapping[str, Mapping[str, JsonValue]]
    ) -> dict[str, AdapterConfig]:
        """Every registered adapter's config, resolved up front so a bad one fails before work."""
        descriptors = registry.descriptors()
        if unknown := sorted(set(config) - set(descriptors)):
            raise JobError(f"config names adapters that are not registered: {unknown}")
        try:
            return {
                adapter_id: configure(descriptor, config.get(adapter_id))
                for adapter_id, descriptor in descriptors.items()
            }
        except ConfigError as exc:
            raise JobError(str(exc)) from exc

    # --- Running -------------------------------------------------------------------------------

    def run(self) -> JobOutcome:
        """Run every phase. Returns when the package is in place or the job was cancelled."""
        if self.state is not JobState.PENDING:
            raise JobError("a job runs once")
        self.state = JobState.RUNNING
        started = _now()
        try:
            with self.workspace.in_use():  # collection waits until the job is done
                self._sweep()
                package = self._phases(started)
        except _Cancelled:
            self._discard()
            self.state = JobState.CANCELLED
            self._emit(events.JOB_CANCELLED, {})
            return self._outcome(None)
        except Exception as exc:
            self._discard()
            self.state = JobState.FAILED
            self._emit(events.JOB_FAILED, {"error": type(exc).__name__})
            if isinstance(exc, WorkspaceError):
                raise JobError(f"the workspace cannot be used: {exc}") from exc
            raise
        except BaseException:
            self._discard()  # the process is going down: leave nothing half-staged
            self.state = JobState.FAILED
            raise
        self.state = JobState.COMMITTED
        return self._outcome(package)

    def _sweep(self) -> None:
        """Remove what killed jobs and calls left: scratch directories and staging debris whose
        lock no live process holds (ADR 0029 §4, ADR 0033 §2). The scratch root must not overlap
        the ingest root, or the job would read its own scratch space as evidence: ``JobError``.
        """
        try:
            scratch = clear_scratch(self.workspace.scratch, ingest_root=self.root)
            staging = self.workspace.clear_staging()
        except ScratchError as exc:
            raise JobError(f"the workspace cannot hold scratch space: {exc}") from exc
        except OSError as exc:
            raise JobError(f"the workspace cannot be swept: {exc}") from exc
        self._emit(events.WORKSPACE_SWEPT, {"scratch": scratch, "staging": staging})

    def _phases(self, started: str) -> ContentId:
        source = self._local = LocalSource(self.root)
        entries = self._discover(source)
        ledger = self._fingerprint(source, entries)
        self._inspect(source)
        self._plan(source)
        self._ingest(source)
        self._assemble(ledger)
        receipt = self._validate()
        return self._commit(receipt, started)

    def _outcome(self, package: ContentId | None) -> JobOutcome:
        return JobOutcome(
            state=self.state,
            job=self.job,
            destination=self.destination,
            package=package,
            ingested=tuple(sorted(self._ingested)),
            findings=tuple(sorted(self._findings.values(), key=lambda f: f.id)),
            durations=self._durations_pairs(),
            cache=self._cache_report(),
        )

    def _cache_report(self) -> CacheReport:
        """What this job reused and recomputed so far, and why (ADR 0031 §5)."""
        sources = []
        for item in self._sources:
            if item.config is None or item.plan_cache is None:
                continue  # never selected, or never reached the plan phase
            plan, transform = item.plan_cache, item.config.transform
            chunks = tuple(
                ChunkCache(chunk.id, Rule.COMMITTED if chunk.id in item.hits else plan.chunk_miss)
                for chunk in item.chunks
            )
            sources.append(
                SourceCache(
                    source=item.content_id,
                    transform=transform.id,
                    adapter=transform.adapter_id,
                    adapter_version=transform.adapter_version,
                    config_hash=transform.config_hash,
                    plan=plan,
                    chunks=chunks,
                )
            )
        return CacheReport(
            sources=tuple(sorted(sources, key=lambda s: (s.source, s.transform))),
            derivatives=tuple(self._derivatives[key] for key in sorted(self._derivatives)),
            calls=Calls(**self._calls),
            receipt=self._receipt,
        )

    # --- Phases, events, checkpoints -----------------------------------------------------------

    @contextmanager
    def _enter(self, phase: Phase) -> Iterator[None]:
        """Time work in ``phase``; its first entry emits ``phase_started``."""
        self._phase = phase
        if phase not in self._started:
            self._started.add(phase)
            self._emit(events.PHASE_STARTED, {})
        self._entered_at = time.perf_counter()
        try:
            yield
        finally:
            self._durations[phase] += time.perf_counter() - self._entered_at
            self._entered_at = None

    def _finish(self, phase: Phase, details: JsonObject) -> None:
        """The job moves past ``phase``: emit its summary (starting it first if it had no work)."""
        if phase not in self._started:
            self._started.add(phase)
            self._emit(events.PHASE_STARTED, {}, phase)
        self._emit(events.PHASE_FINISHED, details, phase)

    def _emit(self, kind: str, details: JsonObject, phase: Phase | None = None) -> None:
        self._on_event(JobEvent(kind, phase if phase is not None else self._phase, details))

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel.is_set():
            raise _Cancelled()

    def _durations_pairs(self) -> tuple[tuple[str, float], ...]:
        current = dict(self._durations)
        if self._entered_at is not None:  # the phase in progress counts up to now
            current[self._phase] += time.perf_counter() - self._entered_at
        return tuple(sorted((str(phase), seconds) for phase, seconds in current.items()))

    def _discard(self) -> None:
        if self._staged is not None:
            self._staged.discard()
            self._staged = None

    def _call(
        self, work: Callable[[], object], codec: sandbox.Codec[T], reader: LocalReader | None = None
    ) -> Returned[T] | Raised | Crashed | Exceeded:
        """One adapter call through the runner; a sandbox that stops working fails the job.

        ``work`` returns the adapter's word, unchecked: the runner checks its type. A call that
        reads the source (``plan``, ``ingest``) is given a fresh scratch directory under the
        workspace, removed when it returns, whatever became of the call (ADR 0033 §2).
        """
        if reader is None:
            return self._run(work, codec, (), None)
        with ExitStack() as stack:
            try:
                space = scratch_space(self.workspace.scratch, ingest_root=self.root)
                # The call writes beneath a directory of its own inside the locked one, so it
                # cannot remove the lock that tells a sweep the directory is in use.
                directory = stack.enter_context(space) / "call"
                directory.mkdir(mode=0o700)
            except (ScratchError, OSError) as exc:
                raise JobError(f"the workspace cannot give a call scratch space: {exc}") from exc
            return self._run(work, codec, (reader.fileno(),), directory)

    def _run(
        self,
        work: Callable[[], object],
        codec: sandbox.Codec[T],
        keep: tuple[int, ...],
        scratch: Path | None,
    ) -> Returned[T] | Raised | Crashed | Exceeded:
        try:
            return self._runner.call(work, codec, keep, scratch)
        except SandboxError as exc:
            raise JobError(str(exc)) from exc

    # --- Findings ------------------------------------------------------------------------------

    def _record(self, finding: IngestFinding, producer: TransformRecord | None = None) -> None:
        """Keep ``finding`` for the package; ``producer`` is its transform if not the runtime's."""
        if producer is not None:
            if producer.id != finding.transform:
                raise ValueError(f"finding {finding.id} is not {producer.id}'s")
            self._producers[producer.id] = producer
        elif finding.transform != self.transform.id:
            raise ValueError(f"finding {finding.id} names a producer the job does not know")
        self._findings[finding.id] = finding

    def _quarantine(self, source: _Source, finding: IngestFinding) -> None:
        """A runtime finding about ``source``: it leaves this package."""
        self._record(finding)
        source.quarantined.append(finding.code)

    def _skip(self, entry: SkippedEntry) -> None:
        """A walk entry that was not read: discovery's finding says why; this is its event."""
        location = local_location(entry.raw_path)
        self._emit(
            events.ENTRY_SKIPPED, {"location": location.to_json(), "reason": str(entry.reason)}
        )

    def _unreadable(self, source: _Source, exc: Exception) -> None:
        """A source could not be read when the job came to it: changed, refused, or an I/O error.

        The finding names the reason and the symbolic errno, never the error's text, which can
        hold a path.
        """
        location = source.location.to_json()
        if isinstance(exc, SourceChangedError):
            finding = lineage.source_changed(self.transform, source.location, source.content_id)
            self._emit(events.SOURCE_CHANGED, {"location": location, "source": source.content_id})
            self._verify(source)
        else:
            reason = exc.reason if isinstance(exc, SourceAccessError) else SkipReason.UNREADABLE
            finding = lineage.source_unreadable(
                self.transform, source.location, reason, _errno_name(exc)
            )
            self._emit(
                events.SOURCE_UNREADABLE,
                {"location": location, "reason": str(reason), "source": source.content_id},
            )
        self._quarantine(source, finding)

    def _verify(self, item: _Source) -> None:
        """Re-read a source that changed or read short against its artifact, and record exactly
        what differs (``verify_artifact``: truncated, grown, changed chunks; ADR 0029 §3).

        One pass over the file as it is now; nothing is said if it cannot be opened, since the
        finding that brought the job here already says the source was not read.
        """
        assert self._local is not None
        try:
            with self._local.open(item.location) as stream:
                found = verify_artifact(stream, item.artifact)
        except _UNREADABLE:
            return
        for finding in found:
            self._record(finding, DISCOVERY_TRANSFORM)

    def _short(
        self, item: _Source, raised: Raised, step: Step, chunk: Chunk | None, attempt: int
    ) -> None:
        """A call raised ``ShortReadError``: never retried (ADR 0033 §3).

        One that names this source and a range inside it is the source's fault: discovery's
        ``short_read`` finding for the unserved range, then ``verify_artifact``'s account, and
        the source is quarantined. One that names any other reader or range is the adapter's
        failure at ``step`` (``plan_failed``, ``chunk_failed``), not retried either: the same
        bytes raise it again.
        """
        assert raised.short_read is not None and item.adapter is not None
        source, offset, length = raised.short_read
        if source != item.content_id or length == 0 or offset + length > item.artifact.size:
            failure = Failure(step, raised.error)
            if chunk is None:
                self._fail_plan(item, failure)
            else:
                self._fail_chunk(item, chunk, attempt, failure)
            return
        finding = short_read_finding(item.content_id, offset, length)
        self._record(finding, DISCOVERY_TRANSFORM)
        item.quarantined.append(finding.code)
        details: dict[str, JsonValue] = {
            "adapter": item.adapter.descriptor.id,
            "length": length,
            "offset": offset,
            "source": item.content_id,
            "step": str(step),
        }
        if chunk is not None:
            details["chunk"] = chunk.id
        self._emit(events.SOURCE_SHORT_READ, details)
        self._verify(item)

    # --- discover ------------------------------------------------------------------------------

    def _discover(self, source: LocalSource) -> tuple[WalkEntry, ...]:
        with self._enter(Phase.DISCOVER):
            entries = tuple(source.walk())
            files = symlinks = skipped = 0
            for entry in entries:
                if isinstance(entry, SourceEntry):
                    files += 1
                elif isinstance(entry, SymlinkEntry):
                    symlinks += 1
                    self._emit(
                        events.SYMLINK_RECORDED,
                        {"location": entry.location.to_json(), "target_hex": entry.target.hex()},
                    )
                else:
                    if entry.raw_path == b".":
                        raise JobError(f"{self.root} cannot be read: {entry.detail}")
                    skipped += 1
                    self._skip(entry)
            self._finish(Phase.DISCOVER, {"files": files, "skipped": skipped, "symlinks": symlinks})
        return entries

    # --- fingerprint ---------------------------------------------------------------------------

    def _fingerprint(self, source: LocalSource, entries: tuple[WalkEntry, ...]) -> SourceLedger:
        with self._enter(Phase.FINGERPRINT):
            try:
                ledger = self.workspace.load_ledger(self.root)
            except (WorkspaceError, ValueError, OSError) as exc:
                raise JobError(f"the ledger of {self.root} cannot be loaded: {exc}") from exc
            result = fingerprint(source, ledger, entries)
            try:
                self.workspace.save_ledger(self.root, ledger)
            except OSError as exc:
                raise JobError(f"the ledger of {self.root} cannot be saved: {exc}") from exc
            for finding in result.findings:  # what the walk saw and did not read (ADR 0029 §1)
                self._record(finding, result.transform)
            walked = {
                (e.raw_path, e.reason, e.detail) for e in entries if isinstance(e, SkippedEntry)
            }
            for entry in result.skipped:  # skipped at open, after the walk listed them
                if (entry.raw_path, entry.reason, entry.detail) not in walked:
                    self._skip(entry)
            by_content: dict[ContentId, _Source] = {}
            new_artifacts = new_revisions = 0
            replaced = _replaced(ledger, result.observations)
            for observation in result.observations:
                revision = observation.revision
                location = revision.location
                if not isinstance(location, LocalPath | RawLocalPath):
                    raise JobError(f"a local scan yielded a non-local location: {location!r}")
                new_artifacts += observation.new_artifact
                new_revisions += observation.new_revision
                artifact = ledger.artifact(revision.content_id)
                if artifact is None:
                    raise JobError(f"the ledger lost artifact {revision.content_id}")
                self._emit(
                    events.SOURCE_HASHED,
                    {
                        "location": location.to_json(),
                        "new_artifact": observation.new_artifact,
                        "new_revision": observation.new_revision,
                        "size": artifact.size,
                        "source": revision.content_id,
                    },
                )
                if revision.content_id not in by_content:
                    by_content[revision.content_id] = _Source(
                        artifact, location, replaced=replaced.get(revision.content_id)
                    )
            for absence in result.absences:
                self._emit(events.SOURCE_ABSENT, {"location": absence.location.to_json()})
            self._sources = list(by_content.values())
            self._finish(
                Phase.FINGERPRINT,
                {
                    "absences": len(result.absences),
                    "locations": len(result.observations),
                    "new_artifacts": new_artifacts,
                    "new_revisions": new_revisions,
                    "sources": len(self._sources),
                },
            )
        return ledger

    # --- inspect -------------------------------------------------------------------------------

    def _probe(self, item: _Source, reader: LocalReader, head: bytes) -> SourceProbe | None:
        """The probe engine over one source, in one sandboxed call (ADR 0027, ADR 0033 §1).

        Every adapter's probe and the container inspection run in the child; its reply is read
        back strictly (``ProbeEngine.source_probe_from_json``). If the call dies, hits a limit,
        raises or replies with anything but what the engine writes, each adapter is asked again
        in a call of its own, so the one that fails is named, and a container is left unopened
        (``inspection_failed``). ``None`` once the source is quarantined: it changed under the
        probe. The engine's findings are recorded under its transform.
        """
        name = _hint_name(item.location)
        size = item.artifact.size
        adapters = self.registry.adapters()
        self._calls["probe"] += len(adapters)
        codec = wire.source_probe(self._engine, item.content_id, size, name, head)
        work = partial(self._engine.probe, reader, name, head)
        outcome = self._run(work, codec, (reader.fileno(),), None)
        if isinstance(outcome, Raised) and outcome.changed:
            self._unreadable(item, SourceChangedError(item.content_id))
            return None
        if isinstance(outcome, Returned):
            probed = outcome.value
        else:
            failed = outcome.cause()
            self._emit(events.PROBE_FAILED, {"source": item.content_id, **failed})

            def ask(adapter: Adapter, head: bytes, hints: ProbeHints) -> ProbeResult | JsonObject:
                self._calls["probe"] += 1
                asked = self._run(partial(adapter.probe, head, hints), wire.PROBE, (), None)
                return asked.value if isinstance(asked, Returned) else asked.cause()

            probed = self._engine.probe_head(item.content_id, size, name, head, ask, failed)
        whole = EvidenceRef(item.content_id, (ByteRange(0, size),))
        for finding in probed.findings:
            self._record(finding, self._engine.transform)
            if finding.code == ADAPTER_FAILED and finding.subject == whole:
                cause = {k: v for k, v in finding.details.items() if k != "version"}
                self._emit(events.PROBE_FAILED, {"source": item.content_id, **cause})
        return probed

    def _inspect(self, source: LocalSource) -> None:
        with self._enter(Phase.INSPECT):
            self._emit(events.SANDBOX_READY, self._runner.describe())
            counts = dict.fromkeys(("ambiguous", "selected", "unreadable", "unsupported"), 0)
            for item in self._sources:
                self._check_cancel()
                try:
                    reader = LocalReader(source, item.location, item.artifact)
                except _UNREADABLE as exc:
                    self._unreadable(item, exc)
                    counts["unreadable"] += 1
                    continue
                with reader:
                    try:
                        head = reader.read(0, min(reader.size, PROBE_HEAD_SIZE))
                    except _UNREADABLE as exc:
                        self._unreadable(item, exc)
                        counts["unreadable"] += 1
                        continue
                    probed = self._probe(item, reader, head)
                if probed is None:
                    counts["unreadable"] += 1
                    continue
                selection = probed.selection
                details: dict[str, JsonValue] = {
                    "location": item.location.to_json(),
                    "source": item.content_id,
                }
                if selection.status is SelectionStatus.SELECTED:
                    best = selection.candidates[0]
                    item.adapter = self.registry.get(best.adapter)
                    item.config = self._configs[best.adapter]
                    counts["selected"] += 1
                    details |= {
                        "adapter": best.adapter,
                        "confidence": best.confidence,
                        "version": best.version,
                    }
                    self._emit(events.SOURCE_SELECTED, details)
                elif selection.status is SelectionStatus.AMBIGUOUS:
                    counts["ambiguous"] += 1
                    details["adapters"] = [c.adapter for c in selection.tied]
                    self._emit(events.SOURCE_AMBIGUOUS, details)
                else:
                    counts["unsupported"] += 1
                    self._emit(events.SOURCE_UNSUPPORTED, details)
            self._finish(Phase.INSPECT, dict(counts))

    # --- plan ----------------------------------------------------------------------------------

    def _explain(self, item: _Source) -> PlanCache:
        """Why ``item`` must be planned: the first invalidation rule that holds (ADR 0031 §3).

        Only the explanation depends on the source's other plans, so if they cannot be listed it
        is made from what is known without them; the job plans the source either way.
        """
        assert item.config is not None
        try:
            kept = self.workspace.transforms_of(item.content_id)
        except (WorkspaceError, ValueError, OSError):
            kept = ()
        return explain_plan(item.config.transform, kept, item.replaced)

    def _plan(self, source: LocalSource) -> None:
        with self._enter(Phase.PLAN):
            planned = chunks_total = committed_total = failed = 0
            for item in self._sources:
                if item.adapter is None or item.config is None or item.quarantined:
                    continue
                self._check_cancel()
                adapter, config = item.adapter, item.config
                try:
                    stored = self.workspace.load_plan(item.content_id, config.transform.id)
                    chunks = tuple(chunk_from_json(c) for c in stored.chunks) if stored else ()
                except (WorkspaceError, ContractError, ValueError, OSError) as exc:
                    raise JobError(
                        f"the stored plan of {item.content_id} cannot be read: {exc}"
                    ) from exc
                reused = stored is not None
                item.plan_cache = PlanCache(Rule.PLANNED) if reused else self._explain(item)
                if stored is None:
                    try:
                        reader = LocalReader(source, item.location, item.artifact)
                    except _UNREADABLE as exc:
                        self._unreadable(item, exc)
                        failed += 1
                        continue
                    with reader:
                        plan = self._make_plan(item, reader)
                    if plan is None:
                        failed += 1
                        continue
                    try:
                        self.workspace.save_plan(config.transform, plan.chunks, plan.findings)
                    except (WorkspaceError, OSError) as exc:
                        raise JobError(
                            f"the plan of {item.content_id} cannot be saved: {exc}"
                        ) from exc
                    chunks = plan.chunks
                item.chunks, item.planned = chunks, True
                done = sum(1 for chunk in chunks if self.workspace.committed(chunk.id))
                planned += 1
                chunks_total += len(chunks)
                committed_total += done
                self._emit(
                    events.SOURCE_PLANNED,
                    {
                        "adapter": adapter.descriptor.id,
                        "chunks": len(chunks),
                        "committed": done,
                        "cost": sum(chunk.cost for chunk in chunks),
                        "reused": reused,
                        "rule": str(item.plan_cache.rule),
                        "source": item.content_id,
                        "transform": config.transform.id,
                    },
                )
            self._finish(
                Phase.PLAN,
                {
                    "chunks": chunks_total,
                    "committed": committed_total,
                    "failed": failed,
                    "sources": planned,
                },
            )

    def _make_plan(self, item: _Source, reader: LocalReader) -> Plan | None:
        """Call the adapter's ``plan`` through the runner and check it; ``None`` once the source
        is quarantined.

        The adapter's reads are its own: a ``SourceChangedError`` is ``source_changed``, and
        anything else it raises, an ``OSError`` included, is ``plan_failed`` at ``plan``. A plan
        that crashes or hits a limit is not retried: a failed plan is never saved, so the next
        job plans again anyway.
        """
        assert item.adapter is not None and item.config is not None
        adapter, config = item.adapter, item.config
        self._calls["plan"] += 1
        outcome = self._call(partial(adapter.plan, reader, config), wire.PLAN, reader)
        if isinstance(outcome, Raised):
            if outcome.changed:
                self._unreadable(item, SourceChangedError(item.content_id))
            elif outcome.short_read is not None:
                self._short(item, outcome, Step.PLAN, None, 1)
            else:
                self._fail_plan(
                    item, _failure(outcome, Step.PLAN, Step.PLAN_RESULT, Step.CHECK_PLAN)
                )
            return None
        if not isinstance(outcome, Returned):
            self._stop(item, outcome, Step.PLAN, None, 1)
            return None
        plan = outcome.value
        try:
            check_plan(adapter.descriptor, reader, config, plan)
        except Exception as exc:
            self._fail_plan(item, Failure.raised(Step.CHECK_PLAN, exc))
            return None
        return plan

    def _fail_plan(self, item: _Source, failure: Failure) -> None:
        assert item.adapter is not None
        descriptor = item.adapter.descriptor
        self._quarantine(
            item,
            lineage.plan_failed(
                self.transform,
                item.content_id,
                item.artifact.size,
                descriptor.id,
                descriptor.version,
                failure,
            ),
        )
        self._emit(
            events.PLAN_FAILED,
            {
                "adapter": descriptor.id,
                "error": failure.error,
                "source": item.content_id,
                "step": str(failure.step),
            },
        )

    def _stop(
        self,
        item: _Source,
        outcome: Crashed | Exceeded,
        step: Step,
        chunk: Chunk | None,
        attempts: int,
    ) -> None:
        """A sandboxed ``plan`` or ``ingest`` died or was stopped: quarantine its source."""
        assert item.adapter is not None
        descriptor = item.adapter.descriptor
        common = (
            self.transform,
            item.content_id,
            item.artifact.size,
            descriptor.id,
            descriptor.version,
            step,
            None if chunk is None else chunk.id,
        )
        if isinstance(outcome, Crashed):
            finding = lineage.adapter_crashed(*common, outcome.cause(), attempts)
        else:
            finding = lineage.limit_exceeded(*common, str(outcome.limit), outcome.value)
        self._quarantine(item, finding)
        details: dict[str, JsonValue] = {
            "adapter": descriptor.id,
            "source": item.content_id,
            "step": str(step),
        }
        if chunk is None:
            self._emit(events.PLAN_FAILED, details | outcome.cause())
        else:
            details |= {"attempts": attempts, "chunk": chunk.id}
            self._emit(events.CHUNK_FAILED, details | outcome.cause())

    # --- parse and normalize, per chunk --------------------------------------------------------

    def _ingest(self, source: LocalSource) -> None:
        if Phase.PARSE not in self._started:
            self._started.add(Phase.PARSE)
            self._emit(events.PHASE_STARTED, {}, Phase.PARSE)
        committed = failed = skipped = 0
        for item in self._sources:
            if not item.planned or item.quarantined:
                continue
            opener = _Opener(source, item)
            try:
                for chunk in item.chunks:
                    self._phase = Phase.PARSE  # between chunks, the job is about to parse
                    self._check_cancel()
                    if self.workspace.committed(chunk.id):
                        item.hits.add(chunk.id)  # its output is reused: no adapter call
                        admitted = self._judge(item, chunk, opener)
                        if admitted is None:  # the source could not be opened to judge it
                            failed += 1
                            break
                        if not admitted:
                            failed += 1
                            continue
                        skipped += 1
                        self._emit(
                            events.CHUNK_SKIPPED,
                            {"chunk": chunk.id, "source": item.content_id},
                            Phase.PARSE,
                        )
                        continue
                    reader: LocalReader | None = None
                    with self._enter(Phase.PARSE):
                        try:
                            reader = opener.open()
                        except _UNREADABLE as exc:
                            self._unreadable(item, exc)
                    if reader is None:
                        failed += 1
                        break
                    parsed = self._parse(item, reader, chunk)
                    if parsed is None:
                        failed += 1
                        if item.quarantined and item.quarantined[-1] in (
                            lineage.SOURCE_CHANGED,
                            lineage.SOURCE_UNREADABLE,
                            SHORT_READ,
                        ):
                            break  # nothing more of this source can be read
                        continue
                    output, attempt = parsed
                    if self._normalize(item, reader, chunk, output, attempt):
                        committed += 1
                    else:
                        failed += 1
            finally:
                opener.close()
        self._finish(
            Phase.PARSE, {"chunks": committed + failed, "failed": failed, "skipped": skipped}
        )
        self._finish(Phase.NORMALIZE, {"committed": committed})

    def _parse(
        self, item: _Source, reader: LocalReader, chunk: Chunk
    ) -> tuple[ChunkOutput, int] | None:
        """``ingest`` one chunk through the runner, up to ``attempts`` times; ``None`` once it has
        failed for good.

        A ``ContractError`` is a bug, not a fault, so it is not retried, and neither is a result
        of the wrong type; a source that changed is reported and never retried; nor is a limit,
        which the same bytes would hit again. Any other exception, and a crash, which may be the
        host's (an OOM killer), get the remaining attempts.
        """
        assert item.adapter is not None and item.config is not None
        adapter, config = item.adapter, item.config
        attempts = self.options.attempts
        work = partial(adapter.ingest, reader, chunk, config)
        for attempt in range(1, attempts + 1):
            with self._enter(Phase.PARSE):
                self._calls["ingest"] += 1
                outcome = self._call(work, wire.OUTPUT, reader)
            if isinstance(outcome, Raised) and outcome.changed:
                self._unreadable(item, SourceChangedError(item.content_id))
                return None
            if isinstance(outcome, Raised) and outcome.short_read is not None:
                self._short(item, outcome, Step.INGEST, chunk, attempt)
                return None
            if not isinstance(outcome, Returned):
                retry = isinstance(outcome, Crashed) or (
                    isinstance(outcome, Raised) and not outcome.contract
                )
                if retry and attempt < attempts:
                    details: dict[str, JsonValue] = {
                        "attempt": attempt,
                        "chunk": chunk.id,
                        "source": item.content_id,
                    }
                    self._emit(events.CHUNK_RETRIED, details | outcome.cause(), Phase.PARSE)
                    continue
                if isinstance(outcome, Raised):
                    failure = _failure(outcome, Step.INGEST, Step.INGEST_RESULT, Step.CHECK_OUTPUT)
                    self._fail_chunk(item, chunk, attempt, failure)
                else:
                    self._stop(item, outcome, Step.INGEST, chunk, attempt)
                return None
            output = outcome.value
            self._emit(
                events.CHUNK_PARSED,
                {
                    "attempt": attempt,
                    "chunk": chunk.id,
                    "findings": len(output.findings),
                    "records": len(output.records),
                    "series": len(output.series),
                    "source": item.content_id,
                },
            )
            return output, attempt
        return None

    def _fail_chunk(self, item: _Source, chunk: Chunk, attempts: int, failure: Failure) -> None:
        assert item.adapter is not None
        descriptor = item.adapter.descriptor
        self._quarantine(
            item,
            lineage.chunk_failed(
                self.transform,
                item.content_id,
                item.artifact.size,
                descriptor.id,
                descriptor.version,
                chunk.id,
                attempts,
                failure,
            ),
        )
        self._emit(
            events.CHUNK_FAILED,
            {
                "adapter": descriptor.id,
                "attempts": attempts,
                "chunk": chunk.id,
                "error": failure.error,
                "source": item.content_id,
                "step": str(failure.step),
            },
        )

    def _check_output(
        self, item: _Source, reader: LocalReader, chunk: Chunk, output: ChunkOutput
    ) -> Failure | None:
        """Whatever breaks while checking one chunk's output is that chunk's failure.

        The output is the adapter's, so a check can meet anything: a record-like object without
        provenance, a ``to_json`` that raises. Each is a ``Failure`` naming the check, never an
        escape that fails the job. The same checks judge a committed output that another runtime
        version admitted (``_judge``), as the workspace keeps it: records and findings sorted by
        id, one sorted batch per stream. So a law, and the facts its failure names, depends on a
        chunk's content and never on the order or batching the adapter emitted it in.
        """
        assert item.adapter is not None and item.config is not None
        try:
            check_chunk_output(item.adapter.descriptor, reader, item.config, chunk, output)
        except Exception as exc:
            return Failure.raised(Step.CHECK_OUTPUT, exc)
        try:
            return _chunk_series_failure(output)
        except Exception as exc:
            return Failure.raised(Step.CHUNK_SERIES, exc)

    def _normalize(
        self, item: _Source, reader: LocalReader, chunk: Chunk, output: ChunkOutput, attempt: int
    ) -> bool:
        """Check one chunk's output against the contract and commit it, whole or not at all.

        Output the checks pass but the workspace still refuses fails the chunk at ``commit``;
        only an I/O error, the workspace or disk failing to write, fails the job.
        """
        with self._enter(Phase.NORMALIZE):
            failure = self._check_output(item, reader, chunk, output)
            if failure is None:
                try:
                    new = self.workspace.commit(
                        chunk,
                        output.records,
                        output.findings,
                        output.series,
                        laws=lineage.RUNTIME_VERSION,
                    )
                except OSError as exc:
                    raise JobError(f"chunk {chunk.id} cannot be committed: {exc}") from exc
                except Exception as exc:
                    failure = Failure.raised(Step.COMMIT, exc)
            if failure is not None:
                self._fail_chunk(item, chunk, attempt, failure)
                return False
            self._emit(
                events.CHUNK_COMMITTED,
                {
                    "chunk": chunk.id,
                    "findings": len(output.findings),
                    "new": new,
                    "records": len(output.records),
                    "rows": sum(batch.length for batch in output.series),
                    "source": item.content_id,
                },
            )
        return True

    def _judge(self, item: _Source, chunk: Chunk, opener: _Opener) -> bool | None:
        """Whether a committed chunk's output is admitted by this runtime's per-chunk laws.

        One this runtime version admitted is, as it stands. One another version admitted (or
        whose version is not recorded) is judged by these laws without the adapter (ADR 0031
        §2); one they refuse fails as it would in a fresh workspace: ``chunk_failed`` at the
        check it broke, after one attempt. ``None`` if the source, needed to judge it, could not
        be opened (it is quarantined as unreadable, as a fresh ingest would find it).
        """
        if self.workspace.admitted(chunk.id) == lineage.RUNTIME_VERSION:
            return True
        with self._enter(Phase.NORMALIZE):
            try:
                failure = self._chunk_laws(item, chunk, opener)
            except _Unopened as exc:
                self._unreadable(item, exc.cause)
                return None
            if failure is not None:
                self._fail_chunk(item, chunk, 1, failure)
                return False
        return True

    def _chunk_laws(self, item: _Source, chunk: Chunk, opener: _Opener) -> Failure | None:
        """This runtime's per-chunk laws over a chunk's committed output, kept as a derivative.

        A function of the chunk's id (its output never changes) and of the runtime's version, so
        it is judged once per version: the next job of this version reads the verdict back, and
        the source is opened only to judge.
        """
        assert item.config is not None
        key = chunk_laws_key(
            item.content_id, item.config.transform.id, chunk.id, lineage.RUNTIME_VERSION
        )

        def build(directory: Path) -> None:
            try:
                reader = opener.open()
            except _UNREADABLE as exc:
                raise _Unopened(exc) from exc
            failure = self._check_output(item, reader, chunk, self._committed_output(chunk))
            verdict: JsonObject = (
                {"admitted": True}
                if failure is None
                else {"admitted": False, "failure": failure.to_json()}
            )
            (directory / VERDICT_FILE).write_bytes(canonical_json.dumps(verdict))

        def read(derivative: Derivative) -> tuple[Failure | None]:
            data = canonical_json.loads(derivative.read(VERDICT_FILE))
            if data == {"admitted": True}:
                return (None,)
            if not isinstance(data, dict) or data.keys() != {"admitted", "failure"}:
                raise ValueError("not a chunk's verdict")
            if data["admitted"] is not False:
                raise ValueError("a chunk's verdict with a failure does not admit it")
            return (failure_from_json(data["failure"]),)

        return self._kept(key, build, read, f"the verdict on chunk {chunk.id}")[0]

    def _committed_output(self, chunk: Chunk) -> ChunkOutput:
        """A committed chunk's output as the laws see it: its records, findings and runs.

        Each stream's run comes back as one batch in run order. A chunk the workspace cannot
        read back is the workspace's fault, not the source's: ``JobError``.
        """
        try:
            committed = self.workspace.load(chunk.id)
            series = tuple(read_run(run) for _, run in sorted(committed.runs.items()))
            return ChunkOutput(committed.records, series, committed.findings)
        except (ValueError, OSError) as exc:  # a WorkspaceError, SeriesError or ContractError too
            raise JobError(f"committed chunk {chunk.id} cannot be read: {exc}") from exc

    def _kept(
        self,
        key: DerivativeKey,
        build: Callable[[Path], None],
        read: Callable[[Derivative], _T],
        what: str,
    ) -> _T:
        """A small derivative read back from the workspace: the one kept, or built now.

        One kept that does not read back (``read`` raises ``ValueError``) is damaged: discarded
        and built again. The workspace failing to keep it fails the job.
        """
        try:
            derivative, held = self.workspace.materialise(key, build)
            try:
                value = read(derivative)
            except ValueError:  # a WorkspaceError too: not what was kept
                self.workspace.discard(key)
                derivative, _ = self.workspace.materialise(key, build)
                held, value = Held.REBUILT, read(derivative)
        except (ValueError, OSError) as exc:
            raise JobError(f"{what} cannot be kept: {exc}") from exc
        self._derived(key, held)
        return value

    # --- assemble ------------------------------------------------------------------------------

    def _cross_chunk_problems(self, item: _Source) -> list[JsonObject]:
        """The contract's cross-chunk laws over a source's committed outputs, in bounded memory.

        Ids are held in a set (the package holds every record in memory anyway, ADR 0022). Each
        run is checked against its stream as the merge and the package will see it (columns,
        order, null rules), one batch at a time, and the runs of one stream must agree on their
        columns. ``seq`` uniqueness across chunks is proven from each run's ``seq`` range:
        ranges that do not overlap, with ``seq`` unique inside each chunk (checked at normalize),
        are unique overall. Memory is one range per chunk, never one entry per row.

        Each problem is an object naming its ``Law`` and the ids it concerns, never text.
        """
        assert item.config is not None
        problems: list[JsonObject] = []
        record_ids: set[RecordId] = set()
        finding_ids: set[RecordId] = set()
        streams: dict[RecordId, Stream] = {}
        runs: dict[RecordId, list[tuple[str, Path]]] = defaultdict(list)
        try:
            stored = self.workspace.load_plan(item.content_id, item.config.transform.id)
        except (WorkspaceError, ValueError, OSError) as exc:
            raise JobError(f"the plan of {item.content_id} cannot be read: {exc}") from exc
        if stored is None:
            raise JobError(f"the plan of {item.content_id} vanished from the workspace")
        said_something = bool(stored.findings)
        for finding in stored.findings:
            finding_ids.add(finding.id)
        for chunk in item.chunks:
            try:
                output = self.workspace.load(chunk.id)
            except (WorkspaceError, ValueError, OSError) as exc:
                raise JobError(f"committed chunk {chunk.id} cannot be read: {exc}") from exc
            said_something = said_something or bool(output.records or output.findings)
            for record in output.records:
                if record.id in record_ids:
                    problems.append(
                        {
                            "chunk": chunk.id,
                            "kind": record.kind,
                            "law": str(Law.RECORD_REPEATED),
                            "record": record.id,
                        }
                    )
                record_ids.add(record.id)
                if isinstance(record, Stream):
                    streams[record.id] = record
            for finding in output.findings:
                if finding.id in finding_ids:
                    problems.append(
                        {
                            "chunk": chunk.id,
                            "finding": finding.id,
                            "law": str(Law.FINDING_REPEATED),
                        }
                    )
                finding_ids.add(finding.id)
            for stream, run in output.runs.items():
                runs[stream].append((chunk.id, run))
        if not said_something:
            problems.append({"law": str(Law.OUTPUT_SILENT)})
        for stream in sorted(set(runs) - set(streams)):
            problems.append({"law": str(Law.STREAM_UNDECLARED), "stream": stream})
        for stream in sorted(set(streams) - set(runs)):
            problems.append({"law": str(Law.STREAM_WITHOUT_RUN), "stream": stream})
        for stream, found in sorted(runs.items()):
            if stream in streams:
                problems.extend(_run_problems(streams[stream], found))
        return problems

    def _verdict(self, item: _Source) -> list[JsonObject]:
        """The cross-chunk laws' verdict on ``item``: kept by the workspace, or computed now.

        A function of the source's chunk ids (their outputs never change) and of the runtime's
        version (which changes with the laws), so it is a derivative (ADR 0031 §4): an unchanged
        source is not read again to be admitted.
        """
        assert item.config is not None
        key = admission_key(
            item.content_id,
            item.config.transform.id,
            [chunk.id for chunk in item.chunks],
            lineage.RUNTIME_VERSION,
        )

        def build(directory: Path) -> None:
            problems: list[JsonValue] = list(self._cross_chunk_problems(item))
            (directory / VERDICT_FILE).write_bytes(canonical_json.dumps({"problems": problems}))

        def read(derivative: Derivative) -> list[JsonObject]:
            data = canonical_json.loads(derivative.read(VERDICT_FILE))
            found = data.get("problems") if isinstance(data, dict) else None
            if not isinstance(found, list):
                raise ValueError("not a source's verdict")
            problems: list[JsonObject] = [p for p in found if isinstance(p, dict)]
            if len(problems) != len(found):
                raise ValueError("a verdict's problems are objects")
            return problems

        return self._kept(key, build, read, f"the verdict on {item.content_id}")

    def _derived(self, key: DerivativeKey, held: Held) -> None:
        """Note a derivative the job read, and how the workspace came by it."""
        entry = DerivativeCache.of(key, held)
        self._derivatives[key.id] = entry
        kind = events.DERIVATIVE_REUSED if held is Held.HELD else events.DERIVATIVE_BUILT
        self._emit(kind, {"derivative": key.id, "recipe": key.recipe, "rule": str(entry.rule)})

    def _assemble(self, ledger: SourceLedger) -> None:
        with self._enter(Phase.ASSEMBLE):
            self._check_cancel()
            quarantined = 0
            for item in self._sources:
                if item.adapter is None or item.config is None:
                    continue  # never selected: nothing to admit, nothing to quarantine
                self._check_cancel()  # each source's runs are read whole: a checkpoint between
                if item.planned and not item.quarantined and (problems := self._verdict(item)):
                    self._quarantine(
                        item,
                        lineage.output_invalid(
                            self.transform,
                            item.content_id,
                            item.artifact.size,
                            item.adapter.descriptor.id,
                            problems,
                        ),
                    )
                details: dict[str, JsonValue] = {
                    "source": item.content_id,
                    "transform": item.key[1],
                }
                if item.quarantined:
                    quarantined += 1
                    details["codes"] = sorted(set(item.quarantined))
                    self._emit(events.SOURCE_QUARANTINED, details)
                else:
                    self._ingested.append(item.key)
                    self._emit(events.SOURCE_ADMITTED, details)
            # A degraded run records its runtime transform even with no findings, so the receipt
            # always names the guarantees it could not give; a sound run adds a transform only to
            # carry a finding, keeping its lineage unchanged (ADR 0030).
            cited = {finding.transform for finding in self._findings.values()}
            if self._lost_guarantees:
                cited.add(self.transform.id)
            extra = [
                *(self._producers[transform] for transform in sorted(cited)),
                *self._findings.values(),
            ]
            try:
                self._staged = stage(
                    self.destination, self.workspace, ledger, self._ingested, extra=extra
                )
            except (PackageError, WorkspaceError, SeriesError, ValueError, OSError) as exc:
                raise JobError(f"the package cannot be assembled: {exc}") from exc
            for use in self._staged.derivatives:
                self._derived(use.key, use.held)
            self._emit(
                events.PACKAGE_STAGED,
                {"package": self._staged.id, "sources": len(self._ingested)},
            )
            self._finish(
                Phase.ASSEMBLE, {"quarantined": quarantined, "sources": len(self._ingested)}
            )

    # --- validate ------------------------------------------------------------------------------

    def _validate(self) -> RecordId:
        """Read the staged package back and verify every file, id, series and the receipt."""
        with self._enter(Phase.VALIDATE):
            self._check_cancel()
            assert self._staged is not None
            try:
                package = read_package(self._staged.path)
            except (PackageError, SeriesError, ValueError, OSError) as exc:
                raise JobError(f"the assembled package does not verify: {exc}") from exc
            summary: dict[str, JsonValue] = {
                "findings": len(package.receipt.findings),
                "package": package.id,
                "records": len(package.records),
                "series": len(package.series),
            }
            self._emit(events.PACKAGE_VERIFIED, summary)
            self._finish(Phase.VALIDATE, summary)
        return package.manifest.receipt

    # --- commit --------------------------------------------------------------------------------

    def _commit(self, receipt: RecordId, started: str) -> ContentId:
        """Write the envelope and the cache report into the staged package; rename it into place."""
        with self._enter(Phase.COMMIT):
            self._check_cancel()
            staged = self._staged
            assert staged is not None
            self._receipt = receipt
            envelope = ReceiptEnvelope(
                receipt=receipt,
                job=self.job,
                started=started,
                finished=_now(),
                host=platform.node() or "unknown",
                root=str(self.root.absolute()),
                durations=self._durations_pairs(),
            )
            try:
                write_envelope(staged.path, envelope)
                write_cache_report(staged.path, self._cache_report().to_json())
                package = publish(staged)
            except (PackageError, OSError) as exc:
                raise JobError(f"the package cannot be committed: {exc}") from exc
            self._staged = None
            self._emit(events.JOB_COMMITTED, {"package": package, "sources": len(self._ingested)})
            self._finish(Phase.COMMIT, {"package": package})
        return package


def collect(
    workspace: Workspace, registry: AdapterRegistry, options: JobOptions | None = None
) -> Collected:
    """Collect ``workspace`` for jobs run with ``registry`` and ``options`` (ADR 0031 §6).

    Keeps what such a job could reuse: plans under the transforms these adapters and this config
    define, for sources some saved ledger still holds, with their chunks and derivatives.
    Removes the rest. Refused (``JobError``) while a job holds the workspace, or when the
    workspace cannot be read well enough to tell what is reachable.
    """
    configs = IngestJob._configure(registry, (options or JobOptions()).config)
    try:
        return workspace.collect({config.transform.id for config in configs.values()})
    except (WorkspaceError, ValueError, TypeError, OSError) as exc:
        raise JobError(f"the workspace cannot be collected: {exc}") from exc
