import io

import pytest

from neptune.identity import canonical_json
from neptune.identity.hashing import digest_stream
from neptune.identity.revisions import SourceLedger, absence_id, revision_id
from neptune.model.ids import ExternalObjectRef, RecordId
from neptune.model.source import LocalPath, SourceAbsence, SourceArtifact, SourceRevision

A = digest_stream(io.BytesIO(b"version A"))
B = digest_stream(io.BytesIO(b"version B"))
RUN = LocalPath("runs/run1.mcap")


def test_first_observation_creates_artifact_and_revision() -> None:
    ledger = SourceLedger()
    obs = ledger.observe(RUN, A)
    assert obs.new_artifact and obs.new_revision
    assert obs.revision.supersedes == ()
    assert obs.revision.id == revision_id(RUN, A.content_id, ())
    assert ledger.head(RUN) == obs.revision


def test_identical_bytes_twice_is_idempotent() -> None:
    ledger = SourceLedger()
    first = ledger.observe(RUN, A)
    snapshot = (ledger.artifacts(), ledger.revisions())
    again = ledger.observe(RUN, A)
    assert not again.new_artifact and not again.new_revision
    assert again.revision == first.revision
    assert (ledger.artifacts(), ledger.revisions()) == snapshot


def test_changed_bytes_create_a_revision_without_mutating_history() -> None:
    ledger = SourceLedger()
    first = ledger.observe(RUN, A).revision
    second = ledger.observe(RUN, B)
    assert second.new_artifact and second.new_revision
    assert second.revision.supersedes == (first.id,)
    assert first in ledger.revisions()
    assert ledger.artifact(A.content_id) == A
    assert ledger.head(RUN) == second.revision


def test_reverting_bytes_is_a_new_revision_of_an_existing_artifact() -> None:
    ledger = SourceLedger()
    first = ledger.observe(RUN, A).revision
    middle = ledger.observe(RUN, B).revision
    back = ledger.observe(RUN, A)
    assert not back.new_artifact and back.new_revision
    assert back.revision.supersedes == (middle.id,)
    assert back.revision.id != first.id
    assert len(ledger.revisions()) == 3


def test_rename_creates_no_new_evidence() -> None:
    ledger = SourceLedger()
    ledger.observe(RUN, A)
    moved = ledger.observe(LocalPath("archive/renamed.mcap"), A)
    assert not moved.new_artifact
    assert moved.revision.content_id == A.content_id
    assert ledger.artifacts() == (A,)


def test_duplicates_at_two_locations_share_one_artifact() -> None:
    ledger = SourceLedger()
    ledger.observe(LocalPath("robot1/robot.urdf"), A)
    ledger.observe(LocalPath("robot2/robot.urdf"), A)
    assert ledger.artifacts() == (A,)
    assert len(ledger.revisions()) == 2


def test_external_token_change_over_same_bytes_is_not_a_revision() -> None:
    ledger = SourceLedger()
    first = ledger.observe(ExternalObjectRef("s3", "b/run.mcap", "etag-1"), A)
    same = ledger.observe(ExternalObjectRef("s3", "b/run.mcap", "etag-2"), A)
    assert not same.new_revision and same.revision == first.revision
    changed = ledger.observe(ExternalObjectRef("s3", "b/run.mcap", "etag-3"), B)
    assert changed.revision.supersedes == (first.revision.id,)


def test_chunk_size_difference_keeps_first_digest() -> None:
    ledger = SourceLedger()
    ledger.observe(RUN, A)
    rechunked = digest_stream(io.BytesIO(b"version A"), chunk_size=4)
    assert not ledger.observe(LocalPath("copy"), rechunked).new_artifact
    assert ledger.artifacts() == (A,)


def test_size_mismatch_for_same_content_id_is_rejected() -> None:
    ledger = SourceLedger()
    ledger.observe(RUN, A)
    corrupt = SourceArtifact(A.content_id, A.size + 1, A.chunk_size, A.chunks)
    with pytest.raises(ValueError, match="corrupt"):
        ledger.observe(RUN, corrupt)


def build_history() -> SourceLedger:
    ledger = SourceLedger()
    ledger.observe(RUN, A)
    ledger.observe(RUN, B)
    ledger.observe(LocalPath("other"), A)
    return ledger


