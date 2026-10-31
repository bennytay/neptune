"""Research (#226): hypotheses tested against the memory by Neptune, recorded, and kept by a
rebuild. No model is called: ``FakeClaude`` answers."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fake_claude import FakeClaude, fact_id

from neptune_memory.site import improve, onboard, research
from neptune_memory.site.log import log_dir, read

MAPPING = """\
mappings: 1
tables:
  - file: files/arm1_export.csv
    source: files
    kind: observed
    changes: true
    subject: "robot:{Robot}"
    time: Timestamp
    facts:
      - {predicate: health, column: Protective_Stop,
         values: {"False": ok, "True": "fault:protective_stop"}}
      - {predicate: cycle, column: Cycle}
"""


def _export() -> str:
    """20 minutes at 1 Hz: cycles 1-4 of 30 s each, in turn; a protective stop in most cycle-3
    passes and in one cycle-1 pass, spread evenly over the 20 minutes."""
    start = datetime(2026, 3, 2, 8, 0, 0)
    lines = ["Timestamp,Robot,Protective_Stop,Cycle"]
    for i in range(1200):
        cycle = (i // 30) % 4 + 1
        passes = i // 120
        stop = (cycle == 3 and i % 30 in (10, 11) and passes % 5 != 4) or (i in (5, 6))
        t = (start + timedelta(seconds=i)).isoformat()
        lines.append(f"{t},arm-1,{stop},{cycle}")
    return "\n".join(lines) + "\n"


def _site(tmp_path: Path) -> Path:
    folder = tmp_path / "robot"
    folder.mkdir()
    (folder / "arm1_export.csv").write_text(_export(), encoding="utf-8")
    site = tmp_path / "site"
    onboard.start(site, folder)
    onboard.save(site, "mappings", MAPPING)
    onboard.mission(
        site,
        "Halve protective stops on arm-1.",
        [{"name": "Fewer stops", "metric": "protective stops per hour", "target": "-50 %"}],
    )
    onboard.build(site)
    return site


STOP = {"subject": "robot:arm-1", "predicate": "health", "value": "fault:protective_stop"}
NONE = {"subject": "", "predicate": "", "op": "", "value": ""}


def _hypotheses(text: str) -> dict[str, Any]:
    evidence = [fact_id(text, "fault:protective_stop")]
    return {
        "summary": "Stops come in bursts.",
        "hypotheses": [
            {
                "key": "stops-in-cycle-3",
                "statement": "Protective stops happen more during program cycle 3",
                "why": "the first stops were mid-program",
                "evidence": evidence,
                "goals": ["goal:fewer-stops"],
                "test": {
                    "type": "rate_when",
                    "event": STOP,
                    "condition": {
                        "subject": "robot:arm-1",
                        "predicate": "cycle",
                        "op": "in",
                        "value": "3",
                    },
                    "at": "",
                    "expect": "more",
                },
                "next_step": "Slow the moves of cycle 3 and compare.",
                "unknowns": [],
            },
            {
                "key": "stops-rising",
                "statement": "Protective stops became more frequent after 08:10",
                "why": "the operator said the afternoon felt worse",
                "evidence": evidence,
                "goals": ["goal:fewer-stops", "goal:not-in-memory"],
                "test": {
                    "type": "before_after",
                    "event": STOP,
                    "condition": NONE,
                    "at": "2026-03-02T08:10:00",
                    "expect": "more",
                },
                "next_step": "none",
                "unknowns": [],
            },
            {
                "key": "stops-when-hot",
                "statement": "Protective stops happen more when joint 1 runs hot",
                "why": "the manual links load and stops",
                "evidence": [*evidence, "fact:not-in-memory"],
                "goals": [],
                "test": {
                    "type": "rate_when",
                    "event": STOP,
                    "condition": {
                        "subject": "component:arm-1-j1",
                        "predicate": "temperature",
                        "op": ">",
                        "value": "40",
                    },
                    "at": "",
                    "expect": "more",
                },
                "next_step": "Log joint temperatures with a unit.",
                "unknowns": ["joint temperature is not in the memory"],
            },
        ],
    }


def _results(site: Path) -> dict[str, str]:
    return {
        e.fact.subject: str(e.fact.object.value)  # type: ignore[union-attr]
        for e in read(site)
        if e.fact is not None and e.fact.predicate == research.PREDICATE
    }


def test_site_research_tests_hypotheses_against_the_memory(tmp_path: Path) -> None:
    site = _site(tmp_path)
    claude = FakeClaude(_hypotheses)
    facts = research.run(site, claude)

    assert claude.calls[0]["system"] == research.SYSTEM
    verdicts = {f.subject: str(f.object.value).split(":")[0] for f in facts}  # type: ignore[union-attr]
    assert verdicts == {
        "hypothesis:stops-in-cycle-3": "supported",
        "hypothesis:stops-rising": "rejected",
        "hypothesis:stops-when-hot": "inconclusive",
    }
    supported = next(f for f in facts if f.subject == "hypothesis:stops-in-cycle-3")
    assert supported.kind == "inferred" and supported.source == research.SOURCE
    assert supported.citations[0].file == research.RUNS
    assert supported.citations[1].file == "static/files/arm1_export.csv"  # an event it counted
    assert "while robot:arm-1 cycle in 3" in str(supported.object.value)  # type: ignore[union-attr]
    hot = next(f for f in facts if f.subject == "hypothesis:stops-when-hot")
    assert "not in the memory" in str(hot.object.value)  # type: ignore[union-attr]

    line = research.rounds(site)[0].data
    assert line["usage"] == {**dict.fromkeys(improve.USAGE, 0), "input_tokens": 1000}
    assert "stops-rising: goal goal:not-in-memory" not in line["dropped"]
    assert any("fact:not-in-memory" in d for d in line["dropped"])
    rising = next(h for h in line["hypotheses"] if h["key"] == "stops-rising")
    assert rising["goals"] == ["goal:fewer-stops"]
    assert rising["result"]["events_before"] > 0 and rising["result"]["events_after"] > 0

    # The next round reads what was learned.
    seen: list[str] = []

    def reread(text: str) -> dict[str, Any]:
        seen.append(text)
        return {"summary": "", "hypotheses": []}

    research.run(site, FakeClaude(reread))
    assert (
        "stops-in-cycle-3: Protective stops happen more during program cycle 3 -> supported"
        in seen[0]
    )


def test_site_research_results_survive_a_rebuild(tmp_path: Path) -> None:
    site = _site(tmp_path)
    research.run(site, FakeClaude(_hypotheses))
    learned = _results(site)
    logs = []
    for _ in range(2):
        onboard.build(site)
        assert _results(site) == learned  # kept without calling a model
        logs.append({p.name: p.read_bytes() for p in sorted(log_dir(site).iterdir())})
    assert logs[0] == logs[1]


def test_site_research_results_for_people_and_agents(tmp_path: Path) -> None:
    from neptune_memory.site import views, web

    site = _site(tmp_path)
    onboard.mission(
        site,
        "Halve protective stops on arm-1. Never touch a safety function.",
        [{"name": "Fewer stops", "metric": "protective stops per hour", "target": "-50 %"}],
        ["Never change a safety function"],
    )
    onboard.ask(site, ["What unit are joint temperatures in?"])
    onboard.build(site)
    research.run(site, FakeClaude(_hypotheses))
    model = views.load(site)

    view = research.results(site, model)
    assert [c["verdict"] for c in view["cards"]] == ["supported", "inconclusive", "rejected"]
    assert view["goals"] == [
        {
            "id": "goal:fewer-stops",
            "name": "Fewer stops",
            "supported": 1,
            "rejected": 1,
            "inconclusive": 0,
        }
    ]
    gaps = [g["text"] for g in view["gaps"]]
    assert "'component:arm-1-j1' is not in the memory" in gaps
    assert "joint temperature is not in the memory" in gaps
    assert "What unit are joint temperatures in?" in gaps
    supported = view["cards"][0]
    assert supported["evidence"][0]["file"] == "static/files/arm1_export.csv"
    assert len(view["log"]) == 3

    brief = research.brief(site, model, "stops-in-cycle-3")
    for part in (
        "# Research result: Protective stops happen more during program cycle 3",
        "- Fewer stops (goal:fewer-stops): protective stops per hour -50 %",
        "- Never change a safety function",
        "## Finding: supported",
        'static/files/arm1_export.csv row 72 col "Protective_Stop" "True"',
        "Slow the moves of cycle 3 and compare.",
        research.BRIEF_RULE,
    ):
        assert part in brief, part
    assert research.brief_for(site, "hypothesis:stops-in-cycle-3") == brief  # the MCP tool
    page = web.Site(site)
    assert page.research_brief("stops-in-cycle-3")["text"] == brief  # "Copy for agent"
    assert page.dashboard()["research"]["cards"][0]["key"] == "stops-in-cycle-3"
    assert "no research result" in page.research_brief("nope")["error"]
