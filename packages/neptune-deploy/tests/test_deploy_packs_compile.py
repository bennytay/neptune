"""Compiling configuration-lineage packs: ambiguity shown, never guessed; every statement cited."""

import copy
import dataclasses
import json
from typing import Any

import pytest

import deploy_pack_graphs as graphs
from deploy_pack_graphs import CIVIL, DAY, RUN1_CLOCK, T0, fixture_path, rec
from deploy_pack_support import (
    AMR,
    ARM,
    DEPLOYMENT,
    FROM_T0,
    SITE,
    configuration,
    configuration_pack,
    plain,
    spec,
)
from neptune_deploy.packs import (
    EvidencePack,
    PackError,
    builtin_registry,
    compile_pack,
    pack_id,
    read_snapshot,
    read_template,
    render_json,
    render_pdf,
)
from neptune_deploy.packs.compile import Section
from neptune_deploy.packs.snapshot import Interval, Node, Snapshot, Stamp


def _section(pack: EvidencePack, section_id: str) -> Section:
    (section,) = [s for s in pack.sections if s.template.id == section_id]
    return section


def _objects(entry: Any) -> list[str]:
    return [
        str(s.claim.object.get("node_id") or s.claim.object.get("record_id"))
        for s in entry.statements
    ]


def test_an_ambiguous_configuration_renders_every_candidate_and_chooses_none() -> None:
    section = _section(configuration_pack(), "configuration-in-force")
    states = [(e.knowledge, e.valid.start.ticks, _objects(e)) for e in section.entries]
    assert states == [
        ("known", T0, ["cmms.config:ARM06-A"]),
        ("ambiguous", T0 + DAY, ["cmms.config:ARM06-B", "cmms.config:ARM06-C·Ω"]),
        ("unknown", T0 + 2 * DAY, [rec("maintenance work order WO-340")]),
        ("known", T0 + 3 * DAY, ["cmms.config:ARM06-D"]),
    ]
    ambiguous = section.entries[1]
    assert {s.role for s in ambiguous.statements} == {"ambiguous"}
    # No has_configuration is invented for the ambiguous span or the gap.
    for entry in section.entries[1:3]:
        assert all(s.claim.predicate != "has_configuration" for s in entry.statements)


def test_the_ambiguity_is_in_the_json_as_data() -> None:
    document = json.loads(render_json(configuration_pack()))
    (section,) = [s for s in document["sections"] if s["id"] == "configuration-in-force"]
    ambiguous = section["entries"][1]
    assert ambiguous["knowledge"] == "ambiguous"
    assert [s["knowledge"] for s in ambiguous["statements"]] == ["ambiguous", "ambiguous"]
    assert all(len(s["claims"]) == 1 for s in ambiguous["statements"])


def test_superseded_versions_are_not_stated_but_their_finding_is_shown() -> None:
    pack = configuration_pack()
    section = _section(pack, "configuration-in-force")
    shown = {i for e in section.entries for i in e.claim_ids}
    superseded = next(c for c in configuration().claims if not c.current)
    assert superseded.id not in shown
    (finding,) = section.findings
    assert finding.code == "overridden_on_arrival"
    assert finding.claim == superseded.id
    # The finding's claims are in the pack's claim set, so the reader can look them up.
    assert {finding.claim, *finding.others} <= {c.id for c in pack.claims}


def test_every_statement_cites_a_claim_of_the_pack() -> None:
    for pack in (configuration_pack(), configuration_pack(subject=SITE, inference="include")):
        document = json.loads(render_json(pack))
        claim_ids = {c["id"] for c in document["claims"]}
        for section in document["sections"]:
            for entry in (*section["entries"], *section["other_clocks"]):
                for statement in entry["statements"]:
                    assert statement["claims"]
                    assert set(statement["claims"]) <= claim_ids
            for node in section["scope"]:
                assert set(node["via"]) <= claim_ids
            if section["knowledge"] != "known":
                assert section["reason"]


