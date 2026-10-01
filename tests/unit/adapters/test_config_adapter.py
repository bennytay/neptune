"""The configuration adapter on real-world-shaped files, malformed and hostile input, boundaries
and determinism (ADR 0037).

The oracles are independent readings. Every value's locator is resolved in the document as
``json.loads`` (members kept as pairs), ``tomllib`` or PyYAML's composer (no types resolved) reads
it, and every span a value cites is parsed again on its own. The adapter must agree with them
however its plan cuts the document into chunks.
"""

import codecs
import json
import math
import tomllib
from collections.abc import Callable, Iterator
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from neptune.adapters.builtin import default_registry
from neptune.adapters.config import DESCRIPTOR, ConfigAdapter
from neptune.adapters.contract import (
    NAME_ONLY,
    PROBE_HEAD_SIZE,
    STRUCTURE,
    ChunkOutput,
    ProbeHints,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.discovery.probe import ProbeEngine
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.identity.configuration import configuration_digest
from neptune.model.configuration import (
    ChangeKind,
    CollectionType,
    ConfigAlias,
    ConfigCollection,
    ConfigFormat,
    ConfigScalar,
    ConfigurationSnapshot,
    ConfigurationValue,
    LineEndings,
    TextEncoding,
    compare_configurations,
)
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.knowledge import (
    Ambiguous,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    Unknown,
)
from neptune.model.provenance import (
    AdapterLocator,
    ByteRange,
    JsonPointer,
    Locator,
    Provenance,
    Span,
)
from neptune.model.scalars import NonFinite
from neptune.model.versions import DeclaredVersion

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "config"
Path_ = tuple[str | int, ...]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def run(data: bytes, chunk_values: int = 10_000, **config: Any) -> SourceOutput:
    return ingest_source(ConfigAdapter(chunk_values), BytesReader(data), config)


def snapshots(output: SourceOutput) -> list[ConfigurationSnapshot]:
    found = [r for r in output.records() if isinstance(r, ConfigurationSnapshot)]
    return sorted(found, key=lambda s: canonical_json.dumps(s.provenance.evidence.locator_json()))


def values(output: SourceOutput) -> list[ConfigurationValue]:
    return [r for r in output.records() if isinstance(r, ConfigurationValue)]


def by_path(output: SourceOutput) -> dict[Path_, ConfigurationValue]:
    found: dict[Path_, ConfigurationValue] = {}
    for value in values(output):
        assert value.path not in found, f"{value.path} repeats"
        found[value.path] = value
    return found


def codes(output: SourceOutput) -> list[str]:
    return sorted(finding.code for finding in output.findings())


def finding(output: SourceOutput, code: str) -> IngestFinding:
    (found,) = [f for f in output.findings() if f.code == code]
    return found


def text_of(data: bytes) -> str:
    for mark, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if data.startswith(mark):
            return data[len(mark) :].decode(encoding)
    return data.decode("utf-8")


def reading(value: ConfigurationValue) -> object:
    """What a value says, without citations: easy to compare in a table."""
    match value.value:
        case Known(value=ConfigScalar(type=kind, value=v)):
            return (str(kind), v)
        case Known(value=ConfigCollection(type=kind, length=length)):
            return (str(kind), length)
        case Known(value=ConfigAlias(target=target)):
            return ("alias", target)
        case Ambiguous(candidates=candidates):
            pairs = []
            for candidate in candidates:
                assert isinstance(candidate.value, ConfigScalar)
                pairs.append((str(candidate.value.type), candidate.value.value))
            return tuple(pairs)
        case KnownAbsent():
            return "null"
        case _:
            return "unknown"


def span_of(value: ConfigurationValue) -> tuple[int, int] | None:
    states = [value.value] if not isinstance(value.value, Ambiguous) else value.value.candidates
    for state in states:
        provenance = getattr(state, "provenance", None)
        if isinstance(provenance, Provenance):
            step = provenance.evidence.locator[0]
            if isinstance(step, Span):  # a null cites its document, not a span
                return step.start, step.end
    return None


# --- Independent readings ------------------------------------------------------------------------


class Pairs(list[tuple[str, Any]]):
    """A JSON object as json.loads reads it with its members kept in order, repeats included."""


def json_tree(text: str) -> Any:
    return json.loads(text, object_pairs_hook=Pairs)


def yaml_documents(text: str) -> list[Any]:
    return list(yaml.compose_all(text, Loader=yaml.BaseLoader))


def tokens(pointer: str) -> Iterator[str]:
    for token in pointer.split("/")[1:]:
        yield token.replace("~1", "/").replace("~0", "~")


def child(node: Any, token: str) -> Any:
    if isinstance(node, Pairs):
        (found,) = [value for key, value in node if key == token]
        return found
    if isinstance(node, dict):
        return node[token]
    if isinstance(node, list):
        return node[int(token)]
    if isinstance(node, yaml.MappingNode):
        (found,) = [v for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == token]
        return found
    assert isinstance(node, yaml.SequenceNode), node
    return node.value[int(token)]


def entry(node: Any, order: int) -> Any:
    if isinstance(node, yaml.MappingNode):
        return node.value[order][1]
    assert isinstance(node, Pairs)
    return node[order][1]


def resolve(tree: Any, locator: tuple[Locator, ...]) -> Any:
    """The node a locator addresses in an independent reading of the document."""
    node = tree
    for step in locator:
        if isinstance(step, JsonPointer):
            for token in tokens(step.pointer):
                node = child(node, token)
        else:
            assert isinstance(step, AdapterLocator)
            fields = dict(step.fields)
            if step.kind == "config:document":
                node = node[fields["index"]]
            else:
                assert step.kind == "config:entry"
                assert isinstance(fields["order"], int)
                node = entry(node, fields["order"])
    return node


def check_json_citations(data: bytes, output: SourceOutput) -> None:
    text = text_of(data)
    tree = json_tree(text)
    for value in values(output):
        node = resolve(tree, value.provenance.evidence.locator)
        match value.value:
            case Known(value=ConfigCollection(type=CollectionType.MAPPING, length=length)):
                assert isinstance(node, Pairs) and len(node) == length
            case Known(value=ConfigCollection(length=length)):
                assert isinstance(node, list) and not isinstance(node, Pairs)
                assert len(node) == length
            case _:
                assert isinstance(value.text, Known)
                declared = value.text.value
                assert node == declared if isinstance(node, str) else node == json.loads(declared)
        spot = span_of(value)
        if spot is not None:
            again = json.loads(text[spot[0] : spot[1]], object_pairs_hook=Pairs)
            assert again == node or (isinstance(node, float) and math.isnan(node))


def check_yaml_citations(
    data: bytes, output: SourceOutput, repair: dict[str, str] | None = None
) -> None:
    """``repair`` replaces text the independent composer refuses (an undefined alias) with text
    of the same length, so every position stays where it was."""
    text = text_of(data)
    oracle = text
    for old, new in (repair or {}).items():
        assert len(old) == len(new)
        oracle = oracle.replace(old, new)
    documents = yaml_documents(oracle)
    for value in values(output):
        node = resolve(documents, value.provenance.evidence.locator)
        match value.value:
            case Known(value=ConfigAlias(target=target, key=key)):
                index = value.provenance.evidence.locator[0]
                assert isinstance(index, AdapterLocator)
                prefix: tuple[Locator, ...] = (index,)
                steps = target[:-1] if key else target
                pointer = "".join("/" + str(s).replace("~", "~0").replace("/", "~1") for s in steps)
                found = resolve(documents, (*prefix, JsonPointer(pointer)))
                if key:  # the key node of the entry the target names
                    assert any(node is k for k, _ in found.value if k.value == target[-1])
                else:
                    assert node is found
            case Known(value=ConfigCollection(type=kind, length=length)):
                mapping = kind is CollectionType.MAPPING
                expected = yaml.MappingNode if mapping else yaml.SequenceNode
                assert isinstance(node, expected) and len(node.value) == length
            case _:
                assert isinstance(node, yaml.ScalarNode)
                if isinstance(value.text, Known):
                    assert node.value == value.text.value
                if isinstance(value.tag, Known) and value.tag.value not in ("?", "!"):
                    assert node.tag == value.tag.value
        spot = span_of(value)
        if spot is not None and isinstance(value.tag, Known) and value.tag.value == "?":
            if isinstance(value.value, Known) and isinstance(value.value.value, ConfigCollection):
                continue  # a block collection's lines need its indentation to parse alone
            again = yaml.compose(text[spot[0] : spot[1]], Loader=yaml.BaseLoader)
            assert again is not None and again.value == node.value


def toml_value(node: Any) -> Any:
    if isinstance(node, datetime | date | time):
        return node.isoformat()
    return node


def check_toml_citations(data: bytes, output: SourceOutput) -> None:
    text = text_of(data)
    tree = tomllib.loads(text)
    for value in values(output):
        node = resolve(tree, value.provenance.evidence.locator)
        match value.value:
            case Known(value=ConfigCollection(type=CollectionType.MAPPING, length=length)):
                assert isinstance(node, dict) and len(node) == length
            case Known(value=ConfigCollection(length=length)):
                assert isinstance(node, list) and len(node) == length
            case Known(value=ConfigScalar(value=scalar)):
                assert toml_value(node) == (
                    scalar if not isinstance(scalar, NonFinite) else toml_value(node)
                )
        spot = span_of(value)
        if spot is None or not value.path:
            continue
        written = text[spot[0] : spot[1]]
        if written.startswith("[") and isinstance(node, dict):
            key = [s for s in value.path if isinstance(s, str)][-1]
            assert written.rstrip("]").lstrip("[").strip().endswith(key)
        else:
            assert tomllib.loads(f"v = {written}")["v"] == node


# --- The descriptor and probing ------------------------------------------------------------------


def test_the_descriptor_configures_with_its_defaults() -> None:
    config = configure(DESCRIPTOR)
    assert config.values == {
        "max_bytes": 8 * 1024 * 1024,
        "max_depth": 200,
        "max_path_ratio": 64,
        "max_scalar_length": 1024 * 1024,
        "yaml_version": "declared",
    }
    assert dict(DESCRIPTOR.libraries).keys() == {"python", "pyyaml"}
    assert DESCRIPTOR.record_kinds == ("configuration_snapshot", "configuration_value")


def probe(data: bytes, name: str = "f") -> tuple[float, list[str], str | None]:
    result = ConfigAdapter().probe(data[:PROBE_HEAD_SIZE], ProbeHints(name, len(data)))
    return result.confidence, [reason.code for reason in result.reasons], result.version


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("nav2_params.yaml", "config.yaml"),
        ("px4_params.json", "config.json"),
        ("gripper_tool.toml", "config.toml"),
        ("robot_config", "config.yaml"),  # no extension: the bytes decide
        ("utf16.yaml", "config.yaml"),
        ("truncated_px4.json", "config.json"),  # cut short: still JSON's grammar
        ("truncated_nav2.yaml", "config.yaml"),
        ("billion_laughs.yaml", "config.yaml"),
    ],
)
def test_configuration_is_claimed_by_its_structure(name: str, code: str) -> None:
    assert probe(fixture(name))[:2] == (STRUCTURE, [code])
    assert probe(fixture(name), "renamed.txt")[:2] == (STRUCTURE, [code])


