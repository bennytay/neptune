"""The adapter ABI: what a format adapter is and the data it exchanges with the runtime.

ADR 0008 fixed the shape and ADR 0024 the exact types. An adapter is a static descriptor plus four
methods::

    descriptor: AdapterDescriptor                  id, version, formats, outputs, config, codes
    probe(head, hints) -> ProbeResult              is this my format? Reads only ``head``
    inspect(source, config) -> InspectResult       a cheap summary; never decodes payloads
    plan(source, config) -> Plan                   chunks with deterministic ids, plus findings
    ingest(source, chunk, config) -> ChunkOutput   pure per chunk: records, series and findings

An adapter reads only through ``SourceReader`` and writes nowhere but the private scratch
directory a ``plan`` or ``ingest`` call may be given (``scratch_directory``): the runtime owns
the store, resume, caching, sandboxing and explanation. Everything here is plain immutable data,
so a chunk and its output can cross a process boundary (MVL-10) unchanged.
``neptune.adapters.check`` turns the contract's laws into checks, and ``neptune.adapters.text``
is the reference implementation.
"""

import hashlib
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NewType, Protocol, TypeAlias

from neptune.identity import canonical_json
from neptune.identity.provenance import EvidenceRecord, transform_record
from neptune.model.finding import IngestFinding
from neptune.model.ids import (
    ContentId,
    RecordId,
    check_text,
    check_token,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.kinds import RECORD_KINDS
from neptune.model.provenance import TransformRecord
from neptune.model.record import Family
from neptune.model.series import SeriesBatch
from neptune.model.versions import SemanticVersion

# The version of this contract. An adapter declares the version it implements, and a registry
# refuses any other: a change to a method's signature or to a type below is a new version.
ABI_VERSION: Final = 1

# The most bytes ``probe`` is given: the first ``min(size, PROBE_HEAD_SIZE)`` bytes of the source.
PROBE_HEAD_SIZE: Final = 64 * 1024

# Confidence bands for ``ProbeResult``. Adapters calibrate against these, so that one adapter's 0.9
# means what another's does; equal evidence then gives equal confidence, and the tie is surfaced.
VERIFIED: Final = 1.0  # the format's structure was parsed and checked beyond its signature
SIGNATURE: Final = 0.9  # the format's magic bytes or signature matched
STRUCTURE: Final = 0.7  # a text format's grammar or root structure matched
GENERIC: Final = 0.4  # only a generic decoding applies: the bytes are UTF-8 text, say
NAME_ONLY: Final = 0.1  # only the name, an extension or an empty file suggests the format

# The record kinds an adapter may emit: evidence records. The source ledger and transforms are the
# runtime's, and findings travel apart from records.
EVIDENCE_KINDS: Final = frozenset(
    kind
    for kind, (cls, _) in RECORD_KINDS.items()
    if vars(cls)["family"] not in (Family.SOURCE, Family.LINEAGE, Family.FINDING)
)

_LOCATOR_STEP_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_FINDING_NAME: Final = re.compile(r"[a-z][a-z0-9_\-]*")


class ContractError(ValueError):
    """An adapter, its descriptor or its output breaks the contract (ADR 0008, ADR 0024)."""


class ConfigError(ContractError):
    """A config names an option the adapter does not have, or gives one a value it cannot take."""


def _check_canonical(what: str, value: JsonValue) -> None:
    try:
        canonical_json.dumps(value)
    except ValueError as exc:
        raise ContractError(f"{what} must be canonical JSON: {exc}") from exc


def _check_sorted(what: str, names: list[str]) -> None:
    if names != sorted(set(names)):
        raise ContractError(f"{what} must be unique and sorted: {names}")


def _check_count(what: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{what} must be a non-negative integer, got {value!r}")


# --- Reading a source --------------------------------------------------------------------------


class SourceReader(Protocol):
    """One source's bytes: read-only, random-access and bounded (ADR 0008 §2).

    The runtime hands an adapter one of these per source; the bytes are the source artifact's, so
    ``content_id`` is what every ``EvidenceRef`` the adapter emits cites.
    """

    @property
    def content_id(self) -> ContentId: ...

    @property
    def size(self) -> int: ...

    def read(self, offset: int, length: int) -> bytes:
        """``length`` bytes from ``offset``; fewer only where the source ends before them."""
        ...


READ_SIZE: Final = 1024 * 1024

# --- Scratch space -----------------------------------------------------------------------------

_SCRATCH: Final[ContextVar[Path | None]] = ContextVar("neptune_adapter_scratch", default=None)


def scratch_directory() -> Path | None:
    """The empty private directory this call may write temporary files in, or ``None``.

    The runtime makes one for each ``plan`` and ``ingest`` call under the workspace's scratch root
    and removes it when the call returns (ADR 0029 §4, ADR 0033 §2): a spool for a nested archive,
    a decoder that wants a file. Nothing in it outlives the call, so output never depends on it.
    In the sandbox it is the only place a call can write, each file at most ``scratch_bytes``
    (``neptune.runtime.sandbox.Limits``). ``None`` for ``probe`` and ``inspect``, outside a job,
    and on a host without Landlock, where the sandbox cannot confine writes to one directory:
    an adapter that needs scratch and has none reports that as a finding.
    """
    return _SCRATCH.get()


@contextmanager
def scratch_granted(directory: Path | None) -> Iterator[None]:
    """The runtime's side: ``scratch_directory()`` answers ``directory`` inside the block."""
    token = _SCRATCH.set(directory)
    try:
        yield
    finally:
        _SCRATCH.reset(token)


class ShortReadError(Exception):
    """A reader served no bytes inside the size it declares (ADR 0029 §3).

    The source is shorter than the artifact it was hashed as, or changed under the reader. It is
    not an adapter bug, and it is not the adapter's to report: adapters let it propagate, and the
    runtime records ``neptune.discovery.verify.short_read_finding(source, offset, length)`` for
    it and goes on with the job. ``[offset, offset + length)`` is the declared range that was not
    served.
    """

    def __init__(self, source: ContentId, offset: int, length: int) -> None:
        super().__init__(f"{source} served no bytes at {offset}; {length} declared bytes unread")
        self.source = source
        self.offset = offset
        self.length = length


def read_pieces(
    source: SourceReader, start: int, end: int, size: int = READ_SIZE
) -> Iterator[bytes]:
    """The bytes ``[start, end)`` of ``source`` in order, at most ``size`` at a time.

    A range outside the source is the caller's error (``ValueError``). A reader that serves no
    bytes inside the range raises ``ShortReadError``.
    """
    if not 0 <= start <= end <= source.size or size <= 0:
        raise ValueError(f"cannot read [{start}, {end}) of {source.size} bytes in {size}s")
    while start < end:
        piece = source.read(start, min(size, end - start))
        if not piece:
            raise ShortReadError(source.content_id, start, end - start)
        yield piece
        start += len(piece)


# --- The descriptor ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Magic:
    """Bytes every file of a format holds at a fixed offset: MCAP's at 0, tar's ``ustar`` at 257."""

    offset: int
    data: bytes

    def __post_init__(self) -> None:
        _check_count("magic offset", self.offset)
        if not isinstance(self.data, bytes) or not self.data:
            raise ContractError("magic must be non-empty bytes")

    def to_json(self) -> JsonObject:
        return {"data_hex": self.data.hex(), "offset": self.offset}


@dataclass(frozen=True)
class FormatSpec:
    """One format an adapter reads, as people and the probe engine (MVL-8) know it.

    ``extensions`` are lowercase with their dot (``.txt``). They are hints, never evidence: probing
    decides from the bytes, so a renamed file is still read.
    """

    name: str
    media_types: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    magic: tuple[Magic, ...] = ()

    def __post_init__(self) -> None:
        check_text("format name", self.name)
        _check_sorted("media types", list(self.media_types))
        for media_type in self.media_types:
            check_text("media type", media_type)
        _check_sorted("extensions", list(self.extensions))
        for extension in self.extensions:
            if not re.fullmatch(r"\.[a-z0-9][a-z0-9_.\-]*", extension):
                raise ContractError(f"an extension is lowercase with its dot: {extension!r}")
        for magic in self.magic:
            if not isinstance(magic, Magic):
                raise ContractError(f"not a Magic: {magic!r}")

    def to_json(self) -> JsonObject:
        return {
            "extensions": list(self.extensions),
            "magic": [magic.to_json() for magic in self.magic],
            "media_types": list(self.media_types),
            "name": self.name,
        }


Setting: TypeAlias = bool | int | float | str


def _setting_type(value: Setting) -> type:
    return bool if isinstance(value, bool) else type(value)


@dataclass(frozen=True)
class ConfigOption:
    """One setting an adapter reads from its config. The default's type is the setting's type.

    A setting is part of the transform (ADR 0006 §4), so changing it gives new record ids. Planning
    granularity is therefore never a setting: chunking must not change what an adapter outputs.
    """

    name: str
    default: Setting
    description: str
    choices: tuple[Setting, ...] = ()

    def __post_init__(self) -> None:
        check_token("option name", self.name)
        check_text("option description", self.description)
        if not isinstance(self.default, bool | int | float | str):
            raise ContractError(f"option {self.name}: a default is a JSON scalar")
        _check_canonical(f"option {self.name}", self.default)
        kind = _setting_type(self.default)
        for choice in self.choices:
            if _setting_type(choice) is not kind:
                raise ContractError(f"option {self.name}: choices must be {kind.__name__}s")
        if self.choices and self.default not in self.choices:
            raise ContractError(f"option {self.name}: the default must be one of the choices")
        if len(set(self.choices)) != len(self.choices):
            raise ContractError(f"option {self.name}: choices repeat")

    def resolve(self, value: JsonValue) -> Setting:
        """``value`` as this setting, or ``ConfigError``. An integer is accepted for a float."""
        kind = _setting_type(self.default)
        if kind is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, bool | int | float | str) or _setting_type(value) is not kind:
            raise ConfigError(f"option {self.name} takes a {kind.__name__}, got {value!r}")
        try:
            _check_canonical(f"option {self.name}", value)
        except ContractError as exc:
            raise ConfigError(str(exc)) from exc
        if self.choices and value not in self.choices:
            raise ConfigError(f"option {self.name} is one of {list(self.choices)}, got {value!r}")
        return value

    def to_json(self) -> JsonObject:
        return {
            "choices": list(self.choices),
            "default": self.default,
            "description": self.description,
            "name": self.name,
        }


@dataclass(frozen=True)
class Documented:
    """A name an adapter puts in its output and what it means: a finding code, a locator step."""

    name: str
    description: str

    def __post_init__(self) -> None:
        check_text("name", self.name)
        check_text("description", self.description)

    def to_json(self) -> JsonObject:
        return {"description": self.description, "name": self.name}


@dataclass(frozen=True)
class Resources:
    """What one ``ingest`` call needs with the default config, for the runtime's limits (MVL-10).

    ``max_memory`` is the bytes it may hold at once. ``streaming`` says it reads its source in
    bounded pieces, so its memory does not grow with the source's size.
    """

    max_memory: int
    streaming: bool

    def __post_init__(self) -> None:
        _check_count("max_memory", self.max_memory)
        if not isinstance(self.streaming, bool):
            raise ContractError("streaming must be a bool")

    def to_json(self) -> JsonObject:
        return {"max_memory": self.max_memory, "streaming": self.streaming}


@dataclass(frozen=True)
class AdapterDescriptor:
    """Everything static about an adapter: who it is, what it reads and writes, and its rules.

    - ``id`` is the transform's ``adapter_id`` and prefixes every code and step the adapter
      emits. ``version`` is SemVer, bumped whenever any output byte may change (ADR 0003).
      ``abi`` is the ``ABI_VERSION`` it implements.
    - ``record_kinds`` are the evidence kinds ``ingest`` emits; ``config`` its settings;
      ``libraries`` the output-affecting dependencies and their versions (ADR 0006 §4).
    - ``finding_codes`` (``<id>.<name>``), ``locator_steps`` (``<id>:<name>``) and
      ``conventions`` document what the output means, as ADR 0006 §3 and ADR 0017 §9 require.
      The checks refuse a code or step that is not declared here.
    - ``resources`` and ``security`` are for the sandbox (MVL-10).
    """

    id: str
    version: str
    abi: int
    summary: str
    formats: tuple[FormatSpec, ...]
    record_kinds: tuple[str, ...]
    config: tuple[ConfigOption, ...]
    libraries: tuple[tuple[str, str], ...]
    finding_codes: tuple[Documented, ...]
    locator_steps: tuple[Documented, ...]
    conventions: tuple[Documented, ...]
    resources: Resources
    security: tuple[str, ...]

    def __post_init__(self) -> None:
        check_token("adapter id", self.id)
        try:
            SemanticVersion(self.version)
        except ValueError as exc:
            raise ContractError(f"adapter {self.id}: version must be SemVer: {exc}") from exc
        _check_count("abi", self.abi)
        check_text("summary", self.summary)
        if "\n" in self.summary:
            raise ContractError("summary is one line")
        if not self.formats or not all(isinstance(f, FormatSpec) for f in self.formats):
            raise ContractError(f"adapter {self.id} reads at least one FormatSpec")
        _check_sorted("record kinds", list(self.record_kinds))
        if not self.record_kinds or not set(self.record_kinds) <= EVIDENCE_KINDS:
            raise ContractError(
                f"adapter {self.id}: record kinds must be evidence kinds, got {self.record_kinds}"
            )
        _check_sorted("config options", [option.name for option in self.config])
        _check_sorted("libraries", [name for name, _ in self.libraries])
        for name, version in self.libraries:
            check_text("library name", name)
            check_text("library version", version)
        _check_sorted("finding codes", [code.name for code in self.finding_codes])
        for code in self.finding_codes:
            producer, _, name = code.name.partition(".")
            if producer != self.id or not _FINDING_NAME.fullmatch(name):
                raise ContractError(f"finding code {code.name!r} must be '{self.id}.<name>'")
        _check_sorted("locator steps", [step.name for step in self.locator_steps])
        for step in self.locator_steps:
            producer, _, name = step.name.partition(":")
            if producer != self.id or not _LOCATOR_STEP_NAME.fullmatch(name):
                raise ContractError(f"locator step {step.name!r} must be '{self.id}:<name>'")
        _check_sorted("conventions", [convention.name for convention in self.conventions])
        if not isinstance(self.resources, Resources):
            raise ContractError("resources must be a Resources")
        for note in self.security:
            check_text("security note", note)

    def option(self, name: str) -> ConfigOption:
        for option in self.config:
            if option.name == name:
                return option
        raise ConfigError(f"adapter {self.id} has no option {name!r}")

    def to_json(self) -> JsonObject:
        """The descriptor for explanations and dry runs (MVL-15)."""
        return {
            "abi": self.abi,
            "config": [option.to_json() for option in self.config],
            "conventions": [convention.to_json() for convention in self.conventions],
            "finding_codes": [code.to_json() for code in self.finding_codes],
            "formats": [spec.to_json() for spec in self.formats],
            "id": self.id,
            "libraries": dict(self.libraries),
            "locator_steps": [step.to_json() for step in self.locator_steps],
            "record_kinds": list(self.record_kinds),
            "resources": self.resources.to_json(),
            "security": list(self.security),
            "summary": self.summary,
            "version": self.version,
        }


# --- Config ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterConfig:
    """An adapter's resolved settings, every option filled in, and the transform they define.

    Build one with ``configure``. ``transform`` is the ``TransformRecord`` of this adapter at this
    version with these settings: the id every record, finding and chunk the adapter emits names.
    """

    values: JsonObject
    transform: TransformRecord

    def __post_init__(self) -> None:
        if dict(self.transform.config) != dict(self.values):
            raise ContractError("the transform's config must be these values")

    def text(self, name: str) -> str:
        value = self.values[name]
        if not isinstance(value, str):
            raise ConfigError(f"option {name} is not text: {value!r}")
        return value

    def integer(self, name: str) -> int:
        value = self.values[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"option {name} is not an integer: {value!r}")
        return value

    def number(self, name: str) -> float:
        value = self.values[name]
        if not isinstance(value, float):
            raise ConfigError(f"option {name} is not a float: {value!r}")
        return value

    def flag(self, name: str) -> bool:
        value = self.values[name]
        if not isinstance(value, bool):
            raise ConfigError(f"option {name} is not a bool: {value!r}")
        return value


def configure(
    descriptor: AdapterDescriptor, values: Mapping[str, JsonValue] | None = None
) -> AdapterConfig:
    """Resolve ``values`` against the descriptor's options: unknown names and bad values raise.

    Omitting an option and giving its default are the same config, with the same hash.
    """
    given = dict(values or {})
    unknown = sorted(set(given) - {option.name for option in descriptor.config})
    if unknown:
        raise ConfigError(f"adapter {descriptor.id} has no options {unknown}")
    resolved: dict[str, JsonValue] = {
        option.name: option.resolve(given[option.name]) if option.name in given else option.default
        for option in descriptor.config
    }
    transform = transform_record(
        adapter_id=descriptor.id,
        adapter_version=descriptor.version,
        config=resolved,
        libraries=dict(descriptor.libraries),
    )
    return AdapterConfig(resolved, transform)


# --- Probe and inspect -------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeHints:
    """What is known of a source besides its first bytes. Advisory only: names lie.

    ``name`` is the last part of a location the source was seen at (``""`` if it has none) and
    ``size`` its size in bytes, so ``probe`` knows whether ``head`` is the whole source.
    """

    name: str
    size: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str):
            raise ContractError("a hint name is text")
        _check_count("size", self.size)


