"""The config adapter's own readings checked against independent ones.

- YAML 1.1 typing against PyYAML's resolver and constructor, which implement the 1.1 type
  repository (with three documented differences), and YAML 1.2 typing against the core schema's
  table in the YAML 1.2.2 specification.
- The TOML span scanner against ``tomllib`` on generated documents: every span a value cites
  parses, alone, to the value ``tomllib`` read at its path, and every comment is found.
- The JSON span pass against ``json`` on generated documents.
"""

import json
import math
import tomllib
from datetime import date, datetime, time
from typing import Any, Final

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from neptune.adapters.config import ConfigAdapter
from neptune.adapters.config._json import spans as json_spans
from neptune.adapters.config._scalars import implicit
from neptune.adapters.config._toml import locate
from neptune.adapters.config._tree import Null, Unreadable, Value
from neptune.adapters.harness import ingest_source
from neptune.discovery.reader import BytesReader
from neptune.model.configuration import ConfigScalar, ScalarType
from neptune.model.scalars import NonFinite

# --- YAML 1.1 against PyYAML ---------------------------------------------------------------------

PLAIN: Final = [
    "",
    "~",
    "null",
    "Null",
    "NULL",
    "nULL",
    "true",
    "True",
    "TRUE",
    "tRUE",
    "false",
    "yes",
    "Yes",
    "YES",
    "no",
    "No",
    "on",
    "On",
    "ON",
    "off",
    "Off",
    "OFF",
    "0",
    "-0",
    "+0",
    "7",
    "-17",
    "+42",
    "1_000",
    "0755",
    "0_7",
    "08",
    "0b1010",
    "-0b1",
    "0x1F",
    "0x_1F",
    "0X1F",
    "1:30",
    "190:20:30",
    "-1:30",
    "0.5",
    ".5",
    "-.5",
    "+.5",
    "1.",
    "1.0e+3",
    "1.0e3",
    "1e3",
    "6.8523015e+5",
    "685.230_15e+03",
    "190:20:30.15",
    "1.2.3",
    ".",
    "._",
    ".inf",
    "-.inf",
    "+.inf",
    ".Inf",
    ".INF",
    ".nan",
    ".NaN",
    ".NAN",
    "inf",
    "nan",
    "2001-12-14",
    "2001-12-14t21:59:43.10-05:00",
    "2001-12-14 21:59:43.10 -5",
    "2001-12-15T02:59:43.1Z",
    "2002-12-14",
    "2001-12-14 21:59:43",
    "2001-1-1",
    "base_link",
    "1 2",
    "0o17",
    "0O17",
    "1e",
    "1__0",
]
# Where the adapter follows the 1.1 type repository and PyYAML does not.
PYYAML_DIFFERS: Final = {
    "y",
    "Y",
    "n",
    "N",  # booleans in the repository; strings to PyYAML
    "-.5",
    "+.5",  # floats in the repository's pattern; PyYAML's omits the sign
    "190:20:30.15",  # PyYAML's base-60 float keeps the fraction in the last part only
}
_KINDS: Final = {
    "tag:yaml.org,2002:null": "null",
    "tag:yaml.org,2002:bool": "bool",
    "tag:yaml.org,2002:int": "int",
    "tag:yaml.org,2002:float": "float",
    "tag:yaml.org,2002:timestamp": "timestamp",
    "tag:yaml.org,2002:str": "string",
}


def pyyaml_kind(text: str) -> str:
    resolver: Any = yaml.SafeLoader("")
    return _KINDS[resolver.resolve(yaml.ScalarNode, text, (True, False))]


def kind_of(text: str, version: str) -> str:
    reading = implicit(text, version)  # type: ignore[arg-type]
    if isinstance(reading, Null):
        return "null"
    if isinstance(reading, Unreadable):
        return "unreadable"
    assert isinstance(reading, Value) and len(reading.readings) == 1
    kind = reading.readings[0].type
    return "timestamp" if "date" in kind or "time" in kind else str(kind)


@pytest.mark.parametrize("text", [t for t in PLAIN if t not in PYYAML_DIFFERS])
def test_yaml_1_1_types_agree_with_pyyaml(text: str) -> None:
    assert kind_of(text, "1.1") == pyyaml_kind(text)


