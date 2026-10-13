"""The acceptance corpus without ingesting it: determinism, the lock, sizes, the storyline's
ingredients in the bytes, and the gold document's and deploy declaration's shape (Platform ADR 0007,
ADR 0008)."""

import copy
import csv
import importlib.util
import io
import json
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from harness import acceptance, corpus, stages
from harness.acceptance import __main__ as acceptance_cli
from harness.acceptance import generate, resolve
from harness.stages import read_deploy

REQUIRED_QUESTIONS: Final = {
    "Q1": "why",
    "Q2": "change",
    "Q3": "configuration",
    "Q4": "gaps",
}


@pytest.fixture(scope="module")
def files() -> dict[str, bytes]:
    return acceptance.build()


@pytest.fixture(scope="module")
def gold() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(acceptance.GOLD.read_text(encoding="utf-8"))
    return document


def test_two_builds_are_byte_identical_and_match_the_committed_lock(
    files: dict[str, bytes],
) -> None:
    assert acceptance.build() == files
    assert list(files) == sorted(files)
    assert acceptance.lock_problems(files) == []
    assert acceptance.LOCK.read_text(encoding="utf-8") == acceptance.render_lock(files)


def test_the_lock_gold_and_version_agree(gold: dict[str, Any]) -> None:
    lock = acceptance.read_lock()
    deploy = json.loads(acceptance.DEPLOY.read_text(encoding="utf-8"))
    assert lock["version"] == acceptance.VERSION == gold["corpus_version"]
    assert deploy["corpus_version"] == acceptance.VERSION
    assert lock["corpus"] == acceptance.NAME == gold["corpus"] == deploy["corpus"]
    assert acceptance.label() == f"acceptance {acceptance.VERSION} (tree {lock['tree']})"


def test_every_file_is_small_and_every_path_is_plain(files: dict[str, bytes]) -> None:
    assert all(0 < len(data) <= acceptance.MAX_FILE_BYTES for data in files.values())
    for path in files:
        parts = path.split("/")
        assert not path.startswith("/") and ".." not in parts and "" not in parts


def test_a_changed_byte_without_a_new_version_breaks_the_lock(files: dict[str, bytes]) -> None:
    changed = dict(files)
    changed["sites/PLANT-2/cell3/config/cell_config.yaml"] += b"# edited\n"
    problems = acceptance.lock_problems(changed)
    assert len(problems) == 1
    assert "sites/PLANT-2/cell3/config/cell_config.yaml" in problems[0]
    assert "bump VERSION" in problems[0]
    big = {**files, "extra.bin": b"\0" * (acceptance.MAX_FILE_BYTES + 1)}
    assert any("over 524288" in p for p in acceptance.lock_problems(big))


