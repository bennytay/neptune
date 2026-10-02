"""Media streams: which streams carry images, video or point clouds, and how a frame is reached
(ADR 0056).

``index_media`` reads a package's ``Stream`` records, the ``stream_semantic`` lines introspection
inferred for them (ADR 0049) and each stream's series row count, and writes one ``media_stream``
line per stream that carries media. It decodes no message and reads no row: a frame's times and
byte range are its series row (ADR 0018), which the SDK reads at query time
(``neptune.sdk.media``). The line says what kind of media the stream carries and why, which
hydrator turns a row's locator into the frame's bytes, and the state of each derivative
(thumbnail, keyframe), so a consumer knows before asking what it can and cannot get.

Frames are budgeted: every media stream's rows are charged, in stream id order, against
``max_frames``. A stream past it is ``not_covered``, with a ``neptune.media.frame_budget``
finding giving the counts; the query refuses it rather than scan it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Final

from neptune.derived.provenance import (
    DERIVED_SCHEMA_VERSION,
    INFERRED,
    InferredProvenance,
    derived_object,
)
from neptune.derived.semantics import Semantic, StreamSemantic
from neptune.identity.findings import ingest_finding
from neptune.identity.ids import record_id
from neptune.identity.provenance import transform_record
from neptune.model._fields import json_int, json_str
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import Known
from neptune.model.provenance import (
    EvidenceRef,
    Provenance,
    TransformRecord,
    evidence_ref_from_json,
)
from neptune.model.run import Stream

MEDIA_KIND: Final = "media_stream"
MEDIA_ID: Final = "neptune.media"
MEDIA_VERSION: Final = "0.1.0"
_PREFIX: Final = "neptune.media."


class Media(StrEnum):
    IMAGE = "image"  # raw pixels in the message
    COMPRESSED_IMAGE = "compressed_image"  # one encoded still per message (JPEG, PNG, ...)
    VIDEO = "video"  # one packet of an encoded video stream per message (H.264, H.265, AV1, ...)
    POINT_CLOUD = "point_cloud"  # a packed point buffer with its declared fields


class MediaState(StrEnum):
    KNOWN = "known"  # indexed: its frames can be queried
    NOT_COVERED = "not_covered"  # past the frame budget: Neptune chose not to index it


class DerivativeState(StrEnum):
    ON_REQUEST = "on_request"  # made lazily from a hydrated frame, with derivative provenance
    NOT_COVERED = "not_covered"  # needs a codec Neptune does not carry
    NOT_APPLICABLE = "not_applicable"  # the derivative has no meaning for this media


# How a row's locator becomes a frame's bytes, by the adapter that wrote the stream. A stream of
# another adapter is indexed (its rows still give times and byte ranges) but not hydrated.
HYDRATORS: Final[Mapping[str, str]] = {"mcap": "mcap_message"}

# Declared type names of video packets. No ADR 0049 semantic is video: a packet's fields
# (``data``, ``format``) are a compressed image's, so only the declared name tells them apart.
VIDEO_TYPES: Final = frozenset(
    {
        "foxglove.CompressedVideo",
        "foxglove_msgs/CompressedVideo",
        "foxglove_msgs/msg/CompressedVideo",
    }
)
_BY_SEMANTIC: Final[Mapping[Semantic, Media]] = {
    Semantic.IMAGE: Media.IMAGE,
    Semantic.COMPRESSED_IMAGE: Media.COMPRESSED_IMAGE,
    Semantic.POINT_CLOUD: Media.POINT_CLOUD,
}
# Raw image payloads whose layout the SDK reads with the standard library (``neptune.sdk.media``).
THUMBNAIL_ENCODINGS: Final = frozenset({"cdr", "ros1"})


@dataclass(frozen=True)
class Derivative:
    """The state of one derivative (``thumbnail``, ``keyframe``) and why."""

    state: DerivativeState
    reason: str | None = None

    def to_json(self) -> JsonObject:
        return _present({"reason": self.reason, "state": str(self.state)})


def _derivative_from_json(data: JsonValue) -> Derivative:
    if not isinstance(data, Mapping) or set(data) - {"reason"} != {"state"}:
        raise ValueError(f"a derivative is {{state, reason?}}, got {data!r}")
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError(f"a derivative's reason is text, got {reason!r}")
    return Derivative(DerivativeState(json_str(data["state"], "state")), reason)


def _present(obj: Mapping[str, "JsonValue | None"]) -> JsonObject:
    """``obj`` without its absent (``None``) members: canonical JSON has no null (ADR 0004)."""
    return {key: value for key, value in obj.items() if value is not None}


def media_id(transform: RecordId, stream: RecordId) -> RecordId:
    return record_id(MEDIA_KIND, {"stream": stream, "transform": transform})


@dataclass(frozen=True)
class MediaStream:
    """One media stream as a derived line, ``inferred``: its kind is read from its semantic (or,
    for video, its declared type name). Everything else on it is a fact about the stream.

    - ``basis``: the ``stream_semantic`` line the kind was read from; ``None`` for video.
    - ``frames``: the rows of the stream's series, counted from the series, not declared.
    - ``hydrator``: how a row's locator becomes the frame's bytes; ``None`` when no hydrator
      covers the stream's adapter (the frames still have times and byte ranges).
    - ``state``: ``not_covered`` past the frame budget, with ``counts``.
    """

    kind: ClassVar[str] = MEDIA_KIND
    id: RecordId
    transform: RecordId
    stream: RecordId
    media: Media
    basis: RecordId | None
    evidence: tuple[EvidenceRef, ...]
    message_encoding: str | None
    frames: int
    hydrator: str | None
    thumbnail: Derivative
    keyframe: Derivative
    state: MediaState = MediaState.KNOWN
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.id != media_id(self.transform, self.stream):
            raise ValueError(f"{self.id} is not the id of this stream's media line")
        if self.basis is not None:
            parse_record_id(self.basis)
        if isinstance(self.frames, bool) or not isinstance(self.frames, int) or self.frames < 0:
            raise ValueError(f"frames is a count, got {self.frames!r}")
        if (self.state is MediaState.NOT_COVERED) != bool(self.counts):
            raise ValueError("a not_covered media line gives its counts, and only it")
        InferredProvenance(self.evidence, self.transform)  # checks the evidence

    @property
    def provenance(self) -> InferredProvenance:
        return InferredProvenance(self.evidence, self.transform)

    def to_json(self) -> JsonObject:
        return _present(
            {
                "assertion_kind": INFERRED,
                "basis": self.basis,
                "counts": dict(sorted(self.counts.items())),
                "evidence": [ref.to_json() for ref in self.evidence],
                "frames": self.frames,
                "hydrator": self.hydrator,
                "id": self.id,
                "keyframe": self.keyframe.to_json(),
                "kind": MEDIA_KIND,
                "media": str(self.media),
                "message_encoding": self.message_encoding,
                "schema_version": DERIVED_SCHEMA_VERSION,
                "state": str(self.state),
                "stream": self.stream,
                "thumbnail": self.thumbnail.to_json(),
                "transform": self.transform,
            }
        )


def _optional_text(value: "JsonValue | None", name: str) -> str | None:
    return None if value is None else json_str(value, name)


def media_stream_from_json(data: JsonValue) -> MediaStream:
    keys = {
        "basis",
        "counts",
        "evidence",
        "frames",
        "hydrator",
        "id",
        "keyframe",
        "media",
        "message_encoding",
        "state",
        "stream",
        "thumbnail",
        "transform",
    }
    optional = {"basis", "hydrator", "message_encoding"}
    present = set(data) & optional if isinstance(data, Mapping) else set()
    obj = derived_object(data, MEDIA_KIND, (keys - optional) | present)
    counts = obj["counts"]
    if not isinstance(counts, Mapping):
        raise ValueError("counts must be an object")
    refs = obj["evidence"]
    if not isinstance(refs, list | tuple):
        raise ValueError("evidence must be an array of evidence refs")
    basis = obj.get("basis")
    return MediaStream(
        id=parse_record_id(json_str(obj["id"], "id")),
        transform=parse_record_id(json_str(obj["transform"], "transform")),
        stream=parse_record_id(json_str(obj["stream"], "stream")),
        media=Media(json_str(obj["media"], "media")),
        basis=None if basis is None else parse_record_id(json_str(basis, "basis")),
        evidence=tuple(evidence_ref_from_json(ref) for ref in refs),
        message_encoding=_optional_text(obj.get("message_encoding"), "message_encoding"),
        frames=json_int(obj["frames"], "frames"),
        hydrator=_optional_text(obj.get("hydrator"), "hydrator"),
        thumbnail=_derivative_from_json(obj["thumbnail"]),
        keyframe=_derivative_from_json(obj["keyframe"]),
        state=MediaState(json_str(obj["state"], "state")),
        counts={str(k): json_int(v, f"counts.{k}") for k, v in counts.items()},
    )


@dataclass(frozen=True)
class MediaConfig:
    max_frames: int = 1 << 24  # the rows of every media stream one package indexes

    def __post_init__(self) -> None:
        value = self.max_frames
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"max_frames must be a positive integer, got {value!r}")

    def to_json(self) -> JsonObject:
        return {"hydrators": dict(sorted(HYDRATORS.items())), "max_frames": self.max_frames}


@dataclass(frozen=True)
class MediaIndex:
    transform: TransformRecord
    lines: tuple[MediaStream, ...]
    findings: tuple[IngestFinding, ...]

    def tables(self) -> dict[str, Iterable[JsonObject]]:
        return {MEDIA_KIND: [line.to_json() for line in sorted(self.lines, key=lambda r: r.id)]}

    def summary(self) -> JsonObject:
        found: dict[str, int] = {}
        for line in self.lines:
            found[str(line.media)] = found.get(str(line.media), 0) + 1
        return {
            "findings": len(self.findings),
            "frames": sum(line.frames for line in self.lines),
            "streams": len(self.lines),
            **{f"media_{k}": v for k, v in sorted(found.items())},
        }


def _text(knowledge: object) -> str | None:
    if isinstance(knowledge, Known) and isinstance(knowledge.value, str):
        return knowledge.value
    return None


def _declared(stream: Stream) -> EvidenceRef:
    name = stream.schema_name
    if isinstance(name, Known) and isinstance(name.provenance, Provenance):
        return name.provenance.evidence
    return stream.provenance.evidence


def media_of(stream: Stream, semantic: StreamSemantic | None) -> Media | None:
    """The media a stream carries: its declared type, if a video packet's, else its semantic."""
    if _text(stream.schema_name) in VIDEO_TYPES:
        return Media.VIDEO
    if semantic is None or semantic.semantic is None:
        return None
    return _BY_SEMANTIC.get(semantic.semantic)


