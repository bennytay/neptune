"""The configuration traceability report (``configuration-traceability@1``, ADR 0014) over the
acceptance corpus snapshot: a manipulator (ARM-3A), a lift AMR (AMR-07) and a legged robot
(LEG-01)."""

import pytest

import deploy_pack_corpus as corpus
from deploy_pack_demo import AMR, LEG, PLANT2_YEAR, S007_YEAR, snapshot, spec, traceability
from deploy_pack_graphs import rec
from deploy_pack_support import plain
from neptune_deploy.packs import PackError, compile_pack, render_pdf
from neptune_deploy.packs.compile import Entry, EvidencePack, Section
from neptune_deploy.packs.pdf import literal
from neptune_deploy.packs.snapshot import Node
from neptune_deploy.packs.text import winansi


def _section(pack: EvidencePack, section_id: str) -> Section:
    (section,) = [s for s in pack.sections if s.template.id == section_id]
    return section


def _objects(entry: Entry) -> list[str]:
    return [
        str(s.claim.object.get("node_id") or s.claim.object.get("record_id"))
        for s in entry.statements
    ]


def _shown(text: str) -> bytes:
    return literal(winansi(text))[1:-1]


def test_the_chain_reads_as_commissioned_changed_and_left_open() -> None:
    chain = _section(traceability(), "configuration-chain")
    assert [e.knowledge for e in chain.entries] == ["known", "unknown", "known", "unknown"]
    assert _objects(chain.entries[0]) == ["cfg:cfg-c3-1.4"]  # as commissioned
    assert sorted(_objects(chain.entries[1])) == sorted(
        [rec("change record CHG0030012"), rec("work order WO-26-0310")]
    )
    assert _objects(chain.entries[2]) == ["cfg:cfg-c3-1.5"]  # changed and requalified
    # WO-26-0911 and WO-26-0912 leave it open from 2026-09-10: a gap, never bridged.
    assert chain.entries[3].valid.end == "open"
    assert sorted(_objects(chain.entries[3])) == sorted(
        [rec("work order WO-26-0911"), rec("work order WO-26-0912")]
    )
    starts = [e.valid.start.ticks for e in chain.entries]
    assert starts == sorted(starts)
    assert all(e.valid.start.domain == corpus.P2_LIFE for e in chain.entries)


def test_no_change_is_claimed_across_a_gap() -> None:
    changes = _section(traceability(), "configuration-changes")
    assert changes.knowledge == "not_covered"
    assert changes.reason is not None
    assert changes.reason["predicates"] == ["succeeds"]
    amr = _section(traceability(AMR, S007_YEAR), "configuration-changes")
    (succession,) = amr.entries
    assert succession.node == Node("configuration", "cfg:AMR-07-B")
    assert _objects(succession) == ["cfg:AMR-07-A"]


def test_an_ambiguous_configuration_link_shows_every_reading() -> None:
    pack = traceability(AMR, S007_YEAR)
    chain = _section(pack, "configuration-chain")
    assert [e.knowledge for e in chain.entries] == [
        "known",
        "known",
        "unknown",
        "ambiguous",
        "known",
    ]
    assert sorted(_objects(chain.entries[3])) == ["cfg:AMR-07-C", "cfg:AMR-07-D"]
    runs = _section(pack, "runs")
    (ambiguous,) = [e for e in runs.other_clocks if e.knowledge == "ambiguous"]
    assert sorted(_objects(ambiguous)) == ["cfg:AMR-07-C", "cfg:AMR-07-D"]
    pdf = render_pdf(pack)
    assert (
        _shown("[AMBIGUOUS - every reading below; none is chosen] machine asset-tag:AMR-07") in pdf
    )


def test_a_legged_robot_run_bound_to_two_exports_is_ambiguous() -> None:
    runs = _section(traceability(LEG, PLANT2_YEAR), "runs")
    (patrol,) = runs.other_clocks
    assert patrol.knowledge == "ambiguous"
    assert sorted(_objects(patrol)) == ["cfg:leg01-patrol-fw-3.1.4", "cfg:leg01-patrol-fw-3.2.0"]


def test_runs_are_listed_on_their_own_clocks_with_their_configuration() -> None:
    pack = traceability()
    runs = _section(pack, "runs")
    assert runs.entries == ()  # never compared with the lifecycle clock
    assert {e.valid.start.domain for e in runs.other_clocks} == {corpus.RUN09, corpus.RUN14}
    assert all(_objects(e) == ["cfg:cfg-c3-1.5"] for e in runs.other_clocks)
    gaps = _section(pack, "authorisation-gaps")
    assert len(gaps.other_clocks) == 2
    assert {s.claim.assertion_kind for e in gaps.other_clocks for s in e.statements} == {"observed"}


def test_calibration_state_and_drift_with_declared_units() -> None:
    pack = traceability()
    calibration = _section(pack, "calibration")
    assert [s.claim.predicate for e in calibration.other_clocks for s in e.statements] == [
        "drift",
        "drift",
    ]
    assert {e.valid.start.domain for e in calibration.other_clocks} == {corpus.CIVIL}
    pdf = render_pdf(pack)
    z = 0.0702 - 0.0745
    assert _shown(f'delta parameter "translation": [0.0, 0.0, {float.__repr__(z)}] m') in pdf
    amr = traceability(AMR, S007_YEAR)
    (in_force,) = _section(amr, "calibration").entries
    assert _objects(in_force) == ["cfg:AMR-07-lidar-2026-04-14"]
    (produced,) = _section(amr, "calibration-records").entries
    assert _objects(produced) == [rec("work order WO-26-0414")]


def test_authorisation_in_force_and_its_absence() -> None:
    plant = _section(traceability(), "authorisation")
    assert plant.knowledge == "not_covered"
    assert plant.reason is not None
    assert {n["node_id"] for n in plain(plant.reason)["nodes"]} == {"site-code:PLANT-2"}
    s007 = _section(traceability(AMR, S007_YEAR), "authorisation")
    (envelope,) = s007.other_clocks  # on the zone register's date clock
    assert envelope.valid.start.domain == corpus.S7_ENV
    assert _objects(envelope) == ["cfg:AMR-07-A"]


def test_the_report_is_for_one_machine() -> None:
    snap = snapshot()
    site = Node("site", "site-code:PLANT-2")
    with pytest.raises(PackError) as caught:
        compile_pack(spec("configuration-traceability", site, PLANT2_YEAR), snap)
    assert caught.value.code == "subject_type_unsupported"


def test_a_machine_nothing_is_claimed_about_is_not_covered_everywhere() -> None:
    lone = Node("machine", "asset-tag:AMR-05")
    pack = traceability(lone, S007_YEAR)
    assert {s.knowledge for s in pack.sections} == {"not_covered"}
    assert pack.claims == ()


def test_the_pdf_cites_every_claim_and_shows_the_gap() -> None:
    pack = traceability()
    pdf = render_pdf(pack)
    for claim in pack.claims:
        assert claim.id.encode() in pdf
    assert b"UNKNOWN - stated as not known" in pdf
    assert b"NOT COVERED" in pdf
