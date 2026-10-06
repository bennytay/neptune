"""The acceptance corpus through the harness: one real ingest, partial success, the storyline's
evidence in the package, every gold evidence item resolved (Platform ADR 0007), and the Deploy map
of the package registered beside it (ADR 0008)."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
from harness import acceptance
from harness.acceptance import generate, resolve
from harness.run import run
from harness.stages import read_deploy

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CASE: Final = f"acceptance-{acceptance.VERSION}"
# The findings the corpus is built to produce. More may appear as the compiler grows; these may
# not disappear without a new corpus version and a reason in the PR.
EXPECTED_FINDINGS: Final = {
    "mcap.truncated": 1,  # LEG-01's patrol bag ends inside a chunk
    "mcap.chunk_truncated": 1,
    "neptune.validate.source_incomplete": 1,  # ...and validation says what that source lost
}
EXPECTED_AT_LEAST: Final = (
    "neptune.clocks.latency_unbounded",  # the cell PC and controller clocks, related by inference
    "neptune.bindings.snapshot_unresolved",  # explicit gaps: runs no record binds
    "geojson.crs_legacy",  # the zone maps state a CRS the legacy way
)


@pytest.fixture(scope="module")
def harness_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], Path]:
    run_dir = tmp_path_factory.mktemp("acceptance") / "run"
    report, code = run(run_dir, owner_tests=False)
    assert code == 0, json.dumps(report, indent=1)[:4000]
    return report, run_dir / "work" / "packages" / CASE


@pytest.fixture(scope="module")
def package(harness_run: tuple[dict[str, Any], Path]) -> resolve.Package:
    return resolve.Package(harness_run[1])


@pytest.fixture(scope="module")
def gold() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(acceptance.GOLD.read_text(encoding="utf-8"))
    return document


def test_the_harness_default_is_the_acceptance_corpus_and_it_is_green(
    harness_run: tuple[dict[str, Any], Path],
) -> None:
    report, _ = harness_run
    assert report["ok"] is True
    lock = acceptance.read_lock()
    assert report["corpus"] == {
        "cases": [CASE],
        "locked": True,
        "name": f"acceptance {acceptance.VERSION}",
        "problems": [],
        "tree": lock["tree"],
        "version": acceptance.VERSION,
    }
    assert report["smoke"]["query"] == gold_question("Q1")


def gold_question(qid: str) -> str:
    document = json.loads(acceptance.GOLD.read_text(encoding="utf-8"))
    return str(next(q["question"] for q in document["questions"] if q["id"] == qid))


def _stage(report: dict[str, Any], stage: str) -> dict[str, Any]:
    return next(entry for entry in report["stages"] if entry["stage"] == stage)


def test_the_deploy_stage_maps_every_declaration_and_the_ledger_registers_both(
    harness_run: tuple[dict[str, Any], Path],
) -> None:
    report, package_root = harness_run
    deploy = _stage(report, "deploy")
    assert (deploy["mode"], deploy["status"], deploy["problems"]) == ("real", "ok", [])
    (row,) = deploy["output"]["cases"]
    plan, _ = read_deploy(acceptance.DEPLOY)
    assert plan is not None
    assert row["state"] == "committed" and row["package_verified"]
    assert row["manifest_valid"] and row["receipt_valid"]
    assert set(row["by_declaration"]) == {f"preset:{name}" for name in plan.presets}
    assert all(count > 0 for count in row["by_declaration"].values())
    assert all(row["records"].get(kind, 0) >= n for kind, n in plan.at_least.items())
    # What no mapping reads (the PDFs' tables, the syslog and downtime exports, the calibration
    # log) is a finding in the mapped package's receipt, and the stage is still green.
    assert row["findings"]["deploy_lifecycle_map.table_unmapped"] >= 3
    ledger = _stage(report, "ledger")["output"]["cases"]
    assert [(r["case"], r["stage"]) for r in ledger] == [
        (CASE, "compiler"),
        (f"{CASE}.deploy", "deploy"),
    ]
    assert all(r["registration"] == "registered" and r["verify"] == "intact" for r in ledger)
    mapped = resolve.Package(package_root.parent / f"{CASE}.deploy")
    envelopes = {
        ident["value"]["value"]
        for record in mapped.kind("authorisation_envelope")
        for ident in record["identifiers"]
    }
    assert {"ENV-P2-01", "ENV-P2-02", "ENV-P2-03", "ENV-S007-03", "ENV-S007-04"} == envelopes


def test_the_package_holds_both_stops_32_s_apart_and_the_assertion_joining_them(
    package: resolve.Package, gold: dict[str, Any]
) -> None:
    resolved = resolve.resolve(package.root, gold)
    rows = {r["id"]: r for r in package.kind("structured_record")}

    def cells(key: str) -> list[str]:
        (record,) = resolved[key]["records"]
        return [str(c.get("value")) for c in rows[record]["cells"]]

    cmms = datetime.fromisoformat(cells("cmms.stop.DT-26-0914-01")[5])
    pstop = datetime.fromisoformat(cells("syslog.pstop")[1])
    assert (cmms - pstop).total_seconds() == 32
    (record,) = resolved["assert.same-stop"]["records"]
    (assertion,) = [a for a in package.kind("assertion") if a["id"] == record]
    assert assertion["provenance"]["assertion_kind"] == "stated"
    assert [s["value"] for s in assertion["scope"]["value"]] == ["DT-26-0914-01", "4182"]


def test_the_calibrations_land_as_configuration_until_the_compiler_reads_easy_handeye(
    package: resolve.Package,
) -> None:
    """MVL-207 adds the format; until then each file is a configuration snapshot whose values the
    gold answers cite by pointer, and only the vision PC's OpenCV export is a calibration."""
    for ident in ("CAL-ARM3A-0818", "CAL-ARM3A-0911"):
        content = package.content(f"sites/PLANT-2/cell3/calibration/{ident}.yaml")
        values = [
            v["path"]
            for v in package.kind("configuration_value")
            if v["provenance"]["evidence"]["source"] == content
        ]
        assert ["transformation", "z"] in values
    assert len(package.kind("calibration")) == 1


