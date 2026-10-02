"""Catalog documents and stated tables over hostile JSON (ADR 0009 §4, §6)."""

import json
import random
from typing import Any

import pytest

from neptune.identity.provenance import transform_record
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.provenance import JsonPointer
from neptune_deploy.sources.stated_records import (
    DeclaredClock,
    DocumentInvalid,
    build_document,
    clock_domain,
    parse_clock,
    parse_json,
    pointer,
    stated_table,
)

TRANSFORM = transform_record(adapter_id="deploy_test", adapter_version="0.0.0", config={})


def table_of(items: list[Any]) -> tuple[Any, Any, dict[str, Any]]:
    document = build_document("deploy_test", "scope:part", items)
    catalog = stated_table(document, "t", TRANSFORM)
    table: Any
    (table,) = catalog.tables
    return document, table, {"skipped": catalog.skipped, "rows": catalog.rows}


# --- Strict JSON ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        b'{"a": 1, "a": 2}',
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b'{"a": 1e999}',  # a number to the parser, and not one that dumps can write
        b"[-1e400]",
        b"\xef\xbb\xbf{}" + b"\xff",
        b"",
        b"{",
        b"[" * 100 + b"]" * 100,
        b"1" * 5000,  # past Python's integer digit limit
    ],
)
def test_parse_json_refuses_what_two_readers_could_read_differently(text: bytes) -> None:
    with pytest.raises(DocumentInvalid):
        parse_json(text)


def test_parse_json_accepts_the_boundaries() -> None:
    assert parse_json(b"[" * 63 + b"]" * 63) is not None  # nested to just under the bound
    assert parse_json(b'{"a": 1e308, "b": -0.0, "c": 12345678901234567890123}') == {
        "a": 1e308,
        "b": -0.0,
        "c": 12345678901234567890123,
    }


# --- Documents --------------------------------------------------------------------------------


def test_a_document_is_a_function_of_its_items_not_of_their_order() -> None:
    items: list[Any] = [{"k": i, "v": [i, {"z": None}]} for i in range(20)]
    reference = build_document("c", "scope:part", items)
    for seed in range(5):
        shuffled = items + items[:3]  # a repeated item is the same item
        random.Random(seed).shuffle(shuffled)
        again = build_document("c", "scope:part", shuffled)
        assert again.data == reference.data and again.ref == reference.ref
    assert (
        reference.ref.revision_token == build_document("c", "scope:part", items).ref.revision_token
    )
    changed = build_document("c", "scope:part", [*items[:-1], {"k": 19, "v": "other"}])
    assert changed.ref.revision_token != reference.ref.revision_token
    assert reference.data.isascii() and b" " not in reference.data


def test_an_empty_document_has_a_table_and_no_rows() -> None:
    document, table, rest = table_of([])
    assert document.data == b'{"items":[]}'
    assert isinstance(table.header, Unknown) and rest["rows"] == ()


def test_a_pointer_escapes_slash_and_tilde() -> None:
    assert pointer("items", 3, "a/b~c") == "/items/3/a~1b~0c"


# --- Tables -----------------------------------------------------------------------------------


def test_an_element_that_is_not_an_object_keeps_every_index_true() -> None:
    items = ["stray", {"a": 2}, None, {"b": 1}, 7]
    document, table, rest = table_of(items)
    # The document is sorted by bytes; every row must still cite the element it came from.
    assert len(rest["rows"]) == len(document.raw_items) == 5
    for row in rest["rows"]:
        (location,) = row.provenance.evidence.locator
        index = int(location.pointer.split("/")[2])
        element = document.raw_items[index]
        cells = dict(zip(table.header.value, row.cells, strict=True))
        if isinstance(element, dict):
            for key, value in element.items():
                assert cells[key].value == value
                assert cells[key].provenance.evidence.locator[0].pointer == f"/items/{index}/{key}"
        else:
            assert all(isinstance(cell, Unknown) for cell in row.cells)
    reasons = sorted(reason for _, _, reason in rest["skipped"])
    assert reasons == ["not_an_object"] * 3


