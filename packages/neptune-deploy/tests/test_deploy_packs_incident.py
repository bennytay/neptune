"""The incident reconstruction (``incident-timeline@1``, ADR 0014) over the acceptance corpus
snapshot: INC-C3-0011 at PLANT-2's manipulator cell, and INC-0007 at S-007, where the CMMS and the
fleet manager's syslog put the protective stop 32 seconds apart."""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import deploy_pack_corpus as corpus
from deploy_pack_demo import (
    ARM,
    CONTROLLER_FAULT,
    INC_0007,
    INC_0007_HOUR,
    INC_0007_REPORT,
    INC_C3,
    INC_C3_HOUR,
    INTERVENTION,
    SAMPLE,
    SYSLOG_PSTOP,
    SYSLOG_WARN,
    demo_packs,
    document,
    incident,
    sample_bytes,
    snapshot,
    spec,
)
from deploy_pack_graphs import rec
from deploy_pack_support import plain
from neptune_deploy.lifecycle.cli import main
from neptune_deploy.packs import (
    PackError,
    compile_pack,
    read_snapshot,
    render_claims,
    render_json,
    render_pdf,
)
from neptune_deploy.packs.compile import Entry, EvidencePack, Section
from neptune_deploy.packs.snapshot import Stamp
from test_deploy_packs_corpus import validator

TESTS = Path(__file__).resolve().parent
THIRTY_TWO_SECONDS = 32 * corpus.SECOND  # ticks of the S-007 CMMS clock (nanoseconds)

# Golden digests of the demo packs (JSON, PDF, claims). A change here changes what the demo shows:
# explain it in the PR, and raise COMPILER_VERSION when a pack's content changes.
GOLDEN = {
    "amr-07.claims": "sha256:65a6bb91fc9908b8947e3785e8311d48c05ae3ceb78e25c6adaafe2b1f176157",
    "amr-07.json": "sha256:b45895703384fb860f3e455597417449a0bd047bdcf638622f07a6b856e5d91f",
    "amr-07.pdf": "sha256:d9f158e34a1990f1bf356ba2e9690985ab41699221ed6128d4dfcfa924494d25",
    "arm-3a.claims": "sha256:32205f5979ad0fb5c806b5f526bc4747e2db412dad0f2378001230f51c7c19d3",
    "arm-3a.json": "sha256:0cb5d214ede4c65d540c4358ac223313ef66082b867773a7131a2775c282b1f7",
    "arm-3a.pdf": "sha256:6d06cf07c1ada9b4186495812be91317b91f3a65a75bcec52fc342525d0524e6",
    "inc-0007.claims": "sha256:67614743a92ba167c4df171e51a11dcd5400d4c180f88d837ac55559b8754c06",
    "inc-0007.json": "sha256:d1002a505fff73d97785437dfbe132de6162472af0b72ca925335934e42c89a9",
    "inc-0007.pdf": "sha256:e55d094bd54dfc2d08bc7daf578b5dd82aff120b824067ef76e7b10844eaea5c",
    "inc-c3-0011.claims": "sha256:b24e459163cec09af8ce619f1e33e6a6ff2f1cf5794edbbe3746d34518e5283a",
    "inc-c3-0011.json": "sha256:0e7815449db07ac509313fa1c4358c2f032992569b8568189ff0dfceb135d6a1",
    "inc-c3-0011.pdf": "sha256:df2300a270dab18403570df50dfeac6cfc16992cb65d25e60d9279ee67d27972",
}
RENDERERS = (("claims", render_claims), ("json", render_json), ("pdf", render_pdf))


def _section(pack: EvidencePack, section_id: str) -> Section:
    (section,) = [s for s in pack.sections if s.template.id == section_id]
    return section


def _kinds(entry: Entry) -> set[str]:
    return {
        str(s.claim.object["value"]) for s in entry.statements if s.claim.predicate == "event_kind"
    }


def _said(entry: Entry) -> str:
    (said,) = [s for s in entry.statements if s.claim.predicate == "has_description"]
    return str(said.claim.object["value"])


def _text(pdf: bytes) -> str:
    """The PDF's shown text, lines joined and wrapping undone (whitespace collapsed)."""
    shown = re.findall(rb"\((.*?)\) Tj ET\n", pdf)
    unescaped = [
        re.sub(
            rb"\\([0-7]{3}|.)",
            lambda m: bytes([int(m[1], 8)]) if len(m[1]) == 3 else m[1],
            line,
        ).decode("cp1252")
        for line in shown
    ]
    return " ".join(" ".join(unescaped).split())


