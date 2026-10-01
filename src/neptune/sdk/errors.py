"""The SDK's error taxonomy: one base class, a stable ``code`` per class (ADR 0035 §6).

Every error an SDK call raises is a ``NeptuneError``. Each class carries a ``code``, a token that
never changes meaning, so a program (or the CLI's exit codes) can branch on it without parsing a
message. Where the runtime or the store raised first, that exception is the ``__cause__``.

::

    NeptuneError                        error
    ├── InvalidRequestError             invalid_request        the call was wrong; nothing ran
    │   ├── InvalidSourceError          invalid_source         missing, neither folder nor file
    │   ├── InvalidDestinationError     invalid_destination    inside the source
    │   │   └── DestinationExistsError  destination_exists     a package is written once
    │   ├── ConfigurationError          invalid_configuration  adapters, options, ignore rules
    │   └── NothingToResumeError        nothing_to_resume      resume asked; no earlier work
    ├── UnsupportedError                unsupported            a scheme or mode not in this version
    ├── NetworkRefusedError             network_refused        the workspace is local-only
    ├── SandboxUnavailableError         sandbox_unavailable    this host cannot confine adapters
    ├── WorkspaceUnusableError          workspace_unusable     cannot open, lock, sweep, read, write
    ├── PackageInvalidError             package_invalid        a package that does not verify
    └── JobFailedError                  job_failed             the job itself could not proceed
        └── PublishIncompleteError      publish_incomplete     in place, but may not survive a crash

A runtime ``JobError`` maps by its cause's type, never its text (``from_job_error``); the CLI
(MVL-11) turns the codes into exit codes:

- ``workspace_unusable``: the workspace cannot be opened, locked or swept; its ledger, a plan, a
  committed chunk, a chunk's runs or a kept derivative cannot be read or written; a call gets no
  scratch space, or scratch overlaps the source. The runtime raises each of these from a
  ``WorkspaceError`` or ``ScratchError`` (an ``OSError`` behind it, if there was one).
- ``sandbox_unavailable``: from a ``SandboxError``; ``invalid_configuration``: a ``ConfigError``,
  or an ``IgnoreError`` (ignore rules, the root's ``.neptune-ignore`` included, ADR 0043).
- ``publish_incomplete``: from the store's ``NotDurableError``, raised only after the job renamed
  its package into place.
- ``destination_exists``: anything else while something is at the destination: the job renames
  last, so another writer put it there.
- ``job_failed``: anything else: the root cannot be read; the package cannot be assembled,
  verified or written beside its destination.

A problem with one source is never an error: it is an ``IngestFinding`` in the result. Errors
are reserved for the call and the job (ADR 0028 §10). ``JobOptions`` validates itself when it is
built and raises the runtime's ``JobError`` there, before any SDK call.
"""

from pathlib import Path
from typing import ClassVar, Final

from neptune.adapters.contract import ConfigError
from neptune.discovery.ignore import IgnoreError
from neptune.discovery.scratch import ScratchError
from neptune.runtime import JobError
from neptune.runtime.sandbox import SandboxError
from neptune.store.assemble import NotDurableError
from neptune.store.workspace import WorkspaceError


class NeptuneError(Exception):
    """Anything an SDK call raises. ``code`` is stable; the message is for people."""

    code: ClassVar[str] = "error"


class InvalidRequestError(NeptuneError):
    """The call itself was wrong: nothing ran and nothing was written."""

    code: ClassVar[str] = "invalid_request"


class InvalidSourceError(InvalidRequestError):
    """The source does not exist, is neither a directory nor a regular file, or is a URI that
    cannot name one."""

    code: ClassVar[str] = "invalid_source"


class InvalidDestinationError(InvalidRequestError):
    """The destination cannot hold the package: it lies inside the source being ingested."""

    code: ClassVar[str] = "invalid_destination"


class DestinationExistsError(InvalidDestinationError):
    """Something is already at the destination; a package is written once (ADR 0026 §4)."""

    code: ClassVar[str] = "destination_exists"


