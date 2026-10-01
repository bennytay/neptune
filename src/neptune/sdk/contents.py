"""What a run contains, asked of a package without decoding a message (ADR 0049 §5).

::

    from neptune.sdk import read_package, run_contents
    from neptune.derived.semantics import Semantic

    for run in run_contents(read_package(path)):
        for stream in run.carrying(Semantic.IMU):
            print(stream.topic, stream.schema_name, [p.path for p in stream.fields])

Each run lists its streams as declared (topic, type, encoding, declared count), each with its
``stream_layout`` (the declared definition's field paths and types: observed) and its
``stream_semantic`` (what it carries: inferred, with confidence, rules and evidence). Both are the
package's derived tables; a package without them (no streams, or written before ADR 0049) gives
``None`` for both, never a guess.
"""

from collections import defaultdict
from dataclasses import dataclass

from neptune.derived.schemas import FieldPath, LayoutState, StreamLayout
from neptune.derived.semantics import Semantic, SemanticState, StreamSemantic
from neptune.derived.sessions import read_derived
from neptune.model.ids import RecordId
from neptune.model.knowledge import Known
from neptune.model.run import Run, Stream
from neptune.sdk.errors import InvalidRequestError, PackageInvalidError
from neptune.store.package import IngestPackage


def _semantic(value: Semantic | str) -> Semantic:
    try:
        return Semantic(value)
    except ValueError:
        known = ", ".join(str(s) for s in Semantic)
        raise InvalidRequestError(f"{value!r} is not a semantic; one of: {known}") from None


def _text(knowledge: object) -> str | None:
    if isinstance(knowledge, Known) and isinstance(knowledge.value, str):
        return knowledge.value
    return None


@dataclass(frozen=True)
class StreamContents:
    """One stream: its record, its declared layout and its inferred semantic."""

    stream: Stream
    layout: StreamLayout | None
    semantic: StreamSemantic | None

    @property
    def id(self) -> RecordId:
        return self.stream.id

    @property
    def topic(self) -> str | None:
        return _text(self.stream.topic)

    @property
    def schema_name(self) -> str | None:
        return _text(self.stream.schema_name)

    @property
    def schema_encoding(self) -> str | None:
        return _text(self.stream.schema_encoding)

    @property
    def message_encoding(self) -> str | None:
        return _text(self.stream.message_encoding)

    @property
    def message_count(self) -> int | None:
        """The count the source declares, when it declares one."""
        count = self.stream.message_count
        return count.value if isinstance(count, Known) else None

    @property
    def layout_state(self) -> LayoutState | None:
        return None if self.layout is None else self.layout.state

    @property
    def fields(self) -> tuple[FieldPath, ...]:
        """Every field path the declared definition gives a message; empty unless ``known``."""
        return () if self.layout is None else self.layout.paths

    @property
    def semantic_state(self) -> SemanticState | None:
        return None if self.semantic is None else self.semantic.state

    def carries(self, semantic: Semantic | str) -> bool:
        """Whether the stream's inferred semantic is ``semantic`` (``known`` only)."""
        wanted = _semantic(semantic)
        return self.semantic is not None and self.semantic.semantic == wanted

    def may_carry(self, semantic: Semantic | str) -> bool:
        """Whether ``semantic`` is the stream's semantic or ties for it (``ambiguous``)."""
        wanted = _semantic(semantic)
        if self.semantic is None or not self.semantic.candidates:
            return False
        top = self.semantic.candidates[0].confidence
        return any(c.semantic == wanted and c.confidence == top for c in self.semantic.candidates)


@dataclass(frozen=True)
class RunContents:
    """One run and its streams, sorted by topic, then id."""

    run: Run
    streams: tuple[StreamContents, ...]

    @property
    def id(self) -> RecordId:
        return self.run.id

    def topic(self, topic: str) -> tuple[StreamContents, ...]:
        """The streams declared on ``topic`` (a run may declare one twice)."""
        return tuple(s for s in self.streams if s.topic == topic)

    def carrying(
        self, semantic: Semantic | str, *, ambiguous: bool = False
    ) -> tuple[StreamContents, ...]:
        """The streams inferred to carry ``semantic``; with ``ambiguous``, also those where it
        ties with another reading."""
        semantic = _semantic(semantic)
        test = StreamContents.may_carry if ambiguous else StreamContents.carries
        return tuple(s for s in self.streams if test(s, semantic))

    def semantics(self) -> dict[Semantic, tuple[StreamContents, ...]]:
        """Every ``known`` semantic in the run, with its streams."""
        found: dict[Semantic, list[StreamContents]] = defaultdict(list)
        for stream in self.streams:
            if stream.semantic is not None and stream.semantic.semantic is not None:
                found[stream.semantic.semantic].append(stream)
        return {semantic: tuple(found[semantic]) for semantic in sorted(found)}


def run_contents(package: IngestPackage) -> tuple[RunContents, ...]:
    """Every run of ``package`` and what it contains, sorted by run id. Reads records and derived
    tables only: no series, no source, no message payload."""
    try:
        derived = read_derived(package.derived)
    except (ValueError, TypeError, KeyError) as exc:
        raise PackageInvalidError(f"the package's derived tables cannot be read: {exc}") from exc
    layouts = {r.stream: r for r in derived if isinstance(r, StreamLayout)}
    semantics = {r.stream: r for r in derived if isinstance(r, StreamSemantic)}
    runs = {r.id: r for r in package.records if isinstance(r, Run)}
    streams: dict[RecordId, list[StreamContents]] = defaultdict(list)
    for record in package.records:
        if isinstance(record, Stream):
            if record.run not in runs:
                raise PackageInvalidError(f"stream {record.id} names a run the package lacks")
            entry = StreamContents(record, layouts.get(record.id), semantics.get(record.id))
            streams[record.run].append(entry)
    return tuple(
        RunContents(
            runs[run],
            tuple(sorted(streams[run], key=lambda s: (s.topic or "", s.id))),
        )
        for run in sorted(runs)
    )
