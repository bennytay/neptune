"""The SDK's error taxonomy: one base class, a stable ``code`` per class (ADR 0035 §5).

Every error an SDK call raises is a ``NeptuneError``. Each class carries a ``code``, a token that
never changes meaning, so a program (or the CLI's exit codes) can branch on it without parsing a
message. Where the runtime or the store raised first, that exception is the ``__cause__``.

::

    NeptuneError                        error
    ├── InvalidRequestError             invalid_request        the call was wrong; nothing ran
    │   ├── InvalidSourceError          invalid_source         missing, not a directory, bad URI
    │   ├── InvalidDestinationError     invalid_destination    inside the source
    │   │   └── DestinationExistsError  destination_exists     a package is written once
    │   └── ConfigurationError          invalid_configuration  adapters, options, a remote URL
    ├── UnsupportedError                unsupported            a scheme or mode not in this version
    ├── NetworkRefusedError             network_refused        the workspace is local-only
    ├── SandboxUnavailableError         sandbox_unavailable    this host cannot confine adapters
    ├── WorkspaceUnusableError          workspace_unusable     cannot open, write, sweep or lock it
    ├── PackageInvalidError             package_invalid        a package that does not verify
    └── JobFailedError                  job_failed             the job itself could not proceed

A problem with one source is never an error: it is an ``IngestFinding`` in the result. Errors
are reserved for the call and the job (ADR 0028 §10). ``JobOptions`` validates itself when it is
built and raises the runtime's ``JobError`` there, before any SDK call.
"""

from pathlib import Path
from typing import ClassVar, Final

from neptune.adapters.contract import ConfigError
from neptune.discovery.scratch import ScratchError
from neptune.runtime import JobError
from neptune.runtime.sandbox import SandboxError
from neptune.store.workspace import WorkspaceError


class NeptuneError(Exception):
    """Anything an SDK call raises. ``code`` is stable; the message is for people."""

    code: ClassVar[str] = "error"


class InvalidRequestError(NeptuneError):
    """The call itself was wrong: nothing ran and nothing was written."""

    code: ClassVar[str] = "invalid_request"


class InvalidSourceError(InvalidRequestError):
    """The source does not exist, is not a directory, or is a URI that cannot name one."""

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
    """The workspace cannot be opened, written, swept or locked, or its scratch overlaps the
    source."""

    code: ClassVar[str] = "workspace_unusable"


class PackageInvalidError(NeptuneError):
    """A package that cannot be read or does not verify."""

    code: ClassVar[str] = "package_invalid"


class JobFailedError(NeptuneError):
    """The job itself could not proceed (an unreadable root, a disk that will not write, a
    package that does not verify): the runtime's ``JobError``, never one source's problem."""

    code: ClassVar[str] = "job_failed"


ERRORS: Final[tuple[type[NeptuneError], ...]] = (
    NeptuneError,
    InvalidRequestError,
    InvalidSourceError,
    InvalidDestinationError,
    DestinationExistsError,
    ConfigurationError,
    UnsupportedError,
    NetworkRefusedError,
    SandboxUnavailableError,
    WorkspaceUnusableError,
    PackageInvalidError,
    JobFailedError,
)


def from_job_error(error: JobError, destination: Path | None = None) -> NeptuneError:
    """The SDK error for a runtime ``JobError``, chosen by its cause's type, never its text.

    A job that fails while something is at its ``destination`` did not put it there (publishing
    is its last step), so another writer took the place a package is written to once:
    ``DestinationExistsError``, as if it had been there when the call was checked.
    """
    cause = error.__cause__
    kind: type[NeptuneError] = JobFailedError
    if isinstance(cause, SandboxError):
        kind = SandboxUnavailableError
    elif isinstance(cause, ConfigError):
        kind = ConfigurationError
    elif isinstance(cause, WorkspaceError | ScratchError):
        kind = WorkspaceUnusableError
    elif destination is not None and (destination.exists() or destination.is_symlink()):
        kind = DestinationExistsError
    return kind(str(error))
