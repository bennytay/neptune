"""Source revisions and deduplication (ADR 0009).

Policy, in one place:

- Bytes are deduplicated by content id: one ``SourceArtifact`` per distinct byte string, however
  many locations hold it. Equal bytes are the same *evidence*, never the same logical thing.
- A location seen holding the same bytes as its latest revision creates nothing (idempotent).
- A location seen holding different bytes gets a new ``SourceRevision`` that supersedes the
  previous one. Nothing earlier is mutated or removed.
- A rename or move is a new location holding known bytes: a new revision for that location, no new
  artifact, so every record keyed by the content id is unchanged.
- For external objects the revision token is observational: a new token over identical bytes is
  not a new revision.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from neptune.identity.ids import record_id
from neptune.model.ids import ContentId, RecordId
from neptune.model.source import SourceArtifact, SourceLocation, SourceRevision


def revision_id(
    location: SourceLocation, content: ContentId, supersedes: tuple[RecordId, ...]
) -> RecordId:
    return record_id(
        "source_revision",
        {"content_id": content, "location": location.to_json(), "supersedes": list(supersedes)},
    )


@dataclass(frozen=True)
class Observation:
    """The outcome of observing one location. ``revision`` is the location's current revision."""

    revision: SourceRevision
    new_artifact: bool
    new_revision: bool


class SourceLedger:
    """Append-only record of which bytes exist and where they were seen.

    Pure and in-memory; persisting it between ingest runs belongs to the store. Construct it from a
    previous run's ``artifacts()`` and ``revisions()`` to continue that history.
    """

    def __init__(
        self, artifacts: Iterable[SourceArtifact] = (), revisions: Iterable[SourceRevision] = ()
    ) -> None:
        self._artifacts: dict[ContentId, SourceArtifact] = {}
        self._revisions: dict[RecordId, SourceRevision] = {}
        self._heads: dict[tuple[str, ...], SourceRevision] = {}
        for artifact in artifacts:
            self._add_artifact(artifact)
        self._load_revisions(list(revisions))

    def observe(self, location: SourceLocation, artifact: SourceArtifact) -> Observation:
        """Record that ``location`` currently holds ``artifact``'s bytes."""
        new_artifact = self._add_artifact(artifact)
        head = self._heads.get(location.key)
        if head is not None and head.content_id == artifact.content_id:
            return Observation(head, new_artifact=new_artifact, new_revision=False)
        supersedes = (head.id,) if head is not None else ()
        revision = SourceRevision(
            id=revision_id(location, artifact.content_id, supersedes),
            location=location,
            content_id=artifact.content_id,
            supersedes=supersedes,
        )
        self._revisions[revision.id] = revision
        self._heads[location.key] = revision
        return Observation(revision, new_artifact=new_artifact, new_revision=True)

    def head(self, location: SourceLocation) -> SourceRevision | None:
        """The latest revision at ``location``, or ``None`` if it has never been observed."""
        return self._heads.get(location.key)

    def artifact(self, content: ContentId) -> SourceArtifact | None:
        return self._artifacts.get(content)

    def artifacts(self) -> tuple[SourceArtifact, ...]:
        """All artifacts, sorted by content id."""
        return tuple(self._artifacts[key] for key in sorted(self._artifacts))

    def revisions(self) -> tuple[SourceRevision, ...]:
        """All revisions, sorted by id."""
        return tuple(self._revisions[key] for key in sorted(self._revisions))

    def _add_artifact(self, artifact: SourceArtifact) -> bool:
        existing = self._artifacts.get(artifact.content_id)
        if existing is None:
            self._artifacts[artifact.content_id] = artifact
            return True
        if existing.size != artifact.size:
            raise ValueError(
                f"{artifact.content_id} seen with sizes {existing.size} and {artifact.size};"
                " the digest is corrupt"
            )
        # Same bytes. Chunk hashes may differ only by chunk size; the first digest is kept.
        return False

    def _load_revisions(self, revisions: list[SourceRevision]) -> None:
        for revision in revisions:
            expected = revision_id(revision.location, revision.content_id, revision.supersedes)
            if revision.id != expected:
                raise ValueError(f"revision {revision.id} does not match its fields ({expected})")
            if revision.content_id not in self._artifacts:
                raise ValueError(f"revision {revision.id} references unknown {revision.content_id}")
            if revision.id in self._revisions:
                raise ValueError(f"duplicate revision {revision.id}")
            self._revisions[revision.id] = revision
        superseded: set[RecordId] = set()
        for revision in revisions:
            for previous_id in revision.supersedes:
                previous = self._revisions.get(previous_id)
                if previous is None or previous.location.key != revision.location.key:
                    raise ValueError(f"revision {revision.id} supersedes unknown {previous_id}")
                if previous_id in superseded:
                    raise ValueError(f"history forks: {previous_id} is superseded twice")
                superseded.add(previous_id)
        for revision in revisions:
            if revision.id in superseded:
                continue
            if revision.location.key in self._heads:
                raise ValueError(f"location {revision.location.key} has more than one head")
            self._heads[revision.location.key] = revision