def test_materialise_writes_the_tree_and_replaces_what_was_there(
    tmp_path: Path, files: dict[str, bytes]
) -> None:
    root = tmp_path / "corpus"
    (root / "stale").mkdir(parents=True)
    acceptance.materialise(root)
    written = {
        p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert written == files


def test_two_sites_and_three_morphologies_are_declared(files: dict[str, bytes]) -> None:
    manifest = yaml.safe_load(files["neptune.yaml"])
    assert {site["id"] for site in manifest["sites"]} == {"S-007", "PLANT-2"}
    kinds = {machine["embodiment"] for machine in manifest["machines"]}
    assert kinds == {"mobile_base", "manipulator", "legged"}
    tops = {path.split("/")[1] for path in files if path.startswith("sites/")}
    assert tops == {"S-007", "PLANT-2"}
    assert any(p.endswith("qd2.urdf") for p in files)
    assert any(p.endswith("arm6.urdf") for p in files)
    assert any(p.endswith("tug_200.urdf") for p in files)


def test_the_d1_archetypes_are_reused_not_forked(files: dict[str, bytes]) -> None:
    fleet = generate.A.fleet()
    cell = generate.A.cell()
    # S-007's runs and the cell's bag and documents are the D1 generator's bytes.
    for path in ("runs/S-007/amr-07_2026-04-02.mcap", "incidents/INC-0007.pdf"):
        assert files[f"sites/S-007/{path.replace('runs/S-007/', 'runs/')}"] == fleet[path]
    for path in ("urdf/arm6.urdf", "documents/sop_CELL-014_finger_set.pdf"):
        assert files[f"sites/PLANT-2/cell3/{path}"] == cell[path]
    # The D1 cell's calibrations are rewritten as easy_handeye output, with the D1's ids, frames
    # and translations (corpus 2.0.0): nothing about them is invented anew.
    d1 = {p: yaml.safe_load(d) for p, d in cell.items() if p.startswith("calibration/")}
    assert d1
    for path, before in d1.items():
        after = yaml.safe_load(files[f"sites/PLANT-2/cell3/{path}"])
        assert {k: after["transformation"][k] for k in "xyz"} == {
            k: before["translation"][k] for k in "xyz"
        }
        frames = ("eye_on_hand", "robot_effector_frame", "tracking_base_frame")
        assert {k: after["parameters"][k] for k in frames} == {k: before[k] for k in frames}
    # Narrowed to S-007: no S-012 row, run, map or robot config survives.
    s007 = [p for p in files if p.startswith("sites/S-007/")]
    assert not [p for p in s007 if "S-012" in p or "AMR-08" in p or "AMR-09" in p]
    for path in s007:
        if path.endswith(".csv"):
            assert b"S-012" not in files[path]


def test_the_storyline_ingredients_are_in_the_bytes(files: dict[str, bytes]) -> None:
    cell = "sites/PLANT-2/cell3"
    # A configuration change recorded only in a maintenance ticket.
    assert b"151.5 mm on pendant" in files["sites/PLANT-2/cmms/work_orders.csv"]
    assert b"151.5" not in files["sites/PLANT-2/changes/servicenow_changes.csv"]
    # A stale configuration export, and an SOP revision without a change record.
    stale = yaml.safe_load(files[f"{cell}/config/cell_config.yaml"])
    assert stale["tool"]["tcp_z_mm"] == 145.5 and stale["exported"].startswith("2026-09-01")
    # One duplicated run under a new name.
    good = files[f"{cell}/bags/pallet_2026-09-09/pallet_2026-09-09_0.mcap"]
    assert files[f"{cell}/shared/for_vendor/cell3_reference_run.mcap"] == good
    # Prompt-injection text.
    assert (
        b"ignore all previous instructions"
        in files["sites/PLANT-2/vendor/SB-2026-117_PG-80_finger_sets.md"]
    )
    # Calibration history across the change: 1.86 px against 0.44 px, and the camera 4.3 mm lower.
    log = _rows(files[f"{cell}/calibration/handeye_calibration_log.csv"], "Calibration ID")
    assert log["CAL-ARM3A-0911"]["Reprojection Error px"] == "1.86"
    assert log["CAL-ARM3A-0818"]["Reprojection Error px"] == "0.44"
    z = {
        ident: yaml.safe_load(files[f"{cell}/calibration/{ident}.yaml"])["transformation"]["z"]
        for ident in ("CAL-ARM3A-0818", "CAL-ARM3A-0911")
    }
    assert round((z["CAL-ARM3A-0818"] - z["CAL-ARM3A-0911"]) * 1000, 6) == 4.3


def _rows(data: bytes, key: str) -> dict[str, dict[str, str]]:
    return {row[key]: row for row in csv.DictReader(io.StringIO(data.decode()))}


# What easy_handeye's HandeyeCalibration.to_dict writes: vars() of HandeyeCalibrationParameters,
# and the transform's translation and rotation (IFL-CAMP/easy_handeye, handeye_calibration.py).
EASY_HANDEYE_PARAMETERS: Final = {
    "eye_on_hand",
    "freehand_robot_movement",
    "move_group",
    "move_group_namespace",
    "namespace",
    "robot_base_frame",
    "robot_effector_frame",
    "tracking_base_frame",
    "tracking_marker_frame",
}


def test_the_calibrations_are_what_easy_handeye_writes(files: dict[str, bytes]) -> None:
    """Its writer is ``yaml.dump(to_dict(c), default_flow_style=False)``: PyYAML, the official
    reader and writer, reads each file to that shape and writes the same bytes back."""
    cal = [p for p in files if "/calibration/CAL-ARM3A-" in p]
    assert len(cal) == len(generate.HANDEYE) == 5
    for path in cal:
        loaded = yaml.safe_load(files[path])
        assert yaml.dump(loaded, default_flow_style=False).encode() == files[path], path
        assert set(loaded) == {"parameters", "transformation"}
        assert set(loaded["parameters"]) == EASY_HANDEYE_PARAMETERS
        assert set(loaded["transformation"]) == {"x", "y", "z", "qx", "qy", "qz", "qw"}
        assert loaded["parameters"]["eye_on_hand"] is True  # so robot_effector_frame is the parent
        q = loaded["transformation"]
        assert abs(q["qx"] ** 2 + q["qy"] ** 2 + q["qz"] ** 2 + q["qw"] ** 2 - 1) < 1e-5
        assert b"ARM-3A" not in files[path]  # the format names no robot: the manifest does


def test_the_manifest_declares_the_machine_of_every_calibration(files: dict[str, bytes]) -> None:
    runs = yaml.safe_load(files["neptune.yaml"])["runs"]
    declared = {path: run for run in runs for path in run["paths"]}
    for c in generate.HANDEYE:
        run = declared[f"sites/PLANT-2/cell3/calibration/{c.ident}.yaml"]
        assert (run["machine"], run["site"]) == ("ARM-3A", "PLANT-2")


def _mapped_namespaces(document: Any) -> set[str]:
    """Every ``namespace`` a mapping declares for its ``machines`` (any depth)."""
    found: set[str] = set()
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "machines" and isinstance(value, list):
                found.update(item["namespace"] for item in value)
            else:
                found |= _mapped_namespaces(value)
    elif isinstance(document, list):
        for item in document:
            found |= _mapped_namespaces(item)
    return found


def test_machine_aliases_are_in_the_namespaces_the_mappings_key_machines_by(
    files: dict[str, bytes],
) -> None:
    """Memory joins a record's machine to the manifest's only through a declared alias spelled as
    the mapping keys it (Platform ADR 0013): Deploy's presets and incident template, and Memory's
    event-table declaration for the syslog export's ``Host``. Each alias names the machine as the
    source writes it."""
    deploy = corpus.REPO / "packages/neptune-deploy/src/neptune_deploy/lifecycle/presets"
    mappings = {
        name: _mapped_namespaces(json.loads((deploy / name).read_text(encoding="utf-8")))
        for name in (
            "cmms_generic.json",
            "cmms_downtime.json",
            "servicenow_csv.json",
            "requalification_csv.json",
            "templates/incident_report.json",
        )
    }
    memory = json.loads(
        (
            corpus.REPO
            / "packages/neptune-memory/tests/fixtures/acceptance_corpus.memory_config.json"
        ).read_text(encoding="utf-8")
    )
    syslog = {t["machine"]["namespace"] for t in memory["memory.events"]["tables"]}
    assert mappings == {
        "cmms_generic.json": {"cmms.asset"},
        "cmms_downtime.json": {"cmms.asset"},
        "servicenow_csv.json": {"servicenow.ci"},
        "requalification_csv.json": {"requalification.robot"},
        "templates/incident_report.json": {"incident_report.machine"},
    }
    assert syslog == {"syslog.host"}
    keyed = set().union(*mappings.values(), syslog)
    for ident, _, aliases in generate.MACHINES:
        assert set(aliases) <= keyed, ident
        assert set(aliases.values()) == {ident}
    by_machine = {ident: set(aliases) for ident, _, aliases in generate.MACHINES}
    assert by_machine["ARM-3A"] == keyed  # every source names the arm of the incident
    assert by_machine["AMR-07"] == keyed - {"syslog.host"}
    # The sources do write the machine that way.
    syslog_rows = _rows(files["sites/PLANT-2/cell3/logs/syslog_LOG-P2_2026-09-14.csv"], "Seq")
    assert "ARM-3A" in {row["Host"] for row in syslog_rows.values()}
    for path, robot in (
        ("sites/PLANT-2/cell3/requalification/requalification_tests.csv", "ARM-3A"),
        ("sites/S-007/requalification/requalification_tests.csv", "AMR-07"),
    ):
        assert {row["Robot"] for row in _rows(files[path], "Requal ID").values()} == {robot}
    assert b"ARM-3A" in files["sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf"]
    assert b"AMR-07" in files["sites/S-007/incidents/INC-0007.pdf"]


def test_the_cmms_and_syslog_stops_of_inc_c3_0011_are_32_s_apart(files: dict[str, bytes]) -> None:
    """The stop the operator joins: the CMMS's hand-entered time is 32 s after the controller's,
    which is the incident report's HMI time and the bag's header stamp of the collision."""
    stops = _rows(files["sites/PLANT-2/cmms/downtime_log.csv"], "Downtime ID")
    syslog = _rows(files["sites/PLANT-2/cell3/logs/syslog_LOG-P2_2026-09-14.csv"], "Seq")
    cmms = datetime.fromisoformat(stops["DT-26-0914-01"]["Stopped"])
    pstop = datetime.fromisoformat(syslog["4182"]["Timestamp"])
    assert (cmms - pstop).total_seconds() == 32
    assert syslog["4182"]["Host"] == "ARM-3A" and "PSTOP" in syslog["4182"]["Message"]
    # One RFC 5424 MSGID per event type, for a mapping to key on (the storyline's four events).
    assert {seq: row["MsgID"] for seq, row in syslog.items()} == {
        "4170": "PGM_START",
        "4182": "PSTOP",
        "4183": "ESTOP",
        "4186": "LOTO",
    }
    assert stops["DT-26-0914-01"]["Restarted"] == ""  # a blank, never a restart time
    seconds = int((pstop - datetime(1970, 1, 1)).total_seconds())  # local wall time, as written
    assert generate.local_ns(2026, 9, 14, 14, 32, 38) == (seconds + 4 * 3600) * 10**9  # EDT
    assert b"2026-09-14 14:32:38" in files["sites/PLANT-2/cell3/incidents/INC-C3-0011.pdf"]
    # The assertion joins exactly these two and the incident report (2.2.0), and says who, when
    # and about what.
    document = json.loads(files["sites/PLANT-2/cell3/incidents/INC-C3-0011.assertions.json"])
    assert (document["format"], document["version"]) == ("neptune.assertions", 1)
    (entry,) = document["assertions"]
    assert entry["assertion_type"] == "same_identity"
    assert entry["scope"] == [
        {"namespace": "cmms.downtime", "value": "DT-26-0914-01"},
        {"namespace": "incident_report.incident", "value": "INC-C3-0011"},
        {"namespace": "syslog", "value": "4182"},
    ]
    assert entry["author"] == {"namespace": "plant-2.staff", "value": "a.novak"}
    assert entry["authored_at"] == "2026-09-15T09:05:00-04:00"
    assert entry["payload"] == {"incident": "INC-C3-0011", "relation": "same_event"}


def test_plant_2_keeps_its_envelopes_in_the_register_s_007_keeps(files: dict[str, bytes]) -> None:
    plant = files["sites/PLANT-2/authorisation/zone_register.csv"]
    s007 = files["sites/S-007/authorisation/zone_register.csv"]
    assert plant.split(b"\n", 1)[0] == s007.split(b"\n", 1)[0]  # register_zone reads both
    rows = _rows(plant, "Envelope ID")
    assert sorted(rows) == ["ENV-P2-01", "ENV-P2-02", "ENV-P2-03"]
    zones = json.loads(files["sites/PLANT-2/maps/PLANT-2_zones.geojson"])
    names = {feature["properties"]["zone_id"] for feature in zones["features"]}
    assert {row["Zone"] for row in rows.values()} <= names
    machines = {m["id"] for m in yaml.safe_load(files["neptune.yaml"])["machines"]}
    assert {row["Robots"] for row in rows.values()} <= machines


def test_the_corrupt_bag_ends_inside_a_chunk_and_its_metadata_still_claims_everything(
    files: dict[str, bytes],
) -> None:
    folder = "sites/PLANT-2/legged/runs/patrol_2026-09-14"
    data = files[f"{folder}/patrol_2026-09-14_0.mcap"]
    assert data.startswith(generate.M.MAGIC) and not data.endswith(generate.M.MAGIC)
    spans = generate.chunk_spans(data)
    offset, length, _, _ = spans[-1]
    assert offset + length > len(data)  # the last chunk is cut short
    metadata = yaml.safe_load(files[f"{folder}/metadata.yaml"])
    claimed = metadata["rosbag2_bagfile_information"]["message_count"]
    assert claimed > 0
    with pytest.raises(ValueError, match="no chunk holds"):
        generate.cut_inside(data, 0)


def test_the_incident_bag_has_two_clocks_that_disagree(files: dict[str, bytes]) -> None:
    """The bag's log times are the cell PC's; the header stamps are the controller's."""
    data = files["sites/PLANT-2/cell3/bags/pallet_2026-09-14/pallet_2026-09-14_0.mcap"]
    offsets = set()
    at = len(generate.M.MAGIC)
    while at + 9 <= len(data):
        opcode, length = struct.unpack_from("<BQ", data, at)
        if opcode == 0x06:  # a chunk: its records start after the fixed fields and the name
            name_length = struct.unpack_from("<I", data, at + 9 + 28)[0]
            inner = at + 9 + 32 + name_length + 8
            end = at + 9 + length
            while inner < end:
                kind, size = struct.unpack_from("<BQ", data, inner)
                if kind == 0x05:
                    log_time = struct.unpack_from("<Q", data, inner + 9 + 6)[0]
                    sec, nanosec = struct.unpack_from("<iI", data, inner + 9 + 22 + 4)
                    offsets.add(log_time - (sec * 10**9 + nanosec))
                inner += 9 + size
        at += 9 + length
    # Every message with a header (joint states, diagnostics) is 96.7 s late; the strings are not
    # stamped, so their first bytes read as a small number.
    assert generate.IPC_AHEAD_2026_09_14 in offsets


def test_the_gold_document_is_sound(gold: dict[str, Any]) -> None:
    assert resolve.check_gold(gold) == []
    questions = {q["id"]: q for q in gold["questions"]}
    for qid, tag in REQUIRED_QUESTIONS.items():
        assert tag in questions[qid]["tags"]
    unknown = [c for c in questions["Q4"]["claims"] if c["knowledge"] == "unknown"]
    assert len(unknown) == len(questions["Q4"]["claims"]) >= 3
    morphologies = {t for q in gold["questions"] for t in q["tags"]}
    assert {"manipulator", "mobile-base", "legged"} <= morphologies
    kinds = {item["select"]["kind"] for item in gold["evidence"].values()}
    assert kinds == set(resolve.SELECTORS)
    for item in gold["evidence"].values():
        assert item["select"]["path"] in acceptance.read_lock()["files"]
    assert {trap["kind"] for trap in gold["traps"]} >= {
        "prompt_injection",
        "stale_config",
        "duplicate_run",
        "corrupt_source",
    }


def test_check_gold_names_every_structural_problem(gold: dict[str, Any]) -> None:
    broken = copy.deepcopy(gold)
    broken["questions"][0]["claims"][0]["evidence"].append("nope")
    broken["questions"][0]["claims"][1]["id"] = "Q9.C1"
    broken["questions"][0]["claims"][2]["knowledge"] = "maybe"
    broken["questions"][1]["id"] = "Q1"
    broken["evidence"]["orphan"] = {"select": {"kind": "telepathy"}, "says": ""}
    problems = resolve.check_gold(broken)
    assert "Q1.C1: cites unknown evidence nope" in problems
    assert "Q1: claim 'Q9.C1' is not numbered under it" in problems
    assert "Q1.C3: knowledge must be known or unknown" in problems
    assert "Q1: repeated question id" in problems
    assert "orphan: unknown selector 'telepathy'" in problems
    assert "orphan: no path" in problems and "orphan: no 'says'" in problems
    assert "orphan: never cited" in problems


def test_a_bad_selector_is_a_gold_error_and_a_missing_source_resolves_to_nothing(
    tmp_path: Path,
) -> None:
    package = resolve.Package(tmp_path)
    with pytest.raises(resolve.GoldError, match="unknown selector kind"):
        resolve.resolve_one(package, {"kind": "guess", "path": "x"})
    with pytest.raises(resolve.GoldError, match="needs path"):
        resolve.resolve_one(package, {"kind": "source"})
    missing = resolve.resolve_one(package, {"kind": "source", "path": "nowhere.csv"})
    assert missing["records"] == [] and "no source at nowhere.csv" in missing["problem"]


def test_select_builds_the_acceptance_corpus_or_the_worked_examples(tmp_path: Path) -> None:
    label, cases = corpus.select(into=tmp_path)
    assert label == f"acceptance {acceptance.VERSION}"
    assert [case.id for case in cases] == [f"acceptance-{acceptance.VERSION}"]
    assert cases[0].gold == acceptance.GOLD and (cases[0].sources / "neptune.yaml").is_file()
    assert cases[0].deploy == acceptance.DEPLOY
    label, cases = corpus.select(name="worked-examples")
    assert label == "worked-examples" and [c.id for c in cases] == list(corpus.EXAMPLE_NAMES)
    with pytest.raises(ValueError, match="unknown corpus"):
        corpus.select(name="nope")


def test_the_harness_workflow_runs_when_an_imported_generator_changes() -> None:
    workflow = (corpus.REPO / ".github" / "workflows" / "harness.yml").read_text(encoding="utf-8")
    writers = (
        generate.ARCHETYPES_SCRIPT,
        Path(generate.A.M.__file__),
        Path(generate.A.R.__file__),
        Path(generate.A.D.__file__),
        Path(generate.A.D.W.__file__),
    )
    for path in writers:
        assert f'- "{path.relative_to(corpus.REPO).as_posix()}"' in workflow, path
    # ...and the required check's plan runs this package's job (and so the lock test) on them.
    spec = importlib.util.spec_from_file_location(
        "acceptance_ci_plan", corpus.REPO / ".github" / "scripts" / "ci_plan.py"
    )
    assert spec is not None and spec.loader is not None
    ci_plan = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ci_plan
    spec.loader.exec_module(ci_plan)
    assert {p.relative_to(corpus.REPO).as_posix() for p in writers} == ci_plan.CORPUS_INPUTS


def test_the_resolver_escapes_pointers_scales_ticks_and_says_why_a_source_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    (root / "records").mkdir(parents=True)
    (root / "derived").mkdir()
    source = "sha256:" + "0" * 64
    ref = {"locator": [], "source": source}

    def lines(*records: dict[str, Any]) -> str:
        return "".join(json.dumps(r) + "\n" for r in records)

    known = {"knowledge": "known"}
    (root / "records" / "source_revision.jsonl").write_text(
        lines({"id": "rev", "content_id": source, "location": {"kind": "local", "path": "a.yaml"}})
    )
    (root / "records" / "configuration_value.jsonl").write_text(
        lines(
            {
                "id": "value",
                "path": ["topics", "/cmd_vel", "~x"],
                "provenance": {"evidence": ref},
                "text": {**known, "value": "1"},
            }
        )
    )
    (root / "records" / "timestamp_domain.jsonl").write_text(
        lines(
            {"id": "us", "resolution": {**known, "value": {"numerator": 1, "denominator": 10**6}}},
            {"id": "ns", "resolution": {**known, "value": {"numerator": 1, "denominator": 10**9}}},
            {"id": "unknown", "resolution": {"knowledge": "unknown"}},
        )
    )

    def mapping(ident: str, source_domain: str, source_ticks: int, target_ticks: int) -> str:
        anchor = {
            "source": {"domain_id": source_domain, "ticks": source_ticks},
            "target": {"domain_id": "ns", "ticks": target_ticks},
        }
        return json.dumps({"id": ident, "anchor": {**known, "value": anchor}, "evidence": [ref]})

    (root / "derived" / "clock_mapping.jsonl").write_text(
        mapping("us-to-ns", "us", 1_000_000, 3 * 10**9) + "\n" + mapping("unknown", "unknown", 1, 3)
    )
    package = resolve.Package(root)
    escaped = {"kind": "config_value", "path": "a.yaml", "pointer": "/topics/~1cmd_vel/~0x"}
    assert resolve.resolve_one(package, escaped)["records"] == ["value"]
    clocks = {"kind": "clock_mapping", "path": "a.yaml", "offset_s": [1.5, 2.5]}
    assert resolve.resolve_one(package, clocks)["records"] == ["us-to-ns"]  # 3 s - 1 s
    resolved = {"gone": resolve.resolve_one(package, {"kind": "source", "path": "b.csv"})}
    assert resolve.summary(resolved)["reasons"] == {"gone": "the package holds no source at b.csv"}


def test_build_refuses_a_directory_that_is_not_an_earlier_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "keep.py").write_text("precious\n", encoding="utf-8")
    monkeypatch.chdir(checkout)
    assert acceptance_cli.main(["build", "."]) == 2
    assert "refusing to replace it" in capsys.readouterr().err
    assert (checkout / "src" / "keep.py").read_text(encoding="utf-8") == "precious\n"
    link = tmp_path / "link"
    link.symlink_to(checkout)
    with pytest.raises(acceptance.CorpusError, match="not a directory"):
        acceptance.materialise(link)
    built = tmp_path / "built"
    acceptance.materialise(built)
    (built / "planted").symlink_to(checkout / "src" / "keep.py")
    with pytest.raises(acceptance.CorpusError, match="first: planted"):
        acceptance.materialise(built)