@dataclass(frozen=True)
class ProbeReason:
    """One structured reason for a probe's confidence, for or against: ``text.utf8``."""

    code: str
    message: str

    def __post_init__(self) -> None:
        producer, _, name = self.code.partition(".")
        check_token("reason producer", producer)
        if not _FINDING_NAME.fullmatch(name):
            raise ContractError(f"a reason code is '<adapter id>.<name>': {self.code!r}")
        check_text("reason message", self.message)

    def to_json(self) -> JsonObject:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ProbeResult:
    """How sure an adapter is that ``head`` starts a source of its format, and why.

    ``confidence`` is in ``[0, 1]``, calibrated against the bands above; 0 means "not mine".
    ``version`` is the format version the head declares, as it writes it (``2.0``), if any.
    """

    confidence: float
    reasons: tuple[ProbeReason, ...]
    version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.confidence, float) or not 0.0 <= self.confidence <= 1.0:
            raise ContractError(f"confidence is a float in [0, 1], got {self.confidence!r}")
        if not all(isinstance(reason, ProbeReason) for reason in self.reasons):
            raise ContractError("reasons must be ProbeReasons")
        if self.version is not None:
            check_text("format version", self.version)

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "confidence": self.confidence,
            "reasons": [reason.to_json() for reason in self.reasons],
        }
        if self.version is not None:
            out["version"] = self.version
        return out