def test_the_probe_reports_the_yaml_version_a_stream_declares() -> None:
    assert probe(fixture("multi_document.yaml"))[2] == "1.1"
    assert probe(fixture("nav2_params.yaml"))[2] is None


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"", "config.empty"),
        (b"PK\x03\x04\x00\x00", "config.not_config"),
        (b"\x89MCAP0\r\n\xff\xfe", "config.not_text"),
        (b"Plain prose about the robot.\nIt has no structure.\n", "config.not_config"),
        (b'"a JSON string is not a configuration"', "config.not_config"),
        (b"42\n", "config.not_config"),
        (b"# only a comment\n", "config.not_config"),
        (b"---\ntitle: Notes\n---\n# Heading\n\nSome *text* here.\n", "config.not_config"),
        (b"[INFO] 12:00 started\n[WARN] 12:01 low battery\n", "config.not_config"),
        # YAML's grammar holds for these notes, but nothing in them looks like configuration.
        (b"Robot: spot-12\nOperator: Ben\nNotes: arm was stiff\n", "config.not_config"),
        (b"# Shopping\n\n- eggs\n- milk\n- yes, coffee\n", "config.not_config"),
        (b"Shift: night\nStart: 14:05\nDone: yes\n", "config.not_config"),
        (b"Robot: spot-12\nNotes: fine\nOops: [\n", "config.not_config"),  # broken at the end
    ],
)
def test_text_that_is_not_configuration_is_not_claimed(data: bytes, code: str) -> None:
    assert probe(data)[:2] == (0.0, [code])


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"rate: 10\nframe: map\n", "config.yaml"),  # a number
        (b"robot:\n  name: spot-12\n", "config.yaml"),  # a nested mapping
        (b"frames: [map, odom]\n", "config.yaml"),  # a flow collection
        (b"---\nname: spot-12\n", "config.yaml"),  # an explicit document
        (b"[tool]\nmass = 0.5\n", "config.toml"),
        (b'{"$schema": "s.json", "ros.namespace": "/r1", "max_speed": 1.5}', "config.json"),
        (b'{"joints": [{"name": "j1"}], "base": "base_link"}', "config.json"),  # one table
        (b'{"type": "lidar", "rate": 10}', "config.json"),  # a type, but not GeoJSON's
    ],
)
def test_configuration_shows_itself_beyond_the_grammar(data: bytes, code: str) -> None:
    assert probe(data)[:2] == (STRUCTURE, [code])


