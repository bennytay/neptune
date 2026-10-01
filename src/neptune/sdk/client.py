"""The sync and async clients, the running-job handles, and the one-call shorthands (ADR 0035).

``Neptune`` and ``AsyncNeptune`` have one surface: the same constructor and the same methods with
the same parameters and results; the async ones are awaited. Both build the runtime's own
``IngestJob`` and nothing else decides what is ingested:

- ``ingest(source, destination)`` runs the whole job and returns an ``IngestResult``;
- ``dry_run(source)`` runs it up to and including ``plan``, reads the sources only and
  returns its ``Explanation`` on the result (``IngestJob.dry_run``, ADR 0044);
- ``start(source, destination)`` and ``start_dry_run(source)`` run it on a thread of its own and
  return a handle to iterate its events, wait for its result, or cancel it.

The sync ``ingest`` runs the job on the calling thread, so ``on_event`` is called there. Every
async method, and every handle, runs the same sync job on one worker thread; events cross to the
caller through a queue (to the event loop with ``call_soon_threadsafe``). Cancelling the awaiting
task sets the job's cancel event, waits for the job to stop at its next checkpoint (ADR 0028 §6),
then lets ``CancelledError`` through, so the workspace never holds half a chunk.

A job past its last checkpoint (the start of ``commit``) publishes its package whatever
interrupts the call; the interruption then propagates carrying the committed result
(``committed_result``, ADR 0035 §3), so a caller never takes a published package for a stopped job.

The SDK reads no clock and draws no random number: what reaches a package is the runtime's.
"""

import asyncio
import contextlib
import os
import queue
import re
import threading
import urllib.parse
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Final, TypeAlias

from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import Adapter, ConfigError, ContractError, configure
from neptune.adapters.registry import AdapterRegistry
from neptune.runtime import EventSink, IngestJob, JobError, JobEvent, JobOptions
from neptune.sdk.errors import (
    ConfigurationError,
    DestinationExistsError,
    InvalidDestinationError,
    InvalidSourceError,
    NetworkRefusedError,
    NothingToResumeError,
    UnsupportedError,
    WorkspaceUnusableError,
    from_job_error,
)
from neptune.sdk.result import IngestResult, attach_committed
from neptune.store.workspace import LocalOnlyError, Workspace, WorkspaceError

StrPath: TypeAlias = str | os.PathLike[str]
Adapters: TypeAlias = AdapterRegistry | Iterable[Adapter]

_URI: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_LOCAL_HOSTS: Final = ("", "localhost")
_REMOTE_SCHEMES: Final = ("http", "https")


# --- Resolving what a call names -----------------------------------------------------------------


def _require_network(workspace: Workspace, purpose: str) -> None:
    try:
        workspace.require_network(purpose)
    except LocalOnlyError as exc:
        raise NetworkRefusedError(str(exc)) from exc


def _local_root(source: StrPath, workspace: Workspace) -> Path:
    """The local directory or file ``source`` names: a path, or a ``file:`` URI on this host.

    Any other scheme names something only a connector can read over the network: refused while
    the workspace is local-only, and unsupported until a connector lands (MVL-45, MVL-46).
    """
    if isinstance(source, str) and _URI.match(source):
        parts = urllib.parse.urlsplit(source)
        scheme = parts.scheme.lower()
        if scheme != "file":
            _require_network(workspace, f"reading {scheme}:// sources")
            raise UnsupportedError(f"no connector reads {scheme}:// sources in this version")
        if parts.netloc.lower() not in _LOCAL_HOSTS:
            raise UnsupportedError(f"{source} names another host; a file URI names this one")
        if parts.query or parts.fragment or not parts.path:
            raise InvalidSourceError(f"{source} is not a file URI of a directory or a file")
        root = Path(os.fsdecode(urllib.parse.unquote_to_bytes(parts.path)))
    else:
        root = Path(source)
    if not root.exists():
        raise InvalidSourceError(f"{root} does not exist")
    if not root.is_dir() and not root.is_file():  # one regular file is a source too (ADR 0043)
        raise InvalidSourceError(f"{root} is neither a directory nor a regular file")
    return root


