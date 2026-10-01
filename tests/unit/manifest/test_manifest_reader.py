"""The manifest reader treats its bytes as hostile: bounded, strict, refused whole (ADR 0047 §2)."""

import pytest

from neptune.manifest import MAX_BYTES, MAX_DEPTH, MAX_NODES, ManifestError, parse_manifest
from neptune.manifest.reader import Map, Scalar, Seq, read_tree


def tree(text: str) -> object:
    return read_tree(text.encode("utf-8"), json_syntax=False)


def test_block_and_flow_collections_read_alike() -> None:
    block = tree("a:\n  - x\n  - y: 1\n    z: 'q'\nb: {k: [1, \"two\"]}\n")
    assert isinstance(block, Map)
    items = dict(block.items)
    seq = items["a"]
    assert isinstance(seq, Seq) and seq.items[0] == Scalar("x", "x", 2)
    inner = seq.items[1]
    assert isinstance(inner, Map) and [k for k, _ in inner.items] == ["y", "z"]
    flow = items["b"]
    assert isinstance(flow, Map)


def test_a_sequence_may_sit_at_its_keys_indentation() -> None:
    node = tree("runs:\n- a\n- b\n")
    assert isinstance(node, Map)
    runs = dict(node.items)["runs"]
    assert isinstance(runs, Seq) and len(runs.items) == 2


def test_plain_scalars_keep_their_text() -> None:
    node = tree("version: 1.10\nflag: true\nnothing: ~\nn: 7\n")
    assert isinstance(node, Map)
    values = dict(node.items)
    assert values["version"] == Scalar(1.1, "1.10", 1)
    assert values["flag"] == Scalar(True, "true", 2)
    assert values["nothing"] == Scalar(None, "~", 3)
    assert values["n"] == Scalar(7, "7", 4)


def test_comments_and_quotes() -> None:
    node = tree("a: \"x # not a comment\"  # a comment\nb: it's # yes\nc: 'it''s'\n# whole line\n")
    assert isinstance(node, Map)
    values = {k: v.value for k, v in node.items if isinstance(v, Scalar)}
    assert values == {"a": "x # not a comment", "b": "it's", "c": "it's"}


def test_double_quoted_escapes() -> None:
    node = tree('a: "tab\\there \\u00e9 \\x41 \\U0001F916 \\"q\\""\n')
    assert isinstance(node, Map)
    assert dict(node.items)["a"].value == 'tab\there \u00e9 A \U0001f916 "q"'  # type: ignore[union-attr]


def test_one_leading_document_marker_is_allowed() -> None:
    assert isinstance(tree("---\na: 1\n"), Map)


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("a: &anchor 1\nb: *anchor\n", "anchors"),
        ("a: *alias\n", "aliases"),
        ("a: !!str 1\n", "tags"),
        ("%YAML 1.2\na: 1\n", "directives"),
        ("a: 1\n---\nb: 2\n", "one YAML document"),
        ("a: |\n  text\n", "block scalars"),
        ("a: >\n  text\n", "block scalars"),
        ("? a: 1\n", "complex keys"),
        ("a:\n\tb: 1\n", "tab"),
        ("a: 1\na: 2\n", "duplicate key"),
        ("a: {k: 1, k: 2}\n", "duplicate key"),
        ("a: one\n  two\n", "unexpected indentation"),
        ("a: one\ntwo\n", "expected 'key: value'"),
        ('a: "open\n', "not closed"),
        ('a: "\\q"\n', "unknown escape"),
        ('a: "\\ud800"\n', "not a character"),
        ("a: [1, 2\n", "flow"),
        ("a: [1] x\n", "text after"),
        ("a: b: c\n", "must be quoted"),
        ("- a\nb: 1\n", "unexpected"),
        ("a: @x\n", "reserved"),
        ("machines: {\n", "ends early"),
        ("a: {b: 1,\n", "ends early"),
        ("a: {b\n", "expected ':'"),
        ("a: [\n", "ends early"),
        ("a: [1,\n", "ends early"),
        ("a: {b: \n", "ends early"),
        ("a: .inf\n", "quote it"),
        ("a: -.Inf\n", "quote it"),
        ("a: .nan\n", "quote it"),
        ("a: 0o17\n", "quote it"),
        ("a: 0x1F\n", "quote it"),
        ("a: 1e999\n", "too large"),
    ],
)
def test_refused_yaml(text: str, says: str) -> None:
    with pytest.raises(ManifestError, match=says):
        tree(text)


def test_control_characters_and_bad_utf8_are_refused() -> None:
    with pytest.raises(ManifestError, match="control character"):
        read_tree(b"a: \x00\n", json_syntax=False)
    with pytest.raises(ManifestError, match="not UTF-8"):
        read_tree(b"a: \xff\n", json_syntax=False)


def test_size_cap_is_exact() -> None:
    body = b"neptune: 1\n"
    at_cap = body + b"#" * (MAX_BYTES - len(body))
    assert parse_manifest(at_cap).version == 1
    with pytest.raises(ManifestError, match="larger than"):
        parse_manifest(at_cap + b"#")


def test_depth_cap_is_exact() -> None:
    # The root mapping is level 1, its value level 2, and the innermost scalar a level too.
    ok = "a: " + "[" * (MAX_DEPTH - 2) + "1" + "]" * (MAX_DEPTH - 2) + "\n"
    tree(ok)
    deep = "a: " + "[" * (MAX_DEPTH - 1) + "1" + "]" * (MAX_DEPTH - 1) + "\n"
    with pytest.raises(ManifestError, match="levels deep"):
        tree(deep)
    nested = "".join(" " * n + f"k{n}:\n" for n in range(40)) + " " * 40 + "v: 1\n"
    with pytest.raises(ManifestError, match="levels deep"):
        tree(nested)


def test_node_cap_bounds_wide_documents() -> None:
    wide = "a: [" + ", ".join("1" for _ in range(MAX_NODES + 1)) + "]\n"
    with pytest.raises(ManifestError, match="more than"):
        tree(wide)


def test_json_numbers_keep_their_literal() -> None:
    node = read_tree(b'{"v": 1.10, "n": 7, "b": true}', json_syntax=True)
    assert isinstance(node, Map)
    values = dict(node.items)
    assert values["v"] == Scalar(1.1, "1.10", None)
    assert values["n"] == Scalar(7, "7", None) and values["b"] == Scalar(True, "true", None)


def test_json_is_strict() -> None:
    assert isinstance(read_tree(b'{"a": [1, 2.5, true, null, "x"]}', json_syntax=True), Map)
    for bad, says in [
        (b'{"a": 1, "a": 2}', "duplicate"),
        (b'{"a": NaN}', "NaN"),
        (b'{"a": Infinity}', "Infinity"),
        (b"{'a': 1}", "not JSON"),
        (b"[" * 10_000 + b"]" * 10_000, "levels deep"),
        (b'{"a": 1e999}', "too large"),
        (b'{"a": "\\ud800"}', "surrogate"),
        (b'{"\\udfff": 1}', "surrogate"),
    ]:
        with pytest.raises(ManifestError, match=says):
            read_tree(bad, json_syntax=True)


def test_reading_is_deterministic() -> None:
    text = b"neptune: 1\nruns:\n  - {name: b, paths: [y, x]}\n  - {name: a, paths: [z]}\n"
    assert parse_manifest(text).to_json() == parse_manifest(bytes(text)).to_json()
    assert parse_manifest(text).to_json()["runs"] == [
        {"name": "b", "paths": ["x", "y"]},
        {"name": "a", "paths": ["z"]},
    ]