SITE_JSON: Final = (
    b'{"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type":'
    b' "Point", "coordinates": [103.8, 1.3]}, "properties": {"name": "dock"}}]}'
)


@pytest.mark.parametrize(
    ("data", "confidence"),
    [
        (SITE_JSON, NAME_ONLY),  # GeoJSON, found by review: geometry, not settings
        (b'{"type": "FeatureCollection", "features": []}', NAME_ONLY),  # site.geojson
        (b'{"type": "Point", "coordinates": [1, 2]}', NAME_ONLY),
        (b'{"rows": [{"t": 0.0, "x": 1.0}, {"t": 0.1, "x": 1.1}]}', NAME_ONLY),  # a table wrapper
        (b'{"train": [{"a": 1}], "test": [{"a": 2}]}', NAME_ONLY),
        (b'{"2024-01-01": 3, "2024-01-02": 4}', NAME_ONLY),  # keyed by data
        (b'{"max speed": 1.5}', NAME_ONLY),
        (b'{"1": "a", "2": "b"}', NAME_ONLY),
        (b'[{"t": 0.0, "x": 1.0}, {"t": 0.1, "x": 1.1}]', 0.0),  # rows.json: rows are data
        (b'["base_link", "odom"]\n', 0.0),  # also a TOML header: JSON first, then its shape
        (b'["base_link"]\n', 0.0),
        (b"[]", 0.0),
        (b"- 1\n- 2\n- 3\n", 0.0),  # notes.md, found by review: a YAML sequence of numbers
        (b"---\n- {a: 1}\n- {a: 2}\n", 0.0),
    ],
)
def test_data_shaped_as_json_or_yaml_is_not_claimed_as_configuration(
    data: bytes, confidence: float
) -> None:
    assert probe(data)[:2] == (confidence, ["config.shape_not_configuration"])


def test_a_cut_head_is_judged_by_the_members_it_holds() -> None:
    rows = b'{"rows": [' + b'{"t": 0.0, "x": 1.0}, ' * 5_000 + b'{"t": 1}]}'
    assert len(rows) > PROBE_HEAD_SIZE
    assert probe(rows)[:2] == (NAME_ONLY, ["config.shape_not_configuration"])
    settings_ = b'{"rate": 10, "rows": [' + b'{"t": 0.0}, ' * 8_000 + b'{"t": 1}]}'
    assert probe(settings_)[:2] == (STRUCTURE, ["config.json"])
    geo = b'{"type": "FeatureCollection", "features": [' + b'{"a": 1}, ' * 8_000 + b"{}]}"
    assert probe(geo)[:2] == (NAME_ONLY, ["config.shape_not_configuration"])


def test_data_shaped_files_are_left_to_the_text_adapter_with_a_name_mismatch() -> None:
    engine = ProbeEngine(default_registry())
    for name, data in (("site.json", SITE_JSON), ("rows.json", b'[{"t": 0.0}, {"t": 0.1}]')):
        result = engine.probe(BytesReader(data), name)
        assert result.adapter == "text"
        assert [f.code for f in result.findings] == ["neptune.probe.name_mismatch"]
    assert engine.probe(BytesReader(fixture("px4_params.json")), "px4.json").adapter == "config"


def test_a_data_shaped_file_still_ingests_when_a_manifest_names_this_adapter() -> None:
    output = run(SITE_JSON)
    (snapshot,) = snapshots(output)
    assert snapshot.format is ConfigFormat.JSON and output.findings() == ()
    assert reading(by_path(output)[("type",)]) == ("string", "FeatureCollection")


def test_a_one_line_json_array_is_json_not_a_toml_table() -> None:
    output = run(b'["base_link"]\n')
    (snapshot,) = snapshots(output)
    assert snapshot.format is ConfigFormat.JSON
    assert {p: reading(v) for p, v in by_path(output).items()} == {
        (): ("sequence", 1),
        (0,): ("string", "base_link"),
    }


@pytest.mark.parametrize(
    "name", ["corrupted_tool.toml", "duplicates.toml", "empty.yaml", "comments_only.toml"]
)
def test_broken_or_empty_files_are_left_to_the_text_adapter(name: str) -> None:
    assert probe(fixture(name))[0] == 0.0


def test_a_head_cut_short_is_judged_by_what_it_holds() -> None:
    data = fixture("px4_params.json")
    big = data[:-3] + b"," + b" " * PROBE_HEAD_SIZE + b"\n}\n"  # past the head, still valid
    assert probe(big)[:2] == (STRUCTURE, ["config.json"])
    yaml_big = b"a: 1\n" + b"b: [" + b"1, " * PROBE_HEAD_SIZE + b"2]\n"
    assert probe(yaml_big)[:2] == (STRUCTURE, ["config.yaml"])


def test_inspect_summarises_the_head() -> None:
    data = fixture("robot_config")
    summary = ConfigAdapter().inspect(BytesReader(data), configure(DESCRIPTOR)).summary
    assert summary == {"bom": True, "encoding": "utf-8", "format": "yaml", "size": len(data)}
    corrupt = ConfigAdapter().inspect(BytesReader(b"\xff\xfe\x00"), configure(DESCRIPTOR))
    assert corrupt.summary["format"] == "unknown"


# --- Real-world files ----------------------------------------------------------------------------


def test_ros2_parameters_are_one_snapshot_with_a_value_per_node() -> None:
    data = fixture("nav2_params.yaml")
    output = run(data)
    (snapshot,) = snapshots(output)
    found = by_path(output)
    assert snapshot.format is ConfigFormat.YAML
    assert snapshot.format_version == Unknown()  # ROS files declare no %YAML version
    assert (snapshot.encoding, snapshot.byte_order_mark) == (TextEncoding.UTF_8, False)
    assert snapshot.line_endings is LineEndings.LF
    assert snapshot.values == len(found) == 107
    assert snapshot.digest == configuration_digest(values(output))
    assert all(value.snapshot == snapshot.id for value in found.values())
    params: Path_ = ("controller_server", "ros__parameters")
    assert reading(found[(*params, "controller_frequency")]) == ("float", 20.0)
    assert reading(found[(*params, "use_sim_time")]) == ("bool", False)
    assert reading(found[(*params, "FollowPath", "vx_samples")]) == ("int", 20)
    assert reading(found[(*params, "FollowPath", "BaseObstacle.scale")]) == ("float", 0.02)
    assert reading(found[(*params, "controller_plugins")]) == ("sequence", 1)
    assert reading(found[(*params, "controller_plugins", 0)]) == ("string", "FollowPath")
    assert found[(*params, "FollowPath", "plugin")].tag == Known("!")  # quoted
    assert found[("amcl", "ros__parameters", "scan_topic")].tag == Known("?")  # plain
    # Key order survives: each value's position among its parent's entries.
    entries = [v for v in found.values() if v.path[:-1] == ("amcl", "ros__parameters")]
    assert [v.path[-1] for v in sorted(entries, key=lambda v: v.order)][:3] == [
        "use_sim_time",
        "alpha1",
        "alpha2",
    ]
    assert output.findings() == ()
    check_yaml_citations(data, output)


