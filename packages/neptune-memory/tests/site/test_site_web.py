"""The live web view (#210): its JSON API over a built Plant 7 log, through a real HTTP server."""

import json
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from neptune_memory.site import corpus
from neptune_memory.site.actions import propose
from neptune_memory.site.build import build
from neptune_memory.site.views import load
from neptune_memory.site.web import serve

JAM = "2026-10-20T10:06:30"


SENT: list[tuple[str, bytes]] = []
"""What the web view dispatched to the fleet manager (instead of MQTT)."""


@pytest.fixture(scope="module")
def site_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    site_dir = tmp_path_factory.mktemp("web") / "site"
    corpus.generate(site_dir)
    build(site_dir, until="2026-10-20T10:10:00")
    return site_dir


@pytest.fixture(scope="module")
def url(site_dir: Path) -> Iterator[str]:
    server = serve(site_dir, port=0, send=lambda topic, payload: SENT.append((topic, payload)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def _get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _post(url: str, body: dict[str, str]) -> Any:
    request = urllib.request.Request(
        url, json.dumps(body).encode(), {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def test_site_web_snapshot_shows_the_jam_on_the_map(url: str) -> None:
    snap = _get(f"{url}/api/snapshot?at={JAM}")
    status = {a["id"]: a["status"] for a in snap["assets"]}
    assert status["machine:cv-2"] == "fault"
    assert status["robot:arm-c3"] == "warn"  # idle_starved
    assert status["robot:amr-01"] == "warn"  # waiting on T-8812
    assert status["robot:amr-03"] == "stale"
    place = {a["id"]: a["place"] for a in snap["assets"]}
    assert place["robot:amr-01"] == "zone:aisle-a"
    assert any(e["from"] == "machine:cv-2" and e["to"] == "cell:c3" for e in snap["edges"])
    findings = [i["value"] for i in snap["findings"]["items"]]
    assert any(f.startswith("starvation chain") for f in findings)
    assert all(i["kind"] == "inferred" for i in snap["findings"]["items"])
    assert any(r["fault"] and r["subject"] == "machine:cv-2" for r in snap["timeline"])
    before = _get(f"{url}/api/snapshot?at=2026-10-20T10:00:00")
    assert {a["id"]: a["status"] for a in before["assets"]}["machine:cv-2"] == "ok"


def test_site_web_a_fact_opens_its_cited_span(url: str) -> None:
    snap = _get(f"{url}/api/snapshot?at={JAM}")
    health = next(
        r for r in snap["table"] if (r["subject"], r["predicate"]) == ("machine:cv-2", "health")
    )
    why = _get(f"{url}/api/why?fact={health['fact']}")
    cite = why["sources"][why["items"][0]["cite"][0]]
    assert cite["file"] == "feed/plc_cv2.jsonl"
    span = _get(f"{url}/api/source?file={cite['file']}&locator={quote(cite['locator'])}")
    hit = next(line for line in span["lines"] if line["n"] == span["hit"])
    assert cite["quote"] in hit["text"]


def test_site_web_source_reads_tables_pages_and_images(url: str) -> None:
    def source(file: str, locator: str) -> Any:
        return _get(f"{url}/api/source?file={quote(file)}&locator={quote(locator)}")

    table = source("static/cmms/assets.csv", 'row 3 col "Serial"')
    row = next(r for r in table["rows"] if r["n"] == table["hit_row"])
    assert row["cells"][table["hit_col"]] == "SN-88412"
    page = source("static/docs/PSR-12_excerpt.pdf", "page 1 block 2")
    assert page["blocks"][page["hit"] - 1]["text"].startswith("4.2 Jams on powered conveyors")
    image = source("static/photos/whiteboard_2026-10-20.png", "region 120,40,380,90")
    assert image["box"] == [120, 40, 380, 90]
    assert image["size"] is not None


def test_site_web_confirm_answers_a_pending_proposal(url: str) -> None:
    pending = _get(f"{url}/api/snapshot")["pending"]["items"]
    assert [(p["subject"], p["value"]) for p in pending] == [("robot:amr-02", "robot:MR-1142")]
    _post(f"{url}/api/confirm", {"proposal": pending[0]["fact"], "answer": "yes"})
    snap = _get(f"{url}/api/snapshot")
    assert snap["pending"]["items"] == []
    amr = next(a for a in snap["assets"] if a["id"] == "robot:amr-02")
    assert amr["aliases"] == ["robot:MR-1142"]


def test_site_web_serves_the_page_and_events(url: str) -> None:
    with urllib.request.urlopen(f"{url}/", timeout=30) as page:
        assert b"<svg" in page.read()
    with urllib.request.urlopen(f"{url}/events", timeout=30) as events:
        line = events.readline()
    assert json.loads(line.removeprefix(b"data: "))["seq"] > 0


def test_site_web_approving_an_action_dispatches_it(url: str, site_dir: Path) -> None:
    model = load(site_dir)
    cite = model.by_key[("robot:amr-01", "in_zone")][-1]
    proposal = propose(site_dir, "send_to", "robot:amr-01", "clear aisle-a", [cite], "zone:aisle-b")
    listed = _get(f"{url}/api/actions")
    assert [(a["id"], a["status"]) for a in listed["items"]] == [(proposal.id, "pending")]
    done = _post(f"{url}/api/actions/approve", {"id": proposal.id, "by": "benjamin"})["result"]
    assert (done["status"], done["by"]) == ("dispatched", "benjamin")
    [(topic, payload)] = SENT
    assert topic == "uagv/v2/p7/amr-01/order"
    assert json.loads(payload)["nodes"][0]["nodeId"] == "zone:aisle-b"
    listed = _get(f"{url}/api/actions")
    assert listed["items"] == []
    assert listed["recent"][0]["dispatched"] == "order on uagv/v2/p7/amr-01/order"
