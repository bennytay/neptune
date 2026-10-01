"""The runtime's transform and findings: declared codes, deterministic ids, no leaked text."""

import pytest

from neptune.discovery.source import SkipReason
from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.model.finding import (
    MAX_MESSAGE_LENGTH,
    FindingCategory,
    IngestFinding,
    Severity,
    subject_to_json,
)
from neptune.model.source import LocalPath, RawLocalPath
from neptune.runtime import lineage
from neptune.runtime.lineage import FINDING_CODES, RUNTIME_ID, RUNTIME_VERSION, runtime_transform
from neptune.runtime.sandbox import DEFAULT_LIMITS, Isolation, Limits

SOURCE = content_id(b"some bytes")
TRANSFORM = runtime_transform(2)
CHUNK = "chunk:sha256:" + "ab" * 32


def test_the_runtime_transform_is_its_id_version_retry_and_isolation_policy() -> None:
    assert TRANSFORM.adapter_id == RUNTIME_ID == "neptune.runtime"
    assert TRANSFORM.adapter_version == RUNTIME_VERSION
    assert dict(TRANSFORM.config) == {
        "attempts": 2,
        "cpu_seconds": 60,
        "isolation": "subprocess",
        "memory_bytes": 2 * 1024**3,
        "wall_seconds": 120,
    }
    assert TRANSFORM.libraries == () and TRANSFORM.upstream == ()
    assert runtime_transform(2) == TRANSFORM
    assert runtime_transform(2, Isolation.SUBPROCESS, DEFAULT_LIMITS) == TRANSFORM
    assert runtime_transform(3).id != TRANSFORM.id  # another policy is another lineage
    assert runtime_transform(2, limits=Limits(cpu_seconds=61)).id != TRANSFORM.id


def test_in_process_records_no_limits_since_none_bound_it() -> None:
    trusted = runtime_transform(2, Isolation.IN_PROCESS)
    assert dict(trusted.config) == {"attempts": 2, "isolation": "in_process"}
    assert trusted.id != TRANSFORM.id
    assert runtime_transform(2, Isolation.IN_PROCESS, Limits(cpu_seconds=5)) == trusted


def test_finding_codes_are_declared_sorted_and_prefixed() -> None:
    names = [code.name for code in FINDING_CODES]
    assert names == sorted(names) and len(set(names)) == len(names)
    assert all(name.startswith(f"{RUNTIME_ID}.") for name in names)
    assert all(code.description for code in FINDING_CODES)


def every_finding() -> list[tuple[str, IngestFinding]]:
    adapter = (TRANSFORM, SOURCE, 10, "text", "0.1.0")
    return [
        ("chunk", lineage.chunk_failed(*adapter, CHUNK, "RuntimeError", 2)),
        ("chunk-problem", lineage.chunk_failed(*adapter, CHUNK, "ContractError", 1, "law")),
        ("plan", lineage.plan_failed(*adapter, "ValueError")),
        ("signal", lineage.adapter_crashed(*adapter, CHUNK, {"signal": "SIGSEGV"}, 2)),
        ("exit", lineage.adapter_crashed(*adapter, None, {"exit_status": 3}, 1)),
        ("reply", lineage.adapter_crashed(*adapter, CHUNK, {"reply": "malformed"}, 2)),
        ("wall", lineage.limit_exceeded(*adapter, CHUNK, "wall_seconds", 120)),
        ("memory", lineage.limit_exceeded(*adapter, None, "memory_bytes", 2**31)),
        ("output", lineage.output_invalid(TRANSFORM, SOURCE, 10, "text", ["a", "b"])),
        ("changed", lineage.source_changed(TRANSFORM, LocalPath("a/b.txt"), SOURCE)),
        (
            "unreadable",
            lineage.source_unreadable(TRANSFORM, LocalPath("a"), SkipReason.UNREADABLE, "EACCES"),
        ),
        (
            "skipped",
            lineage.entry_skipped(TRANSFORM, RawLocalPath(b"\xff"), SkipReason.MISSING, "gone"),
        ),
    ]


def test_every_finding_is_declared_checks_and_is_deterministic() -> None:
    declared = {code.name for code in FINDING_CODES}
    for name, finding in every_finding():
        assert finding.code in declared, name
        assert check_ingest_finding(finding) == finding
        assert finding.transform == TRANSFORM.id
        assert len(finding.message) <= MAX_MESSAGE_LENGTH
    again = dict(every_finding())
    for name, finding in every_finding():
        assert again[name] == finding, name


def test_chunk_failed_cites_the_whole_source_and_names_only_the_error_class() -> None:
    finding = lineage.chunk_failed(TRANSFORM, SOURCE, 10, "text", "0.1.0", CHUNK, "RuntimeError", 2)
    assert subject_to_json(finding.subject) == {
        "kind": "evidence",
        "ref": {"locator": [{"kind": "byte_range", "length": 10, "offset": 0}], "source": SOURCE},
    }
    assert (finding.category, finding.severity) == (FindingCategory.FAILED, Severity.ERROR)
    assert finding.details == {
        "adapter": "text",
        "attempts": 2,
        "chunk": CHUNK,
        "error": "RuntimeError",
        "version": "0.1.0",
    }
    assert "text 0.1.0 failed" in finding.message
    assert "after 2 attempts (RuntimeError)" in finding.message
    once = lineage.chunk_failed(TRANSFORM, SOURCE, 10, "text", "0.1.0", CHUNK, "RuntimeError", 1)
    assert "after 1 attempt (RuntimeError)" in once.message and once.id != finding.id
    with_problem = lineage.chunk_failed(
        TRANSFORM, SOURCE, 10, "text", "0.1.0", CHUNK, "ContractError", 1, "id does not match"
    )
    assert with_problem.details["problem"] == "id does not match"
    upgraded = lineage.chunk_failed(
        TRANSFORM, SOURCE, 10, "text", "0.2.0", CHUNK, "RuntimeError", 2
    )
    assert upgraded.id != finding.id  # another adapter version is another finding


