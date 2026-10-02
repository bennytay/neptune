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

A connector's source (ADR 0067) runs the same phases. ``discover`` is the connector's listing
against the ledger of its URI; ``fingerprint`` carries every object whose revision token the ledger
recognises forward without fetching it, fetches and hashes the rest into the job's spool, and
records what the listing no longer holds as absent; the probe of an unchanged object is the one
the workspace kept. Adapters read the spooled bytes in the sandbox, verified as a local file is.

Cancellation is checked between units of work (sources and chunks) and between phases from
inspect on; the walk and its saved ledger always finish. A chunk in progress finishes and commits;
nothing in the workspace is left half-written.

The workspace is also the cache (ADR 0031): a plan, a chunk's output and a derivative (a source's
verdict on the cross-chunk laws, a stream's series file) are each reused whenever the workspace
keeps them under the key the job needs, so an unchanged source costs a hash and no adapter call.
Each miss names the rule that caused it, and the job leaves a ``CacheReport`` beside the envelope.
"""

import contextlib
import errno
import os
import platform
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import BinaryIO, Final, Protocol, TypeAlias, TypeVar

from neptune.adapters.check import check_chunk_output, check_plan
from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    Adapter,
    AdapterConfig,
    Chunk,
    ChunkOutput,
    ConfigError,
    ContractError,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    ScratchUnavailableError,
    chunk_from_json,
    configure,
)
from neptune.adapters.registry import AdapterRegistry, Candidate, SelectionStatus
from neptune.derived.grouping import Grouping, GroupingConfig, LayoutGrouper
from neptune.derived.introspection import Introspection, introspect
from neptune.derived.media import MediaIndex, index_media
from neptune.derived.temporal import ClockAlignment, align_clocks, clock_records
from neptune.discovery.external import (
    ExternalReader,
    ExternalRoot,
    ExternalSource,
    ExternalSourceError,
    Spool,
    fingerprint_external,
    listed_entries,
    open_spooled_or_listed,
)
from neptune.discovery.ignore import IgnoreError, IgnorePolicy
from neptune.discovery.layout import Layout, layout_from_scan
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
from neptune.manifest import LoadedManifest, ManifestError
from neptune.model.finding import IngestFinding
from neptune.model.ids import ContentId, ExternalObjectRef, RecordId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.package import ReceiptEnvelope, package_manifest_from_json
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.run import Stream
from neptune.model.series import SEQ, SeriesBatch
from neptune.model.source import (
    LocalPath,
    RawLocalPath,
    SourceArtifact,
    SourceLocation,
    local_location,
)
from neptune.runtime import events, explain, lineage, sandbox, wire
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
from neptune.runtime.declared import Declarations
from neptune.runtime.events import PHASES, EventSink, JobEvent, JobState, Phase
from neptune.runtime.lineage import Failure, Law, Step, failure_from_json, type_name
from neptune.runtime.plugins import Plugins
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
from neptune.store.assemble import NotDurableError, StagedPackage, amend, publish, stage
from neptune.store.package import (
    MANIFEST,
    PackageError,
    read_package,
    write_cache_report,
    write_envelope,
)
from neptune.store.series import (
    RunCheck,
    SeriesError,
    SeriesReadError,
    check_run,
    count_rows,
    read_rows,
    read_run,
)
from neptune.store.workspace import (
    Collected,
    Derivative,
    DerivativeKey,
    Held,
    LocalOnlyError,
    Workspace,
    WorkspaceError,
)
from neptune.validate import validate_package

DEFAULT_ATTEMPTS: Final = 2
ADAPTER_FAILED: Final = f"{PROBE_ID}.adapter_failed"
PROBE_AMBIGUOUS: Final = f"{PROBE_ID}.ambiguous"
# What the runtime's own reads of a source raise: opening it, its size, its head. An adapter's
# reads are the adapter's: anything but ``SourceChangedError`` from ``plan`` or ``ingest`` is its
# failure (``plan_failed``, ``chunk_failed``).
_UNREADABLE: Final = (SourceChangedError, SourceAccessError, OSError)
PROBE_RECIPE: Final = "neptune.runtime.probe/1"  # a connector's object's kept probe (ADR 0067)
PROBE_FILE: Final = "probe.json"
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
    names the job in its envelope; by default a fresh random token. ``grouping`` configures
    session grouping (ADR 0036): its gap, and the sessions the user declares, stated and set
    against the rules' readings; it is the grouping transform's config. ``ignore`` says which
    ignore rules the walk applies (ADR 0043): by default version-control internals, OS metadata
    and the root's ``.neptune-ignore``; whatever they leave unread is a finding naming the rule.
    ``manifest`` is the root's manifest, read (ADR 0047): its runs are the declared sessions (so
    ``grouping`` stays default), its adapter options join ``config`` (never the same key twice),
    and its source rules choose adapters, each set against the probe engine's observations.
    """

    attempts: int = DEFAULT_ATTEMPTS
    config: Mapping[str, Mapping[str, JsonValue]] = field(default_factory=dict)
    job: str | None = None
    isolation: Isolation = Isolation.SUBPROCESS
    limits: Limits = DEFAULT_LIMITS
    allow_degraded_sandbox: bool = False
    grouping: GroupingConfig = field(default_factory=GroupingConfig)
    ignore: IgnorePolicy = field(default_factory=IgnorePolicy)
    manifest: LoadedManifest | None = None

    def __post_init__(self) -> None:
        if self.manifest is not None and not isinstance(self.manifest, LoadedManifest):
            raise JobError(f"manifest must be a LoadedManifest, got {self.manifest!r}")
        if not isinstance(self.ignore, IgnorePolicy):
            raise JobError(f"ignore must be an IgnorePolicy, got {self.ignore!r}")
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
        if not isinstance(self.grouping, GroupingConfig):
            raise JobError(f"grouping must be a GroupingConfig, got {self.grouping!r}")


@dataclass(frozen=True)
class JobOutcome:
    """How a job ended: committed with a package, cancelled at a checkpoint without one, or
    planned (a dry run, ADR 0035): stopped after ``plan``, without one.

    ``cache`` says what the job reused and recomputed, and why (ADR 0031 §5). ``explanation`` is
    what a planned dry run found a run would do, and why (ADR 0044); ``None`` otherwise.
    """

    state: JobState
    job: str
    destination: Path | None  # None for a dry run built without one
    package: ContentId | None
    ingested: tuple[tuple[ContentId, RecordId], ...]
    findings: tuple[IngestFinding, ...]
    durations: tuple[tuple[str, float], ...]
    cache: CacheReport = field(default_factory=CacheReport)
    explanation: explain.Explanation | None = None


