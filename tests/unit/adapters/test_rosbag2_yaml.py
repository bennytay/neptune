"""The YAML subset parser: rosbag2's own metadata, byte spans that resolve, and refusals."""

import random
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.rosbag2 import _yaml

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "rosbag2"


def scalars(node: _yaml.Node | None) -> list[_yaml.Scalar]:
    if isinstance(node, _yaml.Scalar):
        return [node]
    if isinstance(node, _yaml.Mapping):
        return [s for e in node.entries for s in [e.key, *scalars(e.value)]]
    if isinstance(node, _yaml.Sequence):
        return [s for item in node.items for s in scalars(item)]
    return []


def test_a_real_metadata_file_reads_with_spans_that_are_the_bytes() -> None:
    data = (FIXTURES / "mobile_base_sqlite3" / "metadata.yaml").read_bytes()
    document = _yaml.parse(data)
    assert document.errors == []
    root = document.root
    assert isinstance(root, _yaml.Mapping)
    info = root.get("rosbag2_bagfile_information")
    assert isinstance(info, _yaml.Mapping)
    found = scalars(info)
    assert len(found) > 40
    for scalar in found:
        raw = data[scalar.start : scalar.end]
        assert raw.decode() == scalar.text or (
            scalar.quoted and raw[:1] in (b'"', b"'", b"|", b">")
        )
    qos = info.get("topics_with_message_count")
    assert isinstance(qos, _yaml.Sequence)
    first = qos.items[0]
    assert isinstance(first, _yaml.Mapping)
    meta = first.get("topic_metadata")
    assert isinstance(meta, _yaml.Mapping)
    profile = meta.get("offered_qos_profiles")
    assert isinstance(profile, _yaml.Scalar) and profile.quoted
    assert profile.text.startswith("- history: 3\n  depth: 0")  # \n escapes resolved
    assert data[profile.start : profile.start + 1] == b'"'


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: 1\n", {"a": "1"}),
        ("a: 'it''s'\n", {"a": "it's"}),
        ('a: "tab\\there \\u00e9 \\x41"\n', {"a": "tab\there \u00e9 A"}),
        ("a: plain text # comment\n", {"a": "plain text"}),
        ("a: 'x # not a comment'\n", {"a": "x # not a comment"}),
        ("a: http://host:80/p\n", {"a": "http://host:80/p"}),
        ("a: ~\nb: ''\n", {"a": "~", "b": ""}),
        ("a:\n", {"a": ""}),
        ("---\na: 1\n...\n", {"a": "1"}),
        ("a: 1\r\nb: 2\r\n", {"a": "1", "b": "2"}),
        ("\ufeffa: 1\n", {"a": "1"}),
        ("'quoted key': 1\n", {"quoted key": "1"}),
    ],
)
def test_scalars_resolve_as_yaml_writes_them(text: str, expected: dict[str, str]) -> None:
    document = _yaml.parse(text.encode())
    assert document.errors == []
    assert isinstance(document.root, _yaml.Mapping)
    assert {e.key.text: scalars(e.value)[0].text for e in document.root.entries} == expected


