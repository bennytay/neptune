"""``incident-timeline@3`` and ``configuration-traceability@3`` (Deploy ADR 0019): a machine is read
under every id a stated or observed ``same_as`` chain joins to it, the ``same_as`` claims are cited,
an inferred link is never followed, and a machine nothing links is shown as not linked."""

import json
from functools import cache
from typing import Any, Final

import pytest

from deploy_pack_graphs import (
    CIVIL,
    HOUR,
    T0,
    VOCABULARY_2_0,
    at,
    claim,
    config,
    event,
    graph,
    node,
    record,
    row,
    run,
    text,
)
from deploy_pack_support import FROM_T0, spec
from neptune_deploy.packs import (
    EvidencePack,
    PackError,
    Snapshot,
    compile_pack,
    read_snapshot,
    read_template,
    render_json,
    render_pdf,
)
from neptune_deploy.packs.compile import Section
from neptune_deploy.packs.pdf import literal
from neptune_deploy.packs.snapshot import Node
from neptune_deploy.packs.text import winansi

# One arm under the ids its sources give it, as Memory's identity consolidator joins a manifest
# machine's declared ids (Memory ADR 0021): the lowest id is the hub, stated same_as each other.
HUB: Final = node("machine", "cmms.asset:ARM-3A")
REPORTED: Final = node("machine", "incident_report.machine:ARM-3A")  # what the incident names
MANIFEST: Final = node("machine", "manifest:ARM-3A")  # what the runs and configuration name
LOOKALIKE: Final = node("machine", "syslog.host:ARM-3A")  # the same text, linked by nothing
GUESSED: Final = node("machine", "manifest:ARM-9")  # linked only by an inferred claim
INCIDENT: Final = event("INC-C3-0011")
STOP: Final = event("DT-26-0914-01")  # a CMMS stop of the hub id, 32 s after the incident
RUN: Final = run("pallet 2026-09-14")
ONSET: Final = T0 + 14 * HOUR


def _as(value: dict[str, Any]) -> Node:
    return Node(value["node_type"], value["node_id"])


def _document(*, linked: bool = True, inferred: bool = True) -> dict[str, Any]:
    identity: dict[str, Any] = {"consolidator": "memory.identity", "consolidator_version": "4"}
    events: dict[str, Any] = {"consolidator": "memory.events", "consolidator_version": "1"}
    placed = (at(CIVIL, T0), "open")

    def same(spoke: dict[str, Any], line: int) -> dict[str, Any]:
        return claim(
            HUB,
            "same_as",
            spoke,
            placed,
            records=("manifest machine ARM-3A",),
            evidence=(row("neptune.yaml", line),),
            **identity,
        )

    claims = [same(REPORTED, 1), same(MANIFEST, 2)] if linked else []
    if inferred:
        claims.append(
            claim(
                REPORTED,
                "same_as",
                GUESSED,
                placed,
                records=("incident INC-C3-0011",),
                evidence=(row("incidents.csv", 1),),
                kind="inferred",
                model={"model_id": "lookalike-ids", "model_version": "0.1"},
                confidence=0.6,
                **identity,
            )
        )
    cfg_a, cfg_9, cfg_x = config("ARM-3A-A"), config("ARM-9-A"), config("SYSLOG-HOST-A")
    for machine, obj, line in ((MANIFEST, cfg_a, 3), (GUESSED, cfg_9, 4), (LOOKALIKE, cfg_x, 5)):
        claims.append(
            claim(
                machine,
                "has_configuration",
                obj,
                placed,
                records=(f"configuration {line}",),
                evidence=(row("configs.csv", line),),
                recorded_at=2,
                consolidator_version="2",
            )
        )
    claims += [
        claim(
            RUN,
            "recorded_by",
            MANIFEST,
            (at(CIVIL, ONSET - HOUR), at(CIVIL, ONSET + HOUR)),
            records=("bag pallet 2026-09-14",),
            evidence=(row("runs.csv", 1),),
            recorded_at=2,
        ),
        claim(
            RUN,
            "configuration_active_during",
            cfg_a,
            (at(CIVIL, ONSET - HOUR), at(CIVIL, ONSET + HOUR)),
            records=("run sheet pin",),
            evidence=(row("runs.csv", 1),),
            recorded_at=2,
        ),
    ]
    for subject, machine, onset, name in (
        (INCIDENT, REPORTED, ONSET, "incident INC-C3-0011"),
        (STOP, HUB, ONSET + 32 * 10**9, "downtime DT-26-0914-01"),
    ):
        for predicate, obj in (
            ("event_kind", text("protective_stop")),
            ("involves", machine),
            ("evidenced_by", record(name)),
        ):
            claims.append(
                claim(
                    subject,
                    predicate,
                    obj,
                    (at(CIVIL, onset), at(CIVIL, onset + 1)),
                    records=(name,),
                    evidence=(row("events.csv", 1),),
                    recorded_at=3,
                    **events,
                )
            )
    vocabulary = json.loads(VOCABULARY_2_0.read_text(encoding="utf-8"))
    return graph(claims, [], 3, vocabulary, 11, release="2.0.0")