def test_a_stream_alone_never_supports_a_message_but_a_row_does() -> None:
    item = {
        "kind": "message",
        "records": ["rec:stream"],
        "citations": [
            {
                "locator": {"log_time": 7, "topic": "/diagnostics"},
                "path": "bag.mcap",
                "record": "rec:stream",
                "seq": 3,
            }
        ],
    }
    assert not resolve.supports(item, {"record": "rec:stream"})
    assert not resolve.supports(item, {"record": "rec:stream", "seq": 4})
    assert resolve.supports(item, {"record": "rec:stream", "seq": 3})
    by_path = {"path": "bag.mcap", "locator": {"log_time": 7, "topic": "/diagnostics"}}
    assert resolve.supports(item, by_path)
    assert not resolve.supports(
        item, {**by_path, "locator": {"log_time": 8, "topic": "/diagnostics"}}
    )


def test_a_package_without_base_records_scores_by_path_and_locator() -> None:
    """Deploy D3's lifecycle records cite a source and a row: matched against the base package."""
    row = {
        "kind": "table_row",
        "citations": [{"locator": {"row": 6}, "path": "w.csv", "record": "r"}],
    }
    assert resolve.supports(row, {"path": "w.csv", "locator": {"row": 6}})
    assert not resolve.supports(row, {"path": "w.csv", "locator": {"row": 5}})
    assert not resolve.supports(row, {"path": "other.csv", "locator": {"row": 6}})
    gap = {"kind": "no_table_row", "citations": [{"locator": None, "path": "c.csv", "record": "t"}]}
    assert not resolve.supports(gap, {"path": "c.csv", "locator": None})
    assert resolve.supports(gap, {"record": "t"})