def test_structures() -> None:
    text = (
        "top:\n"
        "  list:\n"
        "    - a\n"
        "    - key: v\n"
        "      other: w\n"
        "    -\n"
        "      nested: x\n"
        "    - - inner1\n"
        "      - inner2\n"
        "  same:\n"
        "  - s1\n"
        "  - s2\n"
        "  flow: [1, 'two', \"3\"]\n"
        "  map: {a: 1, b: two}\n"
        "  empty: {}\n"
        "  none: []\n"
        "  lit: |\n"
        "    line one\n"
        "      indented\n"
        "\n"
        "    line three\n"
        "  fold: >-\n"
        "    folded\n"
        "    text\n"
        "  after: end\n"
    )
    document = _yaml.parse(text.encode())
    assert document.errors == []
    top = document.root.get("top")  # type: ignore[union-attr]
    assert isinstance(top, _yaml.Mapping)
    items = top.get("list")
    assert isinstance(items, _yaml.Sequence) and len(items.items) == 4
    assert isinstance(items.items[0], _yaml.Scalar)
    assert [e.key.text for e in items.items[1].entries] == ["key", "other"]  # type: ignore[union-attr]
    assert isinstance(items.items[3], _yaml.Sequence) and len(items.items[3].items) == 2
    same = top.get("same")
    assert isinstance(same, _yaml.Sequence) and [s.text for s in same.items] == ["s1", "s2"]  # type: ignore[union-attr]
    flow = top.get("flow")
    assert isinstance(flow, _yaml.Sequence) and [s.text for s in flow.items] == ["1", "two", "3"]  # type: ignore[union-attr]
    mapping = top.get("map")
    assert isinstance(mapping, _yaml.Mapping)
    assert {e.key.text: e.value.text for e in mapping.entries} == {"a": "1", "b": "two"}  # type: ignore[union-attr]
    lit = top.get("lit")
    assert isinstance(lit, _yaml.Scalar) and lit.text == "line one\n  indented\n\nline three\n"
    fold = top.get("fold")
    assert isinstance(fold, _yaml.Scalar) and fold.text == "folded text"
    after = top.get("after")
    assert isinstance(after, _yaml.Scalar) and after.text == "end"
    data = text.encode()
    assert data[lit.start : lit.end].lstrip().startswith(b"line one")


@pytest.mark.parametrize(
    "text",
    [
        "a: &anchor 1\n",
        "a: *alias\n",
        "a: !!str 1\n",
        "a: [[1], 2]\n",
        "a: {b: {c: 1}}\n",
        "a: [1, 2\n",
        "a: 'unterminated\n",
        'a: "bad \\q escape"\n',
        "a: 'x' trailing\n",
        "? complex\n: key\n",
        "a: 1\n\tb: 2\n",
        "%YAML 1.2\na: 1\n",
        "a: 1\n b: 2\n",
        "just a scalar\n",
    ],
)
def test_what_the_subset_does_not_read_is_an_error_not_a_guess(text: str) -> None:
    document = _yaml.parse(text.encode())
    assert document.errors or document.root is None or not scalars(document.root)[1:]


def test_an_error_costs_the_entry_it_belongs_to_not_the_file() -> None:
    text = "good: 1\nbad: &x 2\nalso:\n  nested: 3\nbad2: [[1]]\nlast: 4\n"
    document = _yaml.parse(text.encode())
    assert [e.line for e in document.errors] == [2, 5]
    assert isinstance(document.root, _yaml.Mapping)
    assert [e.key.text for e in document.root.entries] == ["good", "also", "last"]


def test_duplicate_keys_are_recorded_and_the_first_wins() -> None:
    document = _yaml.parse(b"a: 1\nb: 2\na: 3\n")
    assert isinstance(document.root, _yaml.Mapping)
    assert document.root.duplicates == ["a"]
    first = document.root.get("a")
    assert isinstance(first, _yaml.Scalar) and first.text == "1"


def test_limits_hold() -> None:
    assert _yaml.parse(b"").root is None
    assert _yaml.parse(b"# only a comment\n\n").root is None
    big = _yaml.parse(b"a: " + b"x" * (_yaml.MAX_BYTES + 1))
    assert big.root is None and big.errors
    deep = "".join("  " * i + "k:\n" for i in range(_yaml.MAX_DEPTH + 5)) + "  " * 40 + "v: 1\n"
    assert _yaml.parse(deep.encode()).root is None
    wide = "\n".join(f"k{i}: {i}" for i in range(_yaml.MAX_NODES + 10))
    assert _yaml.parse(wide.encode()).root is None
    assert _yaml.parse(b"a: \xff\n").root is None  # not UTF-8


def test_random_text_never_raises_and_spans_stay_inside_the_input() -> None:
    rng = random.Random(3)
    base = (FIXTURES / "split_sqlite3" / "metadata.yaml").read_bytes()
    alphabet = b" :-[]{}'\"#|>\n\t\\&*!?abc123"
    for _ in range(400):
        mutated = bytearray(base)
        for _ in range(rng.choice([1, 2, 8])):
            mutated[rng.randrange(len(mutated))] = rng.choice(alphabet)
        document = _yaml.parse(bytes(mutated))
        for scalar in scalars(document.root):
            assert 0 <= scalar.start <= scalar.end <= len(mutated)