def test_comments_are_recorded_verbatim_and_cited() -> None:
    data = fixture("nav2_params.yaml")
    (snapshot,) = snapshots(run(data))
    text = text_of(data)
    assert [c.value for c in snapshot.comments if isinstance(c, Known)] == [
        "# Nav2 parameters for the AMR-7 field robot (bringup/params/nav2_params.yaml).",
        '# "precise_goal_checker"',
        "# Progress checker parameters",
    ]
    for comment in snapshot.comments:
        assert isinstance(comment, Known) and isinstance(comment.provenance, Provenance)
        (step,) = comment.provenance.evidence.locator
        assert isinstance(step, Span) and text[step.start : step.end] == comment.value


def test_px4_parameters_keep_json_numbers_as_written() -> None:
    data = fixture("px4_params.json")
    output = run(data)
    (snapshot,) = snapshots(output)
    found = by_path(output)
    assert (snapshot.format, snapshot.format_version) == (ConfigFormat.JSON, NotCovered())
    assert snapshot.comments == ()
    assert reading(found[("parameters", 0, "name")]) == ("string", "BAT1_N_CELLS")
    assert reading(found[("parameters", 0, "value")]) == ("int", 4)
    assert reading(found[("parameters", 1, "value")]) == ("float", 4.05)
    assert found[("parameters", 1, "value")].text == Known("4.05")
    assert reading(found[("parameters", 14, "value")]) == ("float", 0.0)  # "0.0" stays a float
    assert all(v.tag == NotCovered() for v in found.values())
    assert output.findings() == ()
    check_json_citations(data, output)


def test_toml_keeps_its_own_types_and_comments() -> None:
    data = fixture("gripper_tool.toml")
    output = run(data)
    (snapshot,) = snapshots(output)
    found = by_path(output)
    assert (snapshot.format, snapshot.format_version) == (ConfigFormat.TOML, NotCovered())
    assert reading(found[("calibration", "performed")]) == (
        "offset_datetime",
        "2026-08-14T09:30:00+08:00",
    )
    assert reading(found[("calibration", "checked")]) == ("local_datetime", "2026-09-01T07:15:00")
    assert found[("calibration", "checked")].text == Known("2026-09-01 07:15:00")
    assert reading(found[("calibration", "valid_until")]) == ("local_date", "2027-02-14")
    assert reading(found[("calibration", "shift_start")]) == ("local_time", "07:30:00")
    assert reading(found[("calibration", "zero_offset")]) == ("float", -0.0004)
    assert found[("calibration", "zero_offset")].text == Known("-4e-4")
    assert reading(found[("fingertips", 1, "width_mm")]) == ("int", 12)
    assert found[("fingertips", 1, "width_mm")].text == Known("0x0C")
    assert reading(found[("controller", "port")]) == ("int", 63352)
    assert reading(found[("controller", "literal key")]) == ("string", "C:\\tools\\robotiq")
    assert reading(found[("controller", "note")]) == ("string", 'Keep the "fingertips" clean.')
    assert reading(found[("limits", "force")]) == ("mapping", 2)
    assert reading(found[("fingertips",)]) == ("sequence", 2)
    assert span_of(found[("fingertips",)]) is None  # an array of tables has no one place
    assert [c.value for c in snapshot.comments if isinstance(c, Known)] == [
        "# End-effector configuration for the UR5e pick cell (tool changer slot 2).",
        "# metres, as the vendor sheet states",
    ]
    assert output.findings() == ()
    check_toml_citations(data, output)


# --- YAML's types ------------------------------------------------------------------------------

YAML_TYPES: Final[dict[Path_, object]] = {
    ("switches", "motors"): (("bool", True), ("string", "on")),
    ("switches", "lights"): (("bool", False), ("string", "off")),
    ("switches", "armed"): (("bool", True), ("string", "yes")),
    ("switches", "answer"): (("bool", False), ("string", "n")),
    ("permissions",): (("int", 493), ("int", 755)),
    ("octal_12",): (("string", "0o17"), ("int", 15)),
    ("binary",): (("int", 10), ("string", "0b1010")),
    ("grouped",): (("int", 1000), ("string", "1_000")),
    ("sexagesimal",): (("int", 90), ("string", "1:30")),
    ("scientific",): (("string", "1e3"), ("float", 1000.0)),
    ("signed_exp",): ("float", 1000.0),
    ("calibrated",): (("local_date", "2026-09-01"), ("string", "2026-09-01")),
    ("stamp",): (
        ("offset_datetime", "2026-09-01T07:15:00+00:00"),
        ("string", "2026-09-01T07:15:00Z"),
    ),
    ("agree", "int"): ("int", 42),
    ("agree", "negative"): ("int", -17),
    ("agree", "hex"): ("int", 31),
    ("agree", "float"): ("float", 0.25),
    ("agree", "inf"): ("float", NonFinite.POSITIVE_INFINITY),
    ("agree", "nan"): ("float", NonFinite.NAN),
    ("agree", "bool"): ("bool", True),
    ("agree", "null_word"): "null",
    ("agree", "tilde"): "null",
    ("agree", "empty"): "null",
    ("agree", "text"): ("string", "base_link"),
    ("quoted", "single"): ("string", "on"),
    ("quoted", "double"): ("string", "0755"),
    ("quoted", "folded"): ("string", "two lines\n"),
    ("quoted", "literal"): ("string", "kept\nas is\n"),
    ("tagged", "str"): ("string", "0755"),
    ("tagged", "int"): ("int", 42),
    ("tagged", "float"): ("float", 0.5),
    ("tagged", "bool"): ("bool", True),
    ("tagged", "null"): "null",
    ("tagged", "binary"): ("binary", "aGVsbG8gcm9ib3Q="),
    ("tagged", "timestamp"): ("offset_datetime", "2001-12-14T21:59:43.100000-05:00"),
    ("tagged", "application"): "unknown",
    ("tagged", "bad_int"): "unknown",
}


def _both(expected: object) -> bool:
    """A table entry giving two readings: YAML 1.1's, then 1.2's."""
    return isinstance(expected, tuple) and isinstance(expected[0], tuple)


