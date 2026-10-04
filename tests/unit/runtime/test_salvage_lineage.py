"""The salvage findings (ADR 0069): a lost chunk cited by its extent, the ``source_partial``
account, and ``salvage_refused``. Deterministic, bounded and checked like every runtime finding."""

import pytest

from neptune.identity.findings import check_ingest_finding
from neptune.identity.hashing import content_id
from neptune.model.finding import MAX_MESSAGE_LENGTH, FindingCategory, Severity
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.runtime import lineage
from neptune.runtime.lineage import (
    CHUNK_FAILED,
    FINDING_CODES,
    LIMIT_EXCEEDED,
    SALVAGE_REFUSED,
    SOURCE_PARTIAL,
    Failure,
    Lost,
    Step,
    merged,
    runtime_transform,
)

SOURCE = content_id(b"a recording")
TRANSFORM = runtime_transform(2)
SIZE = 1000
CRASH = Failure.raised(Step.INGEST, RuntimeError("at 0x7f00 in /home/someone/x"))
ADAPTER = (TRANSFORM, SOURCE, SIZE, "mcap", "1.2.0")


def chunk(n: int) -> str:
    return f"chunk:sha256:{n:064x}"


def test_both_codes_are_declared() -> None:
    declared = {code.name for code in FINDING_CODES}
    assert {SOURCE_PARTIAL, SALVAGE_REFUSED} <= declared


@pytest.mark.parametrize(
    ("extents", "expected"),
    [
        ([], []),
        ([(5, 5)], []),  # an empty range covers nothing
        ([(10, 20), (0, 5)], [(0, 5), (10, 20)]),
        ([(0, 5), (5, 9)], [(0, 9)]),  # touching ranges join
        ([(0, 10), (2, 4), (8, 12), (20, 30)], [(0, 12), (20, 30)]),
        ([(3, 4), (3, 4)], [(3, 4)]),
    ],
)
def test_merged_is_the_fewest_sorted_disjoint_ranges(
    extents: list[tuple[int, int]], expected: list[tuple[int, int]]
) -> None:
    assert merged(extents) == expected


def test_a_lost_chunk_is_cited_by_its_extent_never_the_whole_source() -> None:
    finding = lineage.chunk_failed(*ADAPTER, chunk(1), 2, CRASH, extent=(100, 250))
    assert finding.subject == EvidenceRef(SOURCE, (ByteRange(100, 150),))
    assert finding.details["extent"] == {"length": 150, "offset": 100}
    assert finding.message.endswith("the chunk's output (bytes [100, 250)) is not in this package")
    assert "0x7f00" not in finding.message and "/home" not in finding.message
    crashed = lineage.adapter_crashed(
        *ADAPTER, Step.INGEST, chunk(1), {"signal": "SIGSEGV"}, 2, extent=(0, 8)
    )
    assert crashed.subject == EvidenceRef(SOURCE, (ByteRange(0, 8),))
    stopped = lineage.limit_exceeded(
        *ADAPTER, Step.INGEST, chunk(1), "wall_seconds", 1, extent=(8, 16)
    )
    assert stopped.code == LIMIT_EXCEEDED and stopped.details["extent"] == {
        "length": 8,
        "offset": 8,
    }
    without = lineage.chunk_failed(*ADAPTER, chunk(1), 2, CRASH)
    assert without.subject == EvidenceRef(SOURCE, (ByteRange(0, SIZE),))
    assert "extent" not in without.details


def test_a_plan_has_no_extent() -> None:
    with pytest.raises(ValueError, match="only a chunk has an extent"):
        lineage.adapter_crashed(*ADAPTER, Step.PLAN, None, {"signal": "SIGSEGV"}, 1, extent=(0, 1))


