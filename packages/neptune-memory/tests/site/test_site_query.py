"""The site query API (spec §8) on a small hand-built model: one happy path per question."""

from __future__ import annotations

from typing import TYPE_CHECKING

from site_fake import FakeModel

from neptune_memory.site import query
from neptune_memory.site.facts import Ref
from neptune_memory.site.render import render

if TYPE_CHECKING:
    from neptune_memory.site.query import Answer, Item

MODEL = FakeModel()
T = "2026-10-20T10:35:00"


def _obj(item: Item) -> object:
    if item.object is None:
        return None
    return item.object.ref if isinstance(item.object, Ref) else item.object.value


def _has(answer: Answer, subject: str, predicate: str, obj: object = ..., **want: object) -> bool:
    for item in answer.items:
        if (item.subject, item.predicate) != (subject, predicate):
            continue
        if obj is not ... and _obj(item) != obj:
            continue
        if all(getattr(item, key) == value for key, value in want.items()):
            return True
    return False


def _files(answer: Answer) -> set[str]:
    return {c.file for c in answer.sources.values()}


def test_site_resolve_lists_the_pending_identity_proposal() -> None:
    answer = query.resolve(MODEL, "MR-1142")
    assert _has(
        answer, "robot:amr-02", "same_as", "robot:MR-1142", kind="inferred", state="pending"
    )
    assert _has(answer, "robot:MR-1142", "serial", "SN-88412", kind="stated")
    assert {"feed/fleet.jsonl", "static/cmms/assets.csv"} <= _files(answer)


def test_site_state_at_marks_a_quiet_source_stale() -> None:
    answer = query.state_at(MODEL, "robot:amr-03", at=T)
    assert _has(answer, "robot:amr-03", "battery", 22, kind="observed", state="stale")
    assert any("stale" in gap for gap in answer.gaps)
    assert answer.items[0].cite == ("S1",)


def test_site_state_at_defaults_to_the_latest_report() -> None:
    answer = query.state_at(MODEL, "machine:cv-2")
    assert answer.at == "2026-10-20T11:15:00"
    assert _has(answer, "machine:cv-2", "mode", "running", state="known")
    assert _has(answer, "machine:cv-2", "note", kind="inferred")


def test_site_find_names_who_holds_a_qualification() -> None:
    answer = query.find(MODEL, "holds_qualification", "qualification:arm-reset-c3", at=T)
    assert _has(answer, "person:okafor", "holds_qualification", "qualification:arm-reset-c3")
    assert _files(answer) == {"static/roster/qualifications.csv"}


def test_site_find_brings_what_is_known_about_the_object() -> None:
    answer = query.find(MODEL, "work_order", "work_order:WO-5530")
    assert _has(answer, "machine:cv-2", "work_order", "work_order:WO-5530", kind="stated")
    assert _has(answer, "work_order:WO-5530", "wo_cause", "belt tracking off", kind="stated")


def test_site_timeline_gives_intervals_in_the_window() -> None:
    answer = query.timeline(
        MODEL, "robot:arm-c3", "2026-10-20T10:00:00", "2026-10-20T11:15:00", ["mode"]
    )
    starved = [i for i in answer.items if _obj(i) == "idle_starved"]
    assert starved and starved[0].valid_to == "2026-10-20T10:41:20"
    assert [_obj(i) for i in answer.items] == ["running", "idle_starved", "protective_stop"]


def test_site_why_shows_what_a_fact_replaced_and_what_replaced_it() -> None:
    stopped = query.diff(MODEL, "2026-10-20T10:05:01", "2026-10-20T10:05:02", ["machine:cv-2"])
    fact_id = next(i.fact_id for i in stopped.items if i.predicate == "mode")
    assert fact_id is not None
    answer = query.why(MODEL, fact_id)
    relations = {(i.relation, i.valid_from) for i in answer.items}
    assert ("asked", "2026-10-20T10:05:02") in relations
    assert ("replaced", "2026-10-20T06:00:00") in relations
    assert ("replaced by", "2026-10-20T10:41:00") in relations
    assert answer.sources["S1"].locator == "line 812"


def test_site_diff_lists_facts_that_began_in_the_window() -> None:
    answer = query.diff(MODEL, "2026-10-20T10:00:00", "2026-10-20T10:10:00")
    assert _has(answer, "machine:cv-2", "health", "fault:JAM_PE3", kind="observed")
    assert _has(answer, "machine:cv-2", "work_order", "work_order:WO-5530", kind="stated")
    assert not _has(answer, "machine:cv-2", "setpoint")


def test_site_gaps_lists_stale_unknown_and_pending() -> None:
    answer = query.gaps(MODEL, at=T)
    assert _has(answer, "robot:amr-03", "battery", state="stale")
    assert _has(answer, "machine:cv-2", "belt_tension", state="unknown")
    assert _has(answer, "robot:amr-02", "same_as", "robot:MR-1142", state="pending")


def test_site_impact_walks_downstream_with_states() -> None:
    answer = query.impact(MODEL, "machine:cv-2", at=T)
    assert _has(answer, "machine:cv-2", "mode", "stopped", state="known")
    assert _has(answer, "machine:cv-2", "feeds", "cell:c3", kind="stated")
    assert _has(answer, "robot:arm-c3", "mode", "idle_starved", kind="observed")
    assert _has(answer, "task:T-8812", "task_state", "waiting", kind="observed")
    assert {"feed/plc_cv2.jsonl", "feed/arm_c3.jsonl", "feed/fleet.jsonl"} <= _files(answer)
    assert "static/layout.yaml" in _files(answer)


def test_site_search_evidence_finds_cited_text() -> None:
    answer = query.search_evidence(MODEL, "PE3 dirty")
    assert _has(answer, "machine:cv-2", "note", kind="inferred")
    assert _files(answer) == {"static/photos/whiteboard_2026-10-20.png"}


def test_site_sql_answers_rows_and_cites_selected_facts() -> None:
    answer = query.sql(
        MODEL,
        "SELECT fact_id, object, valid_from FROM facts "
        "WHERE subject = 'machine:cv-2' AND predicate = 'mode' ORDER BY valid_from",
    )
    assert [row[1] for row in answer.rows] == ["running", "stopped", "running"]
    assert len(answer.items) == 3 and _files(answer) == {"feed/plc_cv2.jsonl"}
    seen = query.sql(MODEL, "SELECT source, max(t) FROM seen GROUP BY source ORDER BY source")
    assert ("plc.cv2", "2026-10-20T11:15:00") in seen.rows


def test_site_render_gives_compact_cited_lines_and_a_sources_footer() -> None:
    text = render(query.state_at(MODEL, "machine:cv-2", at=T))
    assert '- machine:cv-2 mode = "stopped" since 10:05:02 · observed [S' in text
    assert "\nSources:\n" in text
    assert '] feed/plc_cv2.jsonl line 812 "\\"state\\": \\"stopped\\""' in text
