"""Mapping files, presets, field shapes and declared time formats (ADR 0002 §3, §5)."""

import json
from fractions import Fraction
from typing import Any

import pytest

from neptune.model.lifecycle import LIFECYCLE_KINDS
from neptune_deploy.lifecycle import PRESETS, MappingError, parse_mapping, preset
from neptune_deploy.lifecycle.mapping import MAX_MAPPING_BYTES, match_pattern
from neptune_deploy.lifecycle.shapes import Shape, fields_of
from neptune_deploy.lifecycle.times import DAY, check_format, read_time


def _document(**rule: Any) -> dict[str, Any]:
    base = {
        "id": "r",
        "kind": "maintenance_event",
        "requires": ["WO"],
        "fields": {"identifiers": [{"column": "WO", "namespace": "wo"}]},
    }
    return {
        "schema": "neptune-deploy.lifecycle-mapping/1",
        "id": "test.mapping",
        "version": "1",
        "zone": "Europe/Berlin",
        "rules": [{**base, **rule}],
    }


def _parse(document: Any) -> Any:
    return parse_mapping(json.dumps(document).encode())


@pytest.mark.parametrize("name", PRESETS)
def test_every_preset_loads_and_names_a_lifecycle_kind(name: str) -> None:
    mapping = preset(name)
    kinds = {kind.kind for kind in LIFECYCLE_KINDS}
    assert all(rule.kind.kind in kinds for rule in mapping.rules)


def test_presets_cover_cmms_ticketing_and_registers() -> None:
    assert {"cmms_generic", "cmms_maximo", "cmms_upkeep"} <= set(PRESETS)
    assert {"jira_json", "linear_csv", "servicenow_csv"} <= set(PRESETS)
    assert {"register_asset", "register_risk", "register_zone"} <= set(PRESETS)


def test_every_field_of_every_kind_has_a_mapping_shape() -> None:
    for kind in LIFECYCLE_KINDS:
        shapes = {shape.name: shape.shape for shape in fields_of(kind)}
        assert shapes["identifiers"] is Shape.IDS
        assert shapes["site"] is Shape.ID
        for shape in fields_of(kind):
            if shape.part is not None:
                assert fields_of(shape.part)


def test_a_valid_mapping_keeps_its_bytes_hash_and_document() -> None:
    data = json.dumps(_document()).encode()
    mapping = parse_mapping(data)
    assert mapping.document == json.loads(data)
    assert mapping.sha256.startswith("sha256:")
    assert mapping.rules[0].columns() == frozenset({"WO"})


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({**_document(), "schema": "other/1"}, "schema"),
        (_document(kind="work_order"), "not a lifecycle kind"),
        (_document(fields={"technician": {"column": "T"}}), "no fields"),
        (_document(fields={"site": {"column": "S"}}), "missing"),
        (_document(fields={"site": {"column": "S", "namespace": "Bad Space"}}), "token"),
        (_document(fields={"performed": {"column": "D", "format": "%d/%m"}}), "needs %Y"),
        (_document(fields={"performed": {"column": "D", "format": "%Y-%m-%d %q"}}), "directive"),
        (_document(fields={"performed": {"column": "D", "format": "%Y-%m-%d %M"}}), "needs %H"),
        (_document(requires=[]), "at least one column"),
        (_document(where={"column": "T", "in": []}), "at least one value"),
        (_document(fields={"actions": {"column": "A"}}), "list of cells"),
        (_document(fields={"actions": [{"column": "A", "split": " "}]}), "whitespace"),
        (
            _document(
                fields={
                    "performed": {"column": "D", "format": "%Y-%m-%d"},
                    "sign_off": None,
                }
            ),
            "null is forbidden",
        ),
        (
            {
                **_document(),
                "rules": [{**_document()["rules"][0]}, {**_document()["rules"][0]}],
            },
            "rule ids repeat",
        ),
    ],
)
def test_malformed_mappings_are_refused_before_any_record(document: Any, message: str) -> None:
    with pytest.raises(MappingError, match=message):
        _parse(document)


def test_a_time_needs_a_declared_zone() -> None:
    document = _document(fields={"performed": {"column": "D", "format": "%Y-%m-%d"}})
    del document["zone"]
    with pytest.raises(MappingError, match="civil zone"):
        _parse(document)