class ConfigurationError(InvalidRequestError):
    """Adapters that conflict, options naming an unknown adapter or option, a value an option
    cannot take, or a remote URL that is not one."""

    code: ClassVar[str] = "invalid_configuration"


class NothingToResumeError(InvalidRequestError):
    """A resume was asked for (``resume=True``) and the workspace holds no earlier work on this
    source: no job, dry run or interrupted ingest ever scanned it here (ADR 0043)."""

    code: ClassVar[str] = "nothing_to_resume"


class UnsupportedError(NeptuneError):
    """A source scheme or an execution mode this version of Neptune does not provide."""

    code: ClassVar[str] = "unsupported"


class NetworkRefusedError(NeptuneError):
    """The call needs the network and the workspace is local-only (ADR 0026 §6).

    ``Workspace.allow_network(True)`` lifts it, and the workspace remembers the choice.
    """

    code: ClassVar[str] = "network_refused"


class SandboxUnavailableError(NeptuneError):
    """This host cannot confine adapter calls (ADR 0030 §3). ``JobOptions`` can ask for
    ``Isolation.IN_PROCESS`` or ``allow_degraded_sandbox``; both are recorded in the lineage."""

    code: ClassVar[str] = "sandbox_unavailable"


class WorkspaceUnusableError(NeptuneError):
    """The workspace cannot be opened, locked or swept, what a job keeps there (its ledger,
    plans, committed chunks and their runs, derivatives, scratch space) cannot be read or
    written, or its scratch overlaps the source."""

    code: ClassVar[str] = "workspace_unusable"


class PackageInvalidError(NeptuneError):
    """A package that cannot be read or does not verify."""

    code: ClassVar[str] = "package_invalid"


class JobFailedError(NeptuneError):
    """The job itself could not proceed for a reason no other class names (an unreadable root, a
    package that cannot be assembled, verified or written beside its destination): the
    runtime's ``JobError``, never one source's problem."""

    code: ClassVar[str] = "job_failed"


class PublishIncompleteError(JobFailedError):
    """The job renamed its package into place, but the directory holding it could not be
    flushed: the package is whole at the destination, and a crash before the disk catches up may
    lose it. ``read_package`` verifies it; it is the job's own, not another writer's.

    ``destination`` is where the package is (ADR 0043). The job did not commit, so
    ``committed_result`` returns ``None`` for this error: this attribute is how a caller finds the
    package.
    """

    code: ClassVar[str] = "publish_incomplete"

    def __init__(self, message: str, destination: Path | None = None) -> None:
        super().__init__(message)
        self.destination = destination


ERRORS: Final[tuple[type[NeptuneError], ...]] = (
    NeptuneError,
    InvalidRequestError,
    InvalidSourceError,
    InvalidDestinationError,
    DestinationExistsError,
    ConfigurationError,
    NothingToResumeError,
    UnsupportedError,
    NetworkRefusedError,
    SandboxUnavailableError,
    WorkspaceUnusableError,
    PackageInvalidError,
    JobFailedError,
    PublishIncompleteError,
)


def from_job_error(error: JobError, destination: Path | None = None) -> NeptuneError:
    """The SDK error for a runtime ``JobError``, chosen by its cause's type, never its text.

    Whether the job renamed its package into place is the job's own knowledge, and its cause
    says so: ``NotDurableError`` is raised only after the rename (only the flush after it
    failed), so the package at ``destination`` is the job's: ``PublishIncompleteError``. Any
    other failure happened before the job renamed anything, so something at its ``destination``
    now is another writer's, which took the place a package is written to once:
    ``DestinationExistsError``, as if it had been there when the call was checked.
    """
    cause = error.__cause__
    kind: type[NeptuneError] = JobFailedError
    if isinstance(cause, SandboxError):
        kind = SandboxUnavailableError
    elif isinstance(cause, ConfigError | IgnoreError):
        kind = ConfigurationError
    elif isinstance(cause, WorkspaceError | ScratchError):
        kind = WorkspaceUnusableError
    elif isinstance(cause, NotDurableError):
        return PublishIncompleteError(str(error), destination)
    elif destination is not None and (destination.exists() or destination.is_symlink()):
        kind = DestinationExistsError
    return kind(str(error))
