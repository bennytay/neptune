"""Demo v1 end to end (``make demo``, Platform ADR 0011): the acceptance corpus through every real
stage, Memory's graph equal to its committed snapshot, the gold questions answered through the
MCP tools with every statement cited, the evidence-pack PDFs, and the same bytes on a second run."""

import gzip
import json
from pathlib import Path
from typing import Any, Final

import pytest
from harness import acceptance, demo

pytestmark = [pytest.mark.integration, pytest.mark.slow]

OUTPUTS: Final = (
    "answers.json",
    "answers.md",
    "configuration-traceability-ARM-3A.pdf",
    "demo.md",
    "graph.json",
    "incident-timeline-INC-C3-0011.pdf",
    "report.json",
    "report.md",
)


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("demo") / "demo"
    code, problems = demo.demo(out)
    assert (code, problems) == (0, [])
    return out


def _stage(out: Path, name: str) -> dict[str, Any]:
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    entry: dict[str, Any] = next(s for s in report["stages"] if s["stage"] == name)
    return entry


def test_every_stage_runs_real_and_the_demo_writes_its_outputs(demo_dir: Path) -> None:
    report = json.loads((demo_dir / "report.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    assert {(s["stage"], s["mode"], s["status"]) for s in report["stages"]} == {
        (name, "real", "ok") for name in ("compiler", "deploy", "ledger", "memory", "context")
    }
    for name in OUTPUTS:
        assert (demo_dir / name).is_file(), name
    assert "# Demo v1: green" in (demo_dir / "demo.md").read_text(encoding="utf-8")


def test_the_graph_is_memorys_committed_snapshot_byte_for_byte(demo_dir: Path) -> None:
    memory = _stage(demo_dir, "memory")["output"]
    assert memory["snapshot"] == {
        "path": "packages/neptune-memory/tests/fixtures/acceptance_corpus.graph.json.gz",
        "verdict": "equal",
    }
    assert (demo_dir / "graph.json").read_bytes() == gzip.decompress(
        acceptance.MEMORY_SNAPSHOT.read_bytes()
    )
    assert memory["verify"] == "ok" and memory["head"] == 2
    assert [p["stage"] for p in memory["packages"]] == ["compiler", "deploy"]


def test_the_gold_questions_are_answered_through_the_mcp_tools_as_pinned(demo_dir: Path) -> None:
    context = _stage(demo_dir, "context")["output"]
    pinned = json.loads(acceptance.ANSWERS.read_text(encoding="utf-8"))
    answers = context["answers"]
    assert list(answers) == [q["id"] for q in pinned["questions"]]
    for question in pinned["questions"]:
        row = answers[question["id"]]
        assert row["supported"] == sorted(question["supported"])
        assert row["co_cited"] == sorted(question["co_cited"])
        assert row["gaps"] == sorted(question["gaps"])
        assert row["hydrated"] == "resolved"  # its first cited source resolves via the Ledger
        assert row["statements"] > 0 and row["cited_claims"] > 0
    assert context["stdio"]["same_as_in_process"] is True
    assert len(context["stdio"]["tools"]) == 6


def test_what_changed_since_the_last_good_run_names_no_calibration(demo_dir: Path) -> None:
    """Q2.C3 is co-cited, not supported (ADR 0011 §4): at the skill's budget the answer cites a
    run-configuration claim resting on the run sheet's calibration pin, but the configuration is an
    opaque Ledger thread, and no statement names CAL-ARM3A-0818 or -0911 or WO-26-0911."""
    graph = json.loads((demo_dir / "graph.json").read_bytes())
    claims = {c["id"]: c for c in graph["claims"]}
    pinned = json.loads(acceptance.ANSWERS.read_text(encoding="utf-8"))
    q2 = next(q for q in pinned["questions"] if q["id"] == "Q2")
    ids = q2["co_cited"]["Q2.C3"]["claims"]
    assert {claims[i]["predicate"] for i in ids} == {"configuration_active_during"}
    transcript = json.loads((demo_dir / "answers.json").read_text(encoding="utf-8"))
    text = "".join(c["text"] for c in transcript["questions"][1]["calls"])
    assert all(i in text for i in ids)
    assert "CAL-ARM3A" not in text and "WO-26-0911" not in text


def test_the_pdfs_open_and_name_their_subject(demo_dir: Path) -> None:
    import pypdf

    incident = pypdf.PdfReader(demo_dir / "incident-timeline-INC-C3-0011.pdf")
    text = "".join(page.extract_text() for page in incident.pages)
    assert len(incident.pages) >= 2 and "Incident reconstruction" in text
    assert "Collision detection on joint 5 at pick P1" in text
    trace = pypdf.PdfReader(demo_dir / "configuration-traceability-ARM-3A.pdf")
    text = "".join(page.extract_text() for page in trace.pages)
    assert "Configuration traceability" in text and "manifest:ARM-3A" in text
    spec = json.loads(
        (demo_dir / "packs" / "incident-timeline-INC-C3-0011" / "spec.json").read_text()
    )
    graph = json.loads((demo_dir / "graph.json").read_bytes())
    from neptune_deploy.packs import snapshot_id

    assert spec["snapshot"] == snapshot_id(graph)  # the pack reads the graph just built


def test_a_second_demo_gives_the_same_bytes(demo_dir: Path, tmp_path: Path) -> None:
    code, problems = demo.demo(tmp_path / "again")
    assert (code, problems) == (0, [])
    for name in OUTPUTS:
        assert (tmp_path / "again" / name).read_bytes() == (demo_dir / name).read_bytes(), name
    text = (demo_dir / "report.json").read_text(encoding="utf-8")
    assert str(demo_dir) not in text and str(tmp_path) not in text