@pytest.mark.parametrize("text", [t for t in PLAIN if pyyaml_kind(t) in ("int", "float", "bool")])
def test_yaml_1_1_values_agree_with_pyyaml(text: str) -> None:
    reading = implicit(text, "1.1")
    assert isinstance(reading, Value)
    (scalar,) = reading.readings
    expected = yaml.safe_load(f"v: {text}")["v"]
    if isinstance(scalar.value, NonFinite):
        assert math.isinf(expected) or math.isnan(expected)
        assert str(scalar.value) == ("nan" if math.isnan(expected) else str(expected))
    else:
        assert scalar.value == expected and type(scalar.value) is type(expected)


def test_the_documented_differences_from_pyyaml() -> None:
    assert [kind_of(t, "1.1") for t in ("y", "n", "-.5")] == ["bool", "bool", "float"]
    assert [pyyaml_kind(t) for t in ("y", "n", "-.5")] == ["string", "string", "string"]


# YAML 1.2.2 §10.3.2, the core schema's tag resolution, and its examples.
CORE: Final = {
    "": "null",
    "null": "null",
    "Null": "null",
    "NULL": "null",
    "~": "null",
    "nULL": "string",
    "true": "bool",
    "True": "bool",
    "TRUE": "bool",
    "false": "bool",
    "on": "string",
    "yes": "string",
    "y": "string",
    "0": "int",
    "-19": "int",
    "+12": "int",
    "0o14": "int",
    "0x3A": "int",
    "0755": "int",
    "0b1010": "string",
    "1_000": "string",
    "1:30": "string",
    "0o8": "string",
    "0.": "float",
    "-0.0": "float",
    ".5": "float",
    "+12e03": "float",
    "-2E+05": "float",
    "1e3": "float",
    ".inf": "float",
    "-.Inf": "float",
    ".NAN": "float",
    "2001-12-14": "string",
}


@pytest.mark.parametrize(("text", "kind"), CORE.items())
def test_yaml_1_2_types_follow_the_core_schema(text: str, kind: str) -> None:
    assert kind_of(text, "1.2") == kind


def test_yaml_1_2_reads_leading_zeros_as_decimal_and_0o_as_octal() -> None:
    assert implicit("0755", "1.2") == Value((ConfigScalar(ScalarType.INT, 755),))
    assert implicit("0755", "1.1") == Value((ConfigScalar(ScalarType.INT, 493),))
    assert implicit("0o14", "1.2") == Value((ConfigScalar(ScalarType.INT, 12),))


# --- TOML spans against tomllib ------------------------------------------------------------------

KEYS: Final = st.sampled_from(["a", "b2", "rate", "with-dash", "x_y", "has space", "dot.ted", "ü"])
INTEGERS: Final = st.sampled_from(["0", "-17", "+42", "1_000", "0x1F", "0o17", "0b101"])
FLOATS: Final = st.sampled_from(["0.5", "-0.0", "1e3", "6.02E+23", "inf", "-nan", "1_0.5_0"])
STRINGS: Final = st.sampled_from(
    [
        '"plain"',
        '"esc\\"aped\\u00e9"',
        "'literal \\ raw'",
        '"""\nmulti\n  line"""',
        "'''\nraw\n'''",
        '""',
    ]
)
DATES: Final = st.sampled_from(
    [
        "1979-05-27T07:32:00Z",
        "1979-05-27 07:32:00-07:00",
        "1979-05-27T07:32:00.999999",
        "1979-05-27",
        "07:32:00",
    ]
)
SCALARS: Final = st.one_of(INTEGERS, FLOATS, STRINGS, DATES, st.sampled_from(["true", "false"]))


def toml_key(key: str) -> str:
    return (
        key
        if key.replace("-", "").replace("_", "").isalnum() and key.isascii()
        else json.dumps(key)
    )


@st.composite
def inline(draw: st.DrawFn, depth: int = 0, one_line: bool = False) -> str:
    """An inline value: a scalar, an array (maybe across lines, with comments) or a table."""
    choice = draw(st.integers(0, 4 if depth < 2 else 0))
    if choice <= 2:
        scalar = draw(SCALARS)
        return scalar if not (one_line and "\n" in scalar) else '"one line"'
    if choice == 3:
        items = draw(st.lists(inline(depth + 1, one_line), max_size=3))
        if not one_line and draw(st.booleans()):
            body = "".join(f"\n  {item}, # item {i}" for i, item in enumerate(items))
            return f"[{body}\n]"
        comma: bool = draw(st.booleans())
        trailing = "," if comma and items else ""
        return "[" + ", ".join(items) + trailing + "]"
    keys = draw(st.lists(KEYS, max_size=3, unique=True))  # an inline table is one line
    pairs = (f"{toml_key(k)} = {draw(inline(depth + 1, one_line=True))}" for k in keys)
    return "{ " + ", ".join(pairs) + " }"


