"""Provenance for inferred records: produced by a model, heuristic or statistical procedure.

``InferredProvenance`` is deliberately not a ``Grounding``: its ``assertion_kind`` is the string
``"inferred"``, not a ``model.knowledge.AssertionKind``, so the type checker and the runtime guard
in ``Knowledge`` both reject it on canonical records (ADR 0006 §5, ADR 0016 §6).
"""

from dataclasses import dataclass
from typing import Final, Literal

from neptune.model._fields import exact_object, json_str
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

INFERRED: Final = "inferred"


@dataclass(frozen=True)
class InferredProvenance:
    """The evidence an inference read, in the order it read it, and the transform that inferred.

    ``transform`` is a ``TransformRecord`` id naming the model or procedure, its version and config.
    """

    evidence: tuple[EvidenceRef, ...]
    transform: RecordId

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, tuple) or not self.evidence:
            raise ValueError("an inference cites at least one EvidenceRef")
        for ref in self.evidence:
            if not isinstance(ref, EvidenceRef):
                raise TypeError(f"evidence must be EvidenceRefs, got {type(ref).__name__}")
        parse_record_id(self.transform)

    @property
    def assertion_kind(self) -> Literal["inferred"]:
        return INFERRED

    def to_json(self) -> JsonObject:
        return {
            "assertion_kind": INFERRED,
            "evidence": [ref.to_json() for ref in self.evidence],
            "transform": self.transform,
        }


def inferred_provenance_from_json(data: JsonValue) -> InferredProvenance:
    obj = exact_object(data, "inferred provenance", {"assertion_kind", "evidence", "transform"})
    if obj["assertion_kind"] != INFERRED:
        raise ValueError(f"assertion_kind must be {INFERRED!r}, got {obj['assertion_kind']!r}")
    refs = obj["evidence"]
    if not isinstance(refs, list | tuple):
        raise ValueError("evidence must be an array of evidence refs")
    return InferredProvenance(
        tuple(evidence_ref_from_json(ref) for ref in refs),
        parse_record_id(json_str(obj["transform"], "transform")),
    )