def test_one_corrupt_bag_is_findings_not_a_failed_job(
    harness_run: tuple[dict[str, Any], Path],
) -> None:
    (row,) = harness_run[0]["stages"][0]["output"]["cases"]
    assert row["state"] == "committed" and row["package_verified"]
    assert row["manifest_valid"] and row["receipt_valid"]
    assert row["sources"] == len(acceptance.read_lock()["files"]) - 1  # the duplicate is one source
    findings = row["findings"]
    for code, count in EXPECTED_FINDINGS.items():
        assert findings.get(code) == count, code
    for code in EXPECTED_AT_LEAST:
        assert findings.get(code, 0) >= 1, code


def test_the_only_error_is_the_truncated_bag(package: resolve.Package) -> None:
    errors = [f for f in package.kind("ingest_finding") if f["severity"] == "error"]
    assert [f["code"] for f in errors] == ["mcap.truncated"]
    bag = "sites/PLANT-2/legged/runs/patrol_2026-09-14/patrol_2026-09-14_0.mcap"
    assert errors[0]["subject"]["ref"]["source"] == package.content(bag)


def test_every_gold_evidence_item_resolves(
    harness_run: tuple[dict[str, Any], Path], package: resolve.Package, gold: dict[str, Any]
) -> None:
    (row,) = harness_run[0]["stages"][0]["output"]["cases"]
    assert row["gold"]["missing"] == [] and row["gold"]["problems"] == []
    assert row["gold"]["resolved"] == row["gold"]["evidence"] == len(gold["evidence"])
    resolved = resolve.resolve(package.root, gold)
    assert all(item["records"] for item in resolved.values())
    assert resolve.resolve(package.root, gold) == resolved  # deterministic