def derivatives(media: Media, encoding: str | None, hydrator: str | None) -> tuple[Derivative, ...]:
    """(thumbnail, keyframe) for a stream: what Neptune can make lazily, and why not otherwise."""
    if media is Media.POINT_CLOUD:
        none = Derivative(DerivativeState.NOT_APPLICABLE, "point_cloud")
        return none, none
    if media is Media.VIDEO:
        codec = Derivative(DerivativeState.NOT_COVERED, "codec_not_covered")
        return codec, codec
    still = Derivative(DerivativeState.NOT_APPLICABLE, "every_frame_is_a_still")
    if hydrator is None:
        return Derivative(DerivativeState.NOT_COVERED, "no_hydrator"), still
    if media is Media.COMPRESSED_IMAGE:
        return Derivative(DerivativeState.NOT_COVERED, "codec_not_covered"), still
    if encoding not in THUMBNAIL_ENCODINGS:
        return Derivative(DerivativeState.NOT_COVERED, "message_encoding_not_covered"), still
    return Derivative(DerivativeState.ON_REQUEST), still


def index_media(
    streams: Iterable[Stream],
    semantics: Iterable[StreamSemantic],
    frames: Mapping[RecordId, int],
    adapters: Mapping[RecordId, str],
    config: MediaConfig | None = None,
) -> MediaIndex | None:
    """One ``media_stream`` line per media stream (module docstring); ``None`` when no stream
    carries media, so a package without media gets no transform and no table.

    ``frames`` counts each stream's series rows; ``adapters`` names the adapter whose transform
    wrote each stream (``Stream.provenance.transform``). Deterministic in any input order.
    """
    config = config or MediaConfig()
    by_stream: dict[RecordId, StreamSemantic] = {}
    for line in semantics:  # one introspection per package; the lowest id if ever several
        if line.stream not in by_stream or line.id < by_stream[line.stream].id:
            by_stream[line.stream] = line
    chosen: list[tuple[Stream, Media]] = []
    for stream in sorted({s.id: s for s in streams}.values(), key=lambda s: s.id):
        media = media_of(stream, by_stream.get(stream.id))
        if media is not None:
            chosen.append((stream, media))
    if not chosen:
        return None
    upstream: set[RecordId] = {s.provenance.transform for s, _ in chosen}
    upstream.update(by_stream[s.id].transform for s, m in chosen if s.id in by_stream)
    transform = transform_record(
        adapter_id=MEDIA_ID,
        adapter_version=MEDIA_VERSION,
        config=config.to_json(),
        upstream=sorted(upstream),
    )
    lines: list[MediaStream] = []
    reports: dict[tuple[str, str], tuple[str, JsonObject, list[Stream]]] = {}
    charged = 0
    for stream, media in chosen:
        semantic = by_stream.get(stream.id) if media is not Media.VIDEO else None
        encoding = _text(stream.message_encoding)
        hydrator = HYDRATORS.get(adapters.get(stream.provenance.transform, ""))
        thumbnail, keyframe = derivatives(media, encoding, hydrator)
        count = frames.get(stream.id, 0)
        state, counts = MediaState.KNOWN, {}
        if charged + count > config.max_frames:
            state = MediaState.NOT_COVERED
            counts = {"frames": count, "indexed": charged, "limit": config.max_frames}
            reports.setdefault(
                ("frame_budget", ""),
                (
                    f"the package's media frames pass {config.max_frames}; streams past it are"
                    " not indexed",
                    {"limit": config.max_frames},
                    [],
                ),
            )[2].append(stream)
        else:
            charged += count
        if hydrator is None:
            reports.setdefault(
                ("hydration_not_covered", ""),
                (
                    "no hydrator reads these streams' frames; their times and byte ranges are"
                    " still indexed",
                    {"hydrators": sorted(HYDRATORS)},
                    [],
                ),
            )[2].append(stream)
        for name, made in (("thumbnail", thumbnail), ("keyframe", keyframe)):
            if made.state is DerivativeState.NOT_COVERED and made.reason != "no_hydrator":
                reports.setdefault(
                    ("derivative_not_covered", f"{name}:{made.reason}"),
                    (
                        f"no {name} is made for these streams ({made.reason})",
                        {"derivative": name, "reason": made.reason or ""},
                        [],
                    ),
                )[2].append(stream)
        evidence = (_declared(stream),) if semantic is None else semantic.evidence
        lines.append(
            MediaStream(
                id=media_id(transform.id, stream.id),
                transform=transform.id,
                stream=stream.id,
                media=media,
                basis=None if semantic is None else semantic.id,
                evidence=evidence,
                message_encoding=encoding,
                frames=count,
                hydrator=hydrator,
                thumbnail=thumbnail,
                keyframe=keyframe,
                state=state,
                counts=counts,
            )
        )
    findings = [
        ingest_finding(
            code=_PREFIX + code,
            category=FindingCategory.LIMIT
            if code == "frame_budget"
            else FindingCategory.UNSUPPORTED,
            severity=Severity.WARNING if code == "frame_budget" else Severity.INFO,
            subject=_declared(members[0]),
            transform=transform,
            message=message,
            details={**details, "streams": len(members)},
            records=[s.id for s in members],
        )
        for (code, _), (message, details, members) in sorted(reports.items())
    ]
    return MediaIndex(transform, tuple(lines), tuple(sorted(findings, key=lambda f: f.id)))
