"""Neptune's Python SDK: ingest from code, sync or async, without the CLI (ADR 0035).

::

    from neptune.sdk import Neptune

    result = Neptune().ingest("runs/2026-09-30", "packages/2026-09-30")
    print(result.package, [finding.code for finding in result.read_receipt().findings])

A thin, typed facade over the runtime: it resolves the source, opens the workspace, builds the
registry and the runtime's ``IngestJob``, runs it, and turns the job's errors into a stable
taxonomy. Events (``JobEvent``), findings (``IngestFinding``), the cache report and the receipt
are the runtime's own objects. Options are the runtime's ``JobOptions``; there is no second
configuration model. Local-only is the default: nothing reaches for the network unless the
workspace allows it.

- ``client``: ``Neptune`` and ``AsyncNeptune`` (one surface), ``Ingestion`` and
  ``AsyncIngestion`` (a job on its own thread), and the shorthands ``ingest`` and ``dry_run``.
- ``result``: ``IngestResult``, ``read_package``, and ``committed_result`` (ADR 0035 §3).
- ``contents``: ``run_contents``, ``RunContents`` and ``StreamContents``: what each run of a
  package contains (streams, declared field paths, inferred semantics) without decoding a
  message (ADR 0049).
- ``errors``: ``NeptuneError`` and its subclasses, each with a stable ``code``.
- ``PluginPolicy`` and ``Plugins``: which installed plugins a client reads, and what it read
  (``neptune.runtime.plugins``, ADR 0058).

Imports the runtime, the store and the adapters; the CLI (MVL-11) wraps this.
"""

from neptune.adapters.builtin import builtin_adapters
from neptune.discovery.ignore import DEFAULT_PATTERNS, IgnorePolicy
from neptune.runtime import (
    EventSink,
    Explanation,
    Isolation,
    JobError,
    JobEvent,
    JobOptions,
    JobState,
    Limits,
    Phase,
)
from neptune.runtime.plugins import PluginPolicy, Plugins
from neptune.sdk.client import (
    AsyncIngestion,
    AsyncNeptune,
    Ingestion,
    Neptune,
    dry_run,
    ingest,
)
from neptune.sdk.contents import RunContents, StreamContents, run_contents
from neptune.sdk.errors import (
    ERRORS,
    ConfigurationError,
    DestinationExistsError,
    InvalidDestinationError,
    InvalidRequestError,
    InvalidSourceError,
    JobFailedError,
    NeptuneError,
    NetworkRefusedError,
    NothingToResumeError,
    PackageInvalidError,
    PublishIncompleteError,
    SandboxUnavailableError,
    UnsupportedError,
    WorkspaceUnusableError,
)
from neptune.sdk.result import IngestResult, committed_result, read_package
from neptune.store.workspace import Workspace

__all__ = [
    "DEFAULT_PATTERNS",
    "ERRORS",
    "AsyncIngestion",
    "AsyncNeptune",
    "ConfigurationError",
    "DestinationExistsError",
    "EventSink",
    "Explanation",
    "IgnorePolicy",
    "IngestResult",
    "Ingestion",
    "InvalidDestinationError",
    "InvalidRequestError",
    "InvalidSourceError",
    "Isolation",
    "JobError",
    "JobEvent",
    "JobFailedError",
    "JobOptions",
    "JobState",
    "Limits",
    "Neptune",
    "NeptuneError",
    "NetworkRefusedError",
    "NothingToResumeError",
    "PackageInvalidError",
    "Phase",
    "PluginPolicy",
    "Plugins",
    "PublishIncompleteError",
    "RunContents",
    "SandboxUnavailableError",
    "StreamContents",
    "UnsupportedError",
    "Workspace",
    "WorkspaceUnusableError",
    "builtin_adapters",
    "committed_result",
    "dry_run",
    "ingest",
    "read_package",
    "run_contents",
]
