"""Provenance for inferred records: produced by a model, heuristic or statistical procedure.

``InferredProvenance`` is deliberately not a ``Grounding``: its ``assertion_kind`` is the string
``"inferred"``, not a ``model.knowledge.AssertionKind``, so the type checker and the runtime guard
in ``Knowledge`` both reject it on canonical records (ADR 0006 §5, ADR 0016 §6).

Every derived record shares an envelope: ``kind``, ``schema_version`` (``DERIVED_SCHEMA_VERSION``,
not the canonical model's) and ``assertion_kind``; ``derived_object`` reads it strictly.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from neptune.model._fields import exact_object, json_str
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json

INFERRED: Final = "inferred"
DERIVED_SCHEMA_VERSION: Final = 1
_ENVELOPE: Final = frozenset({"assertion_kind", "kind", "schema_version"})


def derived_object(
    data: JsonValue, kind: str, keys: set[str], assertions: frozenset[str] = frozenset({INFERRED})
) -> Mapping[str, JsonValue]:
    """One derived record's JSON, strictly: this derived version, this kind, one of
    ``assertions`` (inferred, unless the kind may be stated or observed), ``keys``."""
    if not isinstance(data, Mapping):
        raise ValueError(f"a {kind} must be a JSON object, got {type(data).__name__}")
    if data.get("schema_version") != DERIVED_SCHEMA_VERSION:
        raise ValueError(
            f"derived schema version {data.get('schema_version')!r} is not this reader's"
            f" {DERIVED_SCHEMA_VERSION}"
        )
    if data.get("kind") != kind:
        raise ValueError(f"expected kind {kind!r}, got {data.get('kind')!r}")
    if data.get("assertion_kind") not in assertions:
        allowed = " or ".join(sorted(assertions))
        raise ValueError(f"a {kind} is {allowed}, got {data.get('assertion_kind')!r}")
    return exact_object(data, kind, keys | _ENVELOPE)


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
