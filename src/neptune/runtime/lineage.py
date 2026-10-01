"""The runtime as a producer: its transform record and the findings it makes (ADR 0028 §4).

What an adapter could not say about a source, the runtime says: an adapter raised, crashed or hit
a sandbox limit on a chunk or a plan, a source's chunks broke a cross-chunk law, a file changed
under the job or could not be opened, a walk entry was not read. Each is an ``IngestFinding`` from
the runtime's own ``TransformRecord`` (``neptune.runtime`` at ``RUNTIME_VERSION``, with the job's
retry and isolation policy as its config: attempts, isolation and, when sandboxed, the limits), so
a package records who said it and under what policy (ADR 0028 §4, ADR 0030). The transform enters
a package only with its findings, so a package with none is independent of the runtime's version.

Messages and details hold ids, codes, versions, steps, laws, class names, signal names and limits:
never an exception's text or an object's repr, which may name a path or a memory address and would
make two identical jobs write different packages. Where an adapter failed is a ``Failure``: the
``Step`` it failed at, the exception's class, and facts that are ids, counts or type names. A
sandboxed call that died or was stopped is an ``adapter_crashed`` or ``limit_exceeded`` finding
naming its step and the signal, exit status or limit. An ``output_invalid`` finding lists its
problems as objects, each naming its ``law``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from neptune.adapters.contract import Documented
from neptune.discovery.source import SkipReason
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import ByteRange, EvidenceRef, TransformRecord
from neptune.model.source import LocalPath, RawLocalPath
from neptune.runtime.sandbox import DEFAULT_LIMITS, Isolation, Limits

RUNTIME_ID: Final = "neptune.runtime"
# Changes whenever a law the runtime applies to a chunk's committed output changes: the contract's
# checks (``check_chunk_output``), the series laws within a chunk, the cross-chunk laws. Committed
# chunks and verdicts record it, so a new version judges what is kept again (ADR 0031). The sandbox
# (ADR 0030) does not bump it: isolation and limits ride in the transform config, and a crash or a
# limit stops a chunk before it commits, so they never re-judge what is kept.
RUNTIME_VERSION: Final = "0.1.0"

ADAPTER_CRASHED: Final = f"{RUNTIME_ID}.adapter_crashed"
CHUNK_FAILED: Final = f"{RUNTIME_ID}.chunk_failed"
ENTRY_SKIPPED: Final = f"{RUNTIME_ID}.entry_skipped"
LIMIT_EXCEEDED: Final = f"{RUNTIME_ID}.limit_exceeded"
OUTPUT_INVALID: Final = f"{RUNTIME_ID}.output_invalid"
PLAN_FAILED: Final = f"{RUNTIME_ID}.plan_failed"
SOURCE_CHANGED: Final = f"{RUNTIME_ID}.source_changed"
SOURCE_UNREADABLE: Final = f"{RUNTIME_ID}.source_unreadable"

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        ADAPTER_CRASHED,
        "the sandboxed process running an adapter's plan or a chunk's ingest died without a"
        " reply (a signal, an exit, or a reply that does not decode), after every attempt; the"
        " source is not in this package (failed, error)",
    ),
    Documented(
        CHUNK_FAILED,
        "an adapter raised, or broke the contract, on a chunk after every attempt; the source is"
        " not in this package (failed, error)",
    ),
    Documented(
        ENTRY_SKIPPED,
        "a walk entry was not read: not a regular file (info), vanished (warning), or unreadable"
        " (error) (skipped)",
    ),
    Documented(
        LIMIT_EXCEEDED,
        "an adapter's plan or a chunk's ingest was stopped at a sandbox limit (cpu_seconds,"
        " wall_seconds, memory_bytes, reply_bytes); never retried in the job; the source is not"
        " in this package (failed, error)",
    ),
    Documented(
        OUTPUT_INVALID,
        "a source's committed chunk outputs break a cross-chunk law of the adapter contract; the"
        " source is not in this package (failed, error)",
    ),
    Documented(
        PLAN_FAILED,
        "an adapter raised, or broke the contract, while planning a source; the source is not in"
        " this package (failed, error)",
    ),
    Documented(
        SOURCE_CHANGED,
        "a file's bytes changed after it was fingerprinted; it was not read, and the next job"
        " fingerprints it again (inconsistent, error)",
    ),
    Documented(
        SOURCE_UNREADABLE,
        "a fingerprinted file could not be opened when it was time to read it (skipped, error)",
    ),
)

_SKIP_SEVERITY: Final = {
    SkipReason.NOT_REGULAR_FILE: Severity.INFO,
    SkipReason.MISSING: Severity.WARNING,
    SkipReason.SYMLINK: Severity.WARNING,
    SkipReason.UNREADABLE: Severity.ERROR,
}


class Step(StrEnum):
    """Where an adapter's plan or chunk failed: a ``plan_failed`` or ``chunk_failed``'s ``step``."""

    PLAN = "plan"  # the adapter's plan raised
    PLAN_RESULT = "plan_result"  # plan returned something that is not a Plan
    CHECK_PLAN = "check_plan"  # the plan broke the contract
    INGEST = "ingest"  # the adapter's ingest raised, building its ChunkOutput included
    INGEST_RESULT = "ingest_result"  # ingest returned something that is not a ChunkOutput
    CHECK_OUTPUT = "check_chunk_output"  # the chunk's output broke the contract
    CHUNK_SERIES = "chunk_series"  # the chunk's batches broke a series law within the chunk
    COMMIT = "commit"  # the workspace refused the chunk's output (an I/O error fails the job)


class Law(StrEnum):
    """A series law a chunk broke (``chunk_series``) or a cross-chunk law a source's output broke.

    The first three are checked within one chunk at normalize; the rest across a source's
    committed chunks at assemble (ADR 0028 §5).
    """

    BATCH_COLUMNS_DISAGREE = "batch_columns_disagree"  # two batches of a stream in one chunk
    SEQ_NOT_INTEGER = "seq_not_integer"  # a seq cell that is not an integer (its ``type``)
    SEQ_REPEATED = "seq_repeated"  # a seq twice in one chunk's batches of a stream
    RECORD_REPEATED = "record_repeated"  # a record emitted by two chunks
    FINDING_REPEATED = "finding_repeated"  # a finding emitted twice (by the plan or two chunks)
    OUTPUT_SILENT = "output_silent"  # no record and no finding about the source at all
    STREAM_UNDECLARED = "stream_undeclared"  # series rows of a stream no chunk declares
    STREAM_WITHOUT_RUN = "stream_without_run"  # a declared stream no chunk wrote a run of
    RUN_BREAKS_STREAM = "run_breaks_stream"  # a chunk's run breaks its stream's row contract
    RUN_COLUMNS_DISAGREE = "run_columns_disagree"  # two chunks' runs of a stream differ in columns
    SEQ_RANGES_OVERLAP = "seq_ranges_overlap"  # two chunks' seq ranges of a stream overlap


def type_name(value: object) -> str:
    """``value``'s class as ``module.qualname``: what a finding says instead of a repr."""
    kind = type(value)
    return f"{kind.__module__}.{kind.__qualname__}"