def test_the_deploy_declaration_names_shipped_presets_and_owes_records() -> None:
    plan, problems = read_deploy(acceptance.DEPLOY)
    assert problems == [] and plan is not None
    shipped, clashes = stages._shipped_presets()  # every family: lifecycle and event-log
    assert set(plan.presets) <= set(shipped) and clashes == []
    assert {"cmms_generic", "jira_json", "register_zone", "servicenow_csv"} <= set(plan.presets)
    assert {"cmms_downtime", "requalification_csv", "syslog_csv"} <= set(plan.presets)
    assert plan.templates == (
        "packages/neptune-deploy/src/neptune_deploy/lifecycle/presets/templates/incident_report.json",
    )
    # What Deploy maps from the corpus, pinned so a missing record goes red (MVL-191).
    assert plan.at_least == {
        "authorisation_envelope": 5,  # S-007's two and PLANT-2's three
        "change_record": 5,
        "incident_record": 3,
        "intervention": 3,  # the downtime log's three stops
        "maintenance_event": 16,
        "requalification_record": 4,
        "structured_record": 4,  # the syslog export's four events (syslog_csv)
    }
    # The two zone-less exports of the same-event join are read in PLANT-2's zone, and the join
    # may not dangle (Platform ADR 0009).
    assert {(z.preset, z.source, z.civil_time_zone) for z in plan.sources} == {
        ("cmms_downtime", "sites/PLANT-2/cmms/downtime_log.csv", generate.PLANT_ZONE),
        (
            "syslog_csv",
            "sites/PLANT-2/cell3/logs/syslog_LOG-P2_2026-09-14.csv",
            generate.PLANT_ZONE,
        ),
    }
    assert plan.require_assertion_scopes


