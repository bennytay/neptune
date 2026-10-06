"""Forward compatibility across graph-schema minors (Deploy ADR 0015): under a declared newer minor
of the pinned major, an unknown key is reported once per key path as ``snapshot_key_unread`` and
never read or rendered; under the pin, an older minor or another major it is refused."""

import json
from pathlib import Path
from typing import Any

import pytest

from deploy_pack_graphs import fixture_path
from deploy_pack_support import events, events_pack, spec
from neptune_deploy.lifecycle.cli import main
from neptune_deploy.packs import (
    PackError,
    compile_pack,
    read_snapshot,
    render_claims,
    render_json,
    render_pdf,
)
from neptune_deploy.packs.snapshot import GRAPH_SCHEMA_1X_PIN, snapshot_id

NEWER = "1.9.0"
BUILDS = [{"consolidator_id": "memory.events", "recorded_at": 1, "version": "1"}]
SECRET = "build-detail-that-is-never-shown"


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(fixture_path("arm_cell_events").read_bytes())
    return document


def _newer_minor() -> dict[str, Any]:
    """A graph-schema 1.9.0-style document: ``builds`` at the top, and a key nested in every
    claim's provenance and one inside an object, none of which 1.6.0 names."""
    document = _document()
    document["builds"] = [{**build, "note": SECRET} for build in BUILDS]
    for claim in document["claims"]:
        claim["provenance"]["build_ref"] = SECRET
    document["claims"][0]["object"]["unit_hint"] = SECRET
    return document


def test_a_newer_minor_loads_and_reports_each_key_path_once() -> None:
    snap = read_snapshot(_newer_minor(), schema_version=NEWER)
    paths = {u.key_path: u for u in snap.unread}
    assert list(paths) == sorted(paths)
    assert set(paths) == {"/builds", "/claims/*/object/unit_hint", "/claims/*/provenance/build_ref"}
    assert paths["/builds"].pointer == "/builds"
    assert paths["/builds"].occurrences == 1
    nested = paths["/claims/*/provenance/build_ref"]
    assert nested.pointer == "/claims/0/provenance/build_ref"
    assert nested.occurrences == len(_document()["claims"])
    assert snap.declared_schema_version == NEWER


def test_what_is_read_is_the_pinned_shape_and_the_id_names_the_whole_document() -> None:
    document = _newer_minor()
    snap = read_snapshot(document, schema_version=NEWER)
    plain = events()
    assert snap.id == snapshot_id(document) != plain.id
    assert [c.raw for c in snap.claims] == [c.raw for c in plain.claims]
    assert [c.object_key for c in snap.claims] == [c.object_key for c in plain.claims]


def test_the_pack_lists_the_findings_and_shows_no_content() -> None:
    snap = read_snapshot(_newer_minor(), schema_version=NEWER)
    pack = compile_pack(spec(snap, "event-timeline", interval=events_pack().spec.interval), snap)
    document = json.loads(render_json(pack))
    assert [f["code"] for f in document["findings"]] == ["snapshot_key_unread"] * 3
    assert {f["key_path"] for f in document["findings"]} == {u.key_path for u in snap.unread}
    assert document["snapshot"]["declared_schema_version"] == NEWER
    for rendered in (render_json(pack), render_claims(pack), render_pdf(pack)):
        assert SECRET.encode() not in rendered
    assert b"Not read: /builds" in render_pdf(pack)


def test_the_render_is_deterministic() -> None:
    def render() -> list[bytes]:
        snap = read_snapshot(_newer_minor(), schema_version=NEWER)
        pack = compile_pack(
            spec(snap, "event-timeline", interval=events_pack().spec.interval), snap
        )
        return [render_json(pack), render_claims(pack), render_pdf(pack)]

    assert render() == render()


def test_a_snapshot_without_unknown_keys_is_unchanged_by_a_newer_declaration() -> None:
    plain = events()
    declared = read_snapshot(_document(), schema_version=NEWER)
    assert declared.unread == () and declared.declared_schema_version is None
    assert declared.id == plain.id
    pack = compile_pack(
        spec(declared, "event-timeline", interval=events_pack().spec.interval), declared
    )
    assert "findings" not in json.loads(render_json(pack))


@pytest.mark.parametrize(
    "declared", [None, GRAPH_SCHEMA_1X_PIN, "1.2.0", "1.6.9", "2.0.0", "2.1.0", "0.9.0"]
)
def test_the_pin_an_older_minor_and_another_major_refuse_the_same_keys(
    declared: str | None,
) -> None:
    with pytest.raises(PackError) as caught:
        read_snapshot(_newer_minor(), schema_version=declared)
    assert caught.value.code == "snapshot_malformed"
    assert caught.value.pointer == ""
    assert "builds" in str(caught.value)


def test_a_known_key_stays_strict_under_a_newer_minor() -> None:
    document = _newer_minor()
    document["claims"][0]["recorded_at"] = "later"
    with pytest.raises(PackError) as caught:
        read_snapshot(document, schema_version=NEWER)
    assert caught.value.code == "snapshot_malformed"
    assert caught.value.pointer == "/claims/0/recorded_at"


def test_a_missing_required_key_is_refused_under_a_newer_minor() -> None:
    document = _newer_minor()
    del document["claims"][0]["provenance"]["records"]
    with pytest.raises(PackError, match="missing records"):
        read_snapshot(document, schema_version=NEWER)


@pytest.mark.parametrize("declared", ["1.9", "v1.9.0", "1.9.0-rc1", "", "1.09.0"])
def test_a_malformed_declaration_is_refused(declared: str) -> None:
    with pytest.raises(PackError) as caught:
        read_snapshot(_document(), schema_version=declared)
    assert caught.value.code == "snapshot_unsupported"


def test_the_cli_takes_the_declared_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = _newer_minor()
    snap = read_snapshot(document, schema_version=NEWER)
    spec_file, snapshot_file = tmp_path / "spec.json", tmp_path / "graph.json"
    chosen = spec(snap, "event-timeline", interval=events_pack().spec.interval)
    spec_file.write_text(json.dumps(chosen.to_json()), encoding="utf-8")
    snapshot_file.write_text(json.dumps(document), encoding="utf-8")
    argv = ["pack", "--spec", str(spec_file), "--snapshot", str(snapshot_file)]
    assert main([*argv, "--out", str(tmp_path / "refused")]) == 2
    assert "snapshot_malformed" in capsys.readouterr().err
    out = tmp_path / "out"
    assert main([*argv, "--snapshot-schema-version", NEWER, "--out", str(out)]) == 0
    expected = compile_pack(chosen, snap)
    assert (out / "pack.json").read_bytes() == render_json(expected)