@dataclass
class _Source:
    """One distinct artifact seen by this job, and what became of it."""

    artifact: SourceArtifact
    location: SourceLocation  # the first location holding it, in walk (or listing) order
    adapter: Adapter | None = None
    config: AdapterConfig | None = None
    chunks: tuple[Chunk, ...] = ()
    planned: bool = False
    quarantined: list[str] = field(default_factory=list)  # the codes of its runtime findings
    replaced: ContentId | None = None  # bytes a location of it held before, if any
    plan_cache: PlanCache | None = None  # set once the job decides to plan or reuse
    hits: set[str] = field(default_factory=set)  # chunks the workspace had committed
    intact: tuple[int, ...] | None = None  # the file's state when last verified intact
    locations: list[SourceLocation] = field(default_factory=list)  # every one, walk order
    probe: SourceProbe | None = None  # what the probe engine found, once probed
    inspection: explain.Inspection | None = None  # the adapter's ``inspect``, in a dry run
    pin: explain.Pin | None = None  # the manifest rule that chose its adapter (ADR 0047)

    @property
    def content_id(self) -> ContentId:
        return self.artifact.content_id

    @property
    def key(self) -> tuple[ContentId, RecordId]:
        assert self.config is not None  # only selected sources have a key
        return (self.content_id, self.config.transform.id)


Reader: TypeAlias = LocalReader | ExternalReader


class _Origin(Protocol):
    """Where the job reads its sources' bytes: a local root, or a connector and the job's spool."""

    def reader(self, item: "_Source") -> Reader:
        """A verified reader over ``item``'s bytes; raises what opening it raises."""
        ...

    def open(self, item: "_Source") -> BinaryIO:
        """A stream over ``item``'s bytes as they are now, to see how they differ."""
        ...


class _LocalOrigin:
    def __init__(self, source: LocalSource) -> None:
        self.source = source

    def reader(self, item: "_Source") -> Reader:
        return LocalReader(self.source, item.location, item.artifact)

    def open(self, item: "_Source") -> BinaryIO:
        return self.source.open(item.location)


class _ExternalOrigin:
    def __init__(self, source: ExternalSource, spool: Spool) -> None:
        self.source, self.spool = source, spool

    def reader(self, item: "_Source") -> Reader:
        return ExternalReader(self.source, item.location, item.artifact, self.spool)

    def open(self, item: "_Source") -> BinaryIO:
        return open_spooled_or_listed(self.source, item.location, item.content_id, self.spool)


class _Opener:
    """One source's reader, opened the first time a chunk needs the source; then ``close``."""

    def __init__(self, origin: _Origin, item: _Source) -> None:
        self._origin, self._item = origin, item
        self._reader: Reader | None = None

    def open(self) -> Reader:
        """The reader. The first call opens it and raises whatever opening raises."""
        if self._reader is None:
            self._reader = self._origin.reader(self._item)
        return self._reader

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None