# --- INC-C3-0011: the corpus's arm-cell incident -------------------------------------------------


def test_the_arm_cell_reconstruction_merges_the_incident_and_its_report_statements() -> None:
    timeline = _section(incident(), "reconstruction")
    assert [e.node for e in timeline.entries][1] == INC_C3
    # The report's five timeline entries are reached through the record they share.
    statements = [_said(e) for e in timeline.entries if e.node != INC_C3]
    assert statements == [said for _when, said in corpus.INC_C3_TIMELINE]
    starts = [e.valid.start.ticks for e in timeline.entries]
    assert starts == sorted(starts)
    assert {e.knowledge for e in timeline.entries} == {"known"}
    assert all(e.valid.start.domain == corpus.P2_REPORT for e in timeline.entries)
    (incident_entry,) = [e for e in timeline.entries if e.node == INC_C3]
    assert _kinds(incident_entry) == {"incident"}


def test_what_lies_on_other_clocks_is_listed_never_placed() -> None:
    pack = incident()
    configuration = _section(pack, "configuration-in-force")
    assert configuration.entries == ()
    assert [e.knowledge for e in configuration.other_clocks] == [
        "known",
        "unknown",
        "known",
        "unknown",
    ]
    runs = _section(pack, "runs")
    assert {e.valid.start.domain for e in runs.other_clocks} == {corpus.RUN09, corpus.RUN14}
    assert "On other clocks (never compared with the pack interval)" in _text(render_pdf(pack))


def test_an_inferred_clock_mapping_is_left_out_unless_included_and_then_marked() -> None:
    excluded = _section(incident(), "clocks")
    predicates = {s.claim.predicate for e in excluded.other_clocks for s in e.statements}
    assert predicates == {"has_clock"}
    assert len(excluded.excluded_inferred) == 2  # maps_to and clock_map of the estimate
    included = incident(inference="include")
    clocks = _section(included, "clocks")
    mapped = [s for e in clocks.other_clocks for s in e.statements if s.claim.inferred]
    assert {s.claim.predicate for s in mapped} == {"maps_to", "clock_map"}
    shown = _text(render_pdf(included))
    assert "[INFERRED by neptune.clocks 1, confidence unknown]" in shown
    assert (
        f"(co_sampled): anchor {corpus.RUN14} 1789410576700000000 -> {corpus.CTRL14}"
        " 1789410480000000000, rate 1/1, residual"
        " bound unknown"
    ) in shown


# --- INC-0007: the 32-second disagreement --------------------------------------------------------


def test_cmms_and_syslog_32_seconds_apart_are_one_event_in_conflict_with_both_times() -> None:
    timeline = _section(incident(INC_0007, INC_0007_HOUR), "reconstruction")
    conflict = [e for e in timeline.entries if e.knowledge == "conflict"]
    assert [e.node for e in conflict] == [INC_0007, SYSLOG_PSTOP]
    cmms, syslog = conflict
    assert syslog.valid.start.ticks - cmms.valid.start.ticks == THIRTY_TWO_SECONDS
    assert [(d.node, d.start_difference_ticks) for d in cmms.differences] == [
        (SYSLOG_PSTOP, THIRTY_TWO_SECONDS)
    ]
    assert [(d.node, d.start_difference_ticks) for d in syslog.differences] == [
        (INC_0007, -THIRTY_TWO_SECONDS)
    ]
    # Each keeps its own citations; the same_as claim that joins them is cited too.
    assert cmms.identity == (SYSLOG_PSTOP,) and syslog.identity == (INC_0007,)
    (link,) = cmms.identity_claims
    assert link in {c.id for c in incident(INC_0007, INC_0007_HOUR).claims}
    assert _kinds(cmms) == {"incident"} and _kinds(syslog) == {"protective_stop"}
    # The syslog placement names the mapping it was placed through and its target clock.
    assert syslog.placement_records == tuple(sorted((rec(corpus.SYSLOG_MAP), corpus.S7_LIFE)))
    assert cmms.placement_records == ()


def test_the_conflict_is_rendered_with_both_times_and_the_difference() -> None:
    pack = incident(INC_0007, INC_0007_HOUR)
    shown = _text(render_pdf(pack))
    assert shown.count("CONFLICT - this event is placed at different times on the pack clock") == 2
    assert "starting +32000000000 ticks from this one" in shown
    assert "starting -32000000000 ticks from this one" in shown
    document = json.loads(render_json(pack))
    (section,) = [s for s in document["sections"] if s["id"] == "reconstruction"]
    conflicts = [e for e in section["entries"] if e["knowledge"] == "conflict"]
    assert [c["conflicts_with"][0]["start_difference_ticks"] for c in conflicts] == [
        THIRTY_TWO_SECONDS,
        -THIRTY_TWO_SECONDS,
    ]
    assert all(c["same_event"]["claims"] for c in conflicts)


