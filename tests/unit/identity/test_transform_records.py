from dataclasses import replace

import pytest

from neptune.identity.hashing import content_id
from neptune.identity.ids import adapter_record_id, config_hash
from neptune.identity.provenance import (
    check_transform_record,
    evidence_record_id,
    transform_record,
    transform_record_id,
)
from neptune.model.ids import ExternalObjectRef, RecordId
from neptune.model.provenance import (
    EvidenceRef,
    RowCell,
    TransformRecord,
    transform_record_from_json,
)

SOURCE = content_id(b"joint,max_velocity\nshoulder,90\n")
CELL = EvidenceRef(SOURCE, (RowCell(1, 1, "max_velocity"),))


def test_transform_records_are_deterministic_and_self_verifying() -> None:
    first = transform_record(
        adapter_id="csv",
        adapter_version="1.0.0",
        config={"header_rows": 1, "delimiter": ","},
        libraries={"b": "2", "a": "1"},
    )
    second = transform_record(
        adapter_id="csv",
        adapter_version="1.0.0",
        config={"delimiter": ",", "header_rows": 1},
        libraries={"a": "1", "b": "2"},
    )
    assert first == second
    assert first.id.startswith("rec:sha256:")
    assert first.config_hash == config_hash({"delimiter": ",", "header_rows": 1})
    assert first.libraries == (("a", "1"), ("b", "2"))
    assert check_transform_record(transform_record_from_json(first.to_json())) == first


def test_tampered_transform_records_fail_verification() -> None:
    record = transform_record(adapter_id="csv", adapter_version="1.0.0", config={"a": 1})
    with pytest.raises(ValueError, match="config"):
        check_transform_record(replace(record, config={"a": 2}))
    with pytest.raises(ValueError, match="id"):
        check_transform_record(replace(record, adapter_version="1.0.1"))


def test_every_field_changes_the_transform_id() -> None:
    base = dict(adapter_id="csv", adapter_version="1.0.0", config={"a": 1}, libraries={"x": "1"})
    ids = {
        transform_record(**base).id,  # type: ignore[arg-type]
        transform_record(**{**base, "adapter_id": "tsv"}).id,  # type: ignore[arg-type]
        transform_record(**{**base, "adapter_version": "1.0.1"}).id,  # type: ignore[arg-type]
        transform_record(**{**base, "config": {"a": 2}}).id,  # type: ignore[arg-type]
        transform_record(**{**base, "libraries": {"x": "2"}}).id,  # type: ignore[arg-type]
    }
    upstream = transform_record(**base).id  # type: ignore[arg-type]
    ids.add(transform_record(**base, upstream=[upstream]).id)  # type: ignore[arg-type]
    assert len(ids) == 6


def test_adapter_records_use_exactly_the_adr_0003_formula() -> None:
    adapter = transform_record(adapter_id="csv", adapter_version="1.0.0", config={})
    assert evidence_record_id("joint_limit", CELL, adapter) == adapter_record_id(
        kind="joint_limit",
        source=SOURCE,
        locator=CELL.locator_json(),
        adapter_id="csv",
        adapter_version="1.0.0",
        config=adapter.config_hash,
    )


def test_library_versions_are_not_lineage_inputs_but_adapter_versions_are() -> None:
    v1 = transform_record(adapter_id="csv", adapter_version="1", config={}, libraries={"x": "1"})
    v1_relocked = replace(v1, libraries=(("x", "2"),))
    v2 = replace(v1, adapter_version="2")
    assert evidence_record_id("k", CELL, v1) == evidence_record_id("k", CELL, v1_relocked)
    assert evidence_record_id("k", CELL, v1) != evidence_record_id("k", CELL, v2)


def test_normalised_records_are_scoped_to_their_whole_chain() -> None:
    v1 = transform_record(adapter_id="csv", adapter_version="1", config={})
    v2 = transform_record(adapter_id="csv", adapter_version="2", config={})

    def si(upstream: RecordId) -> TransformRecord:
        return transform_record(
            adapter_id="neptune.si", adapter_version="1", config={}, upstream=[upstream]
        )

    over_v1, over_v2 = si(v1.id), si(v2.id)
    assert evidence_record_id("si_value", CELL, over_v1) != evidence_record_id(
        "si_value", CELL, over_v2
    )
    assert evidence_record_id("si_value", CELL, over_v1) != evidence_record_id("si_value", CELL, v1)
    assert transform_record_id(over_v1) == over_v1.id


def test_unfetched_evidence_has_no_tier_2_id() -> None:
    external = EvidenceRef(ExternalObjectRef("s3", "b/k", "etag"), CELL.locator)
    adapter = transform_record(adapter_id="csv", adapter_version="1", config={})
    with pytest.raises(ValueError, match="content id"):
        evidence_record_id("k", external, adapter)
