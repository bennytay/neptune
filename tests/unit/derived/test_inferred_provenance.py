import pytest

from neptune.derived.provenance import InferredProvenance, inferred_provenance_from_json
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.ids import record_id
from neptune.model.provenance import EvidenceRef, Page, Span

SPEC_SHEET = content_id(b"%PDF-1.7 payload 14 kg")
EXTRACTOR = record_id("transform_record", {"adapter_id": "llm-extract"})
PAYLOAD = EvidenceRef(SPEC_SHEET, (Page(1), Span(120, 160)))


def test_inferred_provenance_round_trips_with_its_kind_fixed() -> None:
    original = InferredProvenance((PAYLOAD,), EXTRACTOR)
    data = canonical_json.loads(canonical_json.dumps(original.to_json()))
    assert data["assertion_kind"] == "inferred"  # type: ignore[index, call-overload]
    assert inferred_provenance_from_json(data) == original


def test_an_inference_cites_evidence() -> None:
    with pytest.raises(ValueError, match="at least one"):
        InferredProvenance((), EXTRACTOR)
    with pytest.raises(TypeError):
        InferredProvenance((SPEC_SHEET,), EXTRACTOR)  # type: ignore[arg-type]


def test_evidence_layer_provenance_is_not_read_as_inferred() -> None:
    data = dict(InferredProvenance((PAYLOAD,), EXTRACTOR).to_json())
    data["assertion_kind"] = "observed"
    with pytest.raises(ValueError, match="inferred"):
        inferred_provenance_from_json(data)
