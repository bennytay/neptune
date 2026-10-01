"""The ingestion runtime: the job that drives every adapter (ADR 0008 §5, ADR 0028, ADR 0030).

- ``events``: the nine phases, the job's states, and the structured events a job emits.
- ``lineage``: the runtime as a producer: its transform record and the findings it makes.
- ``sandbox``: where adapter code runs: a confined, limited child process per call, or in process.
- ``confine``: the Linux controls a sandboxed call runs under (limits, Landlock, seccomp).
- ``wire``: how a sandboxed call's value comes back: JSON, decoded strictly.
- ``cache``: what a job reuses and why it recomputes: invalidation rules and the cache report.
- ``job``: ``IngestJob``, the state machine over discovery, adapters, the workspace and the store;
  ``collect``, which removes from a workspace what no job with these adapters can reuse.

Imports everything above it (``model``, ``identity``, ``discovery``, ``adapters``, ``store``);
nothing imports the runtime but the CLI.
"""

from neptune.runtime.cache import RULES, CacheReport, Rule, cache_report_from_json
from neptune.runtime.events import PHASES, EventSink, JobEvent, JobState, Phase
from neptune.runtime.job import IngestJob, JobError, JobOptions, JobOutcome, collect
from neptune.runtime.lineage import FINDING_CODES, RUNTIME_ID, RUNTIME_VERSION, runtime_transform
from neptune.runtime.sandbox import DEFAULT_LIMITS, Isolation, Limits

__all__ = [
    "DEFAULT_LIMITS",
    "FINDING_CODES",
    "PHASES",
    "RULES",
    "RUNTIME_ID",
    "RUNTIME_VERSION",
    "CacheReport",
    "EventSink",
    "IngestJob",
    "Isolation",
    "JobError",
    "JobEvent",
    "JobOptions",
    "JobOutcome",
    "JobState",
    "Limits",
    "Phase",
    "Rule",
    "cache_report_from_json",
    "collect",
    "runtime_transform",
]
