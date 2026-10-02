"""The phases of an ingest job, its states, and the structured events it emits (ADR 0028).

A job moves through nine phases in order. ``parse`` and ``normalize`` alternate once per chunk;
every other phase runs once. Each phase starts when the job first enters it and finishes when the
job moves past it, so a consumer sees exactly one ``phase_started`` and one ``phase_finished``
per phase whatever the chunk count.

Events are plain data: a kind, the phase they happened in, and canonical JSON details. They carry
no wall-clock time, host or absolute path, so two runs of one job emit the same events; whoever
consumes them (a log, a progress bar, the CLI) adds the clock.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias

from neptune.identity import canonical_json
from neptune.model.ids import check_token
from neptune.model.jsonvalue import JsonObject


class Phase(StrEnum):
    """The job's phases, in the order the job runs them."""

    DISCOVER = "discover"  # walk the root: regular files, symlinks, entries that cannot be read
    FINGERPRINT = "fingerprint"  # hash every file into the root's ledger; reconcile absences
    INSPECT = "inspect"  # probe each distinct source's head; select and configure an adapter
    PLAN = "plan"  # plan each selected source, or reuse the workspace's plan; save it
    PARSE = "parse"  # run the adapter over one chunk the workspace has not committed
    NORMALIZE = "normalize"  # check the chunk's output against the contract; commit it whole
    ASSEMBLE = "assemble"  # admit sources whose chunks all committed; build the package beside
    VALIDATE = "validate"  # read the staged package back and verify it
    COMMIT = "commit"  # write the envelope into it and rename it into place


PHASES: Final = tuple(Phase)


class JobState(StrEnum):
    PENDING = "pending"  # built, not run
    RUNNING = "running"  # in a phase
    COMMITTED = "committed"  # the package is in place
    PLANNED = "planned"  # a dry run stopped after plan: nothing parsed, no package (ADR 0035)
    CANCELLED = "cancelled"  # stopped at a checkpoint on request; the workspace keeps the work
    FAILED = "failed"  # the job itself could not proceed (never one source's problem)


# Event kinds. A consumer matches on these; the details' keys are documented by the job.
PHASE_STARTED: Final = "phase_started"
PHASE_FINISHED: Final = "phase_finished"
WORKSPACE_SWEPT: Final = "workspace_swept"  # scratch and staging debris removed at the start
ENTRY_SKIPPED: Final = "entry_skipped"
SYMLINK_RECORDED: Final = "symlink_recorded"
SOURCE_HASHED: Final = "source_hashed"
SOURCE_ABSENT: Final = "source_absent"
SANDBOX_READY: Final = "sandbox_ready"  # isolation, and the limits and Landlock ABI if sandboxed
PROBE_FAILED: Final = "probe_failed"
SOURCE_SELECTED: Final = "source_selected"
SOURCE_UNSUPPORTED: Final = "source_unsupported"
SOURCE_AMBIGUOUS: Final = "source_ambiguous"
SESSIONS_PROPOSED: Final = "sessions_proposed"  # grouping's counts, at the end of inspect
SOURCE_UNREADABLE: Final = "source_unreadable"
SOURCE_CHANGED: Final = "source_changed"
SOURCE_SHORT_READ: Final = "source_short_read"  # a call read the source short (ADR 0033 §3)
SOURCE_PLANNED: Final = "source_planned"
PLAN_FAILED: Final = "plan_failed"
CHUNK_SKIPPED: Final = "chunk_skipped"
CHUNK_PARSED: Final = "chunk_parsed"
CHUNK_RETRIED: Final = "chunk_retried"
CHUNK_COMMITTED: Final = "chunk_committed"
CHUNK_FAILED: Final = "chunk_failed"
DERIVATIVE_REUSED: Final = "derivative_reused"
DERIVATIVE_BUILT: Final = "derivative_built"
SOURCE_ADMITTED: Final = "source_admitted"
SOURCE_QUARANTINED: Final = "source_quarantined"
STREAMS_INTROSPECTED: Final = "streams_introspected"  # introspection's counts (ADR 0049)
CLOCKS_ALIGNED: Final = "clocks_aligned"  # the clock-alignment pass's counts (ADR 0060)
PACKAGE_STAGED: Final = "package_staged"
PACKAGE_VERIFIED: Final = "package_verified"
JOB_COMMITTED: Final = "job_committed"
JOB_PLANNED: Final = "job_planned"  # a dry run's end (ADR 0035)
JOB_CANCELLED: Final = "job_cancelled"
JOB_FAILED: Final = "job_failed"


@dataclass(frozen=True)
class JobEvent:
    """One thing a job did or found, in one phase, with canonical JSON details."""

    kind: str
    phase: Phase
    details: JsonObject

    def __post_init__(self) -> None:
        check_token("event kind", self.kind)
        if not isinstance(self.phase, Phase):
            raise TypeError(f"phase must be a Phase, got {self.phase!r}")
        canonical_json.dumps(self.details)

    def to_json(self) -> JsonObject:
        return {"details": self.details, "kind": self.kind, "phase": str(self.phase)}


EventSink: TypeAlias = Callable[[JobEvent], None]