_ZONE: Final = {"preset": "x", "source": "sites/a/log.csv", "civil_time_zone": "Europe/Berlin"}
_ZONED: Final = {"deploy_format": 1, "presets": ["x"]}


def test_a_deploy_declaration_states_the_civil_zone_of_sources_that_state_none(
    tmp_path: Path,
) -> None:
    """``sources`` (Platform ADR 0009): per preset and corpus path, the zone the reading transform
    declares; optional, so a declaration without it reads as before."""
    path = tmp_path / "deploy.json"
    later = {**_ZONE, "source": "sites/a/z.csv", "civil_time_zone": "America/New_York"}
    path.write_text(json.dumps({**_ZONED, "sources": [later, _ZONE]}), encoding="utf-8")
    plan, problems = read_deploy(path)
    assert problems == [] and plan is not None
    assert [(z.source, z.civil_time_zone) for z in plan.sources] == [
        ("sites/a/log.csv", "Europe/Berlin"),
        ("sites/a/z.csv", "America/New_York"),
    ]
    path.write_text(json.dumps(_ZONED), encoding="utf-8")
    assert read_deploy(path)[0] == stages.DeployPlan(("x",), (), {}, ())
    # Presets that do not read are the problem; the zones are not judged against them.
    path.write_text(json.dumps({**_ZONED, "presets": [""], "sources": [_ZONE]}), encoding="utf-8")
    assert read_deploy(path) == (
        None,
        [
            "presets is not a list of names",
            "the deploy declaration names no preset and no template",
        ],
    )


