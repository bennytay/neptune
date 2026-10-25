"""Actions (#212): propose -> approve -> dispatched -> outcome, the happy path."""

import json
from pathlib import Path

import pytest

from neptune_memory.site import corpus
from neptune_memory.site.actions import (
    Simulator,
    Watcher,
    actions,
    approve,
    pending,
    propose,
    reject,
)
from neptune_memory.site.build import build
from neptune_memory.site.facts import Citation, Fact, Ref
from neptune_memory.site.log import FactLog
from neptune_memory.site.render import render
from neptune_memory.site.replay import FeedMessage
from neptune_memory.site.views import load

UNTIL = "2026-10-19T13:51:00"  # a minute of feed: every AMR has reported


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site_dir = tmp_path_factory.mktemp("actions") / "site"
    corpus.generate(site_dir)
    build(site_dir, until=UNTIL)
    return site_dir


def _fresh(built: Path, tmp_path: Path) -> Path:
    site_dir = tmp_path / "site"
    for path in built.rglob("*"):
        if path.is_file() and ".neptune/package" not in path.as_posix():
            target = site_dir / path.relative_to(built)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    return site_dir


def _cite(site_dir: Path, subject: str, predicate: str) -> str:
    model = load(site_dir)
    return model.by_key[(subject, predicate)][-1]


def test_site_actions_send_to_closes_the_loop(built: Path, tmp_path: Path) -> None:
    site_dir = _fresh(built, tmp_path)
    zone_fact = _cite(site_dir, "robot:amr-01", "in_zone")
    proposal = propose(
        site_dir, "send_to", "robot:amr-01", "clear aisle-a", [zone_fact], "zone:aisle-b"
    )
    assert (proposal.kind, proposal.source) == ("inferred", "neptune.actions")
    assert proposal.citations[0] == Citation("actions/proposed.jsonl", "line 1", "clear aisle-a")
    assert [a.id for a in pending(site_dir)] == [proposal.id]

    sent: list[tuple[str, bytes]] = []
    action = approve(site_dir, proposal.id, by="benjamin", send=lambda t, p: sent.append((t, p)))
    [(topic, payload)] = sent
    assert topic == "uagv/v2/p7/amr-01/order"
    assert json.loads(payload)["nodes"][0]["nodeId"] == "zone:aisle-b"
    assert action.status == "dispatched" and pending(site_dir) == []
    assert action.intent is not None and action.intent.kind == "stated"

    # The simulator answers the order: amr-01 reports aisle-b a few site seconds later.
    simulator = Simulator()
    first = FeedMessage(UNTIL, "fleet", "uagv/v2/p7/amr-01/state", {"zone": "zone:aisle-a"}, "f", 1)
    simulator.apply(first)
    simulator.receive(topic, payload)
    later = FeedMessage("2026-10-19T13:52:00", "fleet", first.topic, first.payload, "f", 2)
    assert simulator.apply(later).payload["zone"] == "zone:aisle-b"

    watcher = Watcher(site_dir)
    with FactLog(site_dir) as log:
        assert watcher.step(log)  # catches up on the log: proposed, approved, dispatched
        log.seen("fleet", "2026-10-19T13:52:00")
        log.fact(
            Fact(
                "robot:amr-01",
                "in_zone",
                Ref("zone:aisle-b"),
                "2026-10-19T13:52:00",
                None,
                "observed",
                "fleet",
                (Citation("feed/live/segment-000001.jsonl", "line 99", "zone:aisle-b"),),
            )
        )
        watcher.step(log)  # reads the new state, appends the outcome
        assert any("outcome" in e and "done" in e for e in watcher.step(log))

    text = render(actions(load(site_dir)))
    for part in ("proposed, done", "intent, approved by benjamin", "dispatched", "done, in_zone"):
        assert part in text
    assert "actions/dispatched.jsonl line 1" in text


def test_site_actions_reject_and_page(built: Path, tmp_path: Path) -> None:
    site_dir = _fresh(built, tmp_path)
    mode = _cite(site_dir, "robot:amr-02", "mode")
    paused = propose(site_dir, "pause", "robot:amr-02", "blocks the aisle", [mode])
    assert reject(site_dir, paused.id, by="patel").status == "rejected"
    page = propose(site_dir, "page", "person:okafor", "arm-c3 needs a reset", [mode])
    assert approve(site_dir, page.id, send=lambda t, p: None).status == "logged"
    assert pending(site_dir) == []
    with pytest.raises(ValueError, match="known zone"):
        propose(site_dir, "send_to", "robot:amr-02", "why", [mode], "zone:nowhere")