def test_the_entry_kinds_that_exist_are_rendered() -> None:
    """Events, interventions, diagnostics and document statements; configuration changes are a
    section of their own. Zone traversals and episode boundaries wait for G3 (MVL-136/137)."""
    pack = incident(INC_0007, INC_0007_HOUR)
    timeline = _section(pack, "reconstruction")
    kinds = set().union(*(_kinds(e) for e in timeline.entries))
    assert kinds == {"incident", "intervention", "protective_stop", "warning"}
    (intervention,) = [e for e in timeline.entries if e.node == INTERVENTION]
    end = intervention.valid.end
    assert isinstance(end, Stamp)
    assert end.ticks - intervention.valid.start.ticks == 300 * corpus.SECOND
    (warning,) = [e for e in timeline.entries if e.node == SYSLOG_WARN]
    assert {"declared_kind", "in_zone"} <= {s.claim.predicate for s in warning.statements}
    # The report and its statements are on the report's own clock: listed, not placed.
    not_placed = {e.node.node_id for e in timeline.other_clocks}
    assert INC_0007_REPORT.node_id in not_placed
    assert {f"{INC_0007_REPORT.node_id}/timeline/{i}" for i in range(4)} <= not_placed
    (changes,) = _section(pack, "configuration-changes").entries
    assert changes.statements[0].claim.predicate == "succeeds"


@pytest.mark.skip(reason="the G3 gate walk (zone traversals, MVL-136/137) is not built")
def test_the_warehouse_reconstruction_matches_the_g3_gate_walk() -> None:
    raise AssertionError("G3")


def test_an_unmapped_clock_is_not_placed_and_its_mapping_is_absent() -> None:
    timeline = _section(incident(INC_0007, INC_0007_HOUR), "reconstruction")
    (fault,) = [e for e in timeline.other_clocks if e.node == CONTROLLER_FAULT]
    assert fault.valid.start.domain == corpus.AMR07_CTRL
    assert fault.knowledge == "known"
    pdf = render_pdf(incident(INC_0007, INC_0007_HOUR))
    assert b"NOT PLACED - Memory states no placement of these on the pack clock" in pdf


def test_restated_own_clock_claims_are_counted_not_dropped() -> None:
    timeline = _section(incident(INC_0007, INC_0007_HOUR), "reconstruction")
    # The two syslog events' statements on the syslog clock, each restated on the CMMS clock.
    assert timeline.other_clock_restated == 5 + 6
    assert plain(timeline.to_json())["excluded"]["other_clock_restated"] == 11


def test_a_candidate_identity_is_ambiguous_and_never_joined() -> None:
    pack = incident(INC_0007, INC_0007_HOUR)
    records = _section(pack, "records-of-the-incident")
    assert sorted(e.knowledge for e in records.entries) == ["ambiguous", "known"]
    timeline = _section(pack, "reconstruction")
    assert all(INC_0007_REPORT not in e.identity for e in timeline.entries)


def _without(doc: dict[str, Any], keep: Any) -> dict[str, Any]:
    doc["claims"] = [c for c in doc["claims"] if keep(c)]
    return doc


def test_without_a_mapping_the_syslog_is_not_placed_and_nothing_conflicts() -> None:
    doc = _without(
        document(),
        lambda c: (
            not (
                c["subject"]["node_id"] == SYSLOG_PSTOP.node_id
                and c["valid"]["start"]["domain_id"] == corpus.S7_LIFE
            )
        ),
    )
    snap = read_snapshot(doc)
    timeline = _section(incident(INC_0007, INC_0007_HOUR, snap=snap), "reconstruction")
    assert "conflict" not in {e.knowledge for e in timeline.entries}
    (syslog,) = [e for e in timeline.other_clocks if e.node == SYSLOG_PSTOP]
    assert syslog.valid.start.domain == corpus.S7_SYSLOG


