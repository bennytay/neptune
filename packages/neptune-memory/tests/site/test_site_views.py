"""Views over a small hand-made fact log (spec §6): now, history, topology, identity."""

from pathlib import Path

from neptune_memory.site.facts import Citation, Fact, Kind, Ref, Value
from neptune_memory.site.log import FactLog, read
from neptune_memory.site.views import SiteModel, fold, load


def _fact(
    subject: str,
    predicate: str,
    obj: Ref | Value,
    t: str | None = None,
    kind: Kind = "observed",
    source: str = "plc.cv2",
) -> Fact:
    cite = Citation("feed/plc_cv2.jsonl", "line 1")
    return Fact(subject, predicate, obj, t, None, kind, source, (cite,))


def _site(tmp_path: Path) -> Path:
    """A conveyor feeding a cell, an arm in the cell, an AMR that goes silent at 09:40:05."""
    with FactLog(tmp_path) as log:
        log.fact(
            _fact(
                "source:plc.cv2", "report_interval", Value(5, "s"), kind="stated", source="layout"
            )
        )
        log.fact(
            _fact("source:fleet", "report_interval", Value(5, "s"), kind="stated", source="layout")
        )
        log.fact(_fact("machine:cv-2", "feeds", Ref("cell:c3"), kind="stated", source="layout"))
        log.fact(
            _fact("robot:arm-c3", "located_in", Ref("cell:c3"), kind="stated", source="layout")
        )
        log.seen("fleet/amr-03", "2026-10-20T09:40:05")
        log.fact(
            _fact("robot:amr-03", "battery", Value(22, "%"), "2026-10-20T09:40:05", source="fleet")
        )
        log.seen("fleet/amr-01", "2026-10-20T10:04:55")
        log.fact(
            _fact("robot:amr-01", "battery", Value(80, "%"), "2026-10-20T10:04:55", source="fleet")
        )
        log.seen("plc.cv2", "2026-10-20T10:00:00")
        log.fact(_fact("machine:cv-2", "mode", Value("running"), "2026-10-20T10:00:00"))
        log.seen("plc.cv2", "2026-10-20T10:05:02")
        log.fact(_fact("machine:cv-2", "mode", Value("stopped"), "2026-10-20T10:05:02"))
        log.seen("plc.cv2", "2026-10-20T10:41:00")
        log.fact(_fact("machine:cv-2", "mode", Value("running"), "2026-10-20T10:41:00"))
    return tmp_path


def _states(model: SiteModel, subject: str, t: str) -> dict[str, str]:
    return {r.predicate: r.state for r in model.state_at(t, subject)}


def test_site_views_stale_after_three_intervals(tmp_path: Path) -> None:
    model = load(_site(tmp_path))
    amr3 = model.state_at("2026-10-20T10:05:00", "robot:amr-03")
    battery = next(r for r in amr3 if r.predicate == "battery")
    assert battery.state == "stale"
    assert battery.facts[0].object == Value(22, "%")
    assert battery.last_seen == "2026-10-20T09:40:05"
    assert _states(model, "robot:amr-03", "2026-10-20T09:40:20")["battery"] == "known"
    assert _states(model, "robot:amr-01", "2026-10-20T10:05:00")["battery"] == "known"
    # A robot's mode is expected but nobody stated it.
    assert _states(model, "robot:amr-01", "2026-10-20T10:05:00")["mode"] == "unknown"


def test_site_views_ambiguous_on_a_tie(tmp_path: Path) -> None:
    with FactLog(tmp_path) as log:
        log.fact(_fact("robot:amr-02", "serial", Value("SN-88412"), kind="stated", source="cmms"))
        log.fact(_fact("robot:amr-02", "serial", Value("SN-88413"), kind="stated", source="docs"))
        log.fact(
            _fact("robot:amr-02", "firmware", Value("4.1.0"), kind="inferred", source="photos")
        )
        log.fact(_fact("robot:amr-02", "firmware", Value("4.2.0"), kind="stated", source="cmms"))
    readings = {r.predicate: r for r in load(tmp_path).state_at(None, "robot:amr-02")}
    assert readings["serial"].state == "ambiguous"
    assert {f.object for f in readings["serial"].facts} == {Value("SN-88412"), Value("SN-88413")}
    assert readings["firmware"].state == "known"
    assert readings["firmware"].facts[0].object == Value("4.2.0")


def test_site_views_interval_closes_at_next_change(tmp_path: Path) -> None:
    model = load(_site(tmp_path))
    spans = [(i.fact.object, i.start, i.end) for i in model.history("machine:cv-2", "mode")]
    assert spans == [
        (Value("running"), "2026-10-20T10:00:00", "2026-10-20T10:05:02"),
        (Value("stopped"), "2026-10-20T10:05:02", "2026-10-20T10:41:00"),
        (Value("running"), "2026-10-20T10:41:00", None),
    ]
    window = model.history("machine:cv-2", "mode", "2026-10-20T10:10:00", "2026-10-20T10:20:00")
    assert [i.fact.object for i in window] == [Value("stopped")]
    impact = model.topology().downstream("machine:cv-2")
    assert [(h.entity, h.depth) for h in impact] == [("cell:c3", 1), ("robot:arm-c3", 2)]


def test_site_views_incremental_fold_equals_from_scratch(tmp_path: Path) -> None:
    entries = list(read(_site(tmp_path)))
    live = SiteModel.empty()
    for entry in entries:
        live = live.apply(entry)
    scratch = fold(entries)
    assert live == scratch == load(tmp_path)
    t = "2026-10-20T10:35:00"
    assert live.state_at(t) == scratch.state_at(t)
    assert live.history("machine:cv-2") == scratch.history("machine:cv-2")
    assert load(tmp_path, as_of_seq=9) == fold(entries[:9])
    assert load(tmp_path, as_of_seq=9).clock() == "2026-10-20T10:04:55"


def test_site_views_confirmed_same_as_joins(tmp_path: Path) -> None:
    proposal = _fact(
        "robot:amr-02", "same_as", Ref("robot:MR-1142"), kind="inferred", source="identity"
    )
    with FactLog(tmp_path) as log:
        log.fact(proposal)
    before = load(tmp_path)
    assert before.same_as("robot:amr-02") == ("robot:amr-02",)
    assert before.pending() == [proposal]
    assert [r.state for r in before.state_at(None, "robot:amr-02") if r.predicate == "same_as"] == [
        "pending"
    ]

    with FactLog(tmp_path) as log:
        log.confirm(proposal.id, "yes", "operator")
    after = load(tmp_path)
    assert after.same_as("robot:MR-1142") == ("robot:MR-1142", "robot:amr-02")
    assert after.canonical("robot:amr-02") == "robot:MR-1142"
    assert after.pending() == []
    [joined] = [r for r in after.state_at(None, "robot:amr-02") if r.predicate == "same_as"]
    assert (joined.state, joined.facts[0].kind, joined.facts[0].source) == (
        "known",
        "stated",
        "confirm",
    )
    assert joined.facts[0].citations[-1].locator == "line 2"