def test_a_key_that_cannot_be_a_column_is_reported_and_never_crashes() -> None:
    items = [{"\ud800": 1, "ok": 2, "@clock:start": 3, "good\udfff": 4}]
    _, table, rest = table_of(items)
    assert table.header == Known(("ok",), table.header.provenance)
    assert [reason for _, _, reason in rest["skipped"]] == ["key_unusable"]
    (row,) = rest["rows"]
    assert [cell.value for cell in row.cells] == [2]


def test_a_catalog_key_named_like_a_companion_is_never_confused_with_it() -> None:
    document = build_document(
        "c", "s:p", [{"start_time": 5, "@clock:start_time": "forged"}, {"start_time": 6}]
    )
    domain = clock_domain(document, "start_time", ("ds",), TRANSFORM, DeclaredClock())
    assert domain is not None
    catalog = stated_table(document, "t", TRANSFORM, clocks={"start_time": domain})
    table: Any
    (table,) = catalog.tables
    assert table.header.value == ("start_time", "@clock:start_time")
    for row in catalog.rows:
        companion: Any = row.cells[1]
        assert companion.value == domain.id  # the companion, never the catalog's own text


def test_cell_boundaries() -> None:
    big = "x" * (1 << 20)
    items = [
        {"a": big, "b": big + "y", "c": 2**63, "d": [], "e": {}, "f": False, "g": 0, "h": 0.0},
    ]
    _, table, rest = table_of(items)
    (row,) = rest["rows"]
    cells = dict(zip(table.header.value, row.cells, strict=True))
    assert cells["a"].value == big  # exactly the bound is stored
    assert isinstance(cells["b"], Unknown)  # one byte over is not, and is reported
    assert cells["c"].value == 2**63
    assert (cells["d"].value, cells["e"].value) == (
        "[]",
        "{}",
    )  # empty containers are text, not absent
    assert cells["f"].value is False and cells["g"].value == 0 and cells["h"].value == 0.0
    assert [reason for _, _, reason in rest["skipped"]] == ["cell_too_large"]
    assert all(c.provenance.assertion_kind is AssertionKind.STATED for c in row.cells)


def test_a_clock_cites_the_first_object_that_has_the_field_and_is_none_without_one() -> None:
    document = build_document("c", "s:p", [{"t": "x"}, {"t": True}, {"t": 7}, {"t": 3}])
    domain = clock_domain(document, "t", ("ds",), TRANSFORM, DeclaredClock())
    assert domain is not None
    assert domain.provenance.evidence.source == document.content_id
    index = next(i for i, item in enumerate(document.items) if item.get("t") == 3)  # sorted order
    (where,) = domain.provenance.evidence.locator
    assert isinstance(where, JsonPointer)
    assert where.pointer == f"/items/{index}/t"
    assert clock_domain(document, "missing", ("ds",), TRANSFORM, DeclaredClock()) is None
    only_text = build_document("c", "s:p", [{"t": "5"}, {"t": 1.5}])
    assert clock_domain(only_text, "t", ("ds",), TRANSFORM, DeclaredClock()) is None


# --- Declared clocks --------------------------------------------------------------------------


def test_a_declared_clock_round_trips_into_the_transform_config() -> None:
    clock = parse_clock(
        {"epoch": "unix", "timescale": "posix", "resolution": "1/1000000000", "role": "sample"}
    )
    assert clock.config() == {
        "epoch": "unix",
        "resolution": "1/1000000000",
        "role": "sample",
        "timescale": "posix",
    }
    assert parse_clock(None) == DeclaredClock() == parse_clock({})
    assert parse_clock({"resolution": "5"}).config() == {"resolution": "5/1"}


@pytest.mark.parametrize(
    "declared",
    [
        [],
        "unix",
        {"zone": "utc"},
        {"resolution": "0"},
        {"resolution": "1/0"},
        {"resolution": "-1/2"},
        {"resolution": "1/2/3"},
        {"resolution": "²"},
        {"resolution": "9" * 31},
        {"resolution": 0.001},
        {"epoch": "not-an-epoch"},
        {"timescale": "sidereal"},
        {"role": "hands"},
    ],
)
def test_a_bad_clock_declaration_is_refused(declared: Any) -> None:
    with pytest.raises(ValueError):
        parse_clock(declared)


def test_the_transform_config_must_be_json() -> None:
    json.dumps(DeclaredClock().config())