def _destination(destination: StrPath, root: Path) -> Path:
    """``destination``, if a package can be written there: nothing there yet, not in ``root``."""
    path = Path(destination)
    if path.exists() or path.is_symlink():
        raise DestinationExistsError(f"{path} exists; a package is written once")
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError) as exc:  # a symlink loop on the way
        raise InvalidDestinationError(f"{path} cannot be resolved: {exc}") from exc
    if inside:
        raise InvalidDestinationError(
            f"{path} is inside the source {root}; the next ingest would read it as evidence"
        )
    return path


def _workspace(workspace: Workspace | StrPath | None) -> Workspace:
    if isinstance(workspace, Workspace):
        return workspace
    try:
        return Workspace(Path(workspace) if workspace is not None else None)
    except (WorkspaceError, OSError) as exc:
        raise WorkspaceUnusableError(f"the workspace cannot be opened: {exc}") from exc


def _registry(adapters: Adapters | None) -> AdapterRegistry:
    if adapters is None:
        return default_registry()
    if isinstance(adapters, AdapterRegistry):
        return adapters
    try:
        return AdapterRegistry(adapters)
    except (ContractError, TypeError) as exc:
        raise ConfigurationError(f"the adapters cannot be registered: {exc}") from exc


def _check_config(registry: AdapterRegistry, options: JobOptions) -> None:
    """Every adapter ``options.config`` names is registered and takes the values it is given."""
    descriptors = registry.descriptors()
    if unknown := sorted(set(options.config) - set(descriptors)):
        raise ConfigurationError(f"config names adapters that are not registered: {unknown}")
    for adapter_id, values in sorted(options.config.items()):
        try:
            configure(descriptors[adapter_id], values)
        except ConfigError as exc:
            raise ConfigurationError(str(exc)) from exc


def _check_remote(remote: str | None, workspace: Workspace) -> None:
    """Remote execution: a URL, the network allowed, and a service to talk to (MVL-46)."""
    if remote is None:
        return
    parts = urllib.parse.urlsplit(remote)
    if parts.scheme.lower() not in _REMOTE_SCHEMES or not parts.netloc:
        raise ConfigurationError(f"remote must be an http(s) URL, got {remote!r}")
    _require_network(workspace, "remote execution")
    raise UnsupportedError(
        "remote execution needs Neptune's ingestion service (MVL-46), "
        "which this version does not include"
    )


def _execute(job: IngestJob, *, dry: bool) -> IngestResult:
    try:
        outcome = job.dry_run() if dry else job.run()
    except JobError as exc:
        raise from_job_error(exc, job.destination) from exc
    except BaseException as exc:  # ``on_event`` raised: after the publish, the job committed
        if (committed := job.committed) is not None:
            attach_committed(exc, IngestResult(committed))
        raise
    return IngestResult(outcome)


Build: TypeAlias = Callable[[EventSink | None, threading.Event | None], IngestJob]


# --- Handles on a job running on its own thread --------------------------------------------------


class Ingestion:
    """A job running on a thread of its own: iterate its events, wait for it, or cancel it.

    Events are queued from the moment the job starts, so iterating late misses none; iterate
    from one thread. Leaving a ``with`` block by an exception cancels the job and waits for it.
    """

    def __init__(self, build: Build, *, dry: bool, cancel: threading.Event | None) -> None:
        self._events: queue.SimpleQueue[JobEvent | None] = queue.SimpleQueue()
        self._cancel = cancel if cancel is not None else threading.Event()
        job = build(self._events.put, self._cancel)  # a bad call raises here, on this thread
        self._result: IngestResult | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._work, args=(job, dry), name="neptune-ingest", daemon=False
        )
        self._thread.start()

    def _work(self, job: IngestJob, dry: bool) -> None:
        try:
            self._result = _execute(job, dry=dry)
        except BaseException as exc:  # handed to whoever waits for the result
            self._error = exc
        finally:
            self._events.put(None)

    def __iter__(self) -> Iterator[JobEvent]:
        """Every event of the job, as it happens, until the job ends."""
        while (event := self._events.get()) is not None:
            yield event
        self._events.put(None)  # the end stays visible to a later iteration

    def cancel(self) -> None:
        """Ask the job to stop at its next checkpoint (ADR 0028 §6)."""
        self._cancel.set()

    def done(self) -> bool:
        return not self._thread.is_alive()

    def result(self, timeout: float | None = None) -> IngestResult:
        """Wait for the job; its result, or the error that ended it. ``TimeoutError`` if it is
        still running after ``timeout`` seconds."""
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise TimeoutError("the job is still running")
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    def __enter__(self) -> "Ingestion":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if error is not None:
            self.cancel()
        self._thread.join()
        if error is not None and self._result is not None:
            attach_committed(error, self._result)  # past its last checkpoint: it published