def test_a_crash_names_the_adapter_version_call_chunk_and_how_it_died() -> None:
    finding = lineage.adapter_crashed(
        TRANSFORM, SOURCE, 10, "mcap", "1.2.0", CHUNK, {"signal": "SIGSEGV"}, 2
    )
    assert finding.code == "neptune.runtime.adapter_crashed"
    assert (finding.category, finding.severity) == (FindingCategory.FAILED, Severity.ERROR)
    assert finding.details == {
        "adapter": "mcap",
        "attempts": 2,
        "call": "ingest",
        "chunk": CHUNK,
        "signal": "SIGSEGV",
        "version": "1.2.0",
    }
    assert "mcap 1.2.0 crashed on chunk" in finding.message
    assert "after 2 attempts (killed by SIGSEGV)" in finding.message
    planning = lineage.adapter_crashed(
        TRANSFORM, SOURCE, 10, "mcap", "1.2.0", None, {"exit_status": 3}, 1
    )
    assert planning.details == {
        "adapter": "mcap",
        "attempts": 1,
        "call": "plan",
        "exit_status": 3,
        "version": "1.2.0",
    }
    assert "while planning the source after 1 attempt (exit status 3)" in planning.message
    garbled = lineage.adapter_crashed(
        TRANSFORM, SOURCE, 10, "mcap", "1.2.0", CHUNK, {"reply": "malformed"}, 1
    )
    assert "a reply that does not decode" in garbled.message


@pytest.mark.parametrize(
    "cause",
    [{}, {"signal": "SIGSEGV", "exit_status": 1}, {"error": "RuntimeError"}],
)
def test_a_crash_has_exactly_one_cause(cause: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="one cause"):
        lineage.adapter_crashed(TRANSFORM, SOURCE, 10, "t", "1.0.0", CHUNK, cause, 1)  # type: ignore[arg-type]


def test_a_limit_names_the_adapter_version_call_chunk_limit_and_value() -> None:
    finding = lineage.limit_exceeded(
        TRANSFORM, SOURCE, 10, "mcap", "1.2.0", CHUNK, "memory_bytes", 268435456
    )
    assert finding.code == "neptune.runtime.limit_exceeded"
    assert finding.details == {
        "adapter": "mcap",
        "call": "ingest",
        "chunk": CHUNK,
        "limit": "memory_bytes",
        "value": 268435456,
        "version": "1.2.0",
    }
    assert "at its memory_bytes limit (268435456)" in finding.message
    planning = lineage.limit_exceeded(
        TRANSFORM, SOURCE, 10, "mcap", "1.2.0", None, "wall_seconds", 120
    )
    assert planning.details["call"] == "plan" and "chunk" not in planning.details
    assert "while planning the source" in planning.message


def test_output_invalid_lists_every_problem_and_keeps_its_message_short() -> None:
    long = "x" * 2000
    finding = lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", [long, "second"])
    assert finding.details["problems"] == [long, "second"]
    assert len(finding.message) <= MAX_MESSAGE_LENGTH and finding.message.endswith("...")
    assert "2 cross-chunk laws" in finding.message
    one = lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", ["only"])
    assert "1 cross-chunk law: only" in one.message
    with pytest.raises(ValueError, match="at least one problem"):
        lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", [])


def test_location_findings_cite_the_location_not_bytes() -> None:
    changed = lineage.source_changed(TRANSFORM, LocalPath("run/a.mcap"), SOURCE)
    assert changed.subject == LocalPath("run/a.mcap")
    assert changed.category is FindingCategory.INCONSISTENT and changed.details == {
        "source": SOURCE
    }
    unreadable = lineage.source_unreadable(TRANSFORM, LocalPath("a"), SkipReason.SYMLINK, "ELOOP")
    assert unreadable.category is FindingCategory.SKIPPED
    assert unreadable.details == {"reason": "symlink"} and "symlink" in unreadable.message


@pytest.mark.parametrize(
    ("reason", "severity"),
    [
        (SkipReason.NOT_REGULAR_FILE, Severity.INFO),
        (SkipReason.MISSING, Severity.WARNING),
        (SkipReason.SYMLINK, Severity.WARNING),
        (SkipReason.UNREADABLE, Severity.ERROR),
    ],
)
def test_a_skipped_entry_is_as_severe_as_its_reason(reason: SkipReason, severity: Severity) -> None:
    finding = lineage.entry_skipped(TRANSFORM, LocalPath("x"), reason, "detail")
    assert finding.severity is severity
    assert finding.category is FindingCategory.SKIPPED
    assert finding.details == {"reason": str(reason)}
