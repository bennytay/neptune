"""The acceptance corpus without ingesting it: determinism, the lock, sizes, the storyline's
ingredients in the bytes, and the gold document's shape (Platform ADR 0006)."""

import copy
import json
import struct
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from harness import acceptance, corpus
from harness.acceptance import generate, resolve

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
    assert lock["version"] == acceptance.VERSION == gold["corpus_version"]
    assert lock["corpus"] == acceptance.NAME == gold["corpus"]
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
    # S-007's runs and the cell's bag, calibrations and documents are the D1 generator's bytes.
    for path in ("runs/S-007/amr-07_2026-04-02.mcap", "incidents/INC-0007.pdf"):
        assert files[f"sites/S-007/{path.replace('runs/S-007/', 'runs/')}"] == fleet[path]
    for path in ("calibration/CAL-ARM3A-0818.yaml", "documents/sop_CELL-014_finger_set.pdf"):
        assert files[f"sites/PLANT-2/cell3/{path}"] == cell[path]
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
    # Calibration history across the change.
    assert b"reprojection_error_px: 1.86" in files[f"{cell}/calibration/CAL-ARM3A-0911.yaml"]


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