class AsyncIngestion:
    """``Ingestion`` for an event loop: ``async for`` its events, ``await`` its result.

    Built inside a running loop; events and the end reach that loop through
    ``call_soon_threadsafe``. Cancelling a task that awaits ``result`` does not stop the job:
    ``cancel`` does (``AsyncNeptune.ingest`` does both).
    """

    def __init__(self, build: Build, *, dry: bool, cancel: threading.Event | None) -> None:
        self._loop = asyncio.get_running_loop()
        self._events: asyncio.Queue[JobEvent | None] = asyncio.Queue()
        self._ended: asyncio.Future[None] = self._loop.create_future()
        self._cancel = cancel if cancel is not None else threading.Event()
        job = build(self._post, self._cancel)
        self._result: IngestResult | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._work, args=(job, dry), name="neptune-ingest", daemon=False
        )
        self._thread.start()

    def _soon(self, callback: Callable[..., None], *args: object) -> None:
        with contextlib.suppress(RuntimeError):  # the loop has closed: nobody is left to tell
            self._loop.call_soon_threadsafe(callback, *args)

    def _post(self, event: JobEvent) -> None:
        self._soon(self._events.put_nowait, event)

    def _work(self, job: IngestJob, dry: bool) -> None:
        result: IngestResult | None = None
        error: BaseException | None = None
        try:
            result = _execute(job, dry=dry)
        except BaseException as exc:  # handed to whoever awaits the result
            error = exc
        self._soon(self._end, result, error)

    def _end(self, result: IngestResult | None, error: BaseException | None) -> None:
        self._result, self._error = result, error
        self._events.put_nowait(None)
        if not self._ended.done():
            self._ended.set_result(None)

    def __aiter__(self) -> AsyncIterator[JobEvent]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[JobEvent]:
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)  # the end stays visible to a later iteration

    def cancel(self) -> None:
        """Ask the job to stop at its next checkpoint (ADR 0028 §6)."""
        self._cancel.set()

    def done(self) -> bool:
        return self._ended.done()

    async def wait(self) -> None:
        """Wait for the job to end, however it ends."""
        await asyncio.shield(self._ended)

    async def result(self) -> IngestResult:
        """Wait for the job; its result, or the error that ended it."""
        await self.wait()
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result

    def _attach_committed(self, error: BaseException) -> None:
        """If the job, now ended, committed its package, make ``error`` carry the result."""
        if self._result is not None:
            attach_committed(error, self._result)


# --- The clients ---------------------------------------------------------------------------------