def test_reload_continues_history() -> None:
    original = build_history()
    reloaded = SourceLedger(original.artifacts(), reversed(original.revisions()))
    assert reloaded.head(RUN) == original.head(RUN)
    assert not reloaded.observe(RUN, B).new_revision
    assert reloaded.observe(RUN, A).revision.supersedes == (original.head(RUN).id,)  # type: ignore[union-attr]


def test_output_is_deterministic_and_canonical() -> None:
    def encode(ledger: SourceLedger) -> bytes:
        rows = [a.to_json() for a in ledger.artifacts()] + [r.to_json() for r in ledger.revisions()]
        return b"\n".join(canonical_json.dumps(row) for row in rows)

    assert encode(build_history()) == encode(build_history())


def test_reload_rejects_tampered_revision() -> None:
    history = build_history()
    revision = history.revisions()[0]
    tampered = SourceRevision(revision.id, LocalPath("elsewhere"), revision.content_id, ())
    with pytest.raises(ValueError, match="does not match"):
        SourceLedger(history.artifacts(), [tampered])


def test_reload_rejects_revision_without_artifact() -> None:
    with pytest.raises(ValueError, match="unknown sha256"):
        SourceLedger((), build_history().revisions())


def test_reload_rejects_forked_history() -> None:
    root = SourceRevision(revision_id(RUN, A.content_id, ()), RUN, A.content_id, ())
    fork_b = SourceRevision(
        revision_id(RUN, B.content_id, (root.id,)), RUN, B.content_id, (root.id,)
    )
    fork_a = SourceRevision(
        revision_id(RUN, A.content_id, (root.id,)), RUN, A.content_id, (root.id,)
    )
    with pytest.raises(ValueError, match="forks"):
        SourceLedger((A, B), [root, fork_b, fork_a])


def test_reload_rejects_two_roots_at_one_location() -> None:
    a = SourceRevision(revision_id(RUN, A.content_id, ()), RUN, A.content_id, ())
    b = SourceRevision(revision_id(RUN, B.content_id, ()), RUN, B.content_id, ())
    with pytest.raises(ValueError, match="more than one head"):
        SourceLedger((A, B), [a, b])


def test_reload_rejects_dangling_supersedes() -> None:
    missing = RecordId("rec:sha256:" + "0" * 64)
    orphan = SourceRevision(
        revision_id(RUN, A.content_id, (missing,)), RUN, A.content_id, (missing,)
    )
    with pytest.raises(ValueError, match="supersedes unknown"):
        SourceLedger((A,), [orphan])


def test_absence_then_reappearance() -> None:
    ledger = SourceLedger()
    present = ledger.observe(RUN, A).revision
    absence = ledger.mark_absent(RUN)
    assert absence is not None and absence.supersedes == (present.id,)
    assert absence.id == absence_id(RUN, (present.id,))
    assert ledger.head(RUN) == absence
    assert ledger.mark_absent(RUN) is None  # idempotent
    back = ledger.observe(RUN, A)
    assert back.new_revision and not back.new_artifact
    assert back.revision.supersedes == (absence.id,)
    assert present in ledger.revisions() and ledger.absences() == (absence,)


def test_absence_of_never_seen_location_is_not_asserted() -> None:
    assert SourceLedger().mark_absent(RUN) is None


def test_reload_with_absences_continues_history() -> None:
    ledger = SourceLedger()
    ledger.observe(RUN, A)
    ledger.mark_absent(RUN)
    reloaded = SourceLedger(ledger.artifacts(), ledger.revisions(), ledger.absences())
    assert reloaded.head(RUN) == ledger.head(RUN)
    assert reloaded.heads() == ledger.heads()


def test_reload_rejects_absence_superseding_absence() -> None:
    ledger = SourceLedger()
    ledger.observe(RUN, A)
    first = ledger.mark_absent(RUN)
    assert first is not None
    second = SourceAbsence(absence_id(RUN, (first.id,)), RUN, (first.id,))
    with pytest.raises(ValueError, match="another absence"):
        SourceLedger(ledger.artifacts(), ledger.revisions(), [first, second])
