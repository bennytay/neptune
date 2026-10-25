"""Insights (#203) on the generated Plant 7 site: the insight gold questions, the log append, the
analysis tables and the shift report's text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from neptune_memory.site import corpus, insights, log, query, views
from neptune_memory.site.__main__ import main
from neptune_memory.site.build import build
from neptune_memory.site.facts import Ref
from neptune_memory.site.render import render

GOLD = json.loads((Path(__file__).parent / "gold_insights.json").read_text(encoding="utf-8"))
QUESTIONS = {q["id"]: q for q in GOLD["questions"]}
HARBOR = json.loads(
    (Path(__file__).parent / "harbor_gold_insights.json").read_text(encoding="utf-8")
)
HARBOR_QUESTIONS = {q["id"]: q for q in HARBOR["questions"]}


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("site-insights")
    corpus.generate(out)
    build(out)
    return out


@pytest.fixture(scope="module")
def harbor_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("harbor-insights")
    corpus.generate(out, site="harbor")
    build(out)
    return out


def _matches(item: query.Item, want: dict[str, Any]) -> bool:
    obj = item.object
    text = "" if obj is None else obj.ref if isinstance(obj, Ref) else str(obj.value)
    value = None if obj is None else obj.ref if isinstance(obj, Ref) else obj.value
    checks = {"subject": item.subject, "predicate": item.predicate, "kind": item.kind}
    if any(checks[k] != want[k] for k in checks if k in want):
        return False
    if "object" in want and value != want["object"]:
        return False
    if "rule" in want and not text.startswith(want["rule"] + ":"):
        return False
    return all(ref in text for ref in want.get("mentions", ()))


def _ask_gold(site_dir: Path, question: dict[str, Any]) -> None:
    qid = question["id"]
    model = views.load(site_dir)
    answers = [getattr(insights, c["tool"])(model, **c["args"]) for c in question["calls"]]
    items = [item for answer in answers for item in answer.items]
    missing = [w for w in question["expect"] if not any(_matches(i, w) for i in items)]
    assert not missing, f"{qid} lacks {missing}"
    cited = {c.file for answer in answers for c in answer.sources.values()}
    assert set(question["cites"]) <= cited, f"{qid} cites {sorted(cited)}"


@pytest.mark.parametrize("qid", sorted(QUESTIONS))
def test_site_insights_gold_question(site_dir: Path, qid: str) -> None:
    _ask_gold(site_dir, QUESTIONS[qid])


@pytest.mark.parametrize("qid", sorted(HARBOR_QUESTIONS))
def test_site_insights_harbor_gold_question(harbor_dir: Path, qid: str) -> None:
    """The same rules, no Harbor ids in the code: the sorter trip's chain and Reyes's break."""
    _ask_gold(harbor_dir, HARBOR_QUESTIONS[qid])


def test_site_insights_findings_are_inferred_and_never_say_cause(site_dir: Path) -> None:
    found = insights.findings(views.load(site_dir))
    assert len(found) == 5
    for fact in found:
        assert (fact.kind, fact.source, fact.model) == ("inferred", "neptune.insights", None)
        assert fact.citations
        assert "cause" not in str(getattr(fact.object, "value", ""))


def test_site_insights_cli_appends_once(site_dir: Path, tmp_path: Path) -> None:
    site = tmp_path / "site"
    corpus.generate(site)
    build(site)
    before = log.last_seq(site)
    assert main(["insights", str(site)]) == 0
    after = log.last_seq(site)
    assert after == before + 5
    assert main(["insights", str(site)]) == 0
    assert log.last_seq(site) == after
    logged = [e.fact for e in log.read(site) if e.fact and e.fact.source == "neptune.insights"]
    assert [f.id for f in logged] == [f.id for f in insights.findings(views.load(site))]


def test_site_insights_analysis_tables(site_dir: Path) -> None:
    model = views.load(site_dir)
    down = query.sql(model, "SELECT entity, state, cause, fact_id FROM downtime ORDER BY start")
    assert down.rows[0][:3] == ("machine:cv-2", "stopped", "belt tracking off")
    assert down.items and down.items[0].subject == "machine:cv-2"
    clear = query.sql(model, "SELECT entity, fault, seconds FROM fault_clear_time ORDER BY start")
    assert clear.rows == [
        ("machine:cv-2", "fault:JAM_PE3", 2158.0),
        ("robot:arm-c3", "fault:PSTOP_C3", 1360.0),
    ]
    use = query.sql(model, "SELECT seconds FROM utilisation WHERE robot = 'robot:arm-c3'")
    assert use.rows and all(r[0] > 0 for r in use.rows)


def test_site_insights_shift_report_reads_in_sections(site_dir: Path) -> None:
    text = render(insights.shift_report(views.load(site_dir), shift_id="A"))
    headings = [line for line in text.splitlines() if line.endswith(":") and line[0].isupper()]
    assert headings[0].startswith("On shift (shift:A 2026-10-20T06:00:00")
    assert [h.split(" (")[0].rstrip(":") for h in headings[1:6]] == [
        "What happened",
        "Chains",
        "Gaps and checks",
        "Stale sources",
        "Changes before faults",
    ]
    assert "Sources:" in text
    assert "! the log ends at 2026-10-20T11:30:00" in text
