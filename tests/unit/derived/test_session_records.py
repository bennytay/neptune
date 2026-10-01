"""The derived session records: JSON shape, ids, and what their readers refuse (ADR 0036 §6)."""

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from neptune.derived.grouping import GROUPING_ID, LayoutGrouper
from neptune.derived.sessions import (
    DERIVED_SCHEMA_VERSION,
    ROOT_DIRECTORY,
    LinkRelation,
    Placement,
    Reason,
    Role,
    SessionLink,
    SessionMember,
    SessionProposal,
    Status,
    UnassignedFile,
    directory_from_json,
    read_derived,
    session_proposal,
    session_proposal_from_json,
    unassigned_file,
    unassigned_file_from_json,
)
from neptune.discovery.layout import LayoutFile, LayoutLink, layout_of
from neptune.identity import canonical_json
from neptune.identity.hashing import content_id
from neptune.identity.revisions import revision_id
from neptune.model.ids import RecordId
from neptune.model.source import LocalPath, RawLocalPath, local_location

GROUPER = LayoutGrouper()
TRANSFORM = GROUPER.transform.id


def file(path: bytes) -> LayoutFile:
    location = local_location(path)
    content = content_id(path)
    return LayoutFile(revision_id(location, content, ()), location, content)


def member(path: bytes, rule: str = "recording_file") -> SessionMember:
    layout_file = file(path)
    return SessionMember(layout_file.revision, layout_file.location, Role.RECORDING, rule, 0.7)


def proposal(**changes: Any) -> SessionProposal:
    fields: dict[str, Any] = {
        "transform": TRANSFORM,
        "rule": "recording_file",
        "confidence": 0.7,
        "directory": ROOT_DIRECTORY,
        "members": [member(b"a.mcap")],
        "links": [SessionLink(LocalPath("alias"), b"\xffa.mcap", LinkRelation.ALIAS)],
        "reasons": [Reason("recording_file", "a recording by its extension", {"n": 1})],
    }
    return session_proposal(**(fields | changes))


def test_every_record_of_a_real_grouping_reads_back_as_itself() -> None:
    paths = (b"x_0.mcap", b"x_1.mcap", b"notes.txt", b"run_1/\xffa.bag", b"b.ulg")
    nested = (b"drive_1/a.mcap", b"drive_1/camera_2024-05-01_12-30-00/f.png")
    layout = layout_of(
        [file(p) for p in (*paths, *nested)], [LayoutLink(LocalPath("latest"), b"run_1")]
    )
    grouping = GROUPER.propose(layout)
    assert grouping.proposals and grouping.unassigned
    assert any(p.includes for p in grouping.proposals)
    for record in grouping.proposals:
        data = record.to_json()
        assert data["kind"] == "session_proposal" and data["assertion_kind"] == "inferred"
        assert data["schema_version"] == DERIVED_SCHEMA_VERSION
        assert canonical_json.loads(canonical_json.dumps(data)) == data
        assert session_proposal_from_json(data) == record
    for entry in grouping.unassigned:
        assert unassigned_file_from_json(entry.to_json()) == entry
    records = read_derived(grouping.tables())
    assert sorted(r.id for r in records) == sorted(
        [*(p.id for p in grouping.proposals), *(u.id for u in grouping.unassigned)]
    )
    assert grouping.transform.adapter_id == GROUPING_ID


def test_a_link_target_that_is_not_utf8_is_kept_as_hex() -> None:
    record = proposal()
    links = record.to_json()["links"]
    assert isinstance(links, list) and len(links) == 1
    assert links[0] == {
        "location": {"kind": "local", "path": "alias"},
        "relation": "alias",
        "target_hex": "ff612e6d636170",
    }
    assert session_proposal_from_json(record.to_json()) == record


def test_the_id_covers_what_the_proposal_is_not_what_is_said_of_it() -> None:
    base = proposal()
    assert proposal(confidence=0.3, reasons=[], links=[]).id == base.id
    assert proposal(rule="session_directory").id != base.id
    assert proposal(directory=LocalPath("a")).id != base.id
    assert proposal(members=[member(b"b.mcap")]).id != base.id
    with pytest.raises(ValueError, match="id does not match"):
        replace(base, rule="split_sequence")


