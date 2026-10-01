"""Configuration records: what each field may hold, how they compare, and their digest (ADR 0037).

The comparison is the contract MVL-38's bindings and any diff of two runs rely on: two snapshots
have equal digests exactly when ``compare_configurations`` finds no change between them.
"""

from dataclasses import replace
from typing import Any, Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from neptune.identity import canonical_json
from neptune.identity.configuration import configuration_digest
from neptune.identity.hashing import content_id
from neptune.identity.provenance import evidence_record_id, transform_record
from neptune.model.configuration import (
    ChangeKind,
    CollectionType,
    ConfigAlias,
    ConfigCollection,
    ConfigFormat,
    ConfigScalar,
    ConfigurationChange,
    ConfigurationSnapshot,
    ConfigurationValue,
    LineEndings,
    ScalarType,
    TextEncoding,
    ValueDigest,
    compare_configurations,
    comparison_key,
    configuration_snapshot_from_json,
    configuration_value_from_json,
    path_sort_key,
)
from neptune.model.ids import RecordId
from neptune.model.kinds import KIND_SINCE, kinds_at, package_version
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import EvidenceRef, JsonPointer, Provenance, Span
from neptune.model.record import SCHEMA_VERSION, Family, SchemaVersionError
from neptune.model.scalars import NonFinite
from neptune.model.schema import canonical_schema
from neptune.model.versions import DeclaredVersion

SOURCE: Final = content_id(b"controller_server: {}\n")
ADAPTER: Final = transform_record(adapter_id="config", adapter_version="0.1.0", config={})
SNAPSHOT_AT: Final = Provenance(
    EvidenceRef(SOURCE, (JsonPointer(""),)), ADAPTER.id, AssertionKind.OBSERVED
)
SNAPSHOT_ID: Final = evidence_record_id("configuration_snapshot", SNAPSHOT_AT.evidence, ADAPTER)
VALIDATOR: Final = Draft202012Validator(canonical_schema())


def at(pointer: str) -> Provenance:
    return Provenance(
        EvidenceRef(SOURCE, (JsonPointer(pointer),)), ADAPTER.id, AssertionKind.OBSERVED
    )


def span(start: int, end: int) -> Provenance:
    return Provenance(EvidenceRef(SOURCE, (Span(start, end),)), ADAPTER.id, AssertionKind.OBSERVED)


def pointer(path: tuple[str | int, ...]) -> str:
    return "".join("/" + str(s).replace("~", "~0").replace("/", "~1") for s in path)


def value(
    path: tuple[str | int, ...],
    node: Any,
    text: Any = None,
    *,
    order: int = 0,
    tag: Any = None,
    snapshot: RecordId = SNAPSHOT_ID,
    provenance: Provenance | None = None,
) -> ConfigurationValue:
    """A value at ``path``: ``node`` is a ConfigNode, or a ready Knowledge state."""
    provenance = provenance or at(pointer(path))
    state = (
        node if not isinstance(node, ConfigScalar | ConfigCollection | ConfigAlias) else Known(node)
    )
    if text is None:
        text = (
            NotApplicable()
            if isinstance(node, ConfigCollection | ConfigAlias)
            else Known(str(getattr(node, "value", "")))
        )
    return ConfigurationValue(
        id=evidence_record_id("configuration_value", provenance.evidence, ADAPTER),
        provenance=provenance,
        snapshot=snapshot,
        path=path,
        order=order if path else 0,
        tag=tag if tag is not None else NotCovered(),
        text=text,
        value=state,
    )


def scalar(kind: str, raw: Any) -> ConfigScalar:
    return ConfigScalar(ScalarType(kind), raw)


def mapping(length: int) -> ConfigCollection:
    return ConfigCollection(CollectionType.MAPPING, length)


def snapshot(document: list[ConfigurationValue], **fields: Any) -> ConfigurationSnapshot:
    base: dict[str, Any] = {
        "id": SNAPSHOT_ID,
        "provenance": SNAPSHOT_AT,
        "format": ConfigFormat.YAML,
        "format_version": Known(DeclaredVersion("1.2"), span(0, 9)),
        "encoding": TextEncoding.UTF_8,
        "byte_order_mark": False,
        "line_endings": LineEndings.LF,
        "comments": (Known("# a comment", span(10, 21)),),
        "values": len(document),
        "digest": configuration_digest(document),
    }
    return ConfigurationSnapshot(**{**base, **fields})