@pytest.mark.parametrize(
    ("document", "problem"),
    [
        ({"deploy_format": 2, "presets": ["x"]}, "deploy_format is 2, not 1"),
        ({"deploy_format": 1}, "names no preset and no template"),
        ({"deploy_format": 1, "presets": ["a", "a"]}, "presets repeats an entry"),
        ({"deploy_format": 1, "presets": [3]}, "presets is not a list of names"),
        ({"deploy_format": 1, "templates": ["../../etc"]}, "is not a path inside the repository"),
        ({"deploy_format": 1, "templates": ["/etc/passwd"]}, "is not a path inside the repository"),
        ({"deploy_format": 1, "templates": ["harness/nope.json"]}, "does not exist"),
        ({"deploy_format": 1, "presets": ["x"], "at_least": {"k": 0}}, "at_least is not"),
        ({"deploy_format": 1, "presets": ["x"], "at_least": {"k": True}}, "at_least is not"),
        ([], "is not a JSON object"),
        ({**_ZONED, "sources": {}}, "sources is not a list of entries"),
        ({**_ZONED, "sources": [{"preset": "x", "source": "a.csv"}]}, "sources[0] is not {"),
        ({**_ZONED, "sources": [{**_ZONE, "zone": "UTC"}]}, "sources[0] is not {"),
        ({**_ZONED, "sources": [{**_ZONE, "source": ""}]}, "has a value that is not a name"),
        ({**_ZONED, "sources": [{**_ZONE, "preset": "y"}]}, "names preset y, which is not"),
        ({**_ZONED, "sources": [{**_ZONE, "source": "../a.csv"}]}, "is not a plain corpus path"),
        ({**_ZONED, "sources": [{**_ZONE, "source": "/a.csv"}]}, "is not a plain corpus path"),
        ({**_ZONED, "sources": [{**_ZONE, "source": "a\\b.csv"}]}, "is not a plain corpus path"),
        ({**_ZONED, "sources": [{**_ZONE, "civil_time_zone": "+01:00"}]}, "not spelled as an"),
        ({**_ZONED, "sources": [_ZONE, {**_ZONE, "civil_time_zone": "UTC"}]}, "source twice"),
    ],
)
def test_a_malformed_deploy_declaration_is_refused_whole(
    tmp_path: Path, document: Any, problem: str
) -> None:
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    plan, problems = read_deploy(path)
    assert plan is None and any(problem in p for p in problems), problems


