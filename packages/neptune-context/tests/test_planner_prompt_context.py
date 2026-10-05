"""The planner's output contract: the query schema reduced for constrained decoding."""

from __future__ import annotations

import json
from typing import Any

import jsonschema

from neptune_context.query.plan import output_schema, system_prompt, template_sha256
from neptune_context.query.plan import prompt as prompt_module
from neptune_context.query.schema import query_schema
from neptune_context.query.validate import PREDICATES, SUBJECT_KINDS
from planner_golden_context import GOLDEN


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_the_reduced_schema_keeps_only_what_the_api_enforces() -> None:
    schema: dict[str, Any] = dict(output_schema())
    assert schema["type"] == "object" and "$ref" not in schema
    assert not _keys(schema) & (prompt_module._UNSUPPORTED - {"properties"})
    assert "oneOf" not in _keys(schema) and "anyOf" in _keys(schema)
    assert schema["additionalProperties"] is False
    for definition in schema["$defs"].values():
        assert "type" in definition or "anyOf" in definition


def test_the_reduced_schema_has_the_same_members_as_the_query_schema() -> None:
    full: dict[str, Any] = dict(query_schema()["$defs"])  # type: ignore[arg-type]
    reduced: dict[str, Any] = dict(output_schema())
    root = full["Query"]
    assert set(reduced["properties"]) == set(root["properties"])
    assert set(reduced["required"]) == set(root["required"])
    assert set(reduced["$defs"]) == set(full) - {"Query"}


def test_every_golden_expected_query_satisfies_the_reduced_schema() -> None:
    validator = jsonschema.Draft202012Validator(output_schema())
    for line in (GOLDEN / "cases.jsonl").read_text().splitlines():
        expected = json.loads(line)["expected"]["query"]
        if expected is not None:
            assert not list(validator.iter_errors(expected)), expected


def test_the_template_hash_binds_prompt_vocabularies_and_schema() -> None:
    assert template_sha256() == template_sha256()
    text = system_prompt()
    assert all(kind in text for kind in SUBJECT_KINDS) and all(p in text for p in PREDICATES)
    assert "never answer" in text.lower() or "never answer the question" in text.lower().replace(
        "\n", " "
    )
    assert len(template_sha256()) == 64


def test_a_changed_vocabulary_is_a_changed_template(monkeypatch: object) -> None:
    before = template_sha256()
    monkeypatch.setattr(prompt_module, "PREDICATES", PREDICATES | {"novel_predicate"})  # type: ignore[attr-defined]
    assert template_sha256() != before
