"""graph-schema 2.x snapshots (Deploy ADR 0018): the reader takes the release from the document,
the @2 templates read a machine's changes from its own spans (rule 12), and a section that does
not read the snapshot's major is ``section_not_covered``, never an empty "no changes"."""

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from deploy_pack_graphs import (
    AMR05,
    AMR06,
    CIVIL,
    DAY,
    DOCK,
    INCIDENT,
    PALLET_ARM,
    QUAD,
    QUAD_CLOCK,
    T0,
    fixture_path,
)
from deploy_pack_support import CONTRACTS, FROM_T0, configuration, plain, schema_path, spec
from neptune_deploy.lifecycle.cli import main
from neptune_deploy.packs import (
    EvidencePack,
    PackError,
    Snapshot,
    compile_pack,
    load_snapshot,
    read_snapshot,
    render_claims,
    render_json,
    render_pdf,
)
from neptune_deploy.packs.compile import Section
from neptune_deploy.packs.pdf import literal
from neptune_deploy.packs.snapshot import Interval, Node, Stamp
from neptune_deploy.packs.text import winansi

FIXTURE = "dock_fleet_configuration_v2"
GOLDEN_2 = CONTRACTS / "graph-schema" / "v2.0.0" / "golden" / "graph.json"
NODE = {
    name: Node(value["node_type"], value["node_id"])
    for name, value in {
        "AMR05": AMR05,
        "AMR06": AMR06,
        "ARM": PALLET_ARM,
        "QUAD": QUAD,
        "DOCK": DOCK,
        "INCIDENT": INCIDENT,
    }.items()
}
# Digests of the 2.x packs. A change means what these packs say changed: explain it in the PR.
GOLDEN = {
    "incident.json": "sha256:844c068245d9fcacbe061d15a9dec2d396bfff43895f377aab191380bbb8dd51",
    "incident.pdf": "sha256:4e301026dbf1e7c26ad6a05f78615d6075cdde835f0ef8d6166c7c22d64b9d53",
    "lineage.claims": "sha256:b5dfcf92986308b57409fcfab2aeb37ee5940588d052c841655f1186f3d02831",
    "lineage.json": "sha256:4bb4779cc74ac251abca93d723c9d94d9485d5e994c2407602e1f8e3bddc4050",
    "lineage.pdf": "sha256:dd326ea0d0cfdcc1635b098c6b6b43b25fe75a75570781632141cc5c945c4210",
    "amr05.json": "sha256:9ca2d65865d8ae5b5cff48d71f1f4571a7bcdd4deb3153b412eb566f1523b4a7",
    "amr05.pdf": "sha256:4fbb95b1debb1f32f093447e5940f0acbf02d59b5ef2b0dbf9119546ad7bf08b",
}


def _shown(text: str) -> bytes:
    """How ``text`` appears inside a PDF string literal."""
    return literal(winansi(text))[1:-1]


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(fixture_path(FIXTURE).read_bytes())
    return document


@cache
def dock() -> Snapshot:
    return load_snapshot(fixture_path(FIXTURE).read_bytes())


def _pack(template: str, subject: Node, version: int, snap: Snapshot | None = None) -> EvidencePack:
    snap = snap or dock()
    return compile_pack(spec(snap, template, subject, FROM_T0, version=version), snap)


def _section(pack: EvidencePack, section_id: str) -> Section:
    return next(s for s in pack.sections if s.template.id == section_id)


def _changes(pack: EvidencePack) -> Section:
    return _section(pack, "configuration-changes")


# --- The fixture and the reader -----------------------------------------------------------------


def _validator(version: str) -> Draft202012Validator:
    schema = json.loads(schema_path("graph-schema", version).read_text(encoding="utf-8"))
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": "#/$defs/Graph"})


def test_the_fixture_is_a_graph_schema_2_document() -> None:
    document = _document()
    _validator("2.0.0").validate(document)
    with pytest.raises(ValidationError):
        _validator("1.6.0").validate(document)
    # Two machines share the 4.2.0 configuration node; one succeeds claim relates the two nodes.
    shared = [
        c["subject"]["node_id"]
        for c in document["claims"]
        if c["predicate"] == "has_configuration"
        and c["object"]["node_id"] == "cmms.config:fleet-nav-4.2.0"
    ]
    assert sorted(shared) == ["asset-tag:AMR-05", "asset-tag:AMR-06"]
    assert [c["predicate"] for c in document["claims"]].count("succeeds") == 1