def test_claims_on_another_clock_are_listed_never_compared() -> None:
    section = _section(configuration_pack(), "run-configuration")
    assert section.entries == ()
    (run1,) = section.other_clocks
    assert run1.valid.start.domain == RUN1_CLOCK
    assert run1.knowledge == "known"
    # Narrowing the pack interval on the civil clock does not drop a run-clock claim.
    narrow = configuration_pack(interval=Interval(Stamp(CIVIL, T0), Stamp(CIVIL, T0 + 1)))
    assert _section(narrow, "run-configuration").other_clocks == section.other_clocks


def test_claims_on_the_pack_clock_outside_the_interval_are_counted() -> None:
    pack = configuration_pack(interval=Interval(Stamp(CIVIL, T0 + DAY), Stamp(CIVIL, T0 + 2 * DAY)))
    section = _section(pack, "configuration-in-force")
    assert [e.knowledge for e in section.entries] == ["ambiguous"]
    assert section.outside_interval == 3


def test_inference_is_excluded_by_default_and_counted() -> None:
    pack = configuration_pack()
    assert pack.excluded_inferred == 1
    assert pack.included_inferred == 0
    assert all(not c.inferred for c in pack.claims)
    section = _section(pack, "run-configuration")
    runs = {n.node.node_id for n in section.scope}
    assert f"record:{rec('run 3')}" not in runs  # reached only through an inferred link
    assert len(section.excluded_inferred) == 1


def test_included_inference_is_marked_everywhere() -> None:
    pack = configuration_pack(inference="include")
    assert pack.included_inferred == 1
    section = _section(pack, "run-configuration")
    run3 = next(n for n in section.scope if n.node.node_id == f"record:{rec('run 3')}")
    (link,) = run3.via
    assert next(c for c in pack.claims if c.id == link).inferred
    document = json.loads(render_json(pack))
    assert document["inference"] == {"excluded": 0, "included": 1, "policy": "include"}
    inferred = next(c for c in document["claims"] if c["id"] == link)
    assert inferred["assertion_kind"] == "inferred"


def test_an_inferred_statement_carries_its_model_and_confidence() -> None:
    template = json.loads(
        json.dumps(builtin_registry().get("configuration-lineage", 1).sections[0].to_json())
    )
    template.update(
        id="links",
        kind="claims",
        about=[[{"predicate": "recorded_by", "direction": "in"}]],
        predicates={"recorded_by": "known"},
        subject_types=["machine"],
    )
    document = {
        "schema": "neptune-deploy.pack-template/1",
        "id": "run-links",
        "version": 1,
        "title": "Run links",
        "description": "Which runs a machine recorded.",
        "subject_types": ["machine"],
        "sections": [template],
    }
    registry = builtin_registry().with_template(read_template(plain(document)))
    snap = configuration()
    included = compile_pack(spec(snap, "run-links", inference="include"), snap, registry)
    statements = [s.to_json() for e in included.sections[0].other_clocks for s in e.statements]
    marked = [s for s in statements if "inferred" in s]
    assert marked == [
        {
            **marked[0],
            "inferred": {
                "confidence": {"knowledge": "known", "value": 0.6},
                "model": {"model_id": "log-attribution", "model_version": "3"},
            },
        }
    ]
    excluded = compile_pack(spec(snap, "run-links"), snap, registry)
    assert all(
        not s.claim.inferred for e in excluded.sections[0].other_clocks for s in e.statements
    )


def test_a_site_pack_reaches_machines_located_at_it() -> None:
    pack = configuration_pack(subject=SITE)
    in_force = _section(pack, "configuration-in-force")
    assert {e.node for e in in_force.entries} == {ARM, AMR}
    (succession,) = _section(pack, "configuration-succession").entries
    assert succession.statements[0].claim.predicate == "succeeds"
    (authorised,) = _section(pack, "authorisation").entries
    assert authorised.node == SITE


def test_a_deployment_pack_reaches_its_site_and_machines() -> None:
    pack = configuration_pack(subject=DEPLOYMENT)
    assert {e.node for e in _section(pack, "configuration-in-force").entries} == {ARM, AMR}
    assert _section(pack, "authorisation").entries[0].node == SITE