@dataclass(frozen=True)
class InspectResult:
    """A cheap summary of one source, for dry runs and explanations (MVL-15).

    ``summary`` holds facts the adapter documents (sizes, channels, counts and extents as the
    source declares them). ``findings`` are problems seen on the way; a dry run shows them and an
    ingest finds them again, so they never enter a package from here.
    """

    summary: JsonObject
    findings: tuple[IngestFinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.summary, Mapping):
            raise ContractError("a summary is a JSON object")
        _check_canonical("summary", self.summary)
        if not all(isinstance(finding, IngestFinding) for finding in self.findings):
            raise ContractError("findings must be IngestFindings")


# --- Chunks and plans --------------------------------------------------------------------------

# A chunk's identity: "chunk:sha256:<64 hex>". The runtime resumes, caches and isolates by it.
ChunkId = NewType("ChunkId", str)
CHUNK_ID_SCHEME: Final = "neptune.chunk-id/1"
_CHUNK_ID: Final = re.compile(r"chunk:sha256:[0-9a-f]{64}")


def chunk_id(source: ContentId, transform: RecordId, context: JsonObject) -> ChunkId:
    """The id of ``transform`` applied to the part of ``source`` that ``context`` describes.

    It covers the adapter, its version, its config and libraries (through the transform), the
    source's bytes and everything ``ingest`` is told besides them, so equal ids mean equal output
    and any change to the planned context is a new id (ADR 0008 §3).
    """
    if not isinstance(context, Mapping):
        raise ContractError("a chunk's context is a JSON object")
    _check_canonical("chunk context", context)
    payload: JsonObject = {
        "context": context,
        "scheme": CHUNK_ID_SCHEME,
        "source": parse_content_id(source),
        "transform": parse_record_id(transform),
    }
    return ChunkId("chunk:sha256:" + hashlib.sha256(canonical_json.dumps(payload)).hexdigest())