def test_source_partial_accounts_for_every_lost_chunk_and_byte() -> None:
    lost = [
        Lost(chunk(3), CHUNK_FAILED, (600, 700)),
        Lost(chunk(1), LIMIT_EXCEEDED, (100, 300)),
        Lost(chunk(2), CHUNK_FAILED, (250, 400)),  # overlaps the one before: counted once
        Lost(chunk(4), CHUNK_FAILED, None),
    ]
    finding = lineage.source_partial(*ADAPTER, 10, lost)
    assert check_ingest_finding(finding) == finding
    assert finding.code == SOURCE_PARTIAL
    assert (finding.category, finding.severity) == (FindingCategory.FAILED, Severity.ERROR)
    assert finding.subject == EvidenceRef(SOURCE, (ByteRange(0, SIZE),))
    assert finding.related == (
        EvidenceRef(SOURCE, (ByteRange(100, 300),)),
        EvidenceRef(SOURCE, (ByteRange(600, 100),)),
    )
    assert finding.details == {
        "adapter": "mcap",
        "chunks": 10,
        "codes": {CHUNK_FAILED: 3, LIMIT_EXCEEDED: 1},
        "committed": 6,
        "lost": [
            {"chunk": chunk(1), "code": LIMIT_EXCEEDED, "extent": {"length": 200, "offset": 100}},
            {"chunk": chunk(2), "code": CHUNK_FAILED, "extent": {"length": 150, "offset": 250}},
            {"chunk": chunk(3), "code": CHUNK_FAILED, "extent": {"length": 100, "offset": 600}},
            {"chunk": chunk(4), "code": CHUNK_FAILED},
        ],
        "not_covered": [{"length": 300, "offset": 100}, {"length": 100, "offset": 600}],
        "not_covered_bytes": 400,
        "undeclared": 1,
        "version": "1.2.0",
    }
    assert finding.message == (
        "mcap 1.2.0 lost 4 of 10 chunks of the source: 400 bytes not covered ([100, 400),"
        " [600, 700)); 1 lost chunk without a declared extent; the other 6 are in this package"
    )
    # The order the chunks were lost in changes nothing.
    assert lineage.source_partial(*ADAPTER, 10, list(reversed(lost))) == finding


def test_source_partial_stays_bounded_however_many_chunks_were_lost() -> None:
    lost = [Lost(chunk(n), CHUNK_FAILED, (n * 10, n * 10 + 5)) for n in range(5000)]
    finding = lineage.source_partial(TRANSFORM, SOURCE, 10**6, "rosbag1", "1.0.0", 5001, lost)
    assert len(finding.message) <= MAX_MESSAGE_LENGTH
    assert "and 4996 more" in finding.message
    assert finding.details["not_covered_bytes"] == 5 * 5000
    ranges = finding.details["not_covered"]
    assert isinstance(ranges, list) and len(ranges) == 5000  # every range, in the record


def test_a_lost_extent_covering_the_whole_source_is_not_related_twice() -> None:
    finding = lineage.source_partial(*ADAPTER, 2, [Lost(chunk(1), CHUNK_FAILED, (0, SIZE))])
    assert finding.related == ()
    assert finding.details["not_covered"] == [{"length": SIZE, "offset": 0}]


@pytest.mark.parametrize("count", [0, 3])
def test_a_partial_source_lost_some_chunks_not_none_and_not_all(count: int) -> None:
    lost = [Lost(chunk(n), CHUNK_FAILED, None) for n in range(count)]
    with pytest.raises(ValueError, match="not none and not all"):
        lineage.source_partial(*ADAPTER, 3, lost)


def test_salvage_refused_names_the_laws_the_rest_break() -> None:
    problems = [
        {"law": "stream_undeclared", "stream": "rec:sha256:" + "a" * 64},
        {"law": "stream_undeclared", "stream": "rec:sha256:" + "b" * 64},
        {"law": "output_silent"},
    ]
    finding = lineage.salvage_refused(*ADAPTER, 5, 1, problems)
    assert check_ingest_finding(finding) == finding
    assert finding.subject == EvidenceRef(SOURCE, (ByteRange(0, SIZE),))
    assert finding.message == (
        "mcap 1.2.0: 1 of 5 chunks were lost and the rest break 3 cross-chunk laws without"
        " them (stream_undeclared, output_silent); the source is not in this package"
    )
    assert finding.details["problems"] == problems
    every = lineage.salvage_refused(*ADAPTER, 2, 2, [])
    assert "every chunk was lost (2 chunks)" in every.message


@pytest.mark.parametrize(
    ("lost", "problems"),
    [
        (0, []),  # nothing lost: not a salvage
        (3, []),  # more lost than planned
        (1, []),  # some kept, yet no law broken: that is a partial source, not a refusal
        (2, [{"law": "output_silent"}]),  # everything lost, so nothing to break a law
    ],
)
def test_salvage_refused_is_one_of_two_cases(lost: int, problems: list[dict[str, str]]) -> None:
    with pytest.raises(ValueError):
        lineage.salvage_refused(*ADAPTER, 2, lost, problems)


def test_every_problem_names_its_law() -> None:
    with pytest.raises(ValueError, match="names its law"):
        lineage.salvage_refused(*ADAPTER, 2, 1, [{"stream": "x"}])