@dataclass(frozen=True)
class Failure:
    """Where an adapter's plan or chunk failed, and how, in terms that are the same every run.

    ``error`` is the exception's class name (``ContractError`` when the runtime found the breach
    itself); ``facts`` are ids, counts, laws and type names, never text from the exception.
    """

    step: Step
    error: str
    facts: Mapping[str, JsonValue] = field(default_factory=dict)

    @classmethod
    def raised(cls, step: Step, exc: BaseException) -> "Failure":
        """``exc`` was raised at ``step``: only its class is kept."""
        return cls(step, type(exc).__name__)

    @classmethod
    def returned(cls, step: Step, value: object) -> "Failure":
        """The adapter returned ``value``, of the wrong type, at ``step``."""
        return cls(step, "ContractError", {"returned": type_name(value)})

    def details(self) -> dict[str, JsonValue]:
        """The finding's details this failure contributes: its facts, its error and its step."""
        if {"error", "step"} & set(self.facts):
            raise ValueError("a failure's facts never name its error or step")
        return {**self.facts, "error": self.error, "step": str(self.step)}

    def to_json(self) -> JsonObject:
        return {"error": self.error, "facts": dict(self.facts), "step": str(self.step)}


def failure_from_json(data: JsonValue) -> Failure:
    """Parse strictly: exactly ``error`` (text), ``facts`` (an object) and a known ``step``."""
    if not isinstance(data, dict) or data.keys() != {"error", "facts", "step"}:
        raise ValueError(f"a failure is an object of error, facts and step: {data!r}")
    error, facts, step = data["error"], data["facts"], data["step"]
    if not isinstance(error, str) or not error or not isinstance(facts, dict):
        raise ValueError(f"a failure's error is text and its facts an object: {data!r}")
    if step not in tuple(str(known) for known in Step):
        raise ValueError(f"not a step: {step!r}")
    return Failure(Step(str(step)), error, facts)


