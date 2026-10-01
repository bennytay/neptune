"""The runtime's transform and findings: declared codes, deterministic ids, no leaked text."""

from typing import TYPE_CHECKING

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
from neptune.runtime.lineage import (
    FINDING_CODES,
    RUNTIME_ID,
    RUNTIME_VERSION,
    Failure,
    Law,
    Step,
    runtime_transform,
    type_name,
)

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

SOURCE = content_id(b"some bytes")
TRANSFORM = runtime_transform(2)
CHUNK = "chunk:sha256:" + "ab" * 32
STREAM = "rec:sha256:" + "cd" * 32
CRASH = Failure.raised(Step.INGEST, RuntimeError("at 0x7f00 in /home/someone/x"))


def test_the_runtime_transform_is_its_id_version_and_retry_policy() -> None:
    assert TRANSFORM.adapter_id == RUNTIME_ID == "neptune.runtime"
    assert TRANSFORM.adapter_version == RUNTIME_VERSION
    assert dict(TRANSFORM.config) == {"attempts": 2}
    assert TRANSFORM.libraries == () and TRANSFORM.upstream == ()
    assert runtime_transform(2) == TRANSFORM
    assert runtime_transform(3).id != TRANSFORM.id  # another policy is another lineage


def test_finding_codes_are_declared_sorted_and_prefixed() -> None:
    names = [code.name for code in FINDING_CODES]
    assert names == sorted(names) and len(set(names)) == len(names)
    assert all(name.startswith(f"{RUNTIME_ID}.") for name in names)
    assert all(code.description for code in FINDING_CODES)


def every_finding() -> list[tuple[str, IngestFinding]]:
    return [
        ("chunk", lineage.chunk_failed(TRANSFORM, SOURCE, 10, "text", CHUNK, 2, CRASH)),
        (
            "chunk-series",
            lineage.chunk_failed(
                TRANSFORM,
                SOURCE,
                10,
                "text",
                CHUNK,
                1,
                Failure(Step.CHUNK_SERIES, "ContractError", {"law": "seq_repeated", "seq": 3}),
            ),
        ),
        (
            "plan",
            lineage.plan_failed(
                TRANSFORM, SOURCE, 10, "text", Failure.returned(Step.PLAN_RESULT, [])
            ),
        ),
        (
            "output",
            lineage.output_invalid(
                TRANSFORM, SOURCE, 10, "text", [{"law": "output_silent"}, {"law": "x", "n": 1}]
            ),
        ),
        ("changed", lineage.source_changed(TRANSFORM, LocalPath("a/b.txt"), SOURCE)),
        (
            "unreadable",
            lineage.source_unreadable(TRANSFORM, LocalPath("a"), SkipReason.UNREADABLE, "EACCES"),
        ),
        ("skipped", lineage.entry_skipped(TRANSFORM, RawLocalPath(b"\xff"), SkipReason.MISSING)),
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


def test_chunk_failed_cites_the_whole_source_and_names_only_the_step_and_error_class() -> None:
    finding = lineage.chunk_failed(TRANSFORM, SOURCE, 10, "text", CHUNK, 2, CRASH)
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
        "step": "ingest",
    }
    assert "after 2 attempts (RuntimeError at ingest)" in finding.message
    assert "0x" not in finding.message and "/home" not in finding.message  # never the text
    once = lineage.chunk_failed(TRANSFORM, SOURCE, 10, "text", CHUNK, 1, CRASH)
    assert "after 1 attempt (RuntimeError at ingest)" in once.message and once.id != finding.id


def test_a_failure_is_a_step_an_error_class_and_facts_never_a_repr() -> None:
    returned = Failure.returned(Step.PLAN_RESULT, (n for n in range(0)))
    assert returned.details() == {
        "error": "ContractError",
        "returned": "builtins.generator",
        "step": "plan_result",
    }
    assert type_name(None) == "builtins.NoneType" and type_name(Law.SEQ_REPEATED).endswith(".Law")
    finding = lineage.plan_failed(TRANSFORM, SOURCE, 10, "text", returned)
    assert finding.details == {**returned.details(), "adapter": "text"}
    assert "(ContractError at plan_result)" in finding.message
    with pytest.raises(ValueError, match="never name its error or step"):
        Failure(Step.INGEST, "ValueError", {"step": "elsewhere"}).details()
    assert [str(step) for step in Step] == [
        "plan",
        "plan_result",
        "check_plan",
        "ingest",
        "ingest_result",
        "check_chunk_output",
        "chunk_series",
        "commit",
    ]


def test_output_invalid_lists_every_problem_and_names_each_law_once() -> None:
    problems: list[JsonObject] = [
        {"chunks": [CHUNK, CHUNK], "law": str(Law.SEQ_RANGES_OVERLAP), "seq": 4, "stream": STREAM},
        {"law": str(Law.STREAM_WITHOUT_RUN), "stream": STREAM},
        {"chunks": [CHUNK, CHUNK], "law": str(Law.SEQ_RANGES_OVERLAP), "seq": 9, "stream": STREAM},
    ]
    finding = lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", problems)
    assert finding.details["problems"] == problems
    assert "3 cross-chunk laws (seq_ranges_overlap, stream_without_run)" in finding.message
    assert len(finding.message) <= MAX_MESSAGE_LENGTH
    one = lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", [{"law": "output_silent"}])
    assert "1 cross-chunk law (output_silent)" in one.message
    with pytest.raises(ValueError, match="at least one problem"):
        lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", [])
    with pytest.raises(ValueError, match="names its law"):
        lineage.output_invalid(TRANSFORM, SOURCE, 10, "tally", [{"stream": STREAM}])


def test_location_findings_cite_the_location_not_bytes() -> None:
    changed = lineage.source_changed(TRANSFORM, LocalPath("run/a.mcap"), SOURCE)
    assert changed.subject == LocalPath("run/a.mcap")
    assert changed.category is FindingCategory.INCONSISTENT and changed.details == {
        "source": SOURCE
    }
    unreadable = lineage.source_unreadable(TRANSFORM, LocalPath("a"), SkipReason.SYMLINK, "ELOOP")
    assert unreadable.category is FindingCategory.SKIPPED
    assert unreadable.details == {"errno": "ELOOP", "reason": "symlink"}
    assert "(symlink, ELOOP)" in unreadable.message
    plain = lineage.source_unreadable(TRANSFORM, LocalPath("a"), SkipReason.NOT_REGULAR_FILE)
    assert plain.details == {"reason": "not_regular_file"} and "(not_regular_file)" in plain.message


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
    finding = lineage.entry_skipped(TRANSFORM, LocalPath("x"), reason)
    assert finding.severity is severity
    assert finding.category is FindingCategory.SKIPPED
    assert finding.details == {"reason": str(reason)}