def test_yaml_scalars_read_as_both_versions_when_the_document_declares_none() -> None:
    data = fixture("yaml_types.yaml")
    output = run(data)
    found = by_path(output)
    assert {path: reading(found[path]) for path in YAML_TYPES} == YAML_TYPES
    assert found[("agree", "empty")].text == Known("")  # an empty plain scalar: null, not ""
    assert found[("tagged", "application")].tag == Known("!include")
    assert found[("tagged", "int")].tag == Known("tag:yaml.org,2002:int")
    assert codes(output) == [
        "config.invalid_value",
        "config.unresolved_tag",
        "config.yaml_version_undeclared",
    ]
    ambiguous = finding(output, "config.yaml_version_undeclared")
    assert (ambiguous.category, ambiguous.severity) == (
        FindingCategory.AMBIGUOUS,
        Severity.WARNING,
    )
    expected = sorted(found[p].id for p, r in YAML_TYPES.items() if _both(r))
    assert list(ambiguous.records) == expected
    check_yaml_citations(data, output)


def test_a_null_cites_the_document_that_defines_it() -> None:
    output = run(fixture("yaml_types.yaml"))
    (snapshot,) = snapshots(output)
    null = by_path(output)[("agree", "tilde")]
    assert null.value == KnownAbsent(snapshot.provenance)


@pytest.mark.parametrize(("version", "index"), [("1.1", 0), ("1.2", 1)])
def test_an_assumed_yaml_version_reads_undeclared_documents_one_way(
    version: str, index: int
) -> None:
    output = run(fixture("yaml_types.yaml"), yaml_version=version)
    found = by_path(output)
    for path, expected in YAML_TYPES.items():
        if _both(expected):
            assert isinstance(expected, tuple)
            assert reading(found[path]) == expected[index]
    assert "config.yaml_version_undeclared" not in codes(output)


def test_each_document_of_a_stream_is_typed_by_its_own_declaration() -> None:
    output = run(fixture("multi_document.yaml"))
    first, second, third = snapshots(output)
    assert first.format_version == Known(DeclaredVersion("1.1"), first.format_version.provenance)  # type: ignore[union-attr]
    assert isinstance(second.format_version, Known)
    assert second.format_version.value == DeclaredVersion("1.2")
    assert third.format_version == Unknown()
    readings: dict[tuple[str, Path_], object] = {}
    for value in values(output):
        step = value.provenance.evidence.locator[0]
        assert isinstance(step, AdapterLocator)
        readings[(str(dict(step.fields)["index"]), value.path)] = reading(value)
    assert readings[("0", ("gripper",))] == ("bool", True)
    assert readings[("0", ("mode",))] == ("int", 493)
    assert readings[("1", ("gripper",))] == ("string", "on")
    assert readings[("1", ("mode",))] == ("int", 755)
    assert readings[("2", (0,))] == (("bool", True), ("string", "on"))
    assert [c.value for c in first.comments if isinstance(c, Known)] == [
        "# Document 0: YAML 1.1 declared."
    ]
    assert [c.value for c in third.comments if isinstance(c, Known)] == [
        "# Document 2: no version declared."
    ]
    check_yaml_citations(fixture("multi_document.yaml"), output)


# --- Keys, aliases and repeats -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["duplicates.json", "duplicates.yaml"])
def test_a_repeated_key_keeps_every_entry_addressed_by_position(name: str) -> None:
    data = fixture(name)
    output = run(data)
    rates = sorted((v for v in values(output) if v.path == ("rate",)), key=lambda v: v.order)
    assert [reading(v) for v in rates] == [("int", 10), ("int", 20)]
    locators = [v.provenance.evidence.locator for v in rates]
    steps = [s.to_json() for s in locators[1]]
    assert steps[-3:] == [
        {"kind": "json_pointer", "pointer": ""},
        {"kind": "config:entry", "order": 2},
        {"kind": "json_pointer", "pointer": ""},
    ]
    nested = sorted((v for v in values(output) if v.path == ("nested", "x")), key=lambda v: v.order)
    assert [s.to_json() for s in nested[0].provenance.evidence.locator][-3:-1] == [
        {"kind": "json_pointer", "pointer": "/nested"},
        {"kind": "config:entry", "order": 0},
    ]
    duplicate = finding(output, "config.duplicate_key")
    assert duplicate.details["count"] == 4
    assert set(duplicate.records) == {v.id for v in (*rates, *nested)}
    assert duplicate.severity is Severity.WARNING
    if name.endswith(".json"):
        check_json_citations(data, output)
    else:
        check_yaml_citations(data, output)


def test_values_under_a_repeated_key_have_one_order_however_they_are_listed() -> None:
    # Both x values are at path (a, x), order 0; their occurrence is (0, 0) and (1, 0).
    data = b"a: {x: 1}\na: {x: 2}\n"
    output = run(data, yaml_version="1.2")
    (snapshot,) = snapshots(output)
    found = sorted((v for v in values(output) if v.path == ("a", "x")), key=lambda v: v.occurrence)
    assert [(v.occurrence, reading(v)) for v in found] == [
        ((0, 0), ("int", 1)),
        ((1, 0), ("int", 2)),
    ]
    as_stored = sorted(values(output), key=lambda v: v.id)  # a package's table order
    assert configuration_digest(as_stored) == snapshot.digest
    assert configuration_digest(as_stored[::-1]) == snapshot.digest
    for comment in range(20):  # other ids, so other table orders: never a change
        again = run(data + b"#" + str(comment).encode() + b"\n", yaml_version="1.2")
        stored = sorted(values(again), key=lambda v: v.id)
        assert compare_configurations(as_stored, stored) == ()
        assert configuration_digest(stored) == snapshots(again)[0].digest == snapshot.digest
    swapped = run(b"a: {x: 2}\na: {x: 1}\n", yaml_version="1.2")
    changed = compare_configurations(values(output), values(swapped))
    assert [(c.path, c.change) for c in changed] == [(("a", "x"), ChangeKind.CHANGED)]
    assert snapshots(swapped)[0].digest != snapshot.digest


def test_toml_refuses_a_repeated_key_as_every_toml_reader_does() -> None:
    output = run(fixture("duplicates.toml"))
    assert values(output) == [] and codes(output) == ["config.syntax_error"]
    error = finding(output, "config.syntax_error")
    assert error.details["format"] == "toml" and error.details["line"] == 3
    assert "Cannot overwrite a value" in error.message


def test_aliases_are_references_never_expansions() -> None:
    data = fixture("anchors.yaml")
    output = run(data)
    found = by_path(output)
    assert reading(found[("left_arm", "<<")]) == ("alias", ("defaults",))
    assert reading(found[("same",)]) == ("alias", ("defaults",))
    assert found[("same",)].text == NotApplicable() and found[("same",)].tag == NotCovered()
    assert reading(found[("right_arm", "rate")]) == ("float", 25.0)
    assert reading(found[("dangling",)]) == "unknown"
    assert codes(output) == ["config.undefined_alias", "config.unsupported_key"]
    skipped = finding(output, "config.unsupported_key")
    assert skipped.details["count"] == 2 and skipped.severity is Severity.ERROR
    text = text_of(data)
    spans = [r.locator[0] for r in skipped.related]
    assert all(isinstance(s, Span) for s in spans)
    assert [text[s.start : s.end] for s in spans if isinstance(s, Span)] == [
        "[a, b]\n: complex key",
        "*defaults : alias of a mapping as a key",
    ]
    assert reading(found[()]) == ("mapping", 7)  # every entry declared, two not held
    check_yaml_citations(data, output, {"*nowhere": "nowhere_"})