def runtime_transform(
    attempts: int, isolation: Isolation = Isolation.SUBPROCESS, limits: Limits = DEFAULT_LIMITS
) -> TransformRecord:
    """The runtime's transform under one policy: ``attempts`` tries per chunk, adapter calls run
    with ``isolation``, and bounded by ``limits`` when that is the sandbox."""
    config: dict[str, JsonValue] = {"attempts": attempts, "isolation": str(isolation)}
    if isolation is Isolation.SUBPROCESS:
        config |= limits.to_json()
    return transform_record(adapter_id=RUNTIME_ID, adapter_version=RUNTIME_VERSION, config=config)


def _whole(source: ContentId, size: int) -> EvidenceRef:
    return EvidenceRef(source, (ByteRange(0, size),))


def _tries(attempts: int) -> str:
    return f"{attempts} attempt" if attempts == 1 else f"{attempts} attempts"


def chunk_failed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    chunk: str,
    attempts: int,
    failure: Failure,
) -> IngestFinding:
    """``adapter_id`` at ``version`` failed on ``chunk`` as ``failure`` says, after ``attempts``."""
    details = {
        **failure.details(),
        "adapter": adapter_id,
        "attempts": attempts,
        "chunk": chunk,
        "version": version,
    }
    return ingest_finding(
        code=CHUNK_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} failed on chunk {chunk} after {_tries(attempts)}"
        f" ({failure.error} at {failure.step}); the source is not in this package",
        details=details,
    )


def plan_failed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    failure: Failure,
) -> IngestFinding:
    """``adapter_id`` at ``version`` could not plan the source, as ``failure`` says."""
    return ingest_finding(
        code=PLAN_FAILED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} could not plan the source"
        f" ({failure.error} at {failure.step}); it is not in this package",
        details={**failure.details(), "adapter": adapter_id, "version": version},
    )


_CALLS: Final = frozenset({Step.PLAN, Step.INGEST})
_CRASH_CAUSES: Final = frozenset({"exit_status", "reply", "signal"})


def _call(step: Step, chunk: str | None) -> tuple[str, dict[str, JsonValue]]:
    """The sandboxed call, in words and as details: a chunk's ``ingest``, or ``plan``."""
    if step not in _CALLS or (step is Step.INGEST) != (chunk is not None):
        raise ValueError(f"a sandboxed call is plan, or ingest of a chunk: {step}, {chunk}")
    if chunk is None:
        return "while planning the source", {"step": str(step)}
    return f"on chunk {chunk}", {"chunk": chunk, "step": str(step)}


def _death(cause: Mapping[str, JsonValue]) -> str:
    if "signal" in cause:
        return f"killed by {cause['signal']}"
    if "exit_status" in cause:
        return f"exit status {cause['exit_status']}"
    return "a reply that does not decode"


def adapter_crashed(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    step: Step,
    chunk: str | None,
    cause: Mapping[str, JsonValue],
    attempts: int,
) -> IngestFinding:
    """The sandboxed process running ``adapter_id``'s ``step`` died without a reply, every attempt.

    ``chunk`` is the chunk ``ingest`` was reading (``None`` for ``plan``). ``cause`` is exactly
    one of ``{"signal": "SIGSEGV"}``, ``{"exit_status": 3}`` or ``{"reply": "malformed"}``.
    """
    if len(cause) != 1 or not cause.keys() <= _CRASH_CAUSES:
        raise ValueError(f"a crash has one cause: a signal, an exit status or a reply: {cause}")
    where, call = _call(step, chunk)
    details: dict[str, JsonValue] = {"adapter": adapter_id, "attempts": attempts}
    details |= {"version": version, **call, **cause}
    return ingest_finding(
        code=ADAPTER_CRASHED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} crashed {where} after {_tries(attempts)}"
        f" ({_death(cause)}); the source is not in this package",
        details=details,
    )