@dataclass(frozen=True)
class Chunk:
    """One unit of work: what ``ingest`` reads and the runtime caches, resumes, isolates, retries.

    ``context`` is everything ``ingest`` needs beyond the source's bytes, computed by ``plan``: a
    byte range, the offsets the chunk starts from, a schema table it must decode with. The adapter
    documents it. ``cost`` estimates the source bytes the chunk reads, for scheduling only:
    ``ingest`` must not depend on it.
    """

    id: ChunkId
    source: ContentId
    transform: RecordId
    context: JsonObject
    cost: int

    def __post_init__(self) -> None:
        if not isinstance(self.context, Mapping):
            raise ContractError("a chunk's context is a JSON object")
        _check_canonical("chunk context", self.context)
        _check_count("cost", self.cost)
        if self.id != chunk_id(self.source, self.transform, self.context):
            raise ContractError(f"chunk {self.id} does not match its source, transform and context")

    def to_json(self) -> JsonObject:
        return {
            "context": self.context,
            "cost": self.cost,
            "id": self.id,
            "source": self.source,
            "transform": self.transform,
        }


def chunk_from_json(data: JsonValue) -> Chunk:
    """Parse strictly; the id must recompute from the rest."""
    if not isinstance(data, Mapping) or data.keys() != {
        "context",
        "cost",
        "id",
        "source",
        "transform",
    }:
        raise ContractError(f"a chunk is exactly {{context, cost, id, source, transform}}: {data}")
    chunk, context, cost = data["id"], data["context"], data["cost"]
    source, transform = data["source"], data["transform"]
    if not isinstance(chunk, str) or not _CHUNK_ID.fullmatch(chunk):
        raise ContractError(f"not a chunk id: {chunk!r}")
    if not isinstance(source, str) or not isinstance(transform, str):
        raise ContractError("a chunk's source and transform are ids")
    if not isinstance(context, Mapping) or isinstance(cost, bool) or not isinstance(cost, int):
        raise ContractError("a chunk's context is an object and its cost an integer")
    return Chunk(
        ChunkId(chunk), parse_content_id(source), parse_record_id(transform), context, cost
    )