def test_two_records_agreeing_on_the_time_corroborate_without_conflict() -> None:
    doc = document()
    for claim in doc["claims"]:
        if claim["subject"]["node_id"] == SYSLOG_PSTOP.node_id and (
            claim["valid"]["start"]["domain_id"] == corpus.S7_LIFE
        ):
            claim["valid"] = {
                "end": {"domain_id": corpus.S7_LIFE, "ticks": corpus.wall(2026, 4, 2, 14, 7) + 1},
                "start": {"domain_id": corpus.S7_LIFE, "ticks": corpus.wall(2026, 4, 2, 14, 7)},
            }
    timeline = _section(
        incident(INC_0007, INC_0007_HOUR, snap=read_snapshot(doc)), "reconstruction"
    )
    joined = [e for e in timeline.entries if e.identity]
    assert [e.knowledge for e in joined] == ["known", "known"]
    assert all(e.differences == () for e in joined)


def test_an_inferred_identity_joins_only_when_inference_is_included() -> None:
    doc = document()
    (link,) = [c for c in doc["claims"] if c["predicate"] == "same_as"]
    link["assertion_kind"] = "inferred"
    link["confidence"] = {"knowledge": "known", "value": 0.8}
    link["provenance"]["model"] = {"model_id": "incident-linker", "model_version": "2"}
    snap = read_snapshot(doc)
    excluded = _section(incident(INC_0007, INC_0007_HOUR, snap=snap), "reconstruction")
    assert "conflict" not in {e.knowledge for e in excluded.entries}
    assert link["id"] in excluded.excluded_inferred
    included = _section(
        incident(INC_0007, INC_0007_HOUR, snap=snap, inference="include"), "reconstruction"
    )
    assert [e.knowledge for e in included.entries].count("conflict") == 2


def test_the_reconstruction_is_for_an_event() -> None:
    with pytest.raises(PackError) as caught:
        compile_pack(spec("incident-timeline", ARM, INC_C3_HOUR), snapshot())
    assert caught.value.code == "subject_type_unsupported"


def test_an_event_nothing_is_claimed_about_is_not_covered() -> None:
    from neptune_deploy.packs.snapshot import Node

    pack = incident(Node("event", "record:rec:sha256:" + "0" * 64))
    assert {s.knowledge for s in pack.sections} == {"not_covered"}


# --- Exports, determinism, the sample ------------------------------------------------------------


def test_the_claim_set_exports_as_a_graph_schema_claims_result() -> None:
    check = validator("ClaimsResult")
    for pack in demo_packs().values():
        exported = json.loads(render_claims(pack))
        check.validate(exported)
        assert exported["as_of"] == pack.snapshot.head
        assert sorted(c["id"] for c in (*exported["claims"], *exported["other_clocks"])) == [
            c.id for c in pack.claims
        ]
        assert all(c["valid"]["start"]["domain_id"] == pack.spec.clock for c in exported["claims"])


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_double_render_is_byte_identical() -> None:
    first, second = demo_packs(), demo_packs()
    for name in first:
        for _kind, render in RENDERERS:
            assert render(first[name]) == render(second[name])


def test_renders_are_identical_across_processes_and_hash_seeds() -> None:
    script = (
        "import hashlib, sys; sys.path.insert(0, sys.argv[1]);"
        "from deploy_pack_demo import demo_packs;"
        "from neptune_deploy.packs import render_claims, render_json, render_pdf;"
        "packs = demo_packs();"
        "print(*[hashlib.sha256(r(packs[n])).hexdigest() for n in sorted(packs)"
        " for r in (render_claims, render_json, render_pdf)])"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", script, str(TESTS)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        for seed in ("0", "7", "31337")
    }
    assert len(runs) == 1


def test_golden_digests() -> None:
    packs = demo_packs()
    found = {
        f"{name}.{kind}": _digest(render(pack))
        for name, pack in packs.items()
        for kind, render in RENDERERS
    }
    assert found == GOLDEN


def test_the_committed_sample_is_what_the_compiler_renders() -> None:
    assert SAMPLE.read_bytes() == sample_bytes()
    assert len(SAMPLE.read_bytes()) < 200 * 1024


def test_cli_writes_the_claim_set_beside_the_pack(tmp_path: Path) -> None:
    pack = incident(INC_0007, INC_0007_HOUR)
    spec_file, snapshot_file = tmp_path / "spec.json", tmp_path / "graph.json"
    spec_file.write_text(json.dumps(pack.spec.to_json()), encoding="utf-8")
    snapshot_file.write_bytes(corpus.fixture_path().read_bytes())
    out = tmp_path / "out"
    argv = ["pack", "--spec", str(spec_file), "--snapshot", str(snapshot_file), "--out", str(out)]
    assert main(argv) == 0
    assert sorted(p.name for p in out.iterdir()) == ["claims.json", "pack.json", "pack.pdf"]
    assert (out / "claims.json").read_bytes() == render_claims(pack)
