"""The published golden graph: rebuilt byte for byte, valid against the schema, telling its story.

Listed in ``contracts/graph-schema/contract.toml`` as an owner contract test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from memory_golden_fixtures import PUBLISHED, built, generator, published
from neptune.identity import canonical_json
from neptune_memory.contract.golden import GUESS_CONFIG, build_golden
from neptune_memory.schema.claim import is_inferred
from neptune_memory.schema.export import graph_schema
from neptune_memory.schema.interval import OPEN
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.predicates import CORE_PREDICATES, SAME_AS
from neptune_memory.schema.supersede import FindingCode, as_of, is_closure


def _canonical(value: Any) -> str:
    """The registry's canonical file text (``scripts/contracts.py``)."""
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def test_rebuild_is_byte_identical_and_equals_the_published_golden() -> None:
    first, second = built(), built()
    assert canonical_json.dumps(first.to_json()) == canonical_json.dumps(second.to_json())
    published_text = (PUBLISHED / "golden" / "graph.json").read_text(encoding="utf-8")
    assert _canonical(first.to_json()) == published_text
    assert published() == first


def test_generator_output_is_what_was_published() -> None:
    meta = json.loads((PUBLISHED / "version.json").read_text(encoding="utf-8"))
    produced = generator().goldens()
    assert {name: entry["target"] for name, entry in produced.items()} == meta["goldens"]
    for name, entry in produced.items():
        text = (PUBLISHED / "golden" / name).read_text(encoding="utf-8")
        assert _canonical(entry["value"]) == text, name


def test_schema_export_is_published_and_validates_every_golden() -> None:
    schema = graph_schema()
    assert _canonical(schema) == (PUBLISHED / "schema.json").read_text(encoding="utf-8")
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(built().to_json())
    for claim in built().resolution.claims:
        Draft202012Validator({**schema, "anyOf": [{"$ref": "#/$defs/Claim"}]}).validate(
            claim.to_json()
        )
    Draft202012Validator({**schema, "anyOf": [{"$ref": "#/$defs/PredicateRegistry"}]}).validate(
        CORE_PREDICATES.to_json()
    )


def test_input_order_does_not_change_the_golden() -> None:
    examples = generator().worked_examples()
    shuffled = {name: list(reversed(lines)) for name, lines in reversed(examples.items())}
    assert canonical_json.dumps(build_golden(shuffled).to_json()) == canonical_json.dumps(
        built().to_json()
    )


def test_missing_worked_examples_are_refused() -> None:
    examples = generator().worked_examples()
    del examples["quadruped"]
    with pytest.raises(ValueError, match="quadruped"):
        build_golden(examples)


def test_the_golden_spans_embodiments_inference_identity_and_findings() -> None:
    golden = built()
    claims = golden.resolution.claims
    runs = {c.subject for c in claims if c.predicate == "evidenced_by"}
    assert len(runs) == 4  # drone, manipulator, mobile robot, quadruped
    assert golden.head == 3
    assert {c.recorded_at for c in claims} == {1, 2, 3}
    inferred = [c for c in claims if is_inferred(c.assertion_kind)]
    assert len(inferred) == 2
    for guess in inferred:
        assert guess.provenance.model is not None
        assert guess.provenance.model.to_json() == GUESS_CONFIG["model"]
    (same_as,) = [c for c in claims if c.predicate == SAME_AS]
    assert same_as.assertion_kind == "stated" and same_as.provenance.records
    (finding,) = golden.resolution.findings
    assert finding.code is FindingCode.CLOCK_MISMATCH and finding.superseded_at == OPEN
    assert not any(is_closure(c) for c in claims)


def test_the_operator_supersedes_the_quadruped_guess_at_tx_3() -> None:
    golden = built()
    (guess,) = [c for c in golden.resolution.claims if c.superseded_at == 3]
    assert is_inferred(guess.assertion_kind)
    (correction,) = [c for c in golden.resolution.claims if guess.id in c.supersedes]
    assert correction.assertion_kind == "stated" and correction.recorded_at == 3
    assert (
        isinstance(correction.object, NodeRef) and correction.object.node_id == "asset-tag:QUAD-03"
    )
    at_2 = {c.id for c in as_of(golden.resolution, 2).claims}  # type: ignore[arg-type]
    at_3 = {c.id for c in as_of(golden.resolution, 3).claims}  # type: ignore[arg-type]
    assert guess.id in at_2 and guess.id not in at_3 and correction.id in at_3