def test_a_subject_with_no_claims_is_not_covered_with_its_reason() -> None:
    lone = Node("machine", "asset-tag:QUAD-02")
    pack = configuration_pack(subject=lone)
    for section in pack.sections:
        assert section.knowledge == "not_covered"
        assert section.reason is not None
        assert section.reason["snapshot"] == configuration().id
        assert section.reason["predicates"] == sorted(section.template.predicates)
    assert _section(pack, "configuration-in-force").reason == {
        "inferred_excluded": 0,
        "missing_from_vocabulary": [],
        "nodes": [lone.to_json()],
        "outside_interval": 0,
        "predicates": ["configuration_candidate", "configuration_unknown", "has_configuration"],
        "snapshot": configuration().id,
    }


def test_event_sections_over_a_snapshot_without_the_event_vocabulary_say_so() -> None:
    snap = configuration()
    pack = compile_pack(spec(snap, "event-timeline"), snap)
    events = _section(pack, "events")
    assert events.knowledge == "not_covered"
    assert events.reason is not None
    assert "event_kind" in events.reason["missing_from_vocabulary"]  # type: ignore[operator]


def test_a_section_for_other_subject_types_is_not_applicable() -> None:
    template = json.loads(
        json.dumps(builtin_registry().get("configuration-lineage", 1).sections[2].to_json())
    )
    template["subject_types"] = ["site"]
    document = {
        "schema": "neptune-deploy.pack-template/1",
        "id": "site-authorisation",
        "version": 1,
        "title": "Site authorisation",
        "description": "Authorised configurations at a site.",
        "subject_types": ["machine", "site"],
        "sections": [template],
    }
    registry = builtin_registry().with_template(read_template(plain(document)))
    snap = configuration()
    (section,) = compile_pack(spec(snap, "site-authorisation"), snap, registry).sections
    assert section.knowledge == "not_applicable"
    assert section.reason == {"section_subject_types": ["site"], "subject_type": "machine"}


def test_refusals() -> None:
    snap = configuration()
    with pytest.raises(PackError) as caught:
        compile_pack(spec(snap), dataclasses.replace(snap, id="snapshot:sha256:" + "0" * 64))
    assert caught.value.code == "snapshot_mismatch"
    with pytest.raises(PackError) as caught:
        compile_pack(spec(snap, "configuration-lineage", version=9), snap)
    assert caught.value.code == "template_unknown"
    document = json.loads(
        json.dumps(builtin_registry().get("configuration-lineage", 1).sections[0].to_json())
    )
    narrow = {
        "schema": "neptune-deploy.pack-template/1",
        "id": "machines-only",
        "version": 1,
        "title": "Machines only",
        "description": "Only machines.",
        "subject_types": ["machine"],
        "sections": [{**document, "subject_types": ["machine"]}],
    }
    registry = builtin_registry().with_template(read_template(plain(narrow)))
    with pytest.raises(PackError) as caught:
        compile_pack(spec(snap, "machines-only", subject=SITE), snap, registry)
    assert caught.value.code == "subject_type_unsupported"


def test_pack_id_names_spec_template_and_compiler() -> None:
    snap = configuration()
    base = spec(snap)
    template = builtin_registry().get("configuration-lineage", 1)
    assert configuration_pack().id == pack_id(base, template)
    assert configuration_pack().id.startswith("pack:sha256:")
    variants = {
        configuration_pack().id,
        configuration_pack(inference="include").id,
        configuration_pack(subject=SITE).id,
        configuration_pack(interval=Interval(Stamp(CIVIL, T0), Stamp(CIVIL, T0 + DAY))).id,
    }
    assert len(variants) == 4


