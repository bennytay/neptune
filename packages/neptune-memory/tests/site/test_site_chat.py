"""Ask the site: Claude (a fake) calls Neptune's query tools, cites, and is traced."""

from pathlib import Path

import pytest
from fake_claude import FakeChatClaude

from neptune_memory.site import corpus
from neptune_memory.site.build import build
from neptune_memory.site.chat import Chat
from neptune_memory.site.dashboard import graph, thing
from neptune_memory.site.views import SiteModel, load

UNTIL = "2026-10-20T10:10:00"


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site_dir = tmp_path_factory.mktemp("chat") / "site"
    corpus.generate(site_dir)
    build(site_dir, until=UNTIL)
    return site_dir


def test_site_chat_answers_through_traced_tool_calls(site_dir: Path) -> None:
    model = load(site_dir)
    claude = FakeChatClaude(
        [("insights", {}), ("resolve", {"name": "cv-2"})],
        "CV-2 jammed at PE3 (feed/plc_cv2.jsonl line 3423) and starved robot:arm-c3.",
    )

    def site() -> SiteModel:
        return model

    chat = Chat(site_dir, lambda: claude)
    turn = chat.ask("t1", "What held up C3?", site)

    assert turn["reply"].startswith("CV-2 jammed at PE3 (feed/plc_cv2.jsonl line 3423)")
    assert [s["tool"] for s in turn["trace"]] == ["insights", "resolve"]
    # what the answer rests on: what it names, then the things on the result lines it cites
    assert turn["touched"][0] == "robot:arm-c3"
    assert {"machine:cv-2", "task:T-8812"} <= set(turn["touched"])
    assert "finding" in turn["trace"][0]["head"] and "more)" in turn["trace"][0]["head"]
    second = claude.requests[1]["messages"][-1]["content"]
    assert [r["type"] for r in second] == ["tool_result", "tool_result"]
    assert "Sources:" in second[0]["content"] and not second[0]["is_error"]
    assert claude.requests[0]["output_config"] == {"effort": "medium"}
    assert {t["name"] for t in claude.requests[0]["tools"]} >= {"insights", "sql", "improvements"}

    claude.script.append([("nope", {})])
    claude.script.append("I could not find that.")
    again = chat.ask("t1", "and the other one?", site)
    assert again["trace"][0]["error"] is True
    assert len(claude.requests[-1]["messages"]) == 7  # the whole talk goes back each turn


def test_site_chat_graph_and_thing_link_the_site(site_dir: Path) -> None:
    model = load(site_dir)
    found = graph(site_dir, model)
    nodes = {n["id"]: n for n in found["nodes"]}
    assert nodes["machine:cv-2"]["status"] == "fault"
    assert {"goal:c3-output", "constraint:safety", "person:okafor"} <= set(nodes)
    assert any(
        e["from"] == "machine:cv-2" and e["to"] == "cell:c3" and e["predicate"] == "feeds"
        for e in found["edges"]
    )

    cv2 = thing(site_dir, model, "machine:cv-2")
    lanes = {lane["predicate"]: lane["spans"] for lane in cv2["lanes"]}
    assert lanes["health"][-1]["value"] == "fault:JAM_PE3"
    assert any(f["text"].startswith("starvation chain") for f in cv2["findings"])
    assert any(link["predicate"] == "feeds" and link["other"] == "cell:c3" for link in cv2["links"])