DOCUMENT: Final = [
    value((), mapping(2)),
    value(("rate",), scalar("float", 20.0), Known("20.0"), order=0, tag=Known("?")),
    value(("frame",), scalar("string", "base_link"), order=1, tag=Known("!")),
]


# --- The records ---------------------------------------------------------------------------------


def test_both_kinds_are_machine_records_added_in_schema_version_2() -> None:
    for kind in (ConfigurationSnapshot, ConfigurationValue):
        assert kind.family is Family.MACHINE and kind.since == 2
        assert KIND_SINCE[kind.kind] == 2
    assert SCHEMA_VERSION == 2


@pytest.mark.parametrize(
    ("record", "read"),
    [
        (snapshot(DOCUMENT), configuration_snapshot_from_json),
        *((v, configuration_value_from_json) for v in DOCUMENT),
    ],
)
def test_records_round_trip_strictly_at_version_2(record: Any, read: Any) -> None:
    data = canonical_json.loads(canonical_json.dumps(record.to_json()))
    assert isinstance(data, dict) and data["schema_version"] == 2
    assert read(data) == record
    assert list(VALIDATOR.iter_errors(data)) == []
    with pytest.raises(SchemaVersionError, match="from schema version 2"):
        read({**data, "schema_version": 1})  # no version 1 reader ever wrote one
    with pytest.raises(SchemaVersionError, match="newer"):
        read({**data, "schema_version": 3, "later": 1})
    with pytest.raises(ValueError):
        read({**data, "extra": 1})


def test_every_value_shape_round_trips_and_validates() -> None:
    shapes = [
        value(("b",), scalar("bool", True)),
        value(("i",), scalar("int", 10**300)),
        value(("f",), scalar("float", NonFinite.NEGATIVE_INFINITY)),
        value(("z",), scalar("float", -0.0)),
        value(("s",), scalar("string", "")),
        value(("x",), scalar("binary", "aGVsbG8=")),
        value(("o",), scalar("offset_datetime", "2026-08-14T09:30:00+08:00")),
        value(("l",), scalar("local_datetime", "2026-09-01T07:15:00.500000")),
        value(("d",), scalar("local_date", "2027-02-14")),
        value(("t",), scalar("local_time", "07:30:00")),
        value(("seq",), ConfigCollection(CollectionType.SEQUENCE, 0)),
        value(("alias",), ConfigAlias("base", ("defaults", 0))),
        value(("null",), KnownAbsent(SNAPSHOT_AT), Known("~")),
        value(("unknown",), Unknown(span(3, 9)), Unknown()),
        value(
            ("on",),
            Ambiguous((Candidate(scalar("bool", True)), Candidate(scalar("string", "on")))),
            Known("on"),
        ),
        value(("odd key/~",), scalar("int", 1), order=7),
    ]
    for record in shapes:
        data = canonical_json.loads(canonical_json.dumps(record.to_json()))
        assert configuration_value_from_json(data) == record
        assert list(VALIDATOR.iter_errors(data)) == [], record.path


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"path": ("a", True)}, "keys and positions"),
        ({"path": ("a", -1)}, "keys and positions"),
        ({"order": -1}, "position"),
        ({"text": NotApplicable()}, "collections and aliases"),
        ({"value": NotApplicable()}, "every node has a value"),
        ({"tag": Known("")}, "non-empty"),
        ({"snapshot": "rec:nope"}, "record id"),
    ],
)
def test_values_refuse_what_they_cannot_mean(change: dict[str, Any], error: str) -> None:
    with pytest.raises((ValueError, TypeError), match=error):
        replace(DOCUMENT[1], **change)