def test_an_alias_to_a_key_is_a_reference_to_that_key() -> None:
    data = b"defaults: {&r rate: 10, &f frame: base_link}\nright: {*r : 20}\nparent: *f\n"
    output = run(data)
    found = by_path(output)
    assert reading(found[("right", "rate")]) == ("int", 20)  # an alias as a key: the key's text
    parent = found[("parent",)]  # an alias as a value: a reference to the key, never a copy
    assert isinstance(parent.value, Known)
    assert parent.value.value == ConfigAlias("f", ("defaults", "frame"), key=True)
    assert (parent.text, parent.tag) == (NotApplicable(), NotCovered())
    assert text_of(data)[slice(*(span_of(parent) or (0, 0)))] == "*f"
    nested = b"a:\n  &k name: 1\n  other: *k\nb: 2\n"  # found by review: the parent's span
    spans = {v.path: span_of(v) for v in values(run(nested))}
    assert text_of(nested)[slice(*(spans[("a",)] or (0, 0)))] == "&k name: 1\n  other: *k"
    assert text_of(nested)[slice(*(spans[("a", "other")] or (0, 0)))] == "*k"
    assert codes(output) == []
    check_yaml_citations(data, output)
    # Boundary: an anchor marks the last node or key that carries it, and a key on a collection
    # is still no path.
    later = by_path(run(b"a: &x 1\n&x b: 2\nc: *x\n"))
    assert reading(later[("c",)]) == ("alias", ("b",))
    assert codes(run(b"? &k [a]\n: 1\n*k : 2\n")) == ["config.unsupported_key"]


def test_yaml_keys_that_are_not_strings_are_part_of_the_digest() -> None:
    # Found by review: YAML 1.2 "{1: x, '1': y}" and "{'1': x, 1: y}" declare different maps.
    left = run(b"%YAML 1.2\n---\nm: {1: x, '1': y}\n")
    right = run(b"%YAML 1.2\n---\nm: {'1': x, 1: y}\n")
    assert snapshots(left)[0].digest != snapshots(right)[0].digest
    changed = compare_configurations(values(left), values(right))
    assert [(c.path, c.change) for c in changed] == [(("m", "1"), ChangeKind.CHANGED)]
    tags = sorted((v.occurrence, v.key_tag) for v in values(left) if v.path == ("m", "1"))
    assert tags == [((0, 0), Known("tag:yaml.org,2002:int")), ((0, 1), NotApplicable())]
    undeclared = by_path(run(b"on: 1\n"))[("on",)].key_tag
    assert isinstance(undeclared, Ambiguous)  # a boolean in 1.1, text in 1.2
    assert [c.value for c in undeclared.candidates] == [
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:str",
    ]
    # A key whose pattern matches but which no version reads has no type, never a made-up tag.
    for data in (b"0x_: 1\n", b"2026-02-30: 2\n"):
        (entry,) = [v for v in values(run(data)) if v.path]
        assert entry.key_tag == Unknown()
    # String keys add nothing: the same values as JSON and as YAML share a digest.
    as_json = run(b'{"m": {"a": 1, "b": [true]}}')
    as_yaml = run(b"m:\n  'a': 1\n  b: [true]\n")
    assert snapshots(as_json)[0].digest == snapshots(as_yaml)[0].digest


AMPLIFIERS: Final[dict[str, Callable[[str, int], bytes]]] = {
    # A long key above many values: every value's path repeats it.
    "json": lambda key, n: json.dumps({key: [0] * n}, separators=(",", ":")).encode(),
    # An alias to a long scalar, used as a key many times.
    "alias_key": lambda key, n: (
        "v: &a " + key + "\nl:\n" + "".join(f"- {{*a : {i}}}\n" for i in range(n))
    ).encode(),
    # An anchored long key, aliased as a value many times: each alias targets its path.
    "key_alias": lambda key, n: (
        "m:\n  ? &a " + key + "\n  : 1\nl:\n" + "".join("- *a\n" for _ in range(n))
    ).encode(),
}


def output_size(output: SourceOutput) -> int:
    records = (*output.records(), *output.findings())
    return sum(len(canonical_json.dumps(r.to_json())) for r in records)