def test_the_clocks_disagree_by_the_cell_pcs_offset(
    package: resolve.Package, gold: dict[str, Any]
) -> None:
    resolved = resolve.resolve(package.root, gold)
    ahead = generate.IPC_AHEAD_2026_09_14
    estop = resolved["bag0914.estop"]["rows"]
    assert len(estop) == 1
    hmi = generate.local_ns(2026, 9, 14, 14, 32, 41)  # the incident report's E-stop time
    assert estop[0]["time/0"] == hmi + ahead  # the bag's log time is the cell PC's
    collision = resolved["bag0914.collision"]["rows"][0]["time/0"]
    assert collision == generate.local_ns(2026, 9, 14, 14, 32, 38) + 20 * generate.MS + ahead
    mappings = package.kind("clock_mapping", derived=True)
    by_id = {m["id"]: m for m in mappings}
    (first, *_) = resolved["clock.0914"]["records"]
    anchor = by_id[first]["anchor"]["value"]
    assert anchor["target"]["ticks"] - anchor["source"]["ticks"] == ahead
    assert by_id[first]["residual_bound"] == {"knowledge": "unknown"}


def test_the_duplicate_run_is_one_content_under_two_names(package: resolve.Package) -> None:
    original = "sites/PLANT-2/cell3/bags/pallet_2026-09-09/pallet_2026-09-09_0.mcap"
    copy = "sites/PLANT-2/cell3/shared/for_vendor/cell3_reference_run.mcap"
    assert package.content(original) == package.content(copy) is not None
    content = package.content(original)
    runs = [r for r in package.kind("run") if r["provenance"]["evidence"]["source"] == content]
    assert len(runs) == 1


def test_absence_selectors_refuse_a_key_that_is_there(package: resolve.Package) -> None:
    changes = "sites/PLANT-2/changes/servicenow_changes.csv"
    there = resolve.resolve_one(
        package, {"kind": "no_table_row", "path": changes, "key": "CHG0030013"}
    )
    assert there["records"] == []
    absent = resolve.resolve_one(package, {"kind": "no_table_row", "path": changes, "key": "x"})
    assert absent["records"]


def test_the_prompt_injection_is_held_as_text_only(package: resolve.Package) -> None:
    bulletin = "sites/PLANT-2/vendor/SB-2026-117_PG-80_finger_sets.md"
    content = package.content(bulletin)
    blocks = [
        b
        for b in package.kind("document_block")
        if b["provenance"]["evidence"]["source"] == content
    ]
    assert any("ignore all previous instructions" in b["text"]["value"] for b in blocks)
    kinds = Counter(b["provenance"]["assertion_kind"] for b in blocks)
    assert set(kinds) == {
        "observed"
    }  # a document's text is observed text, never a statement of fact


def test_every_item_lists_citations_that_support_it(
    package: resolve.Package, gold: dict[str, Any]
) -> None:
    resolved = resolve.resolve(package.root, gold)
    for key, item in resolved.items():
        assert item["citations"], key
        for citation in item["citations"]:
            assert citation["path"] == gold["evidence"][key]["select"]["path"]
            assert resolve.supports(item, citation), key
    # One /diagnostics stream holds the warnings, the collision and the E-stop: citing it alone
    # supports none of them, and the collision's row does not support the E-stop.
    collision, estop = resolved["bag0914.collision"], resolved["bag0914.estop"]
    assert collision["records"] == estop["records"]
    stream = {"record": collision["records"][0]}
    assert not resolve.supports(collision, stream) and not resolve.supports(estop, stream)
    assert not resolve.supports(estop, collision["citations"][0])
    # The base package's locators are what Deploy D3 matches its own records on.
    wo = resolved["cmms.WO-26-0911"]["citations"][0]
    assert wo["locator"] == {"row": 6}
    page = resolved["inc.timeline.estop"]["citations"][0]["locator"]
    assert page == {"page": 1}
    log_time = generate.local_ns(2026, 9, 14, 14, 32, 41) + generate.IPC_AHEAD_2026_09_14
    assert estop["citations"][0]["locator"] == {"log_time": log_time, "topic": "/diagnostics"}