def test_an_unreadable_deploy_declaration_is_a_problem_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "deploy.json"
    path.write_bytes(b"\xff{")
    assert read_deploy(path) == (
        None,
        ["the deploy declaration cannot be read (UnicodeDecodeError)"],
    )
    assert read_deploy(tmp_path / "absent.json")[1] == [
        "the deploy declaration cannot be read (FileNotFoundError)"
    ]


def test_the_assertion_selector_matches_the_declared_id_and_locates_the_entry(
    tmp_path: Path,
) -> None:
    root = tmp_path / "package"
    (root / "records").mkdir(parents=True)
    source = "sha256:" + "1" * 64

    def assertion(ident: str, value: str, at: int) -> dict[str, Any]:
        evidence = {"locator": [{"kind": "json_pointer", "pointer": f"/assertions/{at}"}]}
        return {
            "id": ident,
            "identifier": {"knowledge": "known", "value": {"namespace": "ops", "value": value}},
            "provenance": {"evidence": {**evidence, "source": source}},
        }

    (root / "records" / "source_revision.jsonl").write_text(
        json.dumps({"content_id": source, "id": "r", "location": {"path": "a.json"}}) + "\n"
    )
    (root / "records" / "assertion.jsonl").write_text(
        "".join(
            json.dumps(assertion(i, v, n)) + "\n"
            for n, (i, v) in enumerate([("x", "A1"), ("y", "A2")])
        )
    )
    package = resolve.Package(root)
    select = {"kind": "assertion", "path": "a.json", "id": {"namespace": "ops", "value": "A2"}}
    found = resolve.resolve_one(package, select)
    assert found["records"] == ["y"]
    assert found["citations"] == [
        {"locator": {"pointer": "/assertions/1"}, "path": "a.json", "record": "y"}
    ]
    assert resolve.supports(found, {"path": "a.json", "locator": {"pointer": "/assertions/1"}})
    assert not resolve.supports(found, {"path": "a.json", "locator": {"pointer": "/assertions/0"}})
    other = {**select, "id": {"namespace": "ops", "value": "A3"}}
    assert resolve.resolve_one(package, other)["records"] == []
    with pytest.raises(resolve.GoldError, match="needs id"):
        resolve.resolve_one(package, {"kind": "assertion", "path": "a.json"})


