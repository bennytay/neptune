"""Onboarding (#225): a folder and a goal become a cited memory with a mission, step by step as an
agent drives it, and the same folder and answers give the same memory."""

from pathlib import Path

from neptune_memory.site import onboard
from neptune_memory.site.log import log_dir, read

EXPORT = """\
Timestamp,Robot,Mode,Protective_Stop,Temperature_J1,Current_J1,Cycle
2026-03-02T08:00:00,arm-1,running,False,31.0,1.2,1
2026-03-02T08:00:01,arm-1,running,False,31.1,1.3,1
2026-03-02T08:00:02,arm-1,running,True,31.4,2.4,1
2026-03-02T08:00:03,arm-1,stopped,True,31.4,0.0,1
2026-03-02T08:00:04,arm-1,running,False,31.2,1.1,2
"""
NOTES = "# Operator notes\n\nTwo protective stops this morning, both near the end of a cycle.\n"


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "robot-data"
    (folder / "controller").mkdir(parents=True)
    (folder / "controller" / "arm1_export.csv").write_text(EXPORT, encoding="utf-8")
    (folder / "notes.md").write_text(NOTES, encoding="utf-8")
    (folder / ".DS_Store").write_text("x", encoding="utf-8")
    return folder


MAPPING = """\
mappings: 1
tables:
  - file: files/controller/arm1_export.csv
    source: files
    kind: observed
    changes: true
    subject: "robot:{Robot}"
    time: Timestamp
    facts:
      - {predicate: mode, column: Mode}
      - {predicate: health, column: Protective_Stop,
         values: {"False": ok, "True": "fault:protective_stop"}}
      - {predicate: cycle, column: Cycle}
"""


def _onboard(folder: Path, site: Path) -> str:
    first = onboard.start(site, folder)
    assert "Files: 2, 0 mapped" in first  # everything is in memory before any mapping
    listing = onboard.inventory(site)
    assert "files/controller/arm1_export.csv: table" in listing
    assert '"Protective_Stop"' in listing and ".DS_Store" not in listing

    drafted = onboard.draft(site, "files/controller/arm1_export.csv")
    assert 'file: "files/controller/arm1_export.csv"' in drafted
    assert "what unit is 'Temperature_J1' in?" in drafted
    assert "what is column 'Current_J1'" in drafted
    onboard.ask(site, [q.removeprefix("- ") for q in drafted.split("\n") if "unit is" in q])

    onboard.save(site, "mappings", MAPPING)
    onboard.mission(
        site,
        "Halve protective stops on arm-1 without slowing its cycles. Don't touch the robot.",
        [
            {
                "name": "Fewer protective stops",
                "metric": "protective stops per hour",
                "target": "<= half of the first week's rate",
            }
        ],
        ["Cycle time may not grow", "Never change a safety function"],
    )
    return onboard.build(site)


def test_site_onboard_folder_and_goal_become_a_cited_memory(tmp_path: Path) -> None:
    folder, site = _folder(tmp_path), tmp_path / "site"
    final = _onboard(folder, site)

    assert (folder / "notes.md").read_text(encoding="utf-8") == NOTES  # originals untouched
    assert "Mission: goal:fewer-protective-stops" in final
    assert "Open questions: 1" in final and "Temperature_J1" in final
    facts = [e.fact for e in read(site) if e.fact is not None]
    health = [f for f in facts if f.subject == "robot:arm-1" and f.predicate == "health"]
    health.sort(key=lambda f: f.valid_from or "")
    assert [f.object.value for f in health] == ["ok", "fault:protective_stop", "ok"]  # type: ignore[union-attr]
    stop = health[1]
    assert stop.kind == "observed"
    assert stop.citations[0].file == "static/files/controller/arm1_export.csv"
    assert stop.citations[0].locator == 'row 4 col "Protective_Stop"'
    assert stop.citations[0].quote == "True"
    goal = next(f for f in facts if f.subject == "goal:fewer-protective-stops")
    assert goal.kind == "stated" and goal.citations[0].file == "static/mission.yaml"
    assert any(f.citations[0].file == "static/goal.md" for f in facts)  # the prompt, cited
    temperatures = [f for f in facts if f.predicate == "temperature"]
    assert temperatures == []  # not mapped while its unit is an open question: unknown, not guessed


def test_site_onboard_same_folder_and_answers_same_memory(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    logs = []
    for name in ("a", "b"):
        site = tmp_path / name
        _onboard(folder, site)
        logs.append({p.name: p.read_bytes() for p in sorted(log_dir(site).iterdir())})
    assert logs[0] == logs[1]
