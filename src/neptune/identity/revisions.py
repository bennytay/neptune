"""Source revisions and deduplication (ADR 0009, amended by ADR 0010).

Policy, in one place:

- Bytes are deduplicated by content id: one ``SourceArtifact`` per distinct byte string, however
  many locations hold it. Equal bytes are the same *evidence*, never the same logical thing.
- A location seen holding the same bytes as its latest revision creates nothing (idempotent).
- A location seen holding different bytes gets a new ``SourceRevision`` that supersedes the
  previous one. Nothing earlier is mutated or removed.
- A rename or move is a new location holding known bytes: a new revision for that location, no new
  artifact, so every record keyed by the content id is unchanged. The old location gets a
  ``SourceAbsence`` if the scan could see that it is gone (``neptune.discovery.scan``).
- Bytes reappearing at an absent location are a new revision superseding the absence.
- For external objects the revision token is observational: a new token over identical bytes is
  not a new revision. The ledger keeps every token seen over the head revision's bytes (ADR 0067),
  so an object re-uploaded unchanged is hashed once under its new token and recognised by it
  afterwards (``recognise``) without being fetched. Tokens never enter content identity.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

from neptune.identity.ids import record_id
from neptune.model.ids import ContentId, ExternalObjectRef, RecordId, check_text
from neptune.model.source import SourceAbsence, SourceArtifact, SourceLocation, SourceRevision

ChainEntry: TypeAlias = SourceRevision | SourceAbsence


def revision_id(
    location: SourceLocation, content: ContentId, supersedes: tuple[RecordId, ...]
) -> RecordId:
    return record_id(
        "source_revision",
        {"content_id": content, "location": location.to_json(), "supersedes": list(supersedes)},
    )


def absence_id(location: SourceLocation, supersedes: tuple[RecordId, ...]) -> RecordId:
    return record_id(
        "source_absence", {"location": location.to_json(), "supersedes": list(supersedes)}
    )


@dataclass(frozen=True)
class Observation:
    """The outcome of observing one location. ``revision`` is the location's current revision.

    ``new_token`` is true when an external location's revision token was not yet known for that
    revision's bytes: a new revision, or the same bytes under a token seen for the first time.
    """

    revision: SourceRevision
    new_artifact: bool
    new_revision: bool
    new_token: bool = False


class SourceLedger:
    """Append-only record of which bytes exist, where they were seen, and where they are gone.

    Pure and in-memory; persisting it between ingest runs belongs to the store. Construct it from a
    previous run's ``artifacts()``, ``revisions()``, ``absences()`` and ``tokens()`` to continue
    that history.

    ``tokens`` are, by external revision id, the revision tokens seen over that revision's bytes
    besides the one its location names (ADR 0067). They are observations about the store, not
    evidence: no record id or content id depends on them.
    """

    def __init__(
        self,
        artifacts: Iterable[SourceArtifact] = (),
        revisions: Iterable[SourceRevision] = (),
        absences: Iterable[SourceAbsence] = (),
        tokens: Mapping[RecordId, Iterable[str]] | None = None,
    ) -> None:
        self._artifacts: dict[ContentId, SourceArtifact] = {}
        self._entries: dict[RecordId, ChainEntry] = {}
        self._heads: dict[tuple[str, ...], ChainEntry] = {}
        self._tokens: dict[RecordId, set[str]] = {}
        for artifact in artifacts:
            self._add_artifact(artifact)
        self._load([*revisions, *absences])
        for identifier, seen in (tokens or {}).items():
            self._load_tokens(identifier, seen)

    def observe(self, location: SourceLocation, artifact: SourceArtifact) -> Observation:
        """Record that ``location`` currently holds ``artifact``'s bytes.

        An external location whose head revision holds the same bytes under another token keeps
        that revision, and the new token is remembered for it (``new_token``).
        """
        new_artifact = self._add_artifact(artifact)
        head = self._heads.get(location.key)
        if isinstance(head, SourceRevision) and head.content_id == artifact.content_id:
            new_token = False
            if isinstance(location, ExternalObjectRef) and not self._knows(head, location):
                self._tokens.setdefault(head.id, set()).add(location.revision_token)
                new_token = True
            return Observation(
                head, new_artifact=new_artifact, new_revision=False, new_token=new_token
            )
        supersedes = (head.id,) if head is not None else ()
        revision = SourceRevision(
            id=revision_id(location, artifact.content_id, supersedes),
            location=location,
            content_id=artifact.content_id,
            supersedes=supersedes,
        )
        self._append(revision)
        external = isinstance(location, ExternalObjectRef)
        return Observation(
            revision, new_artifact=new_artifact, new_revision=True, new_token=external
        )

    def recognise(self, location: ExternalObjectRef) -> SourceRevision | None:
        """The head revision of ``location``'s object if its bytes were seen under this token.

        ``None`` when the object was never seen, is absent, or its token is new for the head's
        bytes: only then does it need fetching and hashing. A token known for an older revision
        is not enough, since the object has held other bytes since.
        """
        if not isinstance(location, ExternalObjectRef):
            raise TypeError(f"only an external location has a revision token: {location!r}")
        head = self._heads.get(location.key)
        if isinstance(head, SourceRevision) and self._knows(head, location):
            return head
        return None

    def tokens(self) -> tuple[tuple[RecordId, tuple[str, ...]], ...]:
        """Every external revision's extra tokens, sorted by revision id, each list sorted."""
        return tuple(
            (identifier, tuple(sorted(self._tokens[identifier])))
            for identifier in sorted(self._tokens)
            if self._tokens[identifier]
        )

    def mark_absent(self, location: SourceLocation) -> SourceAbsence | None:
        """Record that ``location`` holds no bytes. ``None`` if nothing was there to begin with."""
        head = self._heads.get(location.key)
        if not isinstance(head, SourceRevision):
            return None
        absence = SourceAbsence(
            id=absence_id(location, (head.id,)), location=location, supersedes=(head.id,)
        )
        self._append(absence)
        return absence

    def head(self, location: SourceLocation) -> ChainEntry | None:
        """The latest entry at ``location``, or ``None`` if it has never been observed."""
        return self._heads.get(location.key)

    def heads(self) -> tuple[ChainEntry, ...]:
        """The latest entry at every location ever observed, sorted by id."""
        return tuple(sorted(self._heads.values(), key=lambda entry: entry.id))

    def artifact(self, content: ContentId) -> SourceArtifact | None:
        return self._artifacts.get(content)

    def artifacts(self) -> tuple[SourceArtifact, ...]:
        """All artifacts, sorted by content id."""
        return tuple(self._artifacts[key] for key in sorted(self._artifacts))

    def revisions(self) -> tuple[SourceRevision, ...]:
        """All revisions, sorted by id."""
        return tuple(e for _, e in sorted(self._entries.items()) if isinstance(e, SourceRevision))

    def absences(self) -> tuple[SourceAbsence, ...]:
        """All absences, sorted by id."""
        return tuple(e for _, e in sorted(self._entries.items()) if isinstance(e, SourceAbsence))

    def _knows(self, revision: SourceRevision, location: ExternalObjectRef) -> bool:
        own = revision.location
        if not isinstance(own, ExternalObjectRef):
            return False
        token = location.revision_token
        return token == own.revision_token or token in self._tokens.get(revision.id, ())

    def _load_tokens(self, identifier: RecordId, seen: Iterable[str]) -> None:
        revision = self._entries.get(identifier)
        if not isinstance(revision, SourceRevision) or not isinstance(
            revision.location, ExternalObjectRef
        ):
            raise ValueError(f"tokens name {identifier}, which is no external revision")
        if isinstance(seen, str):
            raise ValueError(f"the tokens of {identifier} are a list, not one string")
        own = revision.location.revision_token
        kept = self._tokens.setdefault(identifier, set())
        for token in seen:
            if not isinstance(token, str):
                raise ValueError(f"a revision token is text, got {type(token).__name__}")
            check_text("revision_token", token)
            if token == own:
                raise ValueError(f"{identifier} lists its own token {token!r} as an extra one")
            kept.add(token)

    def _append(self, entry: ChainEntry) -> None:
        self._entries[entry.id] = entry
        self._heads[entry.location.key] = entry

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

    def _load(self, entries: list[ChainEntry]) -> None:
        for entry in entries:
            if isinstance(entry, SourceRevision):
                expected = revision_id(entry.location, entry.content_id, entry.supersedes)
                if entry.content_id not in self._artifacts:
                    raise ValueError(f"revision {entry.id} references unknown {entry.content_id}")
            else:
                expected = absence_id(entry.location, entry.supersedes)
            if entry.id != expected:
                raise ValueError(f"{entry.id} does not match its fields ({expected})")
            if entry.id in self._entries:
                raise ValueError(f"duplicate chain entry {entry.id}")
            self._entries[entry.id] = entry
        superseded: set[RecordId] = set()
        for entry in entries:
            for previous_id in entry.supersedes:
                previous = self._entries.get(previous_id)
                if previous is None or previous.location.key != entry.location.key:
                    raise ValueError(f"{entry.id} supersedes unknown {previous_id}")
                if isinstance(entry, SourceAbsence) and isinstance(previous, SourceAbsence):
                    raise ValueError(f"absence {entry.id} supersedes another absence")
                if previous_id in superseded:
                    raise ValueError(f"history forks: {previous_id} is superseded twice")
                superseded.add(previous_id)
        for entry in entries:
            if entry.id in superseded:
                continue
            if entry.location.key in self._heads:
                raise ValueError(f"location {entry.location.key} has more than one head")
            self._heads[entry.location.key] = entry