def test_the_declaration_selector_cites_a_zone_the_deploy_declaration_states(
    tmp_path: Path,
) -> None:
    """A source that states no zone is read in the zone its mapping's declaration states (root
    ADR 0061 §3); the gold cites that declaration by preset, path and field (Platform ADR 0009)."""
    root = tmp_path / "package"
    (root / "records").mkdir(parents=True)
    (root / "records" / "source_revision.jsonl").write_text(
        json.dumps({"content_id": "sha256:" + "2" * 64, "id": "r", "location": {"path": "a.csv"}})
        + "\n"
    )
    other = {"preset": "p", "source": "b.csv", "civil_time_zone": "UTC"}
    entry = {"preset": "p", "source": "a.csv", "civil_time_zone": "Europe/Berlin"}
    package = resolve.Package(root, declaration={"sources": [other, entry, "junk"]})
    select = {"kind": "declaration", "path": "a.csv", "preset": "p", "field": "civil_time_zone"}
    found = resolve.resolve_one(package, {**select, "equals": "Europe/Berlin"})
    (record,) = found["records"]
    assert record.startswith("declaration:sha256:") and len(record) == 19 + 64
    locator = {
        "declaration": "harness/acceptance/deploy.json",
        "pointer": "/sources/1/civil_time_zone",
    }
    assert found["citations"] == [{"locator": locator, "path": "a.csv", "record": record}]
    assert resolve.supports(found, {"record": record})
    assert resolve.supports(found, {"path": "a.csv", "locator": locator})
    assert not resolve.supports(found, {"path": "a.csv", "locator": {"pointer": "/sources/1"}})
    # Another value, another preset, or a source the package does not hold resolve to nothing.
    assert resolve.resolve_one(package, {**select, "equals": "UTC"})["records"] == []
    assert resolve.resolve_one(package, {**select, "preset": "q"})["records"] == []
    assert (
        "no source at b.csv" in resolve.resolve_one(package, {**select, "path": "b.csv"})["problem"]
    )
    with pytest.raises(resolve.GoldError, match="needs preset, field"):
        resolve.resolve_one(package, {"kind": "declaration", "path": "a.csv"})
    # The id is the entry's content: the same entry elsewhere in the list keeps it, and the
    # locator names the declaration the package was given.
    moved = resolve.Package(root, declaration={"sources": [entry]}, declaration_path="d.json")
    (citation,) = resolve.resolve_one(moved, select)["citations"]
    assert citation["record"] == record and citation["locator"]["declaration"] == "d.json"


def test_the_pin_selector_cites_a_stated_binding_by_its_pin(tmp_path: Path) -> None:
    """A run sheet's pin (root ADR 0072 §4) by manifest, run file and snapshot file (ADR 0009)."""
    root = tmp_path / "package"
    (root / "records").mkdir(parents=True)
    content = {name: "sha256:" + str(i) * 64 for i, name in enumerate(("m", "r", "c", "o"), 1)}
    paths = {"m": "neptune.yaml", "r": "run.mcap", "c": "cal.yaml", "o": "other.yaml"}

    def write(kind: str, *records: dict[str, Any]) -> None:
        lines = "".join(json.dumps(r) + "\n" for r in records)
        (root / "records" / f"{kind}.jsonl").write_text(lines)

    def cited(name: str, kind: str = "stated", pointer: str = "") -> dict[str, Any]:
        locator = [{"kind": "json_pointer", "pointer": pointer}] if pointer else []
        evidence = {"locator": locator, "source": content[name]}
        return {"assertion_kind": kind, "evidence": evidence}

    write(
        "source_revision",
        *({"content_id": content[k], "id": k, "location": {"path": v}} for k, v in paths.items()),
    )
    write("run", {"id": "run", "provenance": cited("r")})
    write("calibration", {"id": "cal", "provenance": cited("c")})
    write("configuration_snapshot", {"id": "oth", "provenance": cited("o")})
    pin = "/runs/0/snapshots/0"
    write(
        "snapshot_binding",
        {"id": "b1", "provenance": cited("m", pointer=pin), "run": "run", "snapshot": "cal"},
        {"id": "b2", "provenance": cited("m", "inferred", pin), "run": "run", "snapshot": "cal"},
        {"id": "b3", "provenance": cited("m", pointer=pin), "run": "run", "snapshot": "oth"},
    )
    package = resolve.Package(root)
    select = {"kind": "pin", "path": "neptune.yaml", "run": "run.mcap", "snapshot": "cal.yaml"}
    found = resolve.resolve_one(package, select)
    assert found["records"] == ["b1"]  # stated only, and only this snapshot
    assert found["citations"] == [
        {"locator": {"pointer": pin}, "path": "neptune.yaml", "record": "b1"}
    ]
    assert resolve.resolve_one(package, {**select, "snapshot": "nowhere.yaml"})["records"] == []
    with pytest.raises(resolve.GoldError, match="needs run, snapshot"):
        resolve.resolve_one(package, {"kind": "pin", "path": "neptune.yaml"})
