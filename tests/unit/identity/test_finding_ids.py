"""Finding ids come from their whole content: identical findings are one finding (ADR 0017 §9)."""

from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from neptune.identity import canonical_json
from neptune.identity.findings import check_ingest_finding, finding_id, ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity, ingest_finding_from_json
from neptune.model.ids import RecordId
from neptune.model.provenance import ByteRange, EvidenceRef
from neptune.model.source import LocalPath

LOG = content_id(b"\x89MCAP0\r\n" + bytes(1024))
MCAP_V1 = transform_record(adapter_id="mcap", adapter_version="1.0.0", config={})
MCAP_V2 = transform_record(adapter_id="mcap", adapter_version="1.1.0", config={})
CHUNK = EvidenceRef(LOG, (ByteRange(512, 256),))
STREAM = RecordId("rec:sha256:" + "a" * 64)
DOMAIN = RecordId("rec:sha256:" + "b" * 64)


def truncated(**changes: Any) -> IngestFinding:
    fields: dict[str, Any] = {
        "code": "mcap.truncated",
        "category": FindingCategory.CORRUPT,
        "severity": Severity.ERROR,
        "subject": CHUNK,
        "transform": MCAP_V1,
        "message": "the file ends inside chunk 3",
        "details": {"missing_bytes": 128},
    }
    fields.update(changes)
    return ingest_finding(**fields)


def test_the_same_problem_found_twice_is_one_finding() -> None:
    assert truncated() == truncated()
    assert truncated().id == truncated().id
    assert truncated().id.startswith("rec:sha256:")
    assert canonical_json.dumps(truncated().to_json()) == canonical_json.dumps(
        truncated().to_json()
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"code": "mcap.bad_magic"},
        {"category": FindingCategory.LIMIT},
        {"severity": Severity.WARNING},
        {"subject": EvidenceRef(LOG, (ByteRange(768, 256),))},
        {"subject": LocalPath("run.mcap")},
        {"transform": MCAP_V2},
        {"message": "the file ends inside chunk 4"},
        {"details": {"missing_bytes": 129}},
        {"related": [EvidenceRef(LOG, (ByteRange(0, 8),))]},
        {"records": [STREAM]},
    ],
)
def test_every_field_changes_the_id(changes: dict[str, Any]) -> None:
    assert truncated(**changes).id != truncated().id


def test_a_new_adapter_version_gives_new_finding_ids() -> None:
    assert truncated(transform=MCAP_V2).id != truncated().id


def test_the_builder_normalises_records_and_copies_details() -> None:
    details = {"missing_bytes": 128}
    finding = truncated(records=[DOMAIN, STREAM, DOMAIN], details=details)
    details["missing_bytes"] = 0
    assert finding.records == (STREAM, DOMAIN)
    assert finding.details == {"missing_bytes": 128}
    assert finding == truncated(records=[STREAM, DOMAIN])


def test_stored_findings_are_verified_against_their_content() -> None:
    stored = canonical_json.loads(canonical_json.dumps(truncated().to_json()))
    assert check_ingest_finding(ingest_finding_from_json(stored)) == truncated()
    with pytest.raises(ValueError, match="does not match"):
        check_ingest_finding(replace(truncated(), message="the file is fine"))
    assert finding_id(truncated()) == truncated().id


@given(st.dictionaries(st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True), st.integers()))
def test_ids_do_not_depend_on_details_key_order(details: dict[str, int]) -> None:
    reordered = dict(reversed(list(details.items())))
    assert truncated(details=details).id == truncated(details=reordered).id