def test_only_scalars_are_ambiguous_and_collections_have_no_text() -> None:
    with pytest.raises(ValueError, match="only scalar readings"):
        value(
            ("x",),
            Ambiguous((Candidate(mapping(1)), Candidate(scalar("string", "x")))),
            Known("x"),
        )
    with pytest.raises(ValueError, match="collections and aliases"):
        value(("x",), mapping(1), Known("{}"))
    with pytest.raises(ValueError, match="root is at order 0"):
        replace(DOCUMENT[0], order=3)


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        ("bool", 1),
        ("int", True),
        ("int", 1.0),
        ("float", 1),
        ("float", float("nan")),
        ("string", 1),
        ("string", NonFinite.NAN),
        ("string", "\ud800"),
        ("binary", "not base64!"),
        ("local_date", "2026-9-1"),
        ("offset_datetime", "2026-09-01T07:15:00Z"),
        ("local_time", "7:30"),
    ],
)
def test_scalars_hold_exactly_their_type(kind: str, raw: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        scalar(kind, raw)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"values": 0}, "at least its root"),
        ({"digest": "sha256:xyz"}, "value digest"),
        ({"comments": (Known("# inherited"),)}, "its own span"),
        ({"comments": (Unknown(span(0, 1)),)}, "its own span"),
        ({"format": "yaml"}, "ConfigFormat"),
        ({"byte_order_mark": 0}, "bool"),
        ({"format_version": Known("1.2")}, "DeclaredVersion"),
    ],
)
def test_snapshots_refuse_what_they_cannot_mean(change: dict[str, Any], error: str) -> None:
    with pytest.raises((ValueError, TypeError), match=error):
        snapshot(DOCUMENT, **change)


# --- Versions and packages ---------------------------------------------------------------------


def test_a_package_is_written_at_the_lowest_version_that_holds_its_records() -> None:
    assert package_version(["run", "stream"]) == 1
    assert package_version([]) == 1
    assert package_version(["run", "configuration_value"]) == 2
    assert set(kinds_at(2)) - set(kinds_at(1)) == {
        "configuration_snapshot",
        "configuration_value",
    }
    with pytest.raises(SchemaVersionError):
        kinds_at(3)


# --- Comparing and digests -----------------------------------------------------------------------


def changes(left: list[ConfigurationValue], right: list[ConfigurationValue]) -> list[Any]:
    return [(c.path, c.change) for c in compare_configurations(left, right)]


def other(record: ConfigurationValue) -> ConfigurationValue:
    """The same declaration in another snapshot: another source, so other ids."""
    source = content_id(b"another file")
    evidence = EvidenceRef(source, record.provenance.evidence.locator)
    provenance = Provenance(evidence, ADAPTER.id, AssertionKind.OBSERVED)
    return replace(
        record,
        id=evidence_record_id("configuration_value", evidence, ADAPTER),
        provenance=provenance,
        snapshot=RecordId("rec:sha256:" + "5" * 64),
    )


def test_equal_declarations_compare_equal_whatever_their_spelling_or_order() -> None:
    left = list(DOCUMENT)
    right = [
        other(DOCUMENT[0]),
        other(replace(DOCUMENT[1], text=Known("2e1"), order=1, tag=Known("!!float"))),
        other(replace(DOCUMENT[2], order=0, tag=Known("?"))),
    ]
    assert compare_configurations(left, right) == ()
    assert configuration_digest(left) == configuration_digest(right)


def test_changes_are_added_removed_and_changed_paths_in_path_order() -> None:
    left = [*DOCUMENT, value(("gone",), scalar("int", 1), order=2)]
    right = [
        other(DOCUMENT[0]),
        other(replace(DOCUMENT[1], value=Known(scalar("float", 25.0)))),
        other(DOCUMENT[2]),
        other(value(("new", 0), scalar("int", 1))),
    ]
    assert changes(left, right) == [
        (("gone",), ChangeKind.REMOVED),
        (("new", 0), ChangeKind.ADDED),
        (("rate",), ChangeKind.CHANGED),
    ]
    (rate,) = (c for c in compare_configurations(left, right) if c.path == ("rate",))
    assert rate == ConfigurationChange(("rate",), ChangeKind.CHANGED, (left[1].id,), (right[1].id,))
    assert configuration_digest(left) != configuration_digest(right)


def test_type_and_state_changes_are_changes() -> None:
    base = value(("v",), scalar("int", 1))
    for changed in (
        value(("v",), scalar("bool", True)),  # 1 == True in Python, never here
        value(("v",), scalar("float", 1.0)),
        value(("v",), KnownAbsent(SNAPSHOT_AT), Known("null")),
        value(("v",), Unknown(), Known("1")),
        value(("v",), Ambiguous((Candidate(scalar("int", 1)), Candidate(scalar("string", "1"))))),
        value(("v",), mapping(0)),
    ):
        assert changes([base], [other(changed)]) == [(("v",), ChangeKind.CHANGED)]