def make_chunk(
    source: SourceReader, config: AdapterConfig, context: JsonObject, cost: int
) -> Chunk:
    """A chunk of ``source`` for this adapter and config, its id derived from the rest."""
    transform = config.transform.id
    return Chunk(
        chunk_id(source.content_id, transform, context),
        source.content_id,
        transform,
        context,
        cost,
    )


@dataclass(frozen=True)
class Plan:
    """What ``plan`` returns: at least one chunk, in order, and the findings planning made.

    A plan is deterministic: the same source, adapter version and config give the same chunks
    in the same order, which is what resume and caching rely on. Its findings go into the
    package like any chunk's.
    """

    chunks: tuple[Chunk, ...]
    findings: tuple[IngestFinding, ...] = ()

    def __post_init__(self) -> None:
        if not self.chunks:
            raise ContractError("a plan has at least one chunk, even for an empty source")
        if not all(isinstance(chunk, Chunk) for chunk in self.chunks):
            raise ContractError("a plan's chunks must be Chunks")
        if len({chunk.id for chunk in self.chunks}) != len(self.chunks):
            raise ContractError("a plan repeats a chunk")
        if not all(isinstance(finding, IngestFinding) for finding in self.findings):
            raise ContractError("findings must be IngestFindings")


# --- Output ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkOutput:
    """What ``ingest`` returns for one chunk: evidence records, series batches and findings.

    Records and findings are complete canonical records; the runtime only stores them.
    """

    records: tuple[EvidenceRecord, ...] = ()
    series: tuple[SeriesBatch, ...] = ()
    findings: tuple[IngestFinding, ...] = ()

    def __post_init__(self) -> None:
        for record in self.records:
            if getattr(record, "kind", None) not in EVIDENCE_KINDS:
                raise ContractError(f"not an evidence record: {record!r}")
        if not all(isinstance(batch, SeriesBatch) for batch in self.series):
            raise ContractError("series must be SeriesBatches")
        if not all(isinstance(finding, IngestFinding) for finding in self.findings):
            raise ContractError("findings must be IngestFindings")


# --- The adapter -------------------------------------------------------------------------------


class Adapter(Protocol):
    """A format adapter (ADR 0008): a structural protocol, not a base class.

    Laws (``docs/adapter-contract.md``): ``ingest`` is pure per chunk; ``plan`` is deterministic;
    problems are findings, never exceptions; ``probe`` reads only ``head`` and ``inspect`` never
    decodes payloads; every record cites exact bytes of the one source it was given.
    """

    @property
    def descriptor(self) -> AdapterDescriptor: ...

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult: ...

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult: ...

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan: ...

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput: ...