def test_the_release_comes_from_the_document() -> None:
    snap = dock()
    assert (snap.major, snap.release) == (2, "2.0.0")
    assert [b["consolidator_id"] for b in snap.builds] == [
        "memory.identity",
        "memory.configuration",
        "memory.release_notes",
        "memory.events",
    ]
    assert snap.unread == () and snap.declared_schema_version is None
    # A matching declaration changes nothing; another release is refused.
    assert read_snapshot(_document(), schema_version="2.0.0").id == snap.id
    with pytest.raises(PackError) as caught:
        read_snapshot(_document(), schema_version="2.1.0")
    assert (caught.value.code, caught.value.pointer) == ("snapshot_unsupported", "/graph_schema")
    with pytest.raises(PackError) as caught:
        read_snapshot(_document(), schema_version="2.1")
    assert caught.value.code == "snapshot_unsupported"


def test_memorys_published_2_0_0_golden_graph_reads() -> None:
    snap = load_snapshot(GOLDEN_2.read_bytes())
    document = json.loads(GOLDEN_2.read_text(encoding="utf-8"))
    assert (snap.major, snap.release) == (2, document["graph_schema"])
    assert len(snap.claims) == len(document["claims"])
    assert list(snap.builds) == document["builds"]


def _mutated(path: list[Any], value: Any) -> dict[str, Any]:
    document = _document()
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return document


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value", "code", "pointer"),
    [
        (["graph_schema"], _DELETE, "snapshot_malformed", ""),
        (["graph_schema"], "1.9.0", "snapshot_malformed", "/graph_schema"),
        (["graph_schema"], "3.0.0", "snapshot_malformed", "/graph_schema"),
        (["graph_schema"], "2.0", "snapshot_malformed", "/graph_schema"),
        (["graph_schema"], 2, "snapshot_malformed", "/graph_schema"),
        (["graph_schema_version"], 3, "snapshot_unsupported", "/graph_schema_version"),
        (["builds"], [], "snapshot_malformed", "/builds"),
        (["builds", 0, "recorded_at"], 99, "snapshot_malformed", "/builds/0"),
        (["builds", 0, "claims", 0], "claim:1", "snapshot_malformed", "/builds/0/claims/0"),
        (["builds", 0, "version"], "", "snapshot_malformed", "/builds/0/version"),
        (["builds", 0, "note"], "x", "snapshot_malformed", "/builds/0"),
        (["claims", 0, "build_ref"], "x", "snapshot_malformed", "/claims/0"),
    ],
)
def test_malformed_2x_documents_are_refused(
    path: list[Any], value: Any, code: str, pointer: str
) -> None:
    with pytest.raises(PackError) as caught:
        read_snapshot(_mutated(path, value))
    assert (caught.value.code, caught.value.pointer) == (code, pointer)


def test_a_build_lists_each_claim_once() -> None:
    document = _document()
    build = document["builds"][0]
    build["claims"] = [build["claims"][0], build["claims"][0]]
    with pytest.raises(PackError, match="unique"):
        read_snapshot(document)


def test_a_1x_document_naming_a_release_is_refused() -> None:
    document = json.loads(fixture_path("arm_cell_configuration").read_bytes())
    document["graph_schema"] = "1.6.0"
    with pytest.raises(PackError) as caught:
        read_snapshot(document)
    assert caught.value.code == "snapshot_malformed"
    assert "graph_schema" in str(caught.value)


def test_a_newer_2x_minor_reports_the_keys_it_adds() -> None:
    """ADR 0015's rule, with the minor read from the document rather than declared."""
    document = _document()
    document["graph_schema"] = "2.1.0"
    for claim in document["claims"]:
        claim["provenance"]["derivation"] = "secret"
    snap = read_snapshot(document)
    assert [u.key_path for u in snap.unread] == ["/claims/*/provenance/derivation"]
    assert snap.declared_schema_version == "2.1.0" and snap.release == "2.1.0"
    pack = _pack("configuration-traceability", NODE["AMR05"], 2, snap)
    out = json.loads(render_json(pack))
    assert out["snapshot"]["graph_schema"] == "2.1.0"
    assert [f["code"] for f in out["findings"]] == ["snapshot_key_unread"]
    assert b"secret" not in render_json(pack) + render_pdf(pack) + render_claims(pack)


# --- @2: a change is one machine's own ----------------------------------------------------------


def _change_claims(section: Section) -> list[tuple[str, str, str]]:
    """(node, before object id, after object id) of each known change."""
    out = []
    for entry in section.entries + section.other_clocks:
        if entry.knowledge != "known":
            continue
        assert entry.change is not None
        before = next(s.claim for s in entry.statements if s.claim.id in entry.change.before)
        after = next(s.claim for s in entry.statements if s.claim.id in entry.change.after)
        out.append(
            (entry.node.node_id, str(before.object["node_id"]), str(after.object["node_id"]))
        )
    return out


