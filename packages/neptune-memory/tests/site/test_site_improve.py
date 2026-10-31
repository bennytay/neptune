"""Improvements: mission -> dossier -> Claude (a fake) -> cited, ranked, re-ranked."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fake_claude import FakeClaude, fact_id

from neptune_memory.site import corpus, improve
from neptune_memory.site.build import build
from neptune_memory.site.facts import Citation, Fact, Value
from neptune_memory.site.log import FactLog
from neptune_memory.site.views import load

UNTIL = "2026-10-20T10:50:00"  # after the jam and into Okafor's break: the qualification gap holds


def item(key: str, evidence: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "key": key,
        "title": extra.pop("title", key.replace("-", " ")),
        "opportunity": "what is wrong",
        "change": "what to change",
        "goals": ["goal:c3-output", "goal:nonexistent"],
        "evidence": evidence,
        "constraints": [
            {"constraint": "constraint:headcount", "status": "respects", "note": "no hire"},
            {"constraint": "constraint:made-up", "status": "breaks", "note": "dropped"},
        ],
        "size": "unknown",
        "unknowns": ["how often jams fall in a break"],
        "confidence": "medium",
        "action": extra.pop("action", {"verb": "none", "target": "", "zone": ""}),
        "since_last": "",
        **extra,
    }


def first(text: str) -> dict[str, Any]:
    gap = fact_id(text, "qualification gap")
    chain = fact_id(text, "starvation chain")
    roster = fact_id(text, "holds_qualification qualification:arm-reset-c3")
    return {
        "summary": "C3 starves when CV-2 jams; the arm reset waits for one person.",
        "improvements": [
            item("cross-train-arm-reset", [gap, roster, "fact:0000000000000000"]),
            item("clean-pe3-each-shift", [chain]),
            item("no-evidence", ["fact:ffffffffffffffff"]),
            item(
                "send-amr-01-to-aisle-b",
                [chain],
                action={"verb": "send_to", "target": "robot:amr-01", "zone": "zone:aisle-b"},
            ),
            item(
                "bad-action",
                [chain],
                action={"verb": "send_to", "target": "robot:amr-01", "zone": "zone:moon"},
            ),
        ],
        "retired": [],
    }


def second(text: str) -> dict[str, Any]:
    chain = fact_id(text, "starvation chain")
    gap = fact_id(text, "qualification gap")
    return {
        "summary": "The new work order moves belt care up.",
        "improvements": [
            item("clean-pe3-each-shift", [chain], since_last="a new work order on CV-2"),
            item("cross-train-arm-reset", [gap]),
        ],
        "retired": [{"key": "bad-action", "reason": "no zone to send it to"}],
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site_dir = tmp_path_factory.mktemp("improve") / "site"
    corpus.generate(site_dir)
    build(site_dir, until=UNTIL)
    return site_dir


@pytest.fixture
def site_dir(built: Path, tmp_path: Path) -> Path:
    site_dir = tmp_path / "site"
    for path in built.rglob("*"):
        if path.is_file() and ".neptune/package" not in path.as_posix():
            target = site_dir / path.relative_to(built)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    return site_dir


def test_site_improve_mission_is_stated_and_cited(built: Path) -> None:
    model = load(built)
    target = model.facts[model.by_key[("goal:c3-output", "target")][0]]
    assert (target.kind, target.object) == ("stated", Value(">= 2400"))
    assert target.citations[0].file == "static/mission.yaml"
    assert re.fullmatch(r"line \d+", target.citations[0].locator)
    assert "constraint:safety" in model.entities


def test_site_improve_run_logs_cited_inferred_improvements(site_dir: Path) -> None:
    claude = FakeClaude(first)
    facts = improve.run(site_dir, claude)

    request = claude.calls[0]
    text = request["messages"][0]["content"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "## Mission" in text and "goal:c3-output target >= 2400" in text
    assert "qualification gap" in text

    keys = [f.subject for f in facts]
    assert keys == [
        "improvement:cross-train-arm-reset",
        "improvement:clean-pe3-each-shift",
        "improvement:send-amr-01-to-aisle-b",
        "improvement:bad-action",
    ]
    top = facts[0]
    assert (top.kind, top.source, top.model) == ("inferred", "neptune.improve", improve.MODEL)
    assert top.citations[0] == Citation(improve.RUNS, "line 1", "cross train arm reset")
    assert any(c.file == "static/roster/qualifications.csv" for c in top.citations)

    recorded = json.loads((site_dir / improve.RUNS).read_text().splitlines()[0])
    assert recorded["improvements"][0]["goals"] == ["goal:c3-output"]
    assert [c["constraint"] for c in recorded["improvements"][0]["constraints"]] == [
        "constraint:headcount"
    ]
    assert recorded["improvements"][3]["action"]["verb"] == "none"
    assert any("fact:0000000000000000" in d for d in recorded["dropped"])
    assert any("no-evidence: dropped" in d for d in recorded["dropped"])

    # the log is the model: a fresh fold reads the same run back, no model called
    runs = improve.runs(site_dir, load(site_dir))
    assert [(i.rank, i.key, i.move, i.status) for i in runs[0].items][:2] == [
        (1, "cross-train-arm-reset", "new", "open"),
        (2, "clean-pe3-each-shift", "new", "open"),
    ]
    assert load(site_dir).facts == load(site_dir).facts


def test_site_improve_rerun_moves_retires_and_sees_what_changed(site_dir: Path) -> None:
    improve.run(site_dir, FakeClaude(first))
    model = load(site_dir)
    assert improve.changes(model, improve.runs(site_dir, model)[-1]) == []

    order = Fact(
        "work_order:WO-5540",
        "wo_problem",
        Value("PE3 lens dirty again"),
        "2026-10-20T10:49:00",
        None,
        "stated",
        "cmms",
        (Citation("static/cmms/work_orders.csv", 'row 4 col "Problem"', "PE3 lens dirty again"),),
    )
    with FactLog(site_dir) as log:
        log.fact(order)
    model = load(site_dir)
    found = improve.changes(model, improve.runs(site_dir, model)[-1])
    assert [(c.kind, c.fact) for c in found] == [("record", order.id)]
    engine = improve.Engine(site_dir, lambda key: FakeClaude(second), cooldown=0)
    engine.set_key("sk-test")
    assert engine.due(model) == "1 change(s) since the last run: record"

    improve.run(site_dir, FakeClaude(second), trigger="1 change")
    last, before = improve.runs(site_dir, load(site_dir))[-2:][::-1]
    assert [(i.key, i.move) for i in last.items] == [
        ("clean-pe3-each-shift", "up 1"),
        ("cross-train-arm-reset", "down 1"),
    ]
    gone = {r["key"]: r["reason"] for r in improve.retired(last, before)}
    assert gone == {"send-amr-01-to-aisle-b": "", "bad-action": "no zone to send it to"}


def test_site_improve_accepting_an_action_dispatches_it(site_dir: Path) -> None:
    facts = improve.run(site_dir, FakeClaude(first))
    send = next(f for f in facts if f.subject == "improvement:send-amr-01-to-aisle-b")
    sent: list[tuple[str, bytes]] = []
    result = improve.answer(
        site_dir, send.id, True, "web-operator", send=lambda t, p: sent.append((t, p))
    )
    assert result["status"] == "accepted"
    assert result["action"]["status"] == "dispatched"
    assert sent and sent[0][0].endswith("/order")
    assert json.loads(sent[0][1])["nodes"][0]["nodeId"] == "zone:aisle-b"

    reject = next(f for f in facts if f.subject == "improvement:clean-pe3-each-shift")
    assert improve.answer(site_dir, reject.id, False, "web-operator")["status"] == "rejected"
    model = load(site_dir)
    status = {i.key: i.status for i in improve.runs(site_dir, model)[-1].items}
    assert status["send-amr-01-to-aisle-b"] == "accepted"
    assert status["clean-pe3-each-shift"] == "rejected"
    proposal = next(f for f in model.facts.values() if f.predicate == "proposed_action")
    assert proposal.citations[0].quote is not None and "send amr 01" in proposal.citations[0].quote
    with pytest.raises(ValueError, match="already accepted"):
        improve.answer(site_dir, send.id, True)