@cache
def _snapshot(linked: bool = True, inferred: bool = True) -> Snapshot:
    return read_snapshot(_document(linked=linked, inferred=inferred))


def _pack(
    template: str, subject: Node, version: int, snap: Snapshot, inference: str = "exclude"
) -> EvidencePack:
    return compile_pack(spec(snap, template, subject, FROM_T0, inference, version), snap)


def _section(pack: EvidencePack, section_id: str) -> Section:
    return next(s for s in pack.sections if s.template.id == section_id)


def _nodes(section: Section) -> set[Node]:
    return {e.node for e in (*section.entries, *section.other_clocks)}


def _scoped(pack: EvidencePack) -> set[Node]:
    return {s.node for section in pack.sections for s in section.scope}


def _same_as_ids(snap: Snapshot, *, inferred: bool) -> set[str]:
    return {
        c.id for c in snap.versions.values() if c.predicate == "same_as" and c.inferred == inferred
    }


def test_the_incident_reads_its_machine_under_every_stated_id_and_cites_the_links() -> None:
    snap = _snapshot()
    pack = _pack("incident-timeline", _as(INCIDENT), 3, snap)
    stated = _same_as_ids(snap, inferred=False)
    configuration = _section(pack, "configuration-in-force")
    assert configuration.knowledge == "known"
    assert _nodes(configuration) == {_as(MANIFEST)}
    reached = {s.node: set(s.via) for s in configuration.scope}
    assert stated <= reached[_as(MANIFEST)]  # the chain incident -> report id -> hub -> manifest
    assert _nodes(_section(pack, "runs")) == {_as(RUN)}
    # The CMMS stop on the hub id is in the reconstruction; the identities are listed.
    assert _as(STOP) in _nodes(_section(pack, "reconstruction"))
    identities = _section(pack, "machine-identities")
    assert identities.knowledge == "known"
    assert {s.claim.id for e in identities.entries for s in e.statements} == stated
    # Every same_as claim followed is in the claim set and so in the provenance appendix.
    assert stated <= {c.id for c in pack.claims}
    cited = {i for item in json.loads(json.dumps(pack.appendix.evidence)) for i in item["claims"]}
    assert stated <= cited
    # The look-alike id and the inferred link's machine are never read.
    assert not {_as(LOOKALIKE), _as(GUESSED)} & _scoped(pack)


def test_the_traceability_report_reads_the_machine_under_its_linked_ids() -> None:
    snap = _snapshot()
    pack = _pack("configuration-traceability", _as(REPORTED), 3, snap)
    assert _nodes(_section(pack, "configuration-chain")) == {_as(MANIFEST)}
    assert _nodes(_section(pack, "runs")) == {_as(RUN)}
    assert _section(pack, "machine-identities").knowledge == "known"
    assert _same_as_ids(snap, inferred=False) <= {c.id for c in pack.claims}