class Neptune:
    """Ingest from Python: one workspace, one set of adapters, one set of runtime options.

    - ``workspace``: a ``Workspace``, a directory for one, or ``None`` for the default
      (``$NEPTUNE_HOME``, else ``$XDG_CACHE_HOME/neptune``, else ``~/.cache/neptune``). New
      workspaces are local-only.
    - ``adapters``: the adapters a job may select from, as a registry or any iterable of
      adapters; ``None`` for the ones Neptune ships (``builtin_adapters()``).
    - ``options``: the runtime's own ``JobOptions`` (attempts, isolation, limits, each adapter's
      config by id, a job name); ``None`` for the defaults, which sandbox every adapter call.
    - ``remote``: the URL of a Neptune service to run jobs on, or ``None`` to run them here. The
      network must be allowed; no service exists in this version (MVL-46), so a URL raises
      ``UnsupportedError`` once the workspace allows the network.

    Problems with the call raise a ``NeptuneError`` before anything runs; a source's problems
    are findings in the result.
    """

    def __init__(
        self,
        workspace: Workspace | StrPath | None = None,
        *,
        adapters: Adapters | None = None,
        options: JobOptions | None = None,
        remote: str | None = None,
    ) -> None:
        self._workspace = _workspace(workspace)
        self._registry = _registry(adapters)
        if options is not None and not isinstance(options, JobOptions):
            raise ConfigurationError(f"options must be JobOptions, got {options!r}")
        self._options = options if options is not None else JobOptions()
        _check_config(self._registry, self._options)
        _check_remote(remote, self._workspace)

    @property
    def workspace(self) -> Workspace:
        return self._workspace

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    @property
    def options(self) -> JobOptions:
        return self._options

    def _builder(self, source: StrPath, destination: StrPath | None, resume: bool = False) -> Build:
        """Resolve and check the call now; return what builds its job around a sink and event."""
        root = _local_root(source, self._workspace)
        target = _destination(destination, root) if destination is not None else None
        if resume and not self._workspace.has_ledger(root):
            raise NothingToResumeError(
                f"the workspace {self._workspace.home} holds no earlier work on {root}; "
                "a resume continues a job that scanned it here"
            )

        def build(on_event: EventSink | None, cancel: threading.Event | None) -> IngestJob:
            try:
                return IngestJob(
                    root,
                    target,
                    self._workspace,
                    self._registry,
                    self._options,
                    on_event=on_event,
                    cancel=cancel,
                )
            except JobError as exc:
                raise from_job_error(exc, target) from exc

        return build

    def ingest(
        self,
        source: StrPath,
        destination: StrPath,
        *,
        on_event: EventSink | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> IngestResult:
        """Ingest the folder or file ``source`` (a path or a ``file:`` URI) into a package at
        ``destination``, on this thread. ``on_event`` gets every ``JobEvent`` as it happens; an
        exception it raises stops the job and propagates. Setting ``cancel`` stops the job at its
        next checkpoint: the result is ``cancelled`` and the workspace keeps the work. Run it
        again to resume: committed chunks are reused, not redone. ``resume=True`` insists on it:
        ``NothingToResumeError`` if the workspace holds no earlier work on ``source``."""
        job = self._builder(source, destination, resume)(on_event, cancel)
        return _execute(job, dry=False)

    def dry_run(
        self,
        source: StrPath,
        *,
        on_event: EventSink | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> IngestResult:
        """What ``ingest`` would do with ``source``, without parsing anything or writing a
        package: the job's ``discover``, ``fingerprint``, ``inspect`` and ``plan`` phases. The
        result is ``planned``; its findings say what is unsupported or ambiguous and its cache
        report what each source's adapter planned and what the workspace already holds."""
        job = self._builder(source, None, resume)(on_event, cancel)
        return _execute(job, dry=True)

    def start(
        self,
        source: StrPath,
        destination: StrPath,
        *,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> Ingestion:
        """``ingest`` on a thread of its own: iterate the handle for events, then ``result()``."""
        return Ingestion(self._builder(source, destination, resume), dry=False, cancel=cancel)

    def start_dry_run(
        self, source: StrPath, *, cancel: threading.Event | None = None, resume: bool = False
    ) -> Ingestion:
        """``dry_run`` on a thread of its own."""
        return Ingestion(self._builder(source, None, resume), dry=True, cancel=cancel)


class AsyncNeptune:
    """``Neptune`` for asyncio: the same constructor, the same methods, awaited.

    Each job runs on a worker thread of its own; ``on_event`` is called on the event loop's
    thread. Cancelling the awaiting task cancels the job, waits for it to reach its next
    checkpoint, and re-raises ``CancelledError``; an exception from ``on_event`` does the same and
    propagates. A job already past its last checkpoint publishes first, and the exception carries
    its committed result (``committed_result``).
    """

    def __init__(
        self,
        workspace: Workspace | StrPath | None = None,
        *,
        adapters: Adapters | None = None,
        options: JobOptions | None = None,
        remote: str | None = None,
    ) -> None:
        self._sync = Neptune(workspace, adapters=adapters, options=options, remote=remote)

    @property
    def workspace(self) -> Workspace:
        return self._sync.workspace

    @property
    def registry(self) -> AdapterRegistry:
        return self._sync.registry

    @property
    def options(self) -> JobOptions:
        return self._sync.options

    async def ingest(
        self,
        source: StrPath,
        destination: StrPath,
        *,
        on_event: EventSink | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> IngestResult:
        """``Neptune.ingest``, awaited."""
        run = self.start(source, destination, cancel=cancel, resume=resume)
        return await _drive(run, on_event)

    async def dry_run(
        self,
        source: StrPath,
        *,
        on_event: EventSink | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> IngestResult:
        """``Neptune.dry_run``, awaited."""
        return await _drive(self.start_dry_run(source, cancel=cancel, resume=resume), on_event)

    def start(
        self,
        source: StrPath,
        destination: StrPath,
        *,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> AsyncIngestion:
        """``ingest`` on a thread of its own: ``async for`` its events, ``await`` its result."""
        builder = self._sync._builder(source, destination, resume)
        return AsyncIngestion(builder, dry=False, cancel=cancel)

    def start_dry_run(
        self, source: StrPath, *, cancel: threading.Event | None = None, resume: bool = False
    ) -> AsyncIngestion:
        """``dry_run`` on a thread of its own."""
        return AsyncIngestion(self._sync._builder(source, None, resume), dry=True, cancel=cancel)


async def _drive(run: AsyncIngestion, on_event: EventSink | None) -> IngestResult:
    """Deliver ``run``'s events to ``on_event`` on this loop; its result. Whatever interrupts
    the delivery (the task cancelled, ``on_event`` raising) cancels the job and waits for it to
    stop before propagating, so nothing is left running behind the caller's back. A job already
    past its last checkpoint publishes instead of stopping; the interruption then propagates
    carrying its committed result (``committed_result``, ADR 0035 §3)."""
    try:
        async for event in run:
            if on_event is not None:
                on_event(event)
    except (Exception, asyncio.CancelledError) as exc:
        run.cancel()
        again = await _until_ended(run)
        run._attach_committed(exc)
        if again is not None:  # cancelled while it waited: the task still ends cancelled
            run._attach_committed(again)
            raise again from exc
        raise
    except BaseException:
        run.cancel()
        raise
    return await run.result()


async def _until_ended(run: AsyncIngestion) -> asyncio.CancelledError | None:
    """Wait for ``run`` to end, through any further cancellation of this task, and return the
    last such cancellation, if one came.

    The job stops at its next checkpoint, or publishes if it is past its last, whatever this
    task is told meanwhile (an ``asyncio.timeout`` firing, then a task group cancelling it), so
    the wait always finishes and the caller always learns whether a package was committed.
    """
    cancelled: asyncio.CancelledError | None = None
    while not run.done():
        try:
            await run.wait()
        except asyncio.CancelledError as exc:
            cancelled = exc
    return cancelled


# --- One call ------------------------------------------------------------------------------------


def ingest(
    source: StrPath,
    destination: StrPath,
    *,
    workspace: Workspace | StrPath | None = None,
    adapters: Adapters | None = None,
    options: JobOptions | None = None,
    on_event: EventSink | None = None,
    cancel: threading.Event | None = None,
    resume: bool = False,
) -> IngestResult:
    """``Neptune(workspace, adapters=..., options=...).ingest(source, destination, ...)``."""
    client = Neptune(workspace, adapters=adapters, options=options)
    return client.ingest(source, destination, on_event=on_event, cancel=cancel, resume=resume)


def dry_run(
    source: StrPath,
    *,
    workspace: Workspace | StrPath | None = None,
    adapters: Adapters | None = None,
    options: JobOptions | None = None,
    on_event: EventSink | None = None,
    cancel: threading.Event | None = None,
    resume: bool = False,
) -> IngestResult:
    """``Neptune(workspace, adapters=..., options=...).dry_run(source, ...)``."""
    client = Neptune(workspace, adapters=adapters, options=options)
    return client.dry_run(source, on_event=on_event, cancel=cancel, resume=resume)
