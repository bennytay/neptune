"""The version-1 schema: strict declarations, pointers in errors, an editor schema (ADR 0047)."""

import json
from pathlib import Path

import jsonschema
import pytest

from neptune.manifest import ManifestError, json_schema, parse_manifest
from neptune.model.source import LocalPath

SCHEMA_FILE = Path(__file__).parents[3] / "docs" / "schema" / "manifest.schema.json"

DIGEST = "ab" * 32
FULL = b"""\
neptune: 1
machines:
  - id: ur5e-cell-2
    name: "UR5e, cell 2"
    embodiment: manipulator
    aliases: {serial: "20235400123", ros_namespace: [/ur_left, ur_left]}
  - {id: anymal-c-03, embodiment: legged}
sites:
  - {id: lab-a, name: Lab A}
tasks:
  - {id: pick-place, description: "Pick parts from the tray"}
software:
  - {id: driver, name: ur_robot_driver, version: 2.10}
runs:
  - name: pick
    paths: [arm/pick_1.mcap, arm/pick_2.mcap]
    machine: ur5e-cell-2
    site: lab-a
    task: pick-place
    software: [driver]
    snapshots:
      - {path: arm/config/controller.yaml}
      - {content: "sha256:%s"}
  - {name: trot, paths: [quadruped/], machine: anymal-c-03}
sources:
  - {path: arm/joint_notes.txt, adapter: markdown}
  - glob: "logs/**/*.csv"
    adapter: tabular
    options: {csv_delimiter: ";"}
adapters:
  tabular: {options: {csv_delimiter: ","}}
grouping:
  gap_seconds: 30
""" % DIGEST.encode()


def test_a_full_manifest_reads() -> None:
    manifest = parse_manifest(FULL)
    assert [e.id for e in manifest.section("machines")] == ["ur5e-cell-2", "anymal-c-03"]
    software = manifest.section("software")[0]
    assert dict(software.extra) == {"version": "2.10"}  # text as written, never 2.1
    pick, trot = manifest.runs
    assert pick.machine == "ur5e-cell-2" and pick.software == ("driver",)
    assert trot.paths == ("quadruped",)
    path_rule, glob_rule = manifest.sources
    assert path_rule.matches(LocalPath("arm/joint_notes.txt"))
    assert not path_rule.matches(LocalPath("arm/joint_notes.txt.bak"))
    assert glob_rule.matches(LocalPath("logs/a/b/x.csv")) and glob_rule.matches(
        LocalPath("logs/x.csv")
    )
    assert not glob_rule.matches(LocalPath("other/logs/x.csv"))
    assert manifest.adapter_options() == {"tabular": {"csv_delimiter": ","}}
    assert manifest.gap_seconds == 30
    aliases = manifest.section("machines")[0].aliases
    assert aliases == (
        ("ros_namespace", "/ur_left"),
        ("ros_namespace", "ur_left"),
        ("serial", "20235400123"),
    )


def test_snapshot_pins_and_alias_pointers_read() -> None:
    manifest = parse_manifest(FULL)
    pick = manifest.runs[0]
    assert [pin.to_json() for pin in pick.snapshots] == [
        {"path": "arm/config/controller.yaml"},
        {"content": f"sha256:{DIGEST}"},
    ]
    assert [pin.pointer for pin in pick.snapshots] == [
        "/runs/0/snapshots/0",
        "/runs/0/snapshots/1",
    ]
    assert manifest.runs[1].snapshots == ()
    assert pick.to_json()["snapshots"] == [pin.to_json() for pin in pick.snapshots]
    machine = manifest.section("machines")[0]
    assert machine.alias_pointers == (
        "/machines/0/aliases/ros_namespace/0",
        "/machines/0/aliases/ros_namespace/1",
        "/machines/0/aliases/serial",
    )
    # An alias written twice cites where it is first written.
    twice = parse_manifest(b"neptune: 1\nmachines:\n  - {id: a, aliases: {x: [b, c, b]}}\n")
    entity = twice.section("machines")[0]
    assert entity.aliases == (("x", "b"), ("x", "c"))
    assert entity.alias_pointers == ("/machines/0/aliases/x/0", "/machines/0/aliases/x/1")


def test_a_directory_path_covers_what_is_below_it() -> None:
    manifest = parse_manifest(b"neptune: 1\nsources:\n  - {path: cam/, adapter: image}\n")
    rule = manifest.sources[0]
    assert rule.matches(LocalPath("cam/a.png")) and not rule.matches(LocalPath("camera/a.png"))


def test_json_and_yaml_give_the_same_declarations() -> None:
    as_json = json.dumps(
        {
            "neptune": 1,
            "runs": [{"name": "pick", "paths": ["arm/pick_2.mcap", "arm/pick_1.mcap"]}],
            "sources": [{"glob": "**/*.txt", "adapter": "text"}],
        }
    ).encode()
    as_yaml = (
        b"neptune: 1\nruns:\n- {name: pick, paths: [arm/pick_1.mcap, arm/pick_2.mcap]}\n"
        b"sources:\n- {glob: '**/*.txt', adapter: text}\n"
    )
    assert parse_manifest(as_json, json_syntax=True).to_json() == parse_manifest(as_yaml).to_json()