@pytest.mark.parametrize("shape", sorted(AMPLIFIERS))
def test_output_stays_linear_in_input_however_keys_repeat(shape: str) -> None:
    # Found by review: 14 KB of JSON made 42 MB of records, 28 KB made 164 MB.
    for size in (2_000, 4_000, 8_000):
        data = AMPLIFIERS[shape]("k" * size, size // 5)
        output = run(data)
        assert output_size(output) <= 50 * len(data), (shape, size)
        assert "config.paths_too_long" in codes(output)
        assert output.records() == ()
    long = finding(run(AMPLIFIERS[shape]("k" * 2_000, 400)), "config.paths_too_long")
    assert long.severity is Severity.ERROR and long.category is FindingCategory.LIMIT
    assert long.details["path_code_points"] > long.details["budget"]  # type: ignore[operator]


def test_the_path_budget_is_a_multiple_of_the_document() -> None:
    data = fixture("nav2_params.yaml")  # 2,867 code points, 5,415 of paths: under the 4 KiB floor
    assert codes(run(data, max_path_ratio=1)) == ["config.paths_too_long"]
    assert len(values(run(data, max_path_ratio=2))) == 107
    stream = b"rate: 1\n---\n" + AMPLIFIERS["alias_key"]("k" * 2_000, 400) + b"---\nb: 2\n"
    output = run(stream)  # a document over budget costs only itself
    assert [s.provenance.evidence.locator[0].to_json()["index"] for s in snapshots(output)] == [
        0,
        2,
    ]
    assert finding(output, "config.paths_too_long").details["document"] == 1
    for name in ("px4_params.json", "gripper_tool.toml", "deep.yaml"):
        assert "config.paths_too_long" not in codes(run(fixture(name), max_depth=400))


@pytest.mark.parametrize(
    ("data", "duplicates"),
    [
        (b'1: a\n"1": b\n', 0),  # an int and a string: two keys of one text
        (b"true: a\n'true': b\n", 0),
        (b"a: 1\n'a': 2\n", 2),  # a plain string and a quoted one: one key
        (b'!!str 1: x\n"1": y\n', 2),
        (b"!!int 1: x\n1: y\n", 2),
        (b"? !custom k\n: 1\nk: 2\n", 0),  # an application's tag is its own type
        (b"%YAML 1.1\n---\non: 1\n'on': 2\n", 0),  # 1.1 reads on as a boolean
        (b"%YAML 1.2\n---\non: 1\n'on': 2\n", 2),
        (b"1: a\n'1': b\n1: c\n", 2),  # three entries of one text, two of one key
    ],
)
def test_yaml_keys_repeat_by_type_and_text_and_every_entry_of_a_text_is_addressed(
    data: bytes, duplicates: int
) -> None:
    output = run(data)
    entries = [v for v in values(output) if v.path]
    assert len({v.path for v in entries}) == 1  # one text: each entry addressed by position
    for v in entries:
        assert [s.to_json()["kind"] for s in v.provenance.evidence.locator][-2] == "config:entry"
    if duplicates:
        assert finding(output, "config.duplicate_key").details["count"] == duplicates
    else:
        assert "config.duplicate_key" not in codes(output)
    check_yaml_citations(data, output)


def test_a_billion_laughs_costs_one_value_per_alias() -> None:
    output = run(fixture("billion_laughs.yaml"))
    assert len(values(output)) == 91  # 9 keys, 9 lists of 9 items, and the root
    assert output.findings() == ()
    found = by_path(output)
    assert reading(found[("i", 8)]) == ("alias", ("h",))


# --- Encodings, line endings, layout -------------------------------------------------------------


def test_the_same_values_however_they_are_written_have_one_digest() -> None:
    plain = run(fixture("run_a_params.yaml"))
    with_bom_and_crlf = run(fixture("robot_config"))
    utf16 = run(fixture("utf16.yaml"))
    as_json = run(
        json.dumps(
            {
                "controller_server": {
                    "ros__parameters": {
                        "use_sim_time": False,
                        "controller_frequency": 20.0,
                        "FollowPath": {
                            "plugin": "dwb_core::DWBLocalPlanner",
                            "max_vel_x": 0.26,
                            "max_vel_theta": 1.0,
                            "sim_time": 1.7,
                            "critics": ["RotateToGoal", "Oscillation", "BaseObstacle"],
                            "debug_trajectory_details": True,
                        },
                    }
                }
            }
        ).encode()
    )
    digests = {snapshots(o)[0].digest for o in (plain, with_bom_and_crlf, utf16, as_json)}
    assert len(digests) == 1
    bom = snapshots(with_bom_and_crlf)[0]
    assert (bom.byte_order_mark, bom.line_endings) == (True, LineEndings.CRLF)
    sixteen = snapshots(utf16)[0]
    assert (sixteen.encoding, sixteen.byte_order_mark) == (TextEncoding.UTF_16_LE, True)
    check_yaml_citations(fixture("utf16.yaml"), utf16)
    check_yaml_citations(fixture("robot_config"), with_bom_and_crlf)


def test_two_runs_compare_field_by_field() -> None:
    run_a, run_b = run(fixture("run_a_params.yaml")), run(fixture("run_b_params.yaml"))
    assert snapshots(run_a)[0].digest != snapshots(run_b)[0].digest
    planner: Path_ = ("controller_server", "ros__parameters", "FollowPath")
    changes = compare_configurations(values(run_a), values(run_b))
    assert [(c.path, c.change) for c in changes] == [
        ((*planner, "critics", 1), ChangeKind.CHANGED),
        ((*planner, "critics", 2), ChangeKind.CHANGED),
        ((*planner, "debug_trajectory_details"), ChangeKind.REMOVED),
        ((*planner, "max_vel_x"), ChangeKind.CHANGED),
        ((*planner, "sim_time"), ChangeKind.CHANGED),
        ((*planner, "xy_goal_tolerance"), ChangeKind.ADDED),
    ]
    a, b = by_path(run_a), by_path(run_b)
    (max_vel,) = (c for c in changes if c.path[-1] == "max_vel_x")
    assert (max_vel.left, max_vel.right) == ((a[max_vel.path].id,), (b[max_vel.path].id,))
    assert compare_configurations(values(run_a), values(run(fixture("robot_config")))) == ()


def test_mixed_line_endings_are_read_and_reported() -> None:
    data = fixture("mixed_endings.yaml")
    output = run(data)
    (snapshot,) = snapshots(output)
    assert snapshot.line_endings is LineEndings.MIXED
    assert {p: reading(v) for p, v in by_path(output).items() if p} == {
        ("a",): ("int", 1),
        ("b",): ("int", 2),
        ("c",): ("int", 3),
        ("d",): ("int", 4),
    }
    report = finding(output, "config.mixed_line_endings")
    assert report.severity is Severity.INFO
    check_yaml_citations(data, output)


def test_a_byte_order_mark_json_does_not_define_is_read_past_and_reported() -> None:
    output = run(codecs.BOM_UTF8 + b'{"a": 1}')
    assert snapshots(output)[0].byte_order_mark is True
    report = finding(output, "config.byte_order_mark")
    assert report.subject.locator == (ByteRange(0, 3),)  # type: ignore[union-attr]


# --- Malformed and hostile input -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "fmt", "line"),
    [
        ("truncated_px4.json", "json", 181),
        ("truncated_nav2.yaml", "yaml", 67),
        ("corrupted_tool.toml", "toml", 13),
    ],
)
def test_a_syntax_error_is_one_finding_and_nothing_is_guessed(
    name: str, fmt: str, line: int
) -> None:
    data = fixture(name)
    output = run(data)
    assert output.records() == () and codes(output) == ["config.syntax_error"]
    error = finding(output, "config.syntax_error")
    assert (error.category, error.severity) == (FindingCategory.CORRUPT, Severity.ERROR)
    assert (error.details["format"], error.details["line"]) == (fmt, line)
    assert error.subject.locator == (Span(0, len(text_of(data))),)  # type: ignore[union-attr]


def test_a_broken_document_costs_only_itself_and_the_ones_after() -> None:
    data = b"a: 1\n---\nb: 2\n---\nc: [3\n"
    output = run(data)
    assert len(snapshots(output)) == 2
    error = finding(output, "config.syntax_error")
    assert error.details["document"] == 2
    assert "documents from 2 on" in error.message


def test_invalid_utf8_reads_nothing_and_says_where() -> None:
    data = fixture("corrupted_nav2.yaml")
    output = run(data)
    assert output.records() == () and codes(output) == ["config.invalid_encoding"]
    report = finding(output, "config.invalid_encoding")
    offset = data.index(b"\xff")
    assert report.details["first_invalid_byte"] == offset
    assert report.subject.locator == (ByteRange(offset, len(data) - offset),)  # type: ignore[union-attr]


@pytest.mark.parametrize("name", ["empty.yaml", "comments_only.toml"])
def test_a_file_that_declares_nothing_is_a_finding_not_a_snapshot(name: str) -> None:
    output = run(fixture(name))
    assert output.records() == () and codes(output) == ["config.no_document"]
    assert output.findings()[0].severity is Severity.INFO


