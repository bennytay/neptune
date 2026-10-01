"""The SDK's error taxonomy: stable codes, one hierarchy, runtime errors mapped by cause."""

from pathlib import Path

import pytest

import neptune.sdk
from neptune.adapters.contract import ConfigError
from neptune.discovery.scratch import ScratchError
from neptune.runtime import JobError
from neptune.runtime.sandbox import SandboxError
from neptune.sdk import (
    ERRORS,
    ConfigurationError,
    DestinationExistsError,
    InvalidDestinationError,
    InvalidRequestError,
    InvalidSourceError,
    JobFailedError,
    NeptuneError,
    NetworkRefusedError,
    PackageInvalidError,
    SandboxUnavailableError,
    UnsupportedError,
    WorkspaceUnusableError,
)
from neptune.sdk.errors import from_job_error
from neptune.store.workspace import WorkspaceBusyError, WorkspaceError

# The codes are a contract: a program, and the CLI's exit codes, branch on them. Never change one.
CODES = {
    NeptuneError: "error",
    InvalidRequestError: "invalid_request",
    InvalidSourceError: "invalid_source",
    InvalidDestinationError: "invalid_destination",
    DestinationExistsError: "destination_exists",
    ConfigurationError: "invalid_configuration",
    UnsupportedError: "unsupported",
    NetworkRefusedError: "network_refused",
    SandboxUnavailableError: "sandbox_unavailable",
    WorkspaceUnusableError: "workspace_unusable",
    PackageInvalidError: "package_invalid",
    JobFailedError: "job_failed",
}

PARENTS = {
    InvalidRequestError: NeptuneError,
    InvalidSourceError: InvalidRequestError,
    InvalidDestinationError: InvalidRequestError,
    DestinationExistsError: InvalidDestinationError,
    ConfigurationError: InvalidRequestError,
    UnsupportedError: NeptuneError,
    NetworkRefusedError: NeptuneError,
    SandboxUnavailableError: NeptuneError,
    WorkspaceUnusableError: NeptuneError,
    PackageInvalidError: NeptuneError,
    JobFailedError: NeptuneError,
}


def test_every_error_has_its_stable_code() -> None:
    assert {kind: kind.code for kind in ERRORS} == CODES
    assert len({kind.code for kind in ERRORS}) == len(ERRORS)


def test_the_hierarchy_is_fixed_and_rooted_in_neptune_error() -> None:
    for kind, parent in PARENTS.items():
        assert kind.__bases__ == (parent,)
    assert NeptuneError.__bases__ == (Exception,)
    assert set(PARENTS) | {NeptuneError} == set(ERRORS)


def test_every_error_is_exported_from_the_sdk() -> None:
    for kind in ERRORS:
        assert getattr(neptune.sdk, kind.__name__) is kind
        assert kind.__name__ in neptune.sdk.__all__


def _caused(cause: BaseException | None) -> JobError:
    error = JobError("the job says why")
    error.__cause__ = cause
    return error


@pytest.mark.parametrize(
    ("cause", "kind"),
    [
        (SandboxError("no Landlock"), SandboxUnavailableError),
        (ConfigError("text has no option nope"), ConfigurationError),
        (WorkspaceError("format 9"), WorkspaceUnusableError),
        (WorkspaceBusyError("collecting"), WorkspaceUnusableError),
        (ScratchError("overlaps the root"), WorkspaceUnusableError),
        (OSError(28, "No space left on device"), JobFailedError),
        (ValueError("anything else"), JobFailedError),
        (None, JobFailedError),
    ],
)
def test_a_job_error_maps_by_the_type_of_its_cause(
    cause: BaseException | None, kind: type[NeptuneError]
) -> None:
    error = from_job_error(_caused(cause))
    assert type(error) is kind
    assert str(error) == "the job says why"  # the runtime's message, unchanged


def test_a_job_error_while_its_destination_is_taken_is_destination_exists(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "package"
    assert type(from_job_error(_caused(None), destination)) is JobFailedError
    destination.mkdir()  # another writer took it: the job publishes last, so it was not the job
    assert type(from_job_error(_caused(OSError(39, "not empty")), destination)) is (
        DestinationExistsError
    )
    # A cause that says more wins: the workspace, the sandbox and the config come first.
    assert type(from_job_error(_caused(WorkspaceError("x")), destination)) is (
        WorkspaceUnusableError
    )
    (tmp_path / "link").symlink_to(tmp_path / "nowhere")
    assert type(from_job_error(_caused(None), tmp_path / "link")) is DestinationExistsError


def test_the_mapping_never_reads_the_message() -> None:
    # A message that names a sandbox or a workspace does not change the class.
    assert type(from_job_error(JobError("the sandbox cannot run"))) is JobFailedError
    assert type(from_job_error(JobError("the workspace cannot be used"))) is JobFailedError