def limit_exceeded(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    version: str,
    step: Step,
    chunk: str | None,
    limit: str,
    value: int,
) -> IngestFinding:
    """``adapter_id``'s ``step`` was stopped at the sandbox's ``limit`` of ``value``.

    Never retried in the job: the same bytes under the same limit hit it again. The next job, or
    a higher limit (another runtime config, so another lineage), tries again.
    """
    where, call = _call(step, chunk)
    details: dict[str, JsonValue] = {"adapter": adapter_id, "limit": limit, "value": value}
    details |= {"version": version, **call}
    return ingest_finding(
        code=LIMIT_EXCEEDED,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id} {version} was stopped {where} at its {limit} limit ({value});"
        " the source is not in this package",
        details=details,
    )


def output_invalid(
    transform: TransformRecord,
    source: ContentId,
    size: int,
    adapter_id: str,
    problems: Sequence[Mapping[str, JsonValue]],
) -> IngestFinding:
    """The source's committed chunk outputs, taken together, break the listed laws.

    Each problem is an object naming its ``law`` and the ids it concerns (chunks, streams,
    records), in the order the runtime found them.
    """
    if not problems:
        raise ValueError("an output_invalid finding names at least one problem")
    laws: list[str] = []
    for problem in problems:
        law = problem.get("law")
        if not isinstance(law, str) or not law:
            raise ValueError("every cross-chunk problem names its law")
        if law not in laws:
            laws.append(law)
    count = len(problems)
    noun = "law" if count == 1 else "laws"
    return ingest_finding(
        code=OUTPUT_INVALID,
        category=FindingCategory.FAILED,
        severity=Severity.ERROR,
        subject=_whole(source, size),
        transform=transform,
        message=f"{adapter_id}'s output breaks {count} cross-chunk {noun} ({', '.join(laws)});"
        " the source is not in this package",
        details={"adapter": adapter_id, "problems": [dict(problem) for problem in problems]},
    )


def source_changed(
    transform: TransformRecord, location: LocalPath | RawLocalPath, source: ContentId
) -> IngestFinding:
    """The file at ``location`` no longer holds the bytes it was fingerprinted as."""
    return ingest_finding(
        code=SOURCE_CHANGED,
        category=FindingCategory.INCONSISTENT,
        severity=Severity.ERROR,
        subject=location,
        transform=transform,
        message="the file changed after it was fingerprinted; it was not read, and the next job"
        " fingerprints it again",
        details={"source": source},
    )


def source_unreadable(
    transform: TransformRecord,
    location: LocalPath | RawLocalPath,
    reason: SkipReason,
    errno: str | None = None,
) -> IngestFinding:
    """A fingerprinted file could not be opened or read when the job came to it.

    ``errno`` is the system's symbolic error (``EACCES``, ``EIO``) when it gave one.
    """
    details: dict[str, JsonValue] = {"reason": str(reason)}
    if errno is not None:
        details["errno"] = errno
    said = f"{reason}, {errno}" if errno is not None else str(reason)
    return ingest_finding(
        code=SOURCE_UNREADABLE,
        category=FindingCategory.SKIPPED,
        severity=Severity.ERROR,
        subject=location,
        transform=transform,
        message=f"the file could not be opened or read ({said})",
        details=details,
    )


def entry_skipped(
    transform: TransformRecord, location: LocalPath | RawLocalPath, reason: SkipReason
) -> IngestFinding:
    """A walk entry discovery did not read, and why."""
    return ingest_finding(
        code=ENTRY_SKIPPED,
        category=FindingCategory.SKIPPED,
        severity=_SKIP_SEVERITY[reason],
        subject=location,
        transform=transform,
        message=f"not read: {reason}",
        details={"reason": str(reason)},
    )