def _now() -> str:
    moment = datetime.now(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{moment.microsecond // 1000:03d}Z"


def _failure(raised: Raised, call: Step, result: Step, check: Step) -> Failure:
    """What an adapter call that raised failed as: at ``call`` with the exception's class, and
    the cause ``scratch_unavailable`` if that is ``ScratchUnavailableError`` (law 11); at
    ``result`` naming the type, if it returned the wrong one; at ``check`` if what it returned
    could not cross the sandbox, as the contract check that would have refused it."""
    if raised.returned is not None:
        return Failure(result, raised.error, {"returned": raised.returned})
    if raised.unencodable:
        return Failure(check, raised.error)
    if raised.contract and raised.error == ScratchUnavailableError.__name__:
        return Failure(call, raised.error, {"cause": lineage.SCRATCH_UNAVAILABLE})
    return Failure(call, raised.error)


def _hint_name(location: SourceLocation) -> str:
    if isinstance(location, ExternalObjectRef):  # advisory, as every name is: the key's last part
        return location.object_id.rpartition("/")[2]
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


def _unusable(exc: Exception) -> Exception:
    """``exc``, raised reading or writing the workspace, as the workspace's failure.

    A ``JobError`` caused by a ``WorkspaceError`` or ``ScratchError`` is the workspace's, and
    the SDK says ``workspace_unusable`` for it (ADR 0035 §6), so every workspace read and write
    that fails the job raises its ``JobError`` from this: ``exc`` itself if it already is one,
    else a ``WorkspaceError`` caused by it (an ``OSError``, a ledger or plan that does not read).
    """
    if isinstance(exc, WorkspaceError | ScratchError):
        return exc
    error = WorkspaceError(str(exc))
    error.__cause__ = exc
    return error


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
            message = f"the run of committed chunk {chunk_id} cannot be read: {exc}"
            raise JobError(message) from _unusable(exc)
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

    Build it, then ``run`` it once, or ``dry_run`` it once to see what ``run`` would ingest
    (ADR 0035); a dry run needs no destination. ``on_event`` receives every ``JobEvent`` as it
    happens; ``cancel`` is checked at every checkpoint. Problems with one source become findings
    in the package; problems with the job raise ``JobError``. ``plugins`` are the plugins the
    registry was built from (ADR 0058): the findings about those refused join the job's.

    ``root`` is a local folder or file, or an ``ExternalRoot``: a source a connector built from a
    URI (ADR 0067), whose ledger is the URI's, and which takes no manifest and no ignore rules
    (those name local paths).
    """

    def __init__(
        self,
        root: Path | ExternalRoot,
        destination: Path | None,
        workspace: Workspace,
        registry: AdapterRegistry,
        options: JobOptions | None = None,
        *,
        on_event: EventSink | None = None,
        cancel: threading.Event | None = None,
        plugins: Plugins | None = None,
    ) -> None:
        self.options = options if options is not None else JobOptions()
        self.root: Path | ExternalRoot
        self._local_root: Path | None  # the root on this host; None for a connector's source
        self._ledger_root: Path | str  # what the workspace keys the root's ledger by
        if isinstance(root, ExternalRoot):
            self.root, self._local_root, self._ledger_root = root, None, root.uri
            if self.options.manifest is not None:
                raise JobError("a manifest is read from a local root; a connector's has none")
            if self.options.ignore.patterns:
                raise JobError(
                    "ignore rules name local paths; a connector lists what its URI names"
                )
        else:
            path = Path(root)
            if not path.is_dir() and not path.is_file():  # one file is a root too (ADR 0043)
                raise JobError(f"{path} is neither a directory nor a regular file")
            self.root, self._local_root, self._ledger_root = path, path, path
        self.destination = Path(destination) if destination is not None else None
        if self.destination is not None and self.destination.exists():
            raise JobError(f"{self.destination} exists; a package is written once")
        self.workspace = workspace
        self.registry = registry
        self._declared = self._declarations(registry, self.options)
        config = self._declared.config if self._declared is not None else self.options.config
        self._configs = self._configure(registry, config)
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
        self._origin: _Origin | None = None  # set by discover
        self._sources: list[_Source] = []
        self._ingested: list[tuple[ContentId, RecordId]] = []
        self._staged: StagedPackage | None = None
        self._calls: dict[str, int] = {"ingest": 0, "plan": 0, "probe": 0}
        self._plugins = plugins if plugins is not None else Plugins()
        self._engine = ProbeEngine(
            registry,
            distributions={
                adapter.descriptor.id: f"{adapter.origin.distribution} {adapter.origin.version}"
                for adapter in self._plugins.adapters
            },
        )
        self._derivatives: dict[str, DerivativeCache] = {}
        self._receipt: RecordId | None = None
        self._grouper = (
            self._declared.grouper()
            if self._declared is not None
            else LayoutGrouper(self.options.grouping)
        )
        if self._declared is not None:  # the manifest's own findings name it
            manifest = self._declared.loaded.transform
            self._producers[manifest.id] = manifest
        # The plugins the registry was built from (ADR 0058): the findings about those refused,
        # and, when any was admitted, the loader's transform naming every one, in every package.
        self._producers[self._plugins.transform.id] = self._plugins.transform
        for finding in self._plugins.findings:
            self._record(finding, self._plugins.transform)
        self._layout = Layout(())
        self._grouping: Grouping | None = None
        self._dry = False  # a dry run: stops after plan and explains (ADR 0035, 0044)
        self._inventory = explain.Inventory.of((), (), ())
        self._inspected = 0  # adapter ``inspect`` calls, in a dry run
        self._explanation: explain.Explanation | None = None
        self._published: ContentId | None = None  # the package, once renamed into place

    @staticmethod
    def _declarations(registry: AdapterRegistry, options: JobOptions) -> Declarations | None:
        """The manifest made ready for this job, checked before any work (ADR 0047 §5)."""
        if options.manifest is None:
            return None
        try:
            return Declarations.build(options.manifest, registry, options.config, options.grouping)
        except (ManifestError, ConfigError) as exc:
            raise JobError(f"the manifest cannot be used: {exc}") from exc

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
        if self.destination is None:
            raise JobError("a job that writes a package needs a destination")
        return self._execute(dry=False)

    def dry_run(self) -> JobOutcome:
        """Run ``discover``, ``fingerprint``, ``inspect`` and ``plan``, then stop (ADR 0035, 0044).

        What ``run`` would ingest: every source's selection, its plan and which of its chunks
        the workspace already holds (``JobOutcome.cache``), the findings so far, and the
        ``Explanation`` of all of it (``JobOutcome.explanation``). Each selected source is also
        given to its adapter's ``inspect``. No chunk is parsed, nothing is assembled and no
        package is written, so the outcome is ``planned`` (or ``cancelled``) with no package.
        The ledger and plans it saves are the ones ``run`` saves, so a later ``run`` reuses them;
        the sources are only read, and no package id depends on the workspace (ADR 0035 §9).
        """
        return self._execute(dry=True)

    @property
    def committed(self) -> JobOutcome | None:
        """The committed outcome once the package is in place, else ``None``.

        ``run`` returns it. It is here too for when ``on_event`` raised after the package was
        published and ``run`` propagated that exception instead: the job is ``committed`` all
        the same, since its package is in place (ADR 0035 §3).
        """
        return self._outcome(self._published) if self._published is not None else None

    def _execute(self, *, dry: bool) -> JobOutcome:
        if self.state is not JobState.PENDING:
            raise JobError("a job runs once")
        self.state = JobState.RUNNING
        self._dry = dry
        started = _now()
        try:
            with self.workspace.in_use():  # collection waits until the job is done
                self._sweep()
                package = self._phases(started, dry=dry)
        except _Cancelled:
            self._discard()
            self.state = JobState.CANCELLED
            self._emit(events.JOB_CANCELLED, {})
            return self._outcome(None)
        except Exception as exc:
            if self._published is not None:  # ``on_event`` raised once the package was in place
                self.state = JobState.COMMITTED
                raise
            self._discard()
            self.state = JobState.FAILED
            self._emit(events.JOB_FAILED, {"error": type(exc).__name__})
            if isinstance(exc, WorkspaceError):
                raise JobError(f"the workspace cannot be used: {exc}") from exc
            raise
        except BaseException:
            if self._published is not None:
                self.state = JobState.COMMITTED
                raise
            self._discard()  # the process is going down: leave nothing half-staged
            self.state = JobState.FAILED
            raise
        if package is None:  # a dry run, stopped after plan
            self.state = JobState.PLANNED
            planned = sum(1 for item in self._sources if item.planned)
            self._emit(events.JOB_PLANNED, {"sources": planned})
            return self._outcome(None)
        self.state = JobState.COMMITTED
        return self._outcome(package)

    def _sweep(self) -> None:
        """Remove what killed jobs and calls left: scratch directories and staging debris whose
        lock no live process holds (ADR 0029 §4, ADR 0033 §2). The scratch root must not overlap
        the ingest root, or the job would read its own scratch space as evidence: ``JobError``.
        """
        try:
            scratch = clear_scratch(self.workspace.scratch, ingest_root=self._local_root)
            staging = self.workspace.clear_staging()
        except ScratchError as exc:
            raise JobError(f"the workspace cannot hold scratch space: {exc}") from exc
        except OSError as exc:
            raise JobError(f"the workspace cannot be swept: {exc}") from _unusable(exc)
        self._emit(events.WORKSPACE_SWEPT, {"scratch": scratch, "staging": staging})

    def _phases(self, started: str, *, dry: bool) -> ContentId | None:
        with ExitStack() as stack:  # a connector's spool lives as long as the job
            if isinstance(self.root, ExternalRoot):
                scanned = self._scan_external(self.root, stack)
            else:
                source = self._source()
                self._origin = _LocalOrigin(source)
                entries = self._discover(source)
                scanned = self._fingerprint(source, entries)
            self._inspect()
            self._plan()
            if dry:
                self._connector_findings()
                self._explanation = self._explain_job()
                return None
            self._ingest()
            self._assemble(scanned)
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
            explanation=self._explanation,
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

    def _explain_job(self) -> explain.Explanation:
        """What a run would do, from what this dry run saw (ADR 0044): no clock, no job id."""
        assert self._grouping is not None  # inspect, which groups, ran
        descriptors = self.registry.descriptors()
        limits = None if self.options.isolation is Isolation.IN_PROCESS else self.options.limits
        sources = []
        selected: dict[str, list[ContentId]] = {adapter_id: [] for adapter_id in descriptors}
        for item in self._sources:
            plan: explain.PlanEstimate | None = None
            heavy: tuple[explain.Heavy, ...] = ()
            if item.planned and not item.quarantined:
                assert item.config is not None and item.plan_cache is not None
                left = [chunk for chunk in item.chunks if chunk.id not in item.hits]
                plan = explain.PlanEstimate(
                    item.config.transform.id,
                    item.plan_cache.rule,
                    len(item.chunks),
                    len(item.chunks) - len(left),
                    sum(chunk.cost for chunk in item.chunks),
                    sum(chunk.cost for chunk in left),
                )
                descriptor = descriptors[item.config.transform.adapter_id]
                heavy = explain.heavy_reasons(item.artifact.size, plan, descriptor, limits)
            if item.probe is None:  # quarantined before it could be probed
                status = explain.SourceStatus.UNREADABLE
            elif item.quarantined:
                status = explain.SourceStatus.QUARANTINED
            elif plan is not None:
                status = explain.SourceStatus.PLANNED
            elif item.probe.selection.status is SelectionStatus.AMBIGUOUS:
                status = explain.SourceStatus.AMBIGUOUS
            else:
                status = explain.SourceStatus.UNSUPPORTED
            adapter = item.adapter.descriptor.id if item.adapter is not None else None
            if adapter is not None and status is explain.SourceStatus.PLANNED:
                selected[adapter].append(item.content_id)
            sources.append(
                explain.SourceExplanation(
                    source=item.content_id,
                    size=item.artifact.size,
                    locations=tuple(sorted(item.locations, key=explain.order)),
                    status=status,
                    probe=item.probe,
                    adapter=adapter,
                    verdicts=(
                        explain.adapter_verdicts(item.probe, descriptors, item.pin)
                        if item.probe
                        else ()
                    ),
                    pin=item.pin,
                    inspection=item.inspection,
                    plan=plan,
                    heavy=heavy,
                    quarantined=tuple(item.quarantined),
                )
            )
        sources.sort(key=lambda s: (explain.order(s.locations[0]), s.source))
        calls: JsonObject = {
            "inspect": self._inspected,
            "plan": self._calls["plan"],
            "probe": self._calls["probe"],
        }
        whole = explain.Explanation(
            inventory=self._inventory,
            sources=tuple(sources),
            adapters=tuple(
                explain.AdapterUse(descriptors[adapter_id], tuple(sorted(selected[adapter_id])))
                for adapter_id in sorted(descriptors)
            ),
            grouping=explain.GroupingExplanation.of(self._grouping),
            work=explain.work_estimate(sources, calls),
            left_out=explain.left_out(sources, self._inventory),
            findings=tuple(sorted(self._findings.values(), key=lambda f: f.id)),
        )
        return explain.bounded(whole)  # ADR 0044 §8

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
        self, work: Callable[[], object], codec: sandbox.Codec[T], reader: Reader | None = None
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
                space = scratch_space(self.workspace.scratch, ingest_root=self._local_root)
                # The call writes beneath a directory of its own inside the locked one, so it
                # cannot remove the lock that tells a sweep the directory is in use.
                directory = stack.enter_context(space) / "call"
                directory.mkdir(mode=0o700)
            except (ScratchError, OSError) as exc:
                message = f"the workspace cannot give a call scratch space: {exc}"
                raise JobError(message) from _unusable(exc)
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
        details: dict[str, JsonValue] = {
            "location": location.to_json(),
            "reason": str(entry.reason),
        }
        if entry.rule is not None:  # left unread by an ignore rule: which one (ADR 0043)
            details["rule"] = entry.rule.to_json()
        self._emit(events.ENTRY_SKIPPED, details)

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

    def _differences(self, item: _Source) -> tuple[IngestFinding, ...] | None:
        """How the source differs now from the artifact it was hashed as: ``verify_artifact``'s
        findings (truncated, grown, changed chunks; ADR 0029 §3), empty when it is intact, and
        ``None`` when it cannot be opened.

        One pass over the file as it is now, except that a file found intact is not read again
        while its device, inode, size and change times stay the same: an adapter whose own
        window reads short fails every chunk, and every attempt, the same way.
        """
        assert self._origin is not None
        state: tuple[int, ...] | None = None
        try:
            with self._origin.open(item) as stream:
                with contextlib.suppress(OSError, ValueError):  # a connector's stream has no fd
                    info = os.fstat(stream.fileno())
                    state = (
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )
                if state is not None and state == item.intact:
                    return ()
                found = verify_artifact(stream, item.artifact)
        except _UNREADABLE:
            return None
        item.intact = None if found else state
        return found

    def _verify(self, item: _Source) -> None:
        """Record exactly what differs in a source that changed under the job.

        Nothing is said if it cannot be opened, since the finding that brought the job here
        already says the source was not read.
        """
        for finding in self._differences(item) or ():
            self._record(finding, DISCOVERY_TRANSFORM)

    def _read_short(self, item: _Source, raised: Raised, step: str, chunk: Chunk | None) -> bool:
        """Whether a call that raised is the source's short read; if so, it is recorded.

        ``LocalReader`` cannot serve a short read: a piece that is not all there fails its hash
        and raises ``SourceChangedError``. So a ``ShortReadError`` naming an intact source came
        from the adapter's own code (a window over the reader with the wrong size, a raise of its
        own), and the job checks before blaming the source (ADR 0033 §3). One that names this
        source and a range inside it, where ``verify_artifact`` finds the file no longer matches
        its artifact (or the file cannot be opened at all), is the source's: discovery's
        ``short_read`` for the unserved range, then the account, the source quarantined and never
        retried, and ``True``. Anything else is ``False``: the adapter's failure at ``step``,
        which the caller handles as any other raise (``plan_failed``; ``chunk_failed`` after the
        usual retries), naming ``ShortReadError`` as its class.
        """
        if raised.short_read is None:
            return False
        assert item.adapter is not None
        source, offset, length = raised.short_read
        if source != item.content_id or length == 0 or offset + length > item.artifact.size:
            return False
        found = self._differences(item)
        if found == ():
            return False  # intact: the adapter's reader read short, not the source
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
        for difference in found or ():
            self._record(difference, DISCOVERY_TRANSFORM)
        return True

    # --- discover ------------------------------------------------------------------------------

    def _source(self) -> LocalSource:
        """The root as a source, walked under the job's ignore rules (ADR 0043).

        The rules are the policy's and the root's ``.neptune-ignore``; one that cannot be used
        fails the job as a configuration error, before anything is walked.
        """
        root = self._local_root
        assert root is not None  # a connector's source is scanned by ``_scan_external``
        with self._enter(Phase.DISCOVER):
            try:
                rules = self.options.ignore.rules(LocalSource(root))
            except IgnoreError as exc:
                raise JobError(f"the ignore rules cannot be used: {exc}") from exc
            except OSError as exc:
                raise JobError(f"{root} cannot be read: {exc}") from exc
            return LocalSource(root, ignore=rules)

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
        """Hash every file into the root's ledger and save it; return what this scan observed.

        The workspace's ledger keeps the root's whole history, for resume and the cache (ADR 0026
        §1, ADR 0031 §3). The package lists only this scan (ADR 0035 §9): a new ledger that
        observed each location as the walk found it, holding the artifact the job reads its bytes
        as. Each revision is the first of its location's chain and nothing is absent, so what
        earlier jobs, dry runs or cancelled ingests saw never reaches a package or its receipt.
        """
        with self._enter(Phase.FINGERPRINT):
            ledger = self._load_ledger()
            result = fingerprint(source, ledger, entries)
            self._save_ledger(ledger)
            producers = result.producers  # discovery's, and the ignore rules' (ADR 0043)
            for finding in result.findings:  # what the walk saw and did not read (ADR 0029 §1)
                self._record(finding, producers[finding.transform])
            walked = {
                (e.raw_path, e.reason, e.detail) for e in entries if isinstance(e, SkippedEntry)
            }
            for entry in result.skipped:  # skipped at open, after the walk listed them
                if (entry.raw_path, entry.reason, entry.detail) not in walked:
                    self._skip(entry)
            for observation in result.observations:
                if not isinstance(observation.revision.location, LocalPath | RawLocalPath):
                    location = observation.revision.location
                    raise JobError(f"a local scan yielded a non-local location: {location!r}")
            scanned, listed, files, summary = self._take(
                ledger, [(o.revision.location, o, True) for o in result.observations]
            )
            for absence in result.absences:
                self._emit(events.SOURCE_ABSENT, {"location": absence.location.to_json()})
            self._check_manifest(result.observations)
            # Grouping reads the revisions the package lists, so it recomputes from the package.
            self._layout = layout_from_scan(listed, result.symlinks)
            skipped = {
                (entry.raw_path, entry.reason): explain.InventorySkipped(
                    local_location(entry.raw_path), entry.reason
                )
                for entry in result.skipped
                if entry.raw_path != b"."
            }
            self._inventory = explain.Inventory.of(
                files,
                (explain.InventoryLink(link.location, link.target) for link in result.symlinks),
                skipped.values(),
            )
            self._finish(
                Phase.FINGERPRINT,
                {
                    "absences": len(result.absences),
                    "locations": len(result.observations),
                    **summary,
                },
            )
        return scanned

    def _load_ledger(self) -> SourceLedger:
        try:
            return self.workspace.load_ledger(self._ledger_root)
        except (WorkspaceError, ValueError, OSError) as exc:
            message = f"the ledger of {self._ledger_root} cannot be loaded: {exc}"
            raise JobError(message) from _unusable(exc)

    def _save_ledger(self, ledger: SourceLedger) -> None:
        try:
            self.workspace.save_ledger(self._ledger_root, ledger)
        except OSError as exc:
            message = f"the ledger of {self._ledger_root} cannot be saved: {exc}"
            raise JobError(message) from _unusable(exc)

    def _take(
        self,
        ledger: SourceLedger,
        observed: Sequence[tuple[SourceLocation, Observation, bool]],
    ) -> tuple[SourceLedger, list[Observation], list[explain.InventoryFile], dict[str, JsonValue]]:
        """This scan's sources, from its observations in ``ledger``: each with the location as
        seen now (a connector's object with the token it is read under, which its ledger revision
        may not name, ADR 0067) and whether it was hashed now (``False``: recognised by its
        token).

        Returns the ledger the package lists (ADR 0035 §9), its observations, the inventory's
        files, and the counts the phase reports.
        """
        by_content: dict[ContentId, _Source] = {}
        scanned = SourceLedger()
        listed: list[Observation] = []
        files: list[explain.InventoryFile] = []
        new_artifacts = new_revisions = 0
        replaced = _replaced(ledger, (observation for _, observation, _ in observed))
        for location, observation, hashed in observed:
            revision = observation.revision
            new_artifacts += observation.new_artifact
            new_revisions += observation.new_revision
            artifact = ledger.artifact(revision.content_id)
            if artifact is None:
                raise JobError(f"the ledger lost artifact {revision.content_id}")
            listed.append(scanned.observe(location, artifact))
            files.append(explain.InventoryFile(location, revision.content_id, artifact.size))
            if hashed:
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
            else:
                self._emit(
                    events.SOURCE_RECOGNISED,
                    {
                        "location": location.to_json(),
                        "new_token": observation.new_token,
                        "size": artifact.size,
                        "source": revision.content_id,
                    },
                )
            if revision.content_id not in by_content:
                by_content[revision.content_id] = _Source(
                    artifact, location, replaced=replaced.get(revision.content_id)
                )
            by_content[revision.content_id].locations.append(location)
        self._sources = list(by_content.values())
        summary: dict[str, JsonValue] = {
            "new_artifacts": new_artifacts,
            "new_revisions": new_revisions,
            "sources": len(self._sources),
        }
        return scanned, listed, files, summary

    # --- a connector's source (ADR 0067) -------------------------------------------------------

    def _scan_external(self, root: ExternalRoot, stack: ExitStack) -> SourceLedger:
        """``discover`` and ``fingerprint`` for a connector's source; what this scan observed.

        The listing is the connector's, against the ledger of the URI; every listed object is
        then classified here (``fingerprint_external``): carried forward by a token the ledger
        knows for its bytes, or fetched once into the spool and hashed. What a complete listing
        no longer holds is absent. A connector that breaks the protocol fails the job: what it
        listed cannot be trusted.
        """
        with self._enter(Phase.DISCOVER):
            try:
                space = stack.enter_context(scratch_space(self.workspace.scratch, ingest_root=None))
            except (ScratchError, OSError) as exc:
                message = f"the workspace cannot hold the job's spool: {exc}"
                raise JobError(message) from _unusable(exc)
            spool = Spool(space)
            self._origin = _ExternalOrigin(root.source, spool)
            ledger = self._load_ledger()
            discovery = self._connector(root, "list", lambda: root.source.discover(ledger))
            entries = self._connector(root, "list", lambda: listed_entries(root.source, discovery))
            self._finish(Phase.DISCOVER, {"files": len(entries), "skipped": 0, "symlinks": 0})
        with self._enter(Phase.FINGERPRINT):
            result = self._connector(
                root, "fetch", lambda: fingerprint_external(root.source, ledger, discovery, spool)
            )
            self._save_ledger(ledger)
            for finding in result.findings:  # what could not be fetched, or changed size
                self._record(finding, DISCOVERY_TRANSFORM)
            for location in result.unread:
                details: JsonObject = {"location": location.to_json(), "reason": "unreadable"}
                self._emit(events.ENTRY_SKIPPED, details)
            self._connector_findings()
            scanned, _, files, summary = self._take(
                ledger, [(item.location, item.observation, item.fetched) for item in result.listed]
            )
            for absence in result.absences:
                self._emit(events.SOURCE_ABSENT, {"location": absence.location.to_json()})
            self._inventory = explain.Inventory.of(files, (), ())
            recognised = sum(not item.fetched for item in result.listed)
            self._finish(
                Phase.FINGERPRINT,
                {
                    "absences": len(result.absences),
                    "complete": discovery.complete,
                    "locations": len(result.listed),
                    "recognised": recognised,
                    **summary,
                },
            )
        return scanned

    def _connector(self, root: ExternalRoot, step: str, call: Callable[[], _T]) -> _T:
        """``call``, which runs the connector's code: a local-only refusal, a broken protocol or
        anything else it raises fails the job, naming the connector and the exception's class."""
        try:
            return call()
        except LocalOnlyError as exc:
            raise JobError(f"connector {root.connector} needs the network: {exc}") from exc
        except ExternalSourceError as exc:
            raise JobError(f"connector {root.connector} broke the Source protocol: {exc}") from exc
        except (ScratchError, WorkspaceError) as exc:
            raise JobError(f"the workspace cannot hold the job's spool: {exc}") from exc
        except Exception as exc:
            message = (
                f"connector {root.connector} failed to {step} {root.uri}: {type(exc).__name__}"
            )
            raise JobError(message) from exc

    def _connector_findings(self) -> None:
        """Record every finding the connector has made so far, under its transform."""
        if not isinstance(self.root, ExternalRoot):
            return
        root = self.root
        transform, found = self._connector(
            root, "report on", lambda: (root.source.transform, root.source.findings())
        )
        if not isinstance(transform, TransformRecord):
            raise JobError(f"connector {root.connector}'s transform is no TransformRecord")
        for finding in found:
            if not isinstance(finding, IngestFinding) or finding.transform != transform.id:
                raise JobError(f"connector {root.connector} reported a finding not its own")
            self._record(finding, transform)

    def _check_manifest(self, observations: Iterable[Observation]) -> None:
        """The manifest the job was given is the one this scan hashed: same place, same bytes."""
        if self._declared is None:
            return
        loaded = self._declared.loaded
        seen = {o.revision.location: o.revision.content_id for o in observations}
        if loaded.location not in seen:
            problem = f"the manifest {loaded.location.path} was not read by the walk (ignored?)"
            raise JobError(problem) from ManifestError(problem)
        if seen[loaded.location] != loaded.content_id:
            raise JobError(f"the manifest {loaded.location.path} changed while the job read it")

    # --- inspect -------------------------------------------------------------------------------

    def _probe(self, item: _Source, reader: Reader, head: bytes) -> tuple[SourceProbe, bool] | None:
        """The probe engine over one source, in one sandboxed call (ADR 0027, ADR 0033 §1).

        Every adapter's probe and the container inspection run in the child; its reply is read
        back strictly (``ProbeEngine.source_probe_from_json``). If the call dies, hits a limit,
        raises or replies with anything but what the engine writes, each adapter is asked again
        in a call of its own, so the one that fails is named, and a container is left unopened
        (``inspection_failed``). ``None`` once the source is quarantined: it changed under the
        probe. The engine's findings are recorded under its transform. The bool says whether the
        one call returned: only such a probe is kept for a connector's object (ADR 0067).
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
        self._probed(item, probed)
        return probed, isinstance(outcome, Returned)

    def _probed(self, item: _Source, probed: SourceProbe) -> None:
        """Record the probe engine's findings about ``item``, and each adapter that failed."""
        whole = EvidenceRef(item.content_id, (ByteRange(0, item.artifact.size),))
        for finding in probed.findings:
            self._record(finding, self._engine.transform)
            if finding.code == ADAPTER_FAILED and finding.subject == whole:
                cause = {k: v for k, v in finding.details.items() if k != "version"}
                self._emit(events.PROBE_FAILED, {"source": item.content_id, **cause})

    def _probe_key(self, item: _Source) -> DerivativeKey:
        """What a connector's object's probe is a function of: its bytes (content id and size),
        its name, the probe engine's policy, and every registered adapter as described, plugin
        distributions included (ADR 0067)."""
        adapters: list[JsonValue] = [
            {
                "descriptor": canonical_json.dumps(descriptor.to_json()).decode("utf-8"),
                "id": adapter_id,
            }
            for adapter_id, descriptor in sorted(self.registry.descriptors().items())
        ]
        distributions: JsonObject = dict(sorted(self._engine.distributions.items()))
        inputs: JsonObject = {
            "adapters": adapters,
            "distributions": distributions,
            "engine": self._engine.transform.id,
            "name": _hint_name(item.location),
            "size": item.artifact.size,
        }
        return DerivativeKey(PROBE_RECIPE, inputs, ((item.content_id, self._engine.transform.id),))

    def _kept_probe(self, item: _Source) -> SourceProbe | None:
        """The probe the workspace kept for a connector's object, if one reads back whole."""
        key = self._probe_key(item)
        try:
            kept = self.workspace.derivative(key)
            if kept is None:
                return None
            data = canonical_json.loads(kept.read(PROBE_FILE))
            probed = self._engine.source_probe_from_json(
                data,
                source=item.content_id,
                size=item.artifact.size,
                name=_hint_name(item.location),
                head=None,
            )
        except (ValueError, OSError):  # damaged: probe again, and keep the new one
            with contextlib.suppress(ValueError, OSError):
                self.workspace.discard(key)
            return None
        self._derived(key, Held.HELD)
        return probed

    def _keep_probe(self, item: _Source, probed: SourceProbe) -> None:
        """Keep a connector's object's probe, so an unchanged object is never fetched again only
        to be probed (ADR 0067)."""
        key = self._probe_key(item)

        def build(directory: Path) -> None:
            (directory / PROBE_FILE).write_bytes(canonical_json.dumps(probed.to_json()))

        try:
            _, held = self.workspace.materialise(key, build)
        except (ValueError, OSError) as exc:
            raise JobError(f"the probe of {item.content_id} cannot be kept: {exc}") from _unusable(
                exc
            )
        self._derived(key, held)

    def _inspect(self) -> None:
        assert self._origin is not None
        external = isinstance(self.root, ExternalRoot)
        with self._enter(Phase.INSPECT):
            self._emit(events.SANDBOX_READY, self._runner.describe())
            counts = dict.fromkeys(("ambiguous", "selected", "unreadable", "unsupported"), 0)
            for item in self._sources:
                self._check_cancel()
                if external and (kept := self._kept_probe(item)) is not None:
                    self._probed(item, kept)
                    self._select(item, kept, counts)
                    if self._dry and item.adapter is not None:
                        try:
                            with self._origin.reader(item) as reader:
                                self._inspect_source(item, reader)
                        except _UNREADABLE as exc:
                            self._unreadable(item, exc)
                    continue
                try:
                    reader = self._origin.reader(item)
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
                    probing = self._probe(item, reader, head)
                    if probing is not None:
                        probed, clean = probing
                        if external and clean:
                            self._keep_probe(item, probed)
                        self._select(item, probed, counts)
                        if self._dry and item.adapter is not None:
                            self._inspect_source(item, reader)
                if probing is None:
                    counts["unreadable"] += 1
            if self._declared is not None:
                every = (
                    location
                    for item in self._sources
                    for location in item.locations
                    if isinstance(location, LocalPath | RawLocalPath)
                )
                for finding in self._declared.unmatched(every):
                    self._record(finding, self._declared.loaded.transform)
            self._group()
            self._finish(Phase.INSPECT, dict(counts))

    def _select(self, item: _Source, probed: SourceProbe, counts: dict[str, int]) -> None:
        """Apply the probe engine's selection to ``item``: its adapter and config, if one won."""
        item.probe = probed
        selection = probed.selection
        details: dict[str, JsonValue] = {
            "location": item.location.to_json(),
            "source": item.content_id,
        }
        if (pinned := self._choose(item, probed)) is not None:  # the manifest's (ADR 0047)
            counts["selected"] += 1
            details |= {
                "adapter": pinned.adapter,
                "confidence": pinned.confidence,
                "manifest": True,
                "version": pinned.version,
            }
            self._emit(events.SOURCE_SELECTED, details)
        elif selection.status is SelectionStatus.SELECTED:
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

    def _inspect_source(self, item: _Source, reader: Reader) -> None:
        """A dry run's ``inspect`` of a selected source, through the runner (ADR 0044 §3).

        What it says is the explanation's alone: a summary, or why there is none. Its findings
        are shown, never recorded, and a failure never quarantines the source, since a run never
        calls ``inspect``; only a source that changed under it is the source's problem, as for
        any call.
        """
        assert item.adapter is not None and item.config is not None
        adapter, config = item.adapter, item.config
        self._inspected += 1
        outcome = self._call(partial(adapter.inspect, reader, config), wire.INSPECT, reader)
        if isinstance(outcome, Returned):
            result: InspectResult = outcome.value
            item.inspection = explain.Inspection(result.summary, result.findings)
        elif isinstance(outcome, Raised):
            # As for ``plan``: a source that changed, or whose bytes are no longer all there, is
            # the source's problem; any other raise is the adapter's, shown (ADR 0033 §3).
            if outcome.changed:
                self._unreadable(item, SourceChangedError(item.content_id))
                return
            if self._read_short(item, outcome, "inspect", None):  # the event's step only
                return
            failure: JsonObject = {"error": outcome.error}
            item.inspection = explain.Inspection(None, (), failure)
        else:
            item.inspection = explain.Inspection(None, (), outcome.cause())

    def _choose(self, item: _Source, probed: SourceProbe) -> Candidate | None:
        """Apply the manifest's source rules to ``item``; the adapter they selected, if any."""
        if self._declared is None:
            return None
        local = [loc for loc in item.locations if isinstance(loc, LocalPath | RawLocalPath)]
        choice = self._declared.choose(item.content_id, item.artifact.size, local, probed.selection)
        for finding in choice.findings:
            self._record(finding, self._declared.loaded.transform)
        if choice.candidate is None or choice.config is None:
            return None
        if choice.answers_tie:  # the tie is answered: the manifest's finding says how
            whole = EvidenceRef(item.content_id, (ByteRange(0, item.artifact.size),))
            for finding in probed.findings:
                if finding.code == PROBE_AMBIGUOUS and finding.subject == whole:
                    self._findings.pop(finding.id, None)
        item.adapter = self.registry.get(choice.candidate.adapter)
        item.config = choice.config
        if choice.rule is not None:
            loaded = self._declared.loaded
            item.pin = explain.Pin(
                choice.candidate.adapter,
                choice.rule.pointer,
                loaded.location.path,
                loaded.content_id,
            )
        return choice.candidate

    def _group(self) -> None:
        """Stage 5, at the end of inspect so a dry run sees it too: propose sessions from this
        scan's layout (ADR 0036). Names and directories only, no adapter call; the proposals
        reach the package as derived tables and the findings under the grouping's transform."""
        self._check_cancel()
        grouping = self._grouper.propose(self._layout)
        self._producers[grouping.transform.id] = grouping.transform
        for finding in grouping.findings:
            self._record(finding, grouping.transform)
        self._grouping = grouping
        self._emit(events.SESSIONS_PROPOSED, grouping.summary())

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

    def _plan(self) -> None:
        assert self._origin is not None
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
                    ) from _unusable(exc)
                reused = stored is not None
                item.plan_cache = PlanCache(Rule.PLANNED) if reused else self._explain(item)
                if stored is None:
                    try:
                        reader = self._origin.reader(item)
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
                        ) from _unusable(exc)
                    chunks = plan.chunks
                item.chunks, item.planned = chunks, True
                held: set[str] = {c.id for c in chunks if self.workspace.committed(c.id)}
                if self._dry:  # never parsed: the report says what the workspace holds
                    item.hits = held
                done = len(held)
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

    def _make_plan(self, item: _Source, reader: Reader) -> Plan | None:
        """Call the adapter's ``plan`` through the runner and check it; ``None`` once the source
        is quarantined.

        The adapter's reads are its own: a ``SourceChangedError`` is ``source_changed``, a
        ``ShortReadError`` the source's ``short_read`` only if the source no longer matches its
        artifact, and anything else it raises, an ``OSError`` included, is ``plan_failed`` at
        ``plan``. A plan that crashes or hits a limit is not retried: a failed plan is never
        saved, so the next job plans again anyway.
        """
        assert item.adapter is not None and item.config is not None
        adapter, config = item.adapter, item.config
        self._calls["plan"] += 1
        outcome = self._call(partial(adapter.plan, reader, config), wire.PLAN, reader)
        if isinstance(outcome, Raised):
            if outcome.changed:
                self._unreadable(item, SourceChangedError(item.content_id))
            elif not self._read_short(item, outcome, Step.PLAN, None):
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

    def _ingest(self) -> None:
        assert self._origin is not None
        if Phase.PARSE not in self._started:
            self._started.add(Phase.PARSE)
            self._emit(events.PHASE_STARTED, {}, Phase.PARSE)
        committed = failed = skipped = 0
        for item in self._sources:
            if not item.planned or item.quarantined:
                continue
            opener = _Opener(self._origin, item)
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
                    reader: Reader | None = None
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

    def _parse(self, item: _Source, reader: Reader, chunk: Chunk) -> tuple[ChunkOutput, int] | None:
        """``ingest`` one chunk through the runner, up to ``attempts`` times; ``None`` once it has
        failed for good.

        A ``ContractError`` is a bug, not a fault, so it is not retried, and neither is a result
        of the wrong type; a source that changed or read short under it is reported and never
        retried; nor is a limit, which the same bytes would hit again. Any other exception (a
        ``ShortReadError`` from the adapter's own reader over an intact source included), and a
        crash, which may be the host's (an OOM killer), get the remaining attempts.
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
            if isinstance(outcome, Raised) and self._read_short(item, outcome, Step.INGEST, chunk):
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
        self, item: _Source, reader: Reader, chunk: Chunk, output: ChunkOutput
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
        self, item: _Source, reader: Reader, chunk: Chunk, output: ChunkOutput, attempt: int
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
                    message = f"chunk {chunk.id} cannot be committed: {exc}"
                    raise JobError(message) from _unusable(exc)
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
            message = f"committed chunk {chunk.id} cannot be read: {exc}"
            raise JobError(message) from _unusable(exc)

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
            raise JobError(f"{what} cannot be kept: {exc}") from _unusable(exc)
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
            message = f"the plan of {item.content_id} cannot be read: {exc}"
            raise JobError(message) from _unusable(exc)
        if stored is None:
            message = f"the plan of {item.content_id} vanished from the workspace"
            raise JobError(message) from WorkspaceError(message)
        said_something = bool(stored.findings)
        for finding in stored.findings:
            finding_ids.add(finding.id)
        for chunk in item.chunks:
            try:
                output = self.workspace.load(chunk.id)
            except (WorkspaceError, ValueError, OSError) as exc:
                message = f"committed chunk {chunk.id} cannot be read: {exc}"
                raise JobError(message) from _unusable(exc)
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

    def _assemble(self, scanned: SourceLedger) -> None:
        """Admit each source that passes the cross-chunk laws and stage the package of those,
        listing ``scanned``: this job's scan, never the workspace's history (ADR 0035 §9)."""
        with self._enter(Phase.ASSEMBLE):
            self._check_cancel()
            self._connector_findings()  # what its reads since the scan found (ADR 0067)
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
            if self._declared is not None:  # a package made under a manifest names it
                cited.add(self._declared.loaded.transform.id)
            if self._lost_guarantees:
                cited.add(self.transform.id)
            if self._plugins.loaded:  # which plugins could change this package (ADR 0058 §5)
                cited.add(self._plugins.transform.id)
            if isinstance(self.root, ExternalRoot):  # the connector that listed the sources
                connector = self.root.source.transform
                self._producers[connector.id] = connector
                cited.add(connector.id)
            derived: dict[str, Iterable[JsonObject]] | None = None
            if self._grouping is not None:  # its derived tables name its transform
                cited.add(self._grouping.transform.id)
                derived = dict(self._grouping.tables())
            streams, frames = self._streams()
            if (introspection := self._introspect(streams)) is not None:
                cited.add(introspection.transform.id)
                derived = {**(derived or {}), **introspection.tables()}
                if (media := self._index_media(streams, introspection, frames)) is not None:
                    cited.add(media.transform.id)
                    derived = {**derived, **media.tables()}
            if (clocks := self._align_clocks()) is not None:
                cited.add(clocks.transform.id)
                derived = {**(derived or {}), **clocks.tables()}
            extra = [
                *(self._producers[transform] for transform in sorted(cited)),
                *self._findings.values(),
            ]
            assert self.destination is not None  # ``run`` refuses to start without one
            try:
                self._staged = stage(
                    self.destination,
                    self.workspace,
                    scanned,
                    self._ingested,
                    extra=extra,
                    derived=derived,
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

    def _streams(self) -> tuple[list[Stream], dict[RecordId, int]]:
        """The admitted sources' streams, and each stream's series rows, counted from its runs'
        footers (no row is read)."""
        streams: list[Stream] = []
        frames: dict[RecordId, int] = {}
        try:
            for content, transform in sorted(set(self._ingested)):
                plan = self.workspace.load_plan(content, transform)
                if plan is None:
                    continue  # staging refuses the package and says why
                for chunk in plan.chunks:
                    output = self.workspace.load(str(chunk["id"]))
                    streams.extend(r for r in output.records if isinstance(r, Stream))
                    for stream, run in output.runs.items():
                        frames[stream] = frames.get(stream, 0) + count_rows(run)
        except (WorkspaceError, SeriesError, ValueError, OSError) as exc:
            raise JobError(f"the package cannot be assembled: {exc}") from exc
        return streams, frames

    def _introspect(self, streams: list[Stream]) -> Introspection | None:
        """Stage 9a, before the package is staged: read the admitted sources' streams' declared
        definitions into layouts and infer what each stream carries (ADR 0049). Only cited byte
        ranges are read, each through a verified reader; no message is decoded and no adapter
        called. A package with no stream gets no introspection, so no tables and no transform."""
        if not streams:
            return None
        items = {item.content_id: item for item in self._sources}
        readers: dict[ContentId, Reader] = {}

        def read(ref: EvidenceRef) -> bytes | None:
            item = items.get(ref.source) if isinstance(ref.source, str) else None
            step = ref.locator[0]
            if item is None or self._origin is None or not isinstance(step, ByteRange):
                return None
            try:
                if item.content_id not in readers:
                    readers[item.content_id] = self._origin.reader(item)
                return readers[item.content_id].read(step.offset, step.length)
            except (SourceChangedError, SourceAccessError, OSError, ValueError):
                return None

        try:
            found = introspect(streams, read)
        finally:
            for reader in readers.values():
                reader.close()
        self._producers[found.transform.id] = found.transform
        for finding in found.findings:
            self._record(finding, found.transform)
        self._emit(events.STREAMS_INTROSPECTED, found.summary())
        return found

    def _align_clocks(self) -> ClockAlignment | None:
        """Stage 9c, before the package is staged: relate the admitted sources' clocks (ADR 0060).
        Reads only the time and value columns of the committed runs its rules name; writes no
        tick. A package with fewer than two clocks gets no alignment: no tables, no transform."""
        records: list[object] = []
        runs: dict[RecordId, list[Path]] = defaultdict(list)
        try:
            for content, transform in sorted(set(self._ingested)):
                plan = self.workspace.load_plan(content, transform)
                if plan is None:
                    continue  # staging refuses the package and says why
                for chunk in plan.chunks:
                    output = self.workspace.load(str(chunk["id"]))
                    records.extend(clock_records(output.records))  # the rest is dropped here
                    for stream, run in sorted(output.runs.items()):
                        runs[stream].append(run)
        except (WorkspaceError, ValueError, OSError) as exc:
            raise JobError(f"the package cannot be assembled: {exc}") from exc

        def rows(stream: Stream, columns: Sequence[str]) -> Iterator[Mapping[str, object]]:
            for run in runs.get(stream.id, ()):
                yield from read_rows(run, columns)

        try:
            found = align_clocks(records, rows)
        except (SeriesError, OSError) as exc:  # the runs were checked when committed
            raise JobError(f"the package cannot be assembled: {exc}") from exc
        if found is None:
            return None
        self._producers[found.transform.id] = found.transform
        for finding in found.findings:
            self._record(finding, found.transform)
        self._emit(events.CLOCKS_ALIGNED, found.summary())
        return found

    def _index_media(
        self, streams: list[Stream], introspection: Introspection, frames: dict[RecordId, int]
    ) -> MediaIndex | None:
        """Stage 9b: one ``media_stream`` line per stream carrying images, video or point clouds
        (ADR 0056). Reads the streams, their inferred semantics and their row counts: no row, no
        source byte. A package without media gets no transform and no table."""
        adapters = {
            item.key[1]: item.adapter.descriptor.id
            for item in self._sources
            if item.adapter is not None and item.config is not None
        }
        found = index_media(streams, introspection.semantics, frames, adapters)
        if found is None:
            return None
        self._producers[found.transform.id] = found.transform
        for finding in found.findings:
            self._record(finding, found.transform)
        self._emit(events.MEDIA_INDEXED, found.summary())
        return found

    # --- validate ------------------------------------------------------------------------------

    def _validate(self) -> RecordId:
        """Read the staged package back and verify it; run the integrity and data-quality rules
        over it (ADR 0054) and, if they find anything, stage it again with their findings."""
        with self._enter(Phase.VALIDATE):
            self._check_cancel()
            assert self._staged is not None
            try:
                package = read_package(self._staged.path)
                report = validate_package(package)
                receipt, identity = package.manifest.receipt, package.id
                if report.findings:  # amend verifies the whole before it moves anything
                    self._staged = amend(self._staged, package, report.records())
                    manifest = package_manifest_from_json(
                        canonical_json.loads((self._staged.path / MANIFEST).read_bytes())
                    )
                    receipt, identity = manifest.receipt, self._staged.id
            except (PackageError, SeriesError, ValueError, OSError) as exc:
                raise JobError(f"the assembled package does not verify: {exc}") from exc
            added = report.records()
            summary: dict[str, JsonValue] = {
                "findings": len(package.receipt.findings) + len(report.findings),
                "package": identity,
                "records": len(package.records) + len(added),
                "series": len(package.series),
                "validation": report.summary(),
            }
            self._emit(events.PACKAGE_VERIFIED, summary)
            self._finish(Phase.VALIDATE, summary)
        return receipt

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
                root=self.root.uri
                if isinstance(self.root, ExternalRoot)
                else str(self.root.absolute()),
                durations=self._durations_pairs(),
            )
            try:
                write_envelope(staged.path, envelope)
                write_cache_report(staged.path, self._cache_report().to_json())
                package = publish(staged)
            except NotDurableError as exc:
                self._staged = None  # renamed into place: nothing staged is left to discard
                raise JobError(
                    f"package {staged.id} is in place, but may not survive a crash: {exc}"
                ) from exc
            except (PackageError, OSError) as exc:
                raise JobError(f"the package cannot be committed: {exc}") from exc
            self._staged = None
            # Past here only ``on_event`` runs: whatever it raises, the job is committed.
            self._published = package
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
    options = options or JobOptions()
    declared = IngestJob._declarations(registry, options)
    config = declared.config if declared is not None else options.config
    live = {c.transform.id for c in IngestJob._configure(registry, config).values()}
    if declared is not None:
        live |= {c.transform.id for c in declared.configs()}
    try:
        return workspace.collect(live)
    except (WorkspaceError, ValueError, TypeError, OSError) as exc:
        raise JobError(f"the workspace cannot be collected: {exc}") from exc
