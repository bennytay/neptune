"""The mappers on the generated Plant 7 corpus: generate, build, and the key facts are in the log
with citations to the right files."""

import pytest

from neptune_memory.site import corpus
from neptune_memory.site.build import build
from neptune_memory.site.facts import Fact, Ref, Value
from neptune_memory.site.log import read


@pytest.fixture(scope="module")
def facts(tmp_path_factory: pytest.TempPathFactory) -> list[Fact]:
    site = tmp_path_factory.mktemp("p7")
    corpus.generate(site)
    build(site)
    return [entry.fact for entry in read(site) if entry.fact is not None]


def _find(facts: list[Fact], subject: str, predicate: str, obj: Ref | Value) -> Fact:
    found = [f for f in facts if (f.subject, f.predicate, f.object) == (subject, predicate, obj)]
    assert len(found) == 1, (subject, predicate, obj, found)
    return found[0]


def test_layout_states_the_feeds_edge(facts: list[Fact]) -> None:
    feeds = _find(facts, "machine:cv-2", "feeds", Ref("cell:c3"))
    assert (feeds.kind, feeds.citations[0].file) == ("stated", "static/layout.yaml")
    assert "predicate: feeds" in (feeds.citations[0].quote or "")


def test_roster_and_change_log_name_real_entities(facts: list[Fact]) -> None:
    held = _find(facts, "person:okafor", "holds_qualification", Ref("qualification:arm-reset-c3"))
    assert held.citations[0].file == "static/roster/qualifications.csv"
    change = _find(facts, "machine:cv-2", "change", Ref("change:CHG-311"))
    assert (change.kind, change.valid_from) == ("stated", "2026-10-20T05:40:00")
    to = _find(facts, "change:CHG-311", "change_to", Value("0.95", "m/s"))
    assert to.citations[0].locator == 'row 2 col "To"'
    assert not [f for f in facts if f.subject.endswith(":") or f.subject.count(":") != 1]


def test_fleet_serial_is_observed_once(facts: list[Fact]) -> None:
    serial = _find(facts, "robot:amr-02", "serial", Value("SN-88412"))
    assert (serial.kind, serial.source, serial.citations[0].file) == (
        "observed",
        "fleet",
        "feed/fleet.jsonl",
    )
    assert _find(facts, "robot:MR-1142", "serial", Value("SN-88412")).kind == "stated"


def test_whiteboard_note_is_inferred_from_the_recorded_extraction(facts: list[Fact]) -> None:
    (note,) = [f for f in facts if f.subject == "machine:cv-2" and f.predicate == "note"]
    assert note.kind == "inferred"
    assert note.model is not None and note.model.startswith("claude-sonnet-4-5@")
    assert note.citations[0].file == "static/photos/whiteboard_2026-10-20.png"
    assert note.citations[0].locator == "region 120,40,380,90"