@pytest.mark.parametrize(
    ("text", "says"),
    [
        ("neptune: 2\n", "version 1"),
        ("neptune: true\n", "version 1"),
        ("runs: []\n", "missing keys"),
        ("neptune: 1\nrobots: []\n", "unknown keys"),
        ("neptune: 1\nmachines:\n  - {id: a, colour: red}\n", r"\(/machines/0\): unknown keys"),
        ("neptune: 1\nmachines:\n  - {id: a}\n  - {id: a}\n", "declared twice"),
        ("neptune: 1\nmachines:\n  - {id: 'has space'}\n", "not an id"),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x], machine: ghost}\n", "no machines entry"),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x], software: [ghost]}\n", "no software entry"),
        ("neptune: 1\nruns:\n  - {name: a, paths: []}\n", "at least one path"),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x, x]}\n", "listed twice"),
        (
            "neptune: 1\nruns:\n  - {name: a, paths: [x]}\n  - {name: a, paths: [y]}\n",
            "declared twice",
        ),
        ("neptune: 1\nruns:\n  - {name: a, paths: [../escape]}\n", "root-relative"),
        ("neptune: 1\nruns:\n  - {name: a, paths: [a/./b]}\n", "root-relative"),
        ("neptune: 1\nruns:\n  - {name: a, paths: [/etc/passwd]}\n", "absolute"),
        ("neptune: 1\nsources:\n  - {glob: '../**', adapter: text}\n", "not a glob"),
        ("neptune: 1\nsources:\n  - {glob: '/abs/*', adapter: text}\n", "absolute"),
        ("neptune: 1\nsources:\n  - {glob: '!x', adapter: text}\n", "negation"),
        ("neptune: 1\nsources:\n  - {glob: 'dir/', adapter: text}\n", "must match files"),
        ("neptune: 1\nsources:\n  - {path: a, glob: b, adapter: text}\n", "exactly one"),
        ("neptune: 1\nsources:\n  - {path: a}\n", "missing keys"),
        (
            "neptune: 1\nsources:\n  - {path: a, adapter: text, options: {Bad-Name: 1}}\n",
            "option name",
        ),
        ("neptune: 1\nsources:\n  - {path: a, adapter: text, options: {x: null}}\n", "null"),
        ("neptune: 1\nadapters:\n  text: {}\n", "missing keys"),
        ("neptune: 1\ngrouping: {gap_seconds: -1}\n", "from 0"),
        ("neptune: 1\ngrouping: {gap_seconds: 1.5}\n", "whole number"),
        ("neptune: 1\nmachines: {id: a}\n", "expected a list"),
        ("neptune: 1\nsites:\n  - {id: s, name: ''}\n", "non-empty"),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{path: ../c.yaml}]}\n", "root"),
        (
            "neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{path: /c.yaml}]}\n",
            "absolute",
        ),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{}]}\n", "exactly one"),
        (
            "neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{path: c, content: d}]}\n",
            "exactly one",
        ),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{url: c}]}\n", "unknown keys"),
        (
            "neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{content: 'sha256:AB'}]}\n",
            "not a content id",
        ),
        (
            "neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: [{path: c}, {path: c/}]}\n",
            "pinned twice",
        ),
        ("neptune: 1\nruns:\n  - {name: a, paths: [x], snapshots: {path: c}}\n", "a list"),
        ("neptune: 1\nmachines:\n  - {id: a, aliases: {'-x': b}}\n", "not a namespace id"),
        ("neptune: 1\nmachines:\n  - {id: a, aliases: {'a b': b}}\n", "not a namespace id"),
    ],
)
def test_refused_declarations(text: str, says: str) -> None:
    with pytest.raises(ManifestError, match=says):
        parse_manifest(text.encode())


def test_errors_name_the_line_and_pointer() -> None:
    with pytest.raises(ManifestError) as error:
        parse_manifest(b"neptune: 1\nruns:\n  - name: a\n    paths: [x]\n    machine: ghost\n")
    assert "line 5" in str(error.value) and "/runs/0/machine" in str(error.value)


def test_empty_sections_are_empty() -> None:
    manifest = parse_manifest(b"neptune: 1\nmachines:\nruns:\nsources:\nadapters:\n")
    assert manifest.to_json() == {"neptune": 1}


def test_the_exported_schema_is_current() -> None:
    assert json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == json_schema()


def test_the_editor_schema_agrees_with_the_reader() -> None:
    validator = jsonschema.Draft202012Validator(json_schema())
    validator.check_schema(json_schema())
    full = parse_manifest(FULL).to_json()
    validator.validate(json.loads(json.dumps(full)))
    again = parse_manifest(json.dumps(full).encode(), json_syntax=True)
    assert again.to_json() == full  # the canonical form is a manifest of the same declarations
    # Plain numbers in text fields are read as text, and the editor's schema accepts them.
    as_numbers = {
        "neptune": 1,
        "software": [{"id": 7, "version": 1.1}],
        "sites": [{"id": "s", "name": True}],
    }
    validator.validate(as_numbers)
    yaml = parse_manifest(b"neptune: 1\nsoftware:\n  - {id: 7, version: 1.10}\n")
    assert yaml.section("software")[0].to_json() == {"id": "7", "version": "1.10"}
    assert not validator.is_valid({"neptune": 1, "robots": []})
    assert not validator.is_valid({"neptune": 2})


def test_a_version_1_alias_namespace_that_is_no_record_namespace_still_reads() -> None:
    # Version 1 accepted any id as a namespace; ADR 0072 §5 keeps that: the records pass says
    # which aliases cannot become identifiers, rather than the manifest being refused.
    text = b"neptune: 1\nmachines:\n  - {id: a, aliases: {Serial: b, 'px4:uuid': c}}\n"
    (machine,) = parse_manifest(text).section("machines")
    assert machine.aliases == (("Serial", "b"), ("px4:uuid", "c"))