def test_appendix_resolves_every_cited_evidence_ref_and_record() -> None:
    pack = configuration_pack(subject=SITE)
    refs = {json.dumps(r, sort_keys=True) for c in pack.claims for r in c.evidence}
    appendix = plain(pack.appendix.to_json())
    listed = {json.dumps(e["ref"], sort_keys=True) for e in appendix["evidence"]}
    assert listed == refs
    records = {r for c in pack.claims for r in c.records} | {
        c.object_record for c in pack.claims if c.object_record
    }
    assert {e["record_id"] for e in appendix["records"]} == records
    vias = {e["resolve"]["via"] for e in appendix["evidence"]}
    assert vias == {"ledger", "connector", "none"}
    external = next(e for e in appendix["evidence"] if e["resolve"]["via"] == "connector")
    assert external["resolve"]["connector_id"] == "deploy_confluence"
    ledger = next(e for e in appendix["evidence"] if e["resolve"]["via"] == "ledger")
    (call,) = ledger["resolve"]["calls"]
    assert call["request"] == {"as_of": configuration().head, "evidence_ref": ledger["ref"]}


def test_scope_hops_cite_their_claims() -> None:
    pack = configuration_pack(subject=SITE)
    section = _section(pack, "configuration-in-force")
    by_node = {s.node: s.via for s in section.scope}
    assert by_node[SITE] == ()
    (located,) = by_node[ARM]
    assert next(c for c in pack.claims if c.id == located).predicate == "located_at"
    assert FROM_T0.start.domain == CIVIL


def _with(document: dict[str, Any], *claims: dict[str, Any]) -> Snapshot:
    document = copy.deepcopy(document)
    document["claims"].extend(claims)
    return read_snapshot(document)


def test_an_inferred_claim_named_by_a_finding_stays_out_under_exclude() -> None:
    """Review finding: a finding's other claims never smuggle an excluded inference in."""
    document = plain(json.loads(fixture_path("arm_cell_configuration").read_bytes()))
    inferred = next(c for c in document["claims"] if c["assertion_kind"] == "inferred")
    document["findings"][0]["others"].append(inferred["id"])
    snap = read_snapshot(document)
    excluded = compile_pack(spec(snap), snap)
    assert inferred["id"] not in {c.id for c in excluded.claims}
    assert excluded.included_inferred == 0
    assert all(
        inferred["id"] not in e["claims"] for e in plain(excluded.appendix.to_json())["evidence"]
    )
    finding = _section(excluded, "configuration-in-force").findings[0]
    assert inferred["id"] in finding.others  # the finding itself is shown whole
    assert f"{inferred['id']} [INFERRED:excluded]".encode() in render_pdf(excluded)
    included = compile_pack(spec(snap, inference="include"), snap)
    assert inferred["id"] in {c.id for c in included.claims}


def test_overlapping_spans_of_a_one_predicate_are_a_conflict() -> None:
    """Review finding: two objects of a ``one`` predicate over overlapping spans conflict."""
    other_site = graphs.node("site", "site-code:CELL-4")
    moved = graphs.claim(
        graphs.ARM,
        "located_at",
        other_site,
        (graphs.at(CIVIL, T0 + DAY), "open"),
        records=("asset register ARM-06 rev 2",),
        evidence=(graphs.row("assets.csv", 9),),
        consolidator="memory.identity",
    )
    document = json.loads(fixture_path("arm_cell_configuration").read_bytes())
    snap = _with(document, moved)
    section_json = plain(builtin_registry().get("configuration-lineage", 1).sections[0].to_json())
    section_json.update(
        id="whereabouts", predicates={"located_at": "known"}, about=[[]], subject_types=["machine"]
    )
    template = {
        "schema": "neptune-deploy.pack-template/1",
        "id": "whereabouts",
        "version": 1,
        "title": "Whereabouts",
        "description": "Where a machine is.",
        "subject_types": ["machine"],
        "sections": [section_json],
    }
    registry = builtin_registry().with_template(read_template(plain(template)))
    (section,) = compile_pack(spec(snap, "whereabouts"), snap, registry).sections
    assert [e.knowledge for e in section.entries] == ["conflict", "conflict"]
    # Without the overlap there is nothing to flag.
    snap = _with(document)
    (section,) = compile_pack(spec(snap, "whereabouts"), snap, registry).sections
    assert [e.knowledge for e in section.entries] == ["known"]