def test_one_column_is_one_clock_in_a_rule() -> None:
    document = _document(
        kind="requalification_record",
        fields={
            "performed": {"column": "D", "format": "%Y-%m-%d"},
            "return_to_service": {"time": {"column": "D", "format": "%Y-%m-%d", "zone": "UTC"}},
        },
    )
    with pytest.raises(MappingError, match="two clocks"):
        _parse(document)


def test_score_names_are_unique_columns() -> None:
    document = _document(
        kind="risk_assessment",
        fields={"hazards": [{"scores": [{"column": "S"}, {"column": "S"}]}]},
    )
    with pytest.raises(MappingError, match="unique"):
        _parse(document)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xff\xfe{}",
        b'{"a": 1, "a": 2}',
        b"[NaN]",
        b"{" * 100_000,
        b" " * (MAX_MAPPING_BYTES + 1),
    ],
)
def test_hostile_mapping_bytes_are_refused(data: bytes) -> None:
    with pytest.raises(MappingError):
        parse_mapping(data)


@pytest.mark.parametrize(
    ("pattern", "column", "matches"),
    [
        ("Notes", "Notes", True),
        ("Notes", "notes", False),
        ("/fields/labels/*", "/fields/labels/0", True),
        ("/fields/labels/*", "/fields/labels", False),
        ("/fields/labels/**", "/fields/labels", True),
        ("/fields/**", "/fields/a/b/c", True),
        ("/fields/*/name", "/fields/status/name", True),
        ("/fields/*/name", "/fields/status/id", False),
        ("*", "Notes", False),
    ],
)
def test_ignore_patterns(pattern: str, column: str, matches: bool) -> None:
    assert match_pattern(pattern, column) is matches


# --- Declared time formats ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "patterns", "ticks", "resolution", "instant"),
    [
        ("1970-01-01", ("%Y-%m-%d",), 0, DAY, False),
        ("1969-12-31", ("%Y-%m-%d",), -1, DAY, False),
        ("2024-02-29", ("%Y-%m-%d",), 19782, DAY, False),
        ("2026-03-02 09:40", ("%Y-%m-%d %H:%M",), 20514 * 86400 + 34800, Fraction(1), False),
        ("02/03/2026 09:40:05", ("%d/%m/%Y %H:%M:%S",), 20514 * 86400 + 34805, Fraction(1), False),
        ("1970-01-01T00:00:00Z", ("%Y-%m-%dT%H:%M:%S%z",), 0, Fraction(1), True),
        ("1970-01-01T01:00:00+01:00", ("%Y-%m-%dT%H:%M:%S%z",), 0, Fraction(1), True),
        (
            "1970-01-01T00:00:00.250-0030",
            ("%Y-%m-%dT%H:%M:%S.%f%z",),
            1_800_250,
            Fraction(1, 1000),
            True,
        ),
        ("2026-03-02", ("%Y-%m-%d %H:%M", "%Y-%m-%d"), 20514, DAY, False),
    ],
)
def test_declared_formats_read_into_ticks(
    text: str, patterns: tuple[str, ...], ticks: int, resolution: Fraction, instant: bool
) -> None:
    reading = read_time(text, patterns)
    assert reading is not None
    assert (reading.ticks, reading.resolution, reading.instant) == (ticks, resolution, instant)


@pytest.mark.parametrize(
    "text",
    [
        "2026-02-30",
        "2026-13-01",
        "03/07/2026",
        "2026-3-2",
        " 2026-03-02",
        "2026-03-02x",
        "0000-01-01",
        "2026-03-02" + "0" * 100,
    ],
)
def test_text_a_declared_format_does_not_read_is_none(text: str) -> None:
    assert read_time(text, ("%Y-%m-%d",)) is None


def test_bad_times_and_offsets_are_none() -> None:
    assert read_time("2026-03-02 24:00", ("%Y-%m-%d %H:%M",)) is None
    assert read_time("2026-03-02T10:00:00+25:00", ("%Y-%m-%dT%H:%M:%S%z",)) is None


@pytest.mark.parametrize("pattern", ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M %%", "%Y%m%d"])
def test_formats_that_check(pattern: str) -> None:
    check_format(pattern)


@pytest.mark.parametrize(
    "pattern", ["%Y-%m-%d%", "%Y-%Y-%m-%d", "%Y-%m-%d %S", "%Y-%m-%d%z", "%Y-%m-%d %H"]
)
def test_formats_that_do_not_check(pattern: str) -> None:
    with pytest.raises(ValueError):
        check_format(pattern)