@pytest.mark.parametrize("name", ["deep.json", "deep.yaml"])
def test_nesting_past_max_depth_is_a_finding(name: str) -> None:
    output = run(fixture(name))
    assert output.records() == () and codes(output) == ["config.too_deep"]
    assert run(fixture(name), max_depth=400).records() != () or name == "deep.json"


def test_size_limits_are_findings() -> None:
    data = fixture("huge_scalar.yaml")
    assert len(values(run(data))) == 3
    small = run(data, max_scalar_length=1000)
    note = by_path(small)[("note",)]
    assert (note.text, note.value) == (Unknown(), Unknown(note.value.provenance))  # type: ignore[union-attr]
    assert codes(small) == ["config.scalar_too_large"]
    assert reading(by_path(small)[("rate",)]) == ("int", 1)
    big = run(data, max_bytes=1024)
    assert big.records() == () and codes(big) == ["config.too_large"]


def test_a_yaml_escape_to_an_unpaired_surrogate_is_unrepresentable() -> None:
    output = run(b'a: "\\ud800"\n"\\udfff": 1\nb: 2\n')  # found by fuzzing
    found = by_path(output)
    assert (reading(found[("a",)]), found[("a",)].text) == ("unknown", Unknown())
    assert reading(found[("b",)]) == ("int", 2)
    assert codes(output) == ["config.unrepresentable_value", "config.unsupported_key"]


def test_a_base_60_number_too_long_to_hold_is_a_finding_not_an_exception() -> None:
    data = ("rate: 1" + ":00" * 180 + ".5\nother: 2\n").encode()  # found by review
    assert ConfigAdapter().probe(data, ProbeHints("c.yaml", len(data))).confidence == STRUCTURE
    output = run(data, yaml_version="1.1")
    found = by_path(output)
    assert reading(found[("rate",)]) == "unknown" and reading(found[("other",)]) == ("int", 2)
    assert codes(output) == ["config.unrepresentable_value"]
    declared = run(data)  # 1.2 reads it as text, so the readings disagree: no reading at all
    assert reading(by_path(declared)[("rate",)]) == "unknown"
    assert codes(declared) == ["config.unrepresentable_value"]


def test_nonstandard_and_unrepresentable_json_numbers() -> None:
    data = fixture("nonfinite.json")
    output = run(data)
    found = by_path(output)
    assert reading(found[("nan",)]) == ("float", NonFinite.NAN)
    assert reading(found[("inf",)]) == ("float", NonFinite.POSITIVE_INFINITY)
    assert reading(found[("ninf",)]) == ("float", NonFinite.NEGATIVE_INFINITY)
    assert reading(found[("overflow",)]) == "unknown"
    assert found[("overflow",)].text == Known("1e400")
    assert reading(found[("underflow",)]) == ("float", 0.0)
    assert reading(found[("negative_zero",)]) == ("float", -0.0)
    assert canonical_json.dumps(found[("negative_zero",)].to_json()).count(b'"value":-0.0') == 1
    assert reading(found[("exact",)]) == ("float", 1.0)
    assert reading(found[("big",)]) == "unknown"
    assert (reading(found[("surrogate",)]), found[("surrogate",)].text) == ("unknown", Unknown())
    assert codes(output) == [
        "config.nonstandard_json",
        "config.unrepresentable_value",
        "config.unsupported_key",
    ]
    assert finding(output, "config.unrepresentable_value").details["count"] == 3


# --- Determinism, chunking and lineage -----------------------------------------------------------

EVERY_FIXTURE: Final = sorted(
    path.name for path in FIXTURES.iterdir() if path.suffix not in (".py", ".md")
)


def as_bytes(output: SourceOutput) -> bytes:
    rows = [record.to_json() for record in output.package_records()]
    return b"".join(canonical_json.dumps(row) + b"\n" for row in rows)


@pytest.mark.parametrize("name", EVERY_FIXTURE)
def test_ingesting_twice_gives_identical_bytes(name: str) -> None:
    data = fixture(name)
    assert as_bytes(run(data)) == as_bytes(run(data))


@pytest.mark.parametrize("name", ["nav2_params.yaml", "px4_params.json", "multi_document.yaml"])
def test_output_never_depends_on_where_chunks_are_cut(name: str) -> None:
    data = fixture(name)
    whole = run(data)
    for size in (1, 7, 64):
        cut = run(data, chunk_values=size)
        assert cut.records() == whole.records() and cut.findings() == whole.findings()
    assert len(ConfigAdapter(7).plan(BytesReader(data), configure(DESCRIPTOR)).chunks) > 1


def test_a_chunk_ingests_the_same_alone_as_in_a_run() -> None:
    data = fixture("px4_params.json")
    adapter, source, config = ConfigAdapter(50), BytesReader(data), configure(DESCRIPTOR)
    plan = adapter.plan(source, config)
    outputs = [adapter.ingest(source, chunk, config) for chunk in plan.chunks]
    assert adapter.ingest(source, plan.chunks[2], config) == outputs[2]
    first = outputs[0]
    assert isinstance(first, ChunkOutput)
    assert sum(isinstance(r, ConfigurationSnapshot) for o in outputs for r in o.records) == 1
    assert isinstance(first.records[0], ConfigurationSnapshot)


def test_another_config_or_version_is_another_lineage() -> None:
    data = fixture("yaml_types.yaml")
    default, assumed = run(data), run(data, yaml_version="1.2")
    assert {r.id for r in default.records()}.isdisjoint(r.id for r in assumed.records())
    assert snapshots(default)[0].digest != snapshots(assumed)[0].digest
    # The values a version changes nothing about keep their digest's part: same paths.
    assert by_path(default).keys() == by_path(assumed).keys()


# --- Robustness: any bytes are findings, never an exception --------------------------------------

SEEDS: Final = [fixture(name) for name in ("nav2_params.yaml", "px4_params.json", "anchors.yaml")]
SEEDS.append(fixture("gripper_tool.toml"))


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    seed=st.sampled_from(SEEDS),
    cut=st.integers(min_value=0, max_value=6000),
    edits=st.lists(st.tuples(st.integers(0, 6000), st.binary(min_size=0, max_size=4)), max_size=4),
)
def test_mutated_configuration_never_raises(
    seed: bytes, cut: int, edits: list[tuple[int, bytes]]
) -> None:
    data = bytearray(seed[: cut or len(seed)])
    for position, replacement in edits:
        at = position % (len(data) + 1)
        data[at : at + len(replacement)] = replacement
    output = run(bytes(data), chunk_values=17)  # the harness checks every law on the way
    assert output.records() or output.findings()
    ConfigAdapter().probe(bytes(data)[:PROBE_HEAD_SIZE], ProbeHints("x", len(data)))


@settings(max_examples=100, deadline=None)
@given(st.binary(max_size=300))
def test_arbitrary_bytes_never_raise(data: bytes) -> None:
    output = run(data)
    assert output.records() or output.findings()