@st.composite
def toml_document(draw: st.DrawFn) -> str:
    lines = ["# generated"]
    for key in draw(st.lists(KEYS, min_size=1, max_size=4, unique=True)):
        lines.append(f"{toml_key(key)} = {draw(inline())}  # after {key}")
    for table in draw(st.lists(KEYS, max_size=2, unique=True)):
        header = f"table {table}"
        lines.append(f"[{toml_key(header)}]")
        for key in draw(st.lists(KEYS, max_size=3, unique=True)):
            lines.append(f"{toml_key(key)}.leaf = {draw(inline())}")
    for _ in range(draw(st.integers(0, 2))):
        lines.append("[[arrays]]")
        for key in draw(st.lists(KEYS, max_size=2, unique=True)):
            lines.append(f"{toml_key(key)} = {draw(inline())}")
        lines.append("  [arrays.sub]")
        lines.append(f"  v = {draw(inline())}")
    return "\n".join(lines) + "\n"


def plain(node: Any) -> Any:
    """A tomllib value with NaN made comparable."""
    if isinstance(node, float) and math.isnan(node):
        return "nan"
    if isinstance(node, dict):
        return {k: plain(v) for k, v in node.items()}
    if isinstance(node, list):
        return [plain(v) for v in node]
    if isinstance(node, datetime | date | time):
        return node.isoformat()
    return node


def at(tree: Any, path: tuple[Any, ...]) -> Any:
    for segment in path:
        tree = tree[segment]
    return tree


@settings(max_examples=300, deadline=None)
@given(toml_document())
def test_every_toml_span_parses_to_the_value_tomllib_read(document: str) -> None:
    tree = tomllib.loads(document)
    found = locate(document)
    for path, (start, end) in found.spans.items():
        written = document[start:end]
        if not path:
            assert written == document
        elif written.startswith("["):
            if isinstance(at(tree, path), dict) and written.rstrip().endswith("]"):
                continue  # a [table] header: its keys, not its value
            assert plain(tomllib.loads(f"v = {written}")["v"]) == plain(at(tree, path))
        else:
            assert plain(tomllib.loads(f"v = {written}")["v"]) == plain(at(tree, path))
    comments = [document[s:e] for s, e in found.comments]
    assert comments[0] == "# generated"
    assert all(c.startswith("#") and "\n" not in c for c in comments)
    assert len(comments) == document.count("#")  # no generated string holds a #


@settings(max_examples=100, deadline=None)
@given(toml_document())
def test_generated_toml_ingests_with_every_scalar_cited(document: str) -> None:
    output = ingest_source(ConfigAdapter(), BytesReader(document.encode()))
    assert not output.findings()
    for record in output.records():
        if record.kind == "configuration_value" and record.path:
            state = record.value
            provenance = getattr(state, "provenance", None)
            if getattr(provenance, "evidence", None) is not None:
                (step,) = provenance.evidence.locator  # type: ignore[union-attr]
                assert step.kind == "span"


# --- JSON spans against json ---------------------------------------------------------------------

JSON_VALUES: Final = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-(10**20), 10**20),
        st.floats(allow_nan=False, allow_infinity=False),
        st.text(max_size=5),
    ),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=4), children, max_size=3)
    ),
    max_leaves=12,
)


def preorder(node: Any) -> list[Any]:
    found = [node]
    if isinstance(node, dict):
        for value in node.values():
            found += preorder(value)
    elif isinstance(node, list):
        for value in node:
            found += preorder(value)
    return found


@settings(max_examples=300, deadline=None)
@given(JSON_VALUES, st.sampled_from([None, 2]), st.booleans())
def test_every_json_span_parses_to_its_value(value: Any, indent: int | None, ascii_: bool) -> None:
    text = json.dumps(value, indent=indent, ensure_ascii=ascii_)
    found = json_spans(text)
    nodes = preorder(json.loads(text))
    assert len(found) == len(nodes)
    for (start, end), node in zip(found, nodes, strict=True):
        assert json.loads(text[start:end]) == node
