"""A contract version's JSON Schema as a page: complete, cross-referenced, deterministic."""

import json
from pathlib import Path
from typing import Any, Final

import pytest
from docsite import schemas

REPO: Final = Path(__file__).resolve().parents[3]

SCHEMA: Final[dict[str, Any]] = {
    "$id": "urn:x:1",
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "X 1",
    "description": "Root *description*.",
    "anyOf": [{"$ref": "#/$defs/Machine"}, {"$ref": "#/$defs/Packet"}],
    "$defs": {
        "Machine": {
            "type": "object",
            "description": "Any robot: arm, AMR, legged, humanoid, marine.",
            "additionalProperties": False,
            "required": ["id", "joints"],
            "properties": {
                "id": {"type": "string", "pattern": "^[a-z]+:[A-Z0-9-]+$"},
                "joints": {"type": "array", "items": {"$ref": "#/$defs/Joint"}},
                "mass": {"anyOf": [{"type": "number"}, {"const": "unknown"}]},
                "pose": {
                    "type": "object",
                    "required": ["frame"],
                    "properties": {"frame": {"type": "string"}},
                },
            },
        },
        "Joint": {"enum": ["revolute", "prismatic"]},
        "Packet": {
            "$id": "urn:x:packet:1",
            "$ref": "#/$defs/Packet",
            "$defs": {
                "Packet": {"type": "object", "properties": {"item": {"$ref": "#/$defs/Item"}}},
                "Item": {"items": [{"type": "object", "properties": {"a": {}}}], "type": "array"},
            },
        },
    },
}
META: Final = {
    "status": "stable",
    "owner_version": "1.0.0",
    "schema_sha256": "sha256:ab",
    "goldens": {"g": 1},
}


def _render(schema: dict[str, Any] = SCHEMA) -> str:
    return schemas.render(
        "x-contract", "1.0.0", schema, META, download="schema.json", owner="neptune"
    )


def test_every_definition_is_a_labelled_section_nested_ones_by_path() -> None:
    page = _render()
    for label, heading in [
        ("schema-x-contract-1-0-0--machine", "Machine"),
        ("schema-x-contract-1-0-0--joint", "Joint"),
        ("schema-x-contract-1-0-0--packet", "Packet"),
        ("schema-x-contract-1-0-0--packet--packet", "Packet / Packet"),
        ("schema-x-contract-1-0-0--packet--item", "Packet / Item"),
    ]:
        assert f"({label})=\n### {heading}\n" in page


def test_refs_resolve_inside_their_own_schema_resource() -> None:
    page = _render()
    # the root's Packet and the nested resource's Packet are different definitions
    assert "{ref}`Packet <schema-x-contract-1-0-0--packet>`" in page
    assert "Accepts: {ref}`Packet / Packet <schema-x-contract-1-0-0--packet--packet>`." in page
    assert "{ref}`Packet / Item <schema-x-contract-1-0-0--packet--item>`" in page


def test_properties_say_required_and_show_every_other_keyword() -> None:
    page = _render()
    assert '- `id` (required): string\n  - `pattern`: `"^[a-z]+:[A-Z0-9-]+$"`' in page
    assert "- `joints` (required): array of {ref}`Joint <schema-x-contract-1-0-0--joint>`" in page
    assert '- `mass` (optional): (number or constant `"unknown"`)' in page
    assert "- `pose` (optional): object\n  - `frame` (required): string" in page
    assert "- `additionalProperties`: `false`" in page
    assert 'Accepts: one of `"revolute"`, `"prismatic"`.' in page


def test_a_subschema_the_summary_cannot_show_is_listed_verbatim() -> None:
    page = _render()
    assert '- `items`: `[{"properties": {"a": {}}, "type": "object"}]`' in page


def test_the_page_states_the_version_facts_and_attaches_the_file() -> None:
    page = _render()
    assert page.startswith("# x-contract 1.0.0\n\nX 1.\n\nRoot \\*description\\*.\n")
    assert "- SHA-256: `sha256:ab`" in page and "- Golden documents: 1" in page
    assert "{download}`schema.json <schema.json>`" in page
    assert "A document is valid when it matches: ({ref}`Machine" in page


def test_an_unresolvable_ref_is_shown_as_written() -> None:
    schema = {"$defs": {"A": {"$ref": "#/properties/b"}, "B": {"$ref": "other.json#/$defs/A"}}}
    page = _render(schema)
    assert "Accepts: `#/properties/b`." in page
    assert "Accepts: `other.json#/$defs/A`." in page
    assert "This version defines no" not in page


def test_definitions_whose_labels_collide_are_refused() -> None:
    with pytest.raises(schemas.SchemaError, match="share a label"):
        _render({"$defs": {"A-b": {}, "a_B": {}}})
    with pytest.raises(schemas.SchemaError, match="not an object"):
        _render({"$defs": {"A": []}})


def test_code_spans_survive_backticks_and_text_is_escaped() -> None:
    assert schemas.code("a") == "`a`"
    assert schemas.code("a`b") == "``a`b``"
    assert schemas.code("`a`") == "`` `a` ``"
    assert schemas.escape("<x> *y* {z}") == "\\<x\\> \\*y\\* \\{z\\}"


def test_versions_sort_numerically() -> None:
    names = ["v1.10.0", "v10.0.0", "v2.0.0", "v1.9.0"]
    assert sorted(names, key=schemas.version_key) == ["v1.9.0", "v1.10.0", "v2.0.0", "v10.0.0"]


def test_every_published_schema_renders_and_rendering_is_deterministic() -> None:
    rendered = 0
    for path in sorted((REPO / "contracts").glob("*/v*/schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        meta = json.loads((path.parent / "version.json").read_text(encoding="utf-8"))
        args = (path.parent.parent.name, path.parent.name[1:], schema, meta)
        first = schemas.render(*args, download="schema.json", owner="o")
        assert first == schemas.render(*args, download="schema.json", owner="o")
        assert first.count("\n### ") == len(schemas.definitions(schema))
        rendered += 1
    assert rendered > 0