def test_the_change_is_on_the_machine_that_changed_only() -> None:
    changed = _changes(_pack("configuration-traceability", NODE["AMR05"], 2))
    assert changed.knowledge == "known"
    assert _change_claims(changed) == [
        ("asset-tag:AMR-05", "cmms.config:fleet-nav-4.2.0", "cmms.config:fleet-nav-4.3.1")
    ]
    entry = changed.entries[0]
    assert entry.change is not None and entry.change.at.to_json() == {
        "domain_id": CIVIL,
        "ticks": T0 + DAY,
    }
    assert {s.claim.subject for s in entry.statements} == {NODE["AMR05"]}

    unchanged = _changes(_pack("configuration-traceability", NODE["AMR06"], 2))
    assert unchanged.knowledge == "not_covered" and unchanged.entries == ()
    assert unchanged.reason is not None
    assert unchanged.reason["spans_read"] == 1
    assert "code" not in unchanged.reason  # read, and no boundary: not a major it cannot read
    # The configuration chain still shows AMR-06 on the shared node, without a change.
    chain = _section(_pack("configuration-traceability", NODE["AMR06"], 2), "configuration-chain")
    assert [e.knowledge for e in chain.entries] == ["known"]


def test_a_fleet_pack_attributes_each_change_to_its_machine() -> None:
    pack = _pack("configuration-lineage", NODE["DOCK"], 2)
    section = _changes(pack)
    assert _change_claims(section) == [
        ("asset-tag:AMR-05", "cmms.config:fleet-nav-4.2.0", "cmms.config:fleet-nav-4.3.1"),
        ("asset-tag:QUAD-03", "cmms.config:QUAD03-gait-1", "cmms.config:QUAD03-gait-2"),
    ]
    for entry in (*section.entries, *section.other_clocks):
        assert {s.claim.subject for s in entry.statements} == {entry.node}
    assert NODE["AMR06"] not in {e.node for e in (*section.entries, *section.other_clocks)}
    # The legged robot changed on its own clock: listed apart, never placed on the pack clock.
    assert [e.node for e in section.other_clocks] == [NODE["QUAD"]]
    assert section.other_clocks[0].valid.start.domain == QUAD_CLOCK


def test_no_change_is_read_from_succeeds() -> None:
    succeeds = {c.id for c in dock().claims if c.predicate == "succeeds"}
    assert len(succeeds) == 1
    for template, subject in (
        ("configuration-lineage", "DOCK"),
        ("configuration-traceability", "AMR05"),
        ("configuration-traceability", "AMR06"),
        ("incident-timeline", "INCIDENT"),
    ):
        pack = _pack(template, NODE[subject], 2)
        assert not succeeds & {c.id for c in pack.claims}
        assert b"succeeds" not in render_claims(pack)


def test_an_ambiguous_boundary_renders_as_ambiguous() -> None:
    pack = _pack("configuration-traceability", NODE["ARM"], 2)
    section = _changes(pack)
    shown = [(e.knowledge, e.valid.start.ticks - T0) for e in section.entries]
    # Decided -> candidates is ambiguous; candidates -> unknown and unknown -> decided are
    # unknown. None of them is a change.
    assert shown == [("ambiguous", DAY), ("unknown", 2 * DAY), ("unknown", 3 * DAY)]
    ambiguous = section.entries[0]
    assert ambiguous.change is not None
    assert [s.role for s in ambiguous.statements] == ["known", "ambiguous", "ambiguous"]
    assert len(ambiguous.change.before) == 1 and len(ambiguous.change.after) == 2
    document = json.loads(render_json(pack))
    entry = next(s for s in document["sections"] if s["id"] == "configuration-changes")["entries"][
        0
    ]
    assert entry["knowledge"] == "ambiguous" and "valid" not in entry
    assert entry["change"]["at"] == {"domain_id": CIVIL, "ticks": T0 + DAY}
    assert sorted(entry["change"]["before"] + entry["change"]["after"]) == sorted(
        s["claims"][0] for s in entry["statements"]
    )
    pdf = render_pdf(pack)
    assert b"AMBIGUOUS BOUNDARY" in pdf
    assert b"UNKNOWN BOUNDARY" in pdf
    assert b"[CHANGE" not in pdf


def test_a_change_outside_the_interval_is_counted_not_shown() -> None:
    snap = dock()
    late = spec(
        snap,
        "configuration-traceability",
        NODE["AMR05"],
        Interval(Stamp(CIVIL, T0 + 2 * DAY), "open"),
        version=2,
    )
    section = _changes(compile_pack(late, snap))
    assert section.entries == () and section.knowledge == "not_covered"
    assert section.outside_interval == 1


def test_the_incident_pack_shows_no_change_for_the_machine_it_involves() -> None:
    pack = _pack("incident-timeline", NODE["INCIDENT"], 2)
    section = _changes(pack)
    assert [s.node for s in section.scope] == [NODE["AMR06"]]
    assert section.knowledge == "not_covered" and section.entries == ()
    assert _section(pack, "reconstruction").knowledge == "known"


