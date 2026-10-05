"""The exported query JSON Schema (ADR 0002 §6): fresh, valid, and never looser than the reader
in the wrong direction (a document the schema rejects is always refused)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune_context.contract import QUERY_VERSION, query_schema
from neptune_context.query import Query, from_json
from neptune_context.query.schema import SCHEMA_ID, main, schema_bytes

PACKAGE = Path(__file__).resolve().parents[1]
EXPORTED = PACKAGE / "docs" / "schema" / "query.schema.json"
GOLDENS = [
    json.loads(p.read_bytes())
    for p in sorted((PACKAGE / "tests" / "golden" / "worked-queries").glob("q*.json"))
]
VALIDATOR = jsonschema.Draft202012Validator(query_schema())


def test_exported_file_is_fresh() -> None:
    assert EXPORTED.read_bytes() == schema_bytes(), (
        "regenerate: uv run python -m neptune_context.query.schema "
        "packages/neptune-context/docs/schema/query.schema.json"
    )


def test_schema_is_valid_draft_2020_12_and_versioned() -> None:
    jsonschema.Draft202012Validator.check_schema(query_schema())
    assert f"urn:neptune:schema:query:{QUERY_VERSION}" == SCHEMA_ID
    schema: Any = query_schema()
    assert schema["$defs"]["Query"]["properties"]["query_version"] == {"const": 1}


def test_export_is_deterministic(tmp_path: Path) -> None:
    out = tmp_path / "q.json"
    assert main([str(out)]) == 0
    assert out.read_bytes() == schema_bytes() == schema_bytes()
    assert main([]) == 2


@pytest.mark.parametrize("doc", GOLDENS)
def test_goldens_satisfy_the_schema(doc: Any) -> None:
    VALIDATOR.validate(doc)


_json = st.recursive(
    st.booleans()
    | st.integers(min_value=-(2**65), max_value=2**65)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=6)
    | st.sampled_from(["head", "open", "any", "m"]),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(
            st.sampled_from(["kind", "ticks", "clock", "frame_id", "x"]), inner, max_size=3
        )
    ),
    max_leaves=10,
)


def _paths(value: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out = [prefix]
    if isinstance(value, dict):
        for key, member in value.items():
            out.extend(_paths(member, (*prefix, key)))
    elif isinstance(value, list):
        for index, member in enumerate(value):
            out.extend(_paths(member, (*prefix, index)))
    return out


def _replace(doc: Any, path: tuple[Any, ...], value: Any) -> Any:
    if not path:
        return value
    copy = json.loads(json.dumps(doc))
    target = copy
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return copy


@settings(max_examples=500, deadline=None)
@given(st.data())
def test_a_document_the_schema_rejects_is_refused(data: st.DataObject) -> None:
    doc = data.draw(st.sampled_from(GOLDENS))
    path = data.draw(st.sampled_from(_paths(doc)))
    mutated = _replace(doc, path, data.draw(_json))
    if not VALIDATOR.is_valid(mutated):
        assert not isinstance(from_json(mutated), Query)