def edit(data: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return data | changes


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda d: edit(d, schema_version=2), "derived schema version"),
        (lambda d: edit(d, kind="run"), "expected kind"),
        (lambda d: edit(d, assertion_kind="observed"), "inferred"),
        (lambda d: edit(d, extra=1), "unexpected"),
        (lambda d: edit(d, confidence=1), "confidence"),
        (lambda d: edit(d, confidence=1.5), "confidence"),
        (lambda d: edit(d, rule="recording_file", id="rec:sha256:" + "0" * 64), "id does not"),
        (lambda d: edit(d, status="contested"), "contested exactly when"),
        (lambda d: edit(d, members=[]), "at least one file"),
        (lambda d: edit(d, includes=[d["id"]]), "includes must be other"),
        (lambda d: edit(d, includes=["rec:sha256:" + "0" * 64]), "contests every proposal"),
        (lambda d: edit(d, members=d["members"] * 2), "each path once"),
        (lambda d: edit(d, directory={"kind": "external"}), "external"),
        (lambda d: edit(d, reasons=[{"rule": "x", "message": "a\nb", "details": {}}]), "one line"),
    ],
)
def test_a_proposal_reader_refuses_what_a_grouper_never_writes(
    change: Callable[[dict[str, Any]], dict[str, Any]], error: str
) -> None:
    data = dict(proposal().to_json())
    with pytest.raises((ValueError, TypeError, KeyError), match=error):
        session_proposal_from_json(change(data))


def test_an_include_is_part_of_what_the_proposal_is() -> None:
    inner = proposal(members=[member(b"run_1/a.mcap")])
    outer = proposal(includes=[inner.id], contested=[inner.id])
    assert outer.includes == (inner.id,) and outer.id != proposal(contested=[inner.id]).id
    assert session_proposal_from_json(outer.to_json()) == outer


def test_contested_names_others_never_itself() -> None:
    other = proposal(members=[member(b"b.mcap")])
    contested = proposal(contested=[other.id])
    assert contested.status is Status.CONTESTED and contested.contested == (other.id,)
    with pytest.raises(ValueError, match="other proposals"):
        replace(contested, contested=(contested.id,))


def test_an_unassigned_placement_agrees_with_its_candidates() -> None:
    layout_file = file(b"notes.txt")
    a, b = proposal().id, proposal(members=[member(b"b.mcap")]).id
    unknown = unassigned_file(
        transform=TRANSFORM,
        revision=layout_file.revision,
        location=layout_file.location,
        reason="no_session",
    )
    ambiguous = unassigned_file(
        transform=TRANSFORM,
        revision=layout_file.revision,
        location=layout_file.location,
        reason="several_sessions",
        candidates=[b, a],
    )
    assert unknown.placement is Placement.UNKNOWN and ambiguous.placement is Placement.AMBIGUOUS
    assert ambiguous.candidates == tuple(sorted([a, b])) and unknown.id == ambiguous.id
    with pytest.raises(ValueError, match="at least two"):
        replace(ambiguous, candidates=(a,))
    with pytest.raises(ValueError, match="no candidates"):
        replace(unknown, candidates=tuple(sorted((a, b))))
    with pytest.raises(ValueError, match="id does not"):
        replace(unknown, revision=file(b"other").revision)
    data = dict(ambiguous.to_json()) | {"placement": "known"}
    with pytest.raises(ValueError):
        unassigned_file_from_json(data)


def test_a_member_and_a_directory_are_local() -> None:
    with pytest.raises(ValueError, match="confidence"):
        SessionMember(member(b"a").revision, LocalPath("a"), Role.CONTEXT, "x", 0.0)
    with pytest.raises(ValueError):
        SessionMember(member(b"a").revision, LocalPath("a"), Role.CONTEXT, "Not A Token", 0.5)
    assert directory_from_json({"kind": "root"}) is ROOT_DIRECTORY
    assert directory_from_json(RawLocalPath(b"\xff").to_json()) == RawLocalPath(b"\xff")
    with pytest.raises(ValueError):
        directory_from_json({"kind": "root", "path": "x"})


def test_read_derived_refuses_a_kind_it_does_not_define() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        read_derived({"caption": []})
    assert read_derived({"session_proposal": [], "session_unassigned": []}) == ()


def test_records_are_typed() -> None:
    record = proposal()
    with pytest.raises(TypeError):
        replace(record, status="proposed")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(record, members=("a",))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        UnassignedFile(
            id=RecordId(record.id),
            transform=TRANSFORM,
            revision=record.members[0].revision,
            location="a.mcap",  # type: ignore[arg-type]
            placement=Placement.UNKNOWN,
            reason="no_session",
            candidates=(),
        )
