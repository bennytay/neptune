"""The published golden graph: rebuilt byte for byte, valid against the schema, telling its story.

Listed in ``contracts/graph-schema/contract.toml`` as an owner contract test.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from jsonschema import Draft202012Validator

from memory_golden_fixtures import EARLIER, PUBLISHED, built, generator, published
from neptune.identity import canonical_json
from neptune_memory.contract._fixture_model import FIXTURE_MODEL
from neptune_memory.contract.golden import TRANSACTIONS, build_golden
from neptune_memory.contract.suite import CHECKS, load_golden
from neptune_memory.schema.claim import is_inferred
from neptune_memory.schema.export import graph_schema
from neptune_memory.schema.interval import OPEN, ledger_tx
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.predicates import CORE_PREDICATES, SAME_AS, SAME_AS_CANDIDATE
from neptune_memory.schema.reference import ReferenceReader
from neptune_memory.schema.supersede import FindingCode, as_of, is_closure

if TYPE_CHECKING:
    from pathlib import Path

    from neptune_memory.schema.claim import Claim


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


@pytest.mark.parametrize("earlier", EARLIER, ids=lambda p: p.name)
def test_every_earlier_published_golden_still_loads_and_passes_the_suite(earlier: Path) -> None:
    """1.1.0, 1.2.0, 1.3.0 and 1.7.0 are minor releases: a consumer pinned to an earlier minor
    keeps its golden and its answers."""
    golden = load_golden(earlier / "golden" / "graph.json")
    for check in CHECKS:
        check(ReferenceReader, golden)


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
    claims, findings = golden.resolution.claims, golden.resolution.findings
    runs = {c.subject for c in claims if c.predicate == "evidenced_by"}
    assert len(runs) == 4  # drone, manipulator, mobile robot, quadruped
    inferred = [c for c in claims if is_inferred(c.assertion_kind)]
    assert all(c.provenance.model == FIXTURE_MODEL for c in inferred)
    # The drone's two ids: the fleet register's identity link and the operator's assertion.
    identities = [c for c in claims if c.predicate == SAME_AS]
    assert len(identities) == 2 and len({c.provenance.records for c in identities}) == 2
    assert all(
        c.assertion_kind == "stated" and c.object == identities[0].object for c in identities
    )
    # Review of PR #60: everything the suite must be able to bite on is in the golden.
    candidates = [c for c in claims if c.predicate == SAME_AS_CANDIDATE]
    assert len(candidates) == 2 and all(is_inferred(c.assertion_kind) for c in candidates)
    codes = {f.code for f in findings}
    assert codes == {FindingCode.CLOCK_MISMATCH, FindingCode.OVERRIDDEN_ON_ARRIVAL}
    assert any(f.superseded_at != OPEN for f in findings)  # a finding that closes
    assert any(is_closure(c) for c in claims)  # a split closure version
    (overridden,) = [f for f in findings if f.code is FindingCode.OVERRIDDEN_ON_ARRIVAL]
    (winner,) = overridden.others
    assert {c.id: c for c in claims}[winner].superseded_at != OPEN  # its winner is superseded
    reader = ReferenceReader(golden)
    assert any(_corroborated(reader.claims(r, "recorded_by", golden.head).claims) for r in runs)
    assert any(reader.neighbours(r, 2, golden.head).findings for r in runs)


def _corroborated(current: tuple[Claim, ...]) -> bool:
    objects = [(c.object, c.valid.domain_id) for c in current]
    return len(objects) != len(set(objects))


def test_a_transaction_with_no_claims_is_still_the_head() -> None:
    golden = built()
    assert golden.head == TRANSACTIONS[-1][0] == 5
    assert max(c.recorded_at for c in golden.resolution.claims) == 4
    reader = ReferenceReader(golden)
    assert reader.head == 5
    run = golden.resolution.claims[0].subject
    assert reader.claims(run, None, ledger_tx(5)) == replace(
        reader.claims(run, None, ledger_tx(4)), as_of=ledger_tx(5)
    )


def test_the_operator_supersedes_the_quadruped_guess_at_tx_3() -> None:
    golden = built()
    (guess,) = [
        c
        for c in golden.resolution.claims
        if c.superseded_at == 3 and c.recorded_at < 3 and is_inferred(c.assertion_kind)
    ]
    (correction,) = [c for c in golden.resolution.claims if guess.id in c.supersedes]
    assert correction.assertion_kind == "stated" and correction.recorded_at == 3
    assert isinstance(correction.object, NodeRef)
    assert correction.object.node_id == "asset-tag:QUAD-03"
    at_2 = {c.id for c in as_of(golden.resolution, ledger_tx(2)).claims}
    at_3 = {c.id for c in as_of(golden.resolution, ledger_tx(3)).claims}
    assert guess.id in at_2 and guess.id not in at_3 and correction.id in at_3