def test_the_v2_templates_still_read_only_the_named_id() -> None:
    """@2 is unchanged (a registered version never changes): over the same graph it does not
    follow the machine's same_as, so the configuration of the involved machine is not covered."""
    snap = _snapshot()
    pack = _pack("incident-timeline", _as(INCIDENT), 2, snap)
    assert _section(pack, "configuration-in-force").knowledge == "not_covered"
    assert _section(pack, "runs").knowledge == "not_covered"
    assert "machine-identities" not in {s.template.id for s in pack.sections}
    trace = _pack("configuration-traceability", _as(REPORTED), 2, snap)
    assert _section(trace, "configuration-chain").knowledge == "not_covered"


def test_a_machine_no_same_as_links_is_not_linked_never_guessed() -> None:
    snap = _snapshot(linked=False, inferred=False)
    pack = _pack("incident-timeline", _as(INCIDENT), 3, snap)
    identities = _section(pack, "machine-identities")
    assert identities.knowledge == "not_covered"
    assert identities.reason is not None
    assert identities.reason["nodes"] == [_as(REPORTED).to_json()]
    assert identities.reason["predicates"] == ["same_as", "same_as_candidate"]
    for section_id in ("configuration-in-force", "configuration-changes", "runs"):
        assert _section(pack, section_id).knowledge == "not_covered", section_id
    # ARM-3A under another namespace is never taken as the same machine.
    assert _scoped(pack) & {_as(MANIFEST), _as(HUB), _as(LOOKALIKE)} == set()
    pdf = render_pdf(pack)
    assert literal(winansi("not linked"))[1:-1] in pdf
    assert (
        literal(winansi("NOT COVERED - the snapshot holds no current claim of same_as"))[1:-1]
        in pdf
    )
    trace = _pack("configuration-traceability", _as(REPORTED), 3, snap)
    assert _section(trace, "machine-identities").knowledge == "not_covered"
    assert _section(trace, "configuration-chain").knowledge == "not_covered"


@pytest.mark.parametrize("inference", ["exclude", "include"])
def test_an_inferred_same_as_is_never_followed(inference: str) -> None:
    """Even a spec that includes inference reads no machine through an inferred link: identity
    is stated or observed only (Memory states inferred identity as candidates, never same_as)."""
    snap = _snapshot()
    pack = _pack("incident-timeline", _as(INCIDENT), 3, snap, inference)
    assert _as(GUESSED) not in _scoped(pack)
    assert _nodes(_section(pack, "configuration-in-force")) == {_as(MANIFEST)}
    guessed = _same_as_ids(snap, inferred=True)
    vias = {i for section in pack.sections for s in section.scope for i in s.via}
    assert not guessed & vias
    if inference == "exclude":
        assert not guessed & {c.id for c in pack.claims}
        assert pack.excluded_inferred >= 1


def test_the_v3_packs_are_deterministic() -> None:
    for template, subject in (
        ("incident-timeline", _as(INCIDENT)),
        ("configuration-traceability", _as(MANIFEST)),
    ):
        first = _pack(template, subject, 3, read_snapshot(_document()))
        again = _pack(template, subject, 3, read_snapshot(_document()))
        assert render_json(first) == render_json(again)
        assert render_pdf(first) == render_pdf(again)
        assert first.id != _pack(template, subject, 2, _snapshot()).id


def _ids_template(direction: str) -> dict[str, Any]:
    return {
        "schema": "neptune-deploy.pack-template/1",
        "id": "ids",
        "version": 1,
        "title": "Ids",
        "description": "A machine's ids.",
        "subject_types": ["machine"],
        "sections": [
            {
                "id": "ids",
                "title": "Ids",
                "description": "Its same_as.",
                "kind": "claims",
                "about": [[{"predicate": "same_as", "direction": direction}]],
                "predicates": {"same_as": "known"},
            }
        ],
    }


def test_closure_is_a_hop_direction_and_others_are_refused() -> None:
    template = read_template(_ids_template("closure"))
    assert template.sections[0].about[0][0].direction == "closure"
    with pytest.raises(PackError) as caught:
        read_template(_ids_template("both"))
    assert (caught.value.code, caught.value.pointer) == (
        "template_malformed",
        "/sections/0/about/0/0/direction",
    )
