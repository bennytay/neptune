"""MVL-14 acceptance: a small manifest turns an ambiguous folder into deterministic ingestion.

The folder spans two embodiments: a manipulator cell whose two numbered recordings could be one
recording split or two episodes, beside notes that two adapters claim equally; and a quadruped
whose two recordings start 30 s apart, one session started in steps or two. Ingested as it is,
every one of those is an explicit ambiguity finding. ``neptune init-manifest`` writes the choices
as comments; uncommenting three lines (and declaring the two machines) resolves them all, with no
code, and the package is byte-identical across reruns from fresh workspaces. Editing the manifest
is a new lineage. The installed command, the default sandbox.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.derived.sessions import SessionProposal, Status, read_derived
from neptune.manifest import MANIFEST_ID
from neptune.model.finding import IngestFinding
from neptune.model.provenance import TransformRecord
from neptune.sdk import read_package

pytestmark = pytest.mark.integration

MCAP: Final = Path(__file__).parents[1] / "fixtures" / "mcap"
NEPTUNE: Final = Path(sys.executable).parent / "neptune"
NOTES: Final = (
    b"step,**joint**,[spec](spec.pdf)\n1,**shoulder**,[a](a.pdf)\n2,**elbow**,[b](b.pdf)\n"
)
AMBIGUITIES: Final = {
    "neptune.grouping.ambiguous_member": 1,
    "neptune.grouping.contested": 2,
    "neptune.probe.ambiguous": 1,
}
CHOICES: Final = (
    '  # - {name: "arm/pick_1 +1", paths: ["arm/pick_1.mcap", "arm/pick_2.mcap"]}',
    '  # - {name: "quadruped", paths: ["quadruped"]}',
    '  # - {path: "arm/joint_notes.txt", adapter: markdown}',
)


def build(root: Path) -> Path:
    arm, legs = root / "arm", root / "quadruped"
    arm.mkdir(parents=True)
    legs.mkdir()
    shutil.copy(MCAP / "robot.mcap", arm / "pick_1.mcap")
    shutil.copy(MCAP / "robot_lz4.mcap", arm / "pick_2.mcap")
    (arm / "joint_notes.txt").write_bytes(NOTES)
    shutil.copy(MCAP / "robot_plain.mcap", legs / "trot_2024-05-01_10-00-00.mcap")
    shutil.copy(MCAP / "unchunked.mcap", legs / "trot_2024-05-01_10-00-30.mcap")
    return root


def neptune(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(NEPTUNE), *argv], cwd=cwd, capture_output=True, text=True, timeout=300, check=False
    )


def ingest(cwd: Path, out: str, workspace: str) -> dict[str, Any]:
    done = neptune("ingest", "fold", "--out", out, "-w", workspace, "--json", cwd=cwd)
    assert done.returncode == 0, done.stderr
    result: dict[str, Any] = json.loads(done.stdout.splitlines()[-1])
    return result


def ambiguity(by_code: dict[str, int]) -> dict[str, int]:
    return {code: n for code, n in by_code.items() if code in AMBIGUITIES}


def test_a_generated_and_edited_manifest_resolves_every_ambiguity(tmp_path: Path) -> None:
    build(tmp_path / "fold")
    before = ingest(tmp_path, "before", "ws0")
    assert ambiguity(before["findings"]["by_code"]) == AMBIGUITIES

    made = neptune("init-manifest", "fold", "-w", "ws0", cwd=tmp_path)
    assert made.returncode == 0, made.stderr
    manifest = tmp_path / "fold" / "neptune.yaml"
    generated = manifest.read_text(encoding="utf-8")
    again = neptune("init-manifest", "fold", "-o", "-", "-w", "ws-other", cwd=tmp_path)
    assert again.stdout == generated  # deterministic, from any workspace
    refused = neptune("init-manifest", "fold", "-w", "ws0", cwd=tmp_path)
    assert refused.returncode == 5 and manifest.read_text(encoding="utf-8") == generated

    as_generated = ingest(tmp_path, "as-generated", "ws1")  # declares nothing yet
    assert ambiguity(as_generated["findings"]["by_code"]) == AMBIGUITIES

    edited = generated
    for line in CHOICES:
        assert line in edited
        edited = edited.replace(line, line.replace("  # - ", "  - ", 1))
    edited = edited.replace(
        "machines:\n",
        "machines:\n  - {id: ur5e-cell-3, embodiment: manipulator}\n"
        "  - {id: anymal-c-03, embodiment: legged}\n",
        1,
    )
    manifest.write_text(edited, encoding="utf-8")

    first = ingest(tmp_path, "first", "ws2")
    second = ingest(tmp_path, "second", "ws3")
    assert first["package"] == second["package"]
    assert (tmp_path / "first" / "manifest.json").read_bytes() == (
        tmp_path / "second" / "manifest.json"
    ).read_bytes()
    assert ambiguity(first["findings"]["by_code"]) == {}
    assert first["findings"]["by_code"]["neptune.manifest.adapter_pinned"] == 1

    package = read_package(tmp_path / "first")
    # The manifest transform reads the manifest alone; its run declarations' transform (same id,
    # with the adapters it read upstream, ADR 0072 §6) is another record.
    transforms = {
        r.adapter_id: r
        for r in package.records
        if isinstance(r, TransformRecord) and not (r.adapter_id == MANIFEST_ID and r.upstream)
    }
    stated = transforms[MANIFEST_ID]
    assert stated.config["location"] == {"kind": "local", "path": "neptune.yaml"}
    sources = {s.location.key: s.content_id for s in package.receipt.sources}
    assert stated.config["source"] == sources[("local", "neptune.yaml")]  # the manifest is a source
    declarations: Any = stated.config["declarations"]
    assert [m["embodiment"] for m in declarations["machines"]] == ["manipulator", "legged"]
    # The grouping (ADR 0066) names the manifest that declared its sessions, and the adapters
    # whose records it read as evidence.
    grouping = transforms["neptune.grouping"]
    assert stated.id in grouping.upstream
    assert set(grouping.upstream) - {stated.id} <= {t.id for t in transforms.values()}
    assert "markdown" in transforms  # the notes, read by the adapter the manifest chose

    proposals = [r for r in read_derived(package.derived) if isinstance(r, SessionProposal)]
    declared = sorted(p.declared[0].name for p in proposals if p.declared)
    assert declared == ["arm/pick_1 +1", "quadruped"]
    assert all(p.status is not Status.CONTESTED for p in proposals)
    pinned = [
        f
        for f in package.records
        if isinstance(f, IngestFinding) and f.code == "neptune.manifest.adapter_pinned"
    ]
    assert pinned[0].related[0].source == stated.config["source"]

    manifest.write_text(edited + "# a reviewer's note\n", encoding="utf-8")
    third = ingest(tmp_path, "third", "ws2")
    assert third["package"] != first["package"]  # an edited manifest is a new lineage
    manifest.write_text(edited, encoding="utf-8")
    assert ingest(tmp_path, "fourth", "ws2")["package"] == first["package"]


def test_a_contradicting_declaration_is_a_finding_not_an_override(tmp_path: Path) -> None:
    build(tmp_path / "fold")
    (tmp_path / "fold" / "neptune.yaml").write_text(
        "neptune: 1\n"
        "runs:\n"
        "  - {name: half-a-trot, paths: [quadruped/trot_2024-05-01_10-00-00.mcap,"
        " arm/pick_1.mcap]}\n"
        "sources:\n"
        "  - {path: arm/joint_notes.txt, adapter: image}\n"
        "  - {glob: 'nothing/**', adapter: text}\n",
        encoding="utf-8",
    )
    result = ingest(tmp_path, "out", "ws")
    codes = result["findings"]["by_code"]
    assert codes["neptune.manifest.pin_refused"] == 1  # image's probe declines the notes
    assert codes["neptune.probe.ambiguous"] == 1  # so the tie stands, as observed
    assert codes["neptune.manifest.rule_unmatched"] == 1
    assert codes["neptune.grouping.declared_contradicts_layout"] >= 1


def test_an_unusable_manifest_stops_before_anything_runs(tmp_path: Path) -> None:
    build(tmp_path / "fold")
    manifest = tmp_path / "fold" / "neptune.yaml"
    for text in (
        "neptune: 1\nrobots: []\n",
        "neptune: 1\na: &x [1]\nb: *x\n",
        "neptune: 1\nsources:\n  - {glob: '../**', adapter: text}\n",
        "neptune: 1\nsources:\n  - {path: arm, adapter: ghost}\n",
    ):
        manifest.write_text(text, encoding="utf-8")
        done = neptune("ingest", "fold", "--out", "out", "-w", "ws", "--json", cwd=tmp_path)
        assert done.returncode == 6, (text, done.stdout)
        assert json.loads(done.stdout.splitlines()[-1])["error"]["code"] == "invalid_configuration"
        assert not (tmp_path / "out").exists()