def test_a_value_with_no_reading_compares_by_its_text_and_tag() -> None:
    custom = value(("v",), Unknown(), Known("abc"), tag=Known("!secret"))
    assert changes([custom], [other(custom)]) == []
    assert changes([custom], [other(replace(custom, tag=Known("!vault")))]) == [
        (("v",), ChangeKind.CHANGED)
    ]
    assert changes([custom], [other(replace(custom, text=Known("abd")))]) == [
        (("v",), ChangeKind.CHANGED)
    ]


def test_a_collection_compares_by_its_type_and_an_alias_by_its_target() -> None:
    assert changes([value(("m",), mapping(1))], [other(value(("m",), mapping(5)))]) == []
    left = value(("a",), ConfigAlias("x", ("defaults",)))
    assert changes([left], [other(value(("a",), ConfigAlias("y", ("defaults",))))]) == []
    assert changes([left], [other(value(("a",), ConfigAlias("x", ("other",))))]) == [
        (("a",), ChangeKind.CHANGED)
    ]


def test_repeated_keys_compare_in_source_order() -> None:
    first = value(("k",), scalar("int", 1), order=0)
    second = value(("k",), scalar("int", 2), order=1, provenance=at("/k2"))
    swapped = [other(replace(first, order=1)), other(replace(second, order=0))]
    assert changes([first, second], swapped) == [(("k",), ChangeKind.CHANGED)]
    assert changes([first, second], [other(first)]) == [(("k",), ChangeKind.CHANGED)]


def test_values_of_two_snapshots_are_never_compared_as_one() -> None:
    with pytest.raises(ValueError, match="one snapshot at a time"):
        compare_configurations([*DOCUMENT, other(DOCUMENT[1])], DOCUMENT)
    with pytest.raises(ValueError, match="one snapshot at a time"):
        configuration_digest([*DOCUMENT, other(DOCUMENT[1])])


def test_paths_sort_positions_before_keys_segment_by_segment() -> None:
    paths: list[tuple[str | int, ...]] = [("b",), ("a", "z"), ("a", 10), ("a", 2), ()]
    assert sorted(paths, key=path_sort_key) == [(), ("a", 2), ("a", 10), ("a", "z"), ("b",)]


def test_the_comparison_key_never_holds_a_citation() -> None:
    for record in DOCUMENT:
        assert "provenance" not in canonical_json.dumps(comparison_key(record)).decode()


def test_the_digest_is_a_value_digest() -> None:
    digest = configuration_digest(DOCUMENT)
    assert digest == ValueDigest(digest) and digest.startswith("sha256:")


# --- The digest agrees with the comparison -------------------------------------------------------

LEAVES: Final = st.one_of(
    st.booleans().map(lambda b: scalar("bool", b)),
    st.integers(-5, 5).map(lambda i: scalar("int", i)),
    st.sampled_from([0.0, 0.5, -0.0]).map(lambda f: scalar("float", f)),
    st.sampled_from(["", "a", "on"]).map(lambda s: scalar("string", s)),
)


@settings(max_examples=200, deadline=None)
@given(
    left=st.dictionaries(st.sampled_from("abcd"), LEAVES, max_size=4),
    right=st.dictionaries(st.sampled_from("abcd"), LEAVES, max_size=4),
)
def test_equal_digests_exactly_when_nothing_changes(
    left: dict[str, ConfigScalar], right: dict[str, ConfigScalar]
) -> None:
    def document(entries: dict[str, ConfigScalar], snapshot: RecordId) -> list[ConfigurationValue]:
        return [
            value((), mapping(len(entries)), snapshot=snapshot),
            *(
                value((key,), leaf, order=i, snapshot=snapshot)
                for i, (key, leaf) in enumerate(entries.items())
            ),
        ]

    a = document(left, SNAPSHOT_ID)
    b = [other(record) for record in document(right, SNAPSHOT_ID)]
    same = compare_configurations(a, b) == ()
    assert same == (configuration_digest(a) == configuration_digest(b))