# --- A template that does not read the snapshot's major ----------------------------------------


@pytest.mark.parametrize(
    ("template", "subject", "section_id"),
    [
        ("configuration-traceability", "AMR05", "configuration-changes"),
        ("configuration-lineage", "DOCK", "configuration-succession"),
        ("incident-timeline", "INCIDENT", "configuration-changes"),
    ],
)
def test_an_at_1_template_on_2x_is_section_not_covered(
    template: str, subject: str, section_id: str
) -> None:
    pack = _pack(template, NODE[subject], 1)
    section = _section(pack, section_id)
    assert section.knowledge == "not_covered" and section.entries == ()
    assert section.reason is not None
    assert section.reason["code"] == "section_not_covered"
    assert section.reason["meaning_changed"] == ["succeeds"]
    assert section.reason["section_reads_majors"] == [1]
    assert section.reason["graph_schema_major"] == 2
    # Every other section reads the 2.x snapshot.
    assert all(
        s.reason is None or s.reason.get("code") != "section_not_covered"
        for s in pack.sections
        if s is not section
    )
    document = json.loads(render_json(pack))
    assert document["findings"] == [{**plain(section.reason), "section": section_id}]
    assert document["snapshot"]["graph_schema"] == "2.0.0"
    assert document["snapshot"]["graph_schema_version"] == 2
    pdf = render_pdf(pack)
    number = [s.template.id for s in pack.sections].index(section_id) + 1
    assert _shown(f"Not covered: section {number} ({section_id})") in pdf
    assert _shown("NOT COVERED (section_not_covered) - the section selects succeeds") in pdf
    assert b"graph-schema 2.0.0, Ledger head tx" in pdf


def test_an_at_2_template_on_1x_is_section_not_covered() -> None:
    snap = configuration()
    old = compile_pack(spec(snap, "configuration-lineage", version=1), snap)
    new = compile_pack(spec(snap, "configuration-lineage", version=2), snap)
    section = _changes(new)
    assert section.knowledge == "not_covered" and section.entries == ()
    assert section.reason is not None
    assert section.reason["code"] == "section_not_covered"
    assert section.reason["section_reads_majors"] == [2]
    assert section.reason["meaning_changed"] == []
    assert section.reason["graph_schema_major"] == 1
    assert "declares this section reads graph-schema 2 only" in str(section.reason["reason"])
    # The sections @1 and @2 share read the 1.x snapshot exactly as @1 does.
    for kept in ("configuration-in-force", "authorisation", "run-configuration"):
        assert _section(new, kept).to_json() == _section(old, kept).to_json()
    document = json.loads(render_json(new))
    assert [f["section"] for f in document["findings"]] == ["configuration-changes"]
    assert "graph_schema" not in document["snapshot"]
    assert _shown("NOT COVERED (section_not_covered) - the template declares") in render_pdf(new)


# --- Determinism, goldens and the CLI -----------------------------------------------------------


def _renders() -> dict[str, bytes]:
    snap = load_snapshot(fixture_path(FIXTURE).read_bytes())
    lineage = _pack("configuration-lineage", NODE["DOCK"], 2, snap)
    amr = _pack("configuration-traceability", NODE["AMR05"], 2, snap)
    incident = _pack("incident-timeline", NODE["INCIDENT"], 2, snap)
    return {
        "lineage.json": render_json(lineage),
        "lineage.pdf": render_pdf(lineage),
        "lineage.claims": render_claims(lineage),
        "amr05.json": render_json(amr),
        "amr05.pdf": render_pdf(amr),
        "incident.json": render_json(incident),
        "incident.pdf": render_pdf(incident),
    }


def test_a_double_render_is_byte_identical() -> None:
    assert _renders() == _renders()


def test_golden_digests() -> None:
    from neptune.identity.hashing import content_id

    digests = {name: content_id(data) for name, data in _renders().items()}
    assert digests == GOLDEN


def test_the_cli_reads_a_2x_snapshot_without_a_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    chosen = spec(dock(), "configuration-traceability", NODE["AMR05"], FROM_T0, version=2)
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(chosen.to_json()), encoding="utf-8")
    out = tmp_path / "out"
    argv = ["pack", "--spec", str(spec_file), "--snapshot", str(fixture_path(FIXTURE))]
    assert main([*argv, "--out", str(out)]) == 0
    assert (out / "pack.json").read_bytes() == render_json(compile_pack(chosen, dock()))
    assert main([*argv, "--snapshot-schema-version", "2.3.0", "--out", str(tmp_path / "x")]) == 2
    assert "snapshot_unsupported" in capsys.readouterr().err


def test_the_fixture_is_not_mutated_by_reading() -> None:
    document = _document()
    before = copy.deepcopy(document)
    read_snapshot(document)
    assert document == before
