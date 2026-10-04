"""The receipt's deterministic core: built from a package's records, checked, rendered (ADR 0022).

``build_receipt`` derives everything from the records alone (the source ledger, transforms,
evidence records and findings), so the same records always give the same receipt, byte for
byte, and anyone holding a package can recompute its receipt and compare. ``render_receipt``
writes the same content for people as Markdown: no wall clock, no host, and no time converted,
so a time reads as ticks on a named clock.
"""

import itertools
from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping
from dataclasses import replace
from typing import Any, Final

from neptune.identity.ids import record_id
from neptune.model.finding import IngestFinding, Severity
from neptune.model.ids import ContentId, LogicalId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.kinds import RECORD_KINDS, kinds_at, record_version, records_version
from neptune.model.knowledge import Ambiguous, Knowledge, Known, KnownAbsent
from neptune.model.package import (
    SEVERITY_ORDER,
    AmbiguousField,
    IngestReceipt,
    ReceiptClock,
    ReceiptEntity,
    ReceiptFinding,
    ReceiptRun,
    ReceiptSource,
    ReceiptStream,
    ReceiptTransform,
    finding_order,
)
from neptune.model.run import Stream
from neptune.model.source import LocalPath, RawLocalPath, SourceLocation
from neptune.model.time import Timestamp

RECEIPT_KIND: Final = "ingest_receipt"
_LEDGER: Final = frozenset({"source_artifact", "source_revision", "source_absence"})
# Producers that cite sources without reading them: validation judges the stored package, never
# a source's bytes (ADR 0054), and clock alignment reads committed series columns (ADR 0060), so
# their findings never make them readers in ``read_by``.
NON_READERS: Final = frozenset({"neptune.validate", "neptune.clocks"})


def _walk(value: JsonValue, pointer: str = "") -> Iterator[tuple[str, JsonValue]]:
    """Every value in a JSON document with its RFC 6901 pointer."""
    yield pointer, value
    if isinstance(value, dict):
        for key, item in value.items():
            token = key.replace("~", "~0").replace("/", "~1")
            yield from _walk(item, f"{pointer}/{token}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{pointer}/{index}")


def _cited_sources(data: JsonValue) -> Iterator[tuple[str, str]]:
    """``(source, transform)`` for every provenance a record's JSON holds, at any depth."""
    for _, value in _walk(data):
        if isinstance(value, dict) and value.keys() == {"assertion_kind", "evidence", "transform"}:
            evidence, transform = value["evidence"], value["transform"]
            if isinstance(evidence, dict) and isinstance(transform, str):
                source = evidence.get("source")
                if isinstance(source, str):  # an external object not yet fetched reads nothing
                    yield source, transform


def _evidence_sources(data: JsonValue) -> Iterator[str]:
    for _, value in _walk(data):
        if isinstance(value, dict) and value.keys() == {"locator", "source"}:
            source = value["source"]
            if isinstance(source, str):
                yield source


def _read_by(record: Any, data: JsonValue) -> Iterator[tuple[str, str]]:
    """``(source, transform)`` for each source ``record`` cites, with the transform citing it."""
    yield from _cited_sources(data)
    if isinstance(record, Stream):
        yield record.series.source, record.provenance.transform
    if isinstance(record, IngestFinding):
        for source in _evidence_sources(data):
            yield source, record.transform


def cited_sources(records: Iterable[Any]) -> frozenset[ContentId]:
    """Every source the records cite: those a transform read to make them (``read_by``)."""
    return frozenset(
        ContentId(source)
        for record in records
        if record.kind not in _LEDGER and record.kind != "transform_record"
        for source, _ in _read_by(record, record.to_json())
    )


def _stated_ids(identifiers: Iterable[Knowledge[LogicalId]]) -> tuple[LogicalId, ...]:
    values: set[LogicalId] = set()
    for knowledge in identifiers:
        if isinstance(knowledge, Known):
            values.add(knowledge.value)
        elif isinstance(knowledge, Ambiguous):
            values.update(candidate.value for candidate in knowledge.candidates)
    return tuple(sorted(values, key=lambda value: (value.namespace, value.value)))


def cite(
    record: Any, data: JsonValue, judges: Collection[str], read_by: dict[str, set[str]]
) -> list[AmbiguousField]:
    """What one evidence record (not the ledger, not a transform) gives the receipt: each source
    it cites is added to ``read_by`` under the transform citing it (unless that transform only
    judges, ``NON_READERS``), and its ambiguous fields are returned. ``data`` is its JSON. The
    streaming writer (ADR 0065) calls this per record, as ``build_receipt`` does."""
    for source, transform in _read_by(record, data):
        if transform not in judges:
            read_by[source].add(transform)
    return [
        AmbiguousField(record.id, pointer)
        for pointer, value in _walk(data)
        if isinstance(value, dict) and value.get("knowledge") == "ambiguous"
    ]


def ledger_sections(
    revisions: Iterable[Any],
    absences: Iterable[Any],
    sizes: Mapping[ContentId, int],
    read_by: Mapping[str, set[str]],
) -> tuple[tuple[ReceiptSource, ...], tuple[SourceLocation, ...]]:
    """The receipt's ``sources`` and ``absent``: the head of each location's chain, sorted by
    location, each source with its size and the transforms that read it."""
    revisions, absences = list(revisions), list(absences)
    superseded = {previous for entry in (*revisions, *absences) for previous in entry.supersedes}
    heads = sorted((r for r in revisions if r.id not in superseded), key=lambda r: r.location.key)
    sources = tuple(
        ReceiptSource(
            location=revision.location,
            content_id=revision.content_id,
            size=sizes[revision.content_id],
            read_by=tuple(sorted(RecordId(t) for t in read_by.get(revision.content_id, ()))),
        )
        for revision in heads
    )
    absent = tuple(
        sorted((a.location for a in absences if a.id not in superseded), key=lambda loc: loc.key)
    )
    return sources, absent


def build_receipt(records: Iterable[Any], version: int | None = None) -> IngestReceipt:
    """The receipt core of a package holding ``records`` (ledger, transforms, records, findings).

    ``version`` is the package's schema version: by default the lowest that holds the records
    (``records_version``). The receipt counts the kinds of that version (ADR 0037 §1).
    """
    by_kind: dict[str, list[Any]] = defaultdict(list)
    for record in records:
        by_kind[record.kind].append(record)
    unknown = set(by_kind) - set(RECORD_KINDS)
    if unknown:
        raise ValueError(f"not record kinds: {sorted(unknown)}")
    if version is None:
        version = records_version(record for members in by_kind.values() for record in members)
    kinds = kinds_at(version)
    newer = sorted(set(by_kind) - set(kinds))
    if newer:
        raise ValueError(f"a schema version {version} package cannot hold {newer}")
    later = sorted(
        kind
        for kind, members in by_kind.items()
        if any(record_version(r) > version for r in members)
    )
    if later:
        raise ValueError(f"a schema version {version} package cannot hold later {later} records")
    artifacts = {artifact.content_id: artifact for artifact in by_kind["source_artifact"]}
    revisions, absences = by_kind["source_revision"], by_kind["source_absence"]

    judges = {t.id for t in by_kind["transform_record"] if t.adapter_id in NON_READERS}
    read_by: dict[str, set[str]] = defaultdict(set)
    ambiguous: list[AmbiguousField] = []
    for kind, members in by_kind.items():
        if kind in _LEDGER or kind == "transform_record":
            continue
        for record in members:
            ambiguous += cite(record, record.to_json(), judges, read_by)

    sizes = {content: artifact.size for content, artifact in artifacts.items()}
    sources, absent = ledger_sections(revisions, absences, sizes, read_by)
    transforms = sorted(by_kind["transform_record"], key=lambda t: t.id)
    runs = sorted(by_kind["run"], key=lambda r: r.id)
    streams = sorted(by_kind["stream"], key=lambda s: s.id)
    entities = sorted(
        (*by_kind["machine"], *by_kind["site"], *by_kind["asset"]), key=lambda e: e.id
    )
    findings = by_kind["ingest_finding"]
    draft = IngestReceipt(
        id=RecordId("rec:sha256:" + "0" * 64),
        sources=sources,
        absent=absent,
        transforms=tuple(
            ReceiptTransform(
                t.id, t.adapter_id, t.adapter_version, t.config_hash, t.libraries, t.upstream
            )
            for t in transforms
        ),
        records=tuple((kind, len(by_kind.get(kind, ()))) for kind in sorted(kinds)),
        clocks=tuple(
            ReceiptClock(clock.id, clock.field, clock.scope)
            for clock in sorted(by_kind["timestamp_domain"], key=lambda c: c.id)
        ),
        runs=tuple(
            ReceiptRun(
                id=run.id,
                logical_id=run.logical_id,
                machine=run.machine,
                first=run.first,
                last=run.last,
                streams=sum(1 for stream in streams if stream.run == run.id),
            )
            for run in runs
        ),
        streams=tuple(
            ReceiptStream(
                id=stream.id,
                run=stream.run,
                topic=stream.topic,
                clocks=stream.clocks,
                message_count=stream.message_count,
                first=stream.first,
                last=stream.last,
            )
            for stream in streams
        ),
        entities=tuple(
            ReceiptEntity(entity.id, entity.kind, _stated_ids(entity.identifiers))
            for entity in entities
        ),
        findings=tuple(
            sorted(
                (ReceiptFinding(f.id, f.code, f.category, f.severity, f.message) for f in findings),
                key=finding_order,
            )
        ),
        ambiguous=tuple(sorted(ambiguous, key=lambda field: (field.record, field.pointer))),
        version=version,
    )
    return replace(draft, id=receipt_id(draft))


def receipt_id(receipt: IngestReceipt) -> RecordId:
    """The core's own hash: the record id over everything but the id (ADR 0022 §3)."""
    return record_id(RECEIPT_KIND, receipt.content_json())


def check_receipt(receipt: IngestReceipt, records: Iterable[Any]) -> IngestReceipt:
    """Verify a receipt read from a package: its id recomputes, and so does all of it, at the
    schema version the receipt says it was written at."""
    if receipt_id(receipt) != receipt.id:
        raise ValueError(f"receipt {receipt.id}: id does not match its content")
    rebuilt = build_receipt(records, receipt.version)
    if rebuilt != receipt:
        raise ValueError(f"receipt {receipt.id} is not the receipt of these records ({rebuilt.id})")
    return receipt


# --- For people --------------------------------------------------------------------------------


def _short(identifier: str) -> str:
    """``rec:sha256:3a7b…`` as ``rec:3a7b9c2e41d0``; ``sha256:9f86…`` as ``sha256:9f86d081884c``."""
    prefix, _, digest = identifier.rpartition(":")
    return f"{'rec' if prefix.startswith('rec') else prefix}:{digest[:12]}"


def _cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _code(text: str) -> str:
    return f"`{_cell(text)}`"


def _location(location: SourceLocation) -> str:
    """A location as a person would type it: the path, or the store and object."""
    if isinstance(location, LocalPath):
        return location.path
    if isinstance(location, RawLocalPath):
        return f"(bytes) {location.path.hex()}"
    return f"{location.connector_id}:{location.object_id}"


def _clock_names(receipt: IngestReceipt) -> dict[str, str]:
    """A readable name per clock: its field, and where it is read when that is not everywhere."""
    names: dict[str, str] = {}
    for clock in receipt.clocks:
        where = f" ({' / '.join(clock.scope)})" if clock.scope else ""
        names[clock.id] = f"{clock.field}{where}"
    return names


def _value(value: Any, clocks: dict[str, str]) -> str:
    if isinstance(value, Timestamp):
        name = clocks.get(value.domain_id, _short(value.domain_id))
        return f"{value.ticks} on {_code(name)}"
    if isinstance(value, LogicalId):
        return _code(f"{value.namespace}:{value.value}")
    return _code(str(value))


def _state(knowledge: Knowledge[Any], clocks: dict[str, str]) -> str:
    match knowledge:
        case Known(value=value):
            return _value(value, clocks)
        case Ambiguous(candidates=candidates):
            return "ambiguous: " + " or ".join(_value(c.value, clocks) for c in candidates)
        case KnownAbsent():
            return "none (declared)"
        case _:
            return str(knowledge.state).replace("_", " ")


def _table(header: tuple[str, ...], rows: Iterable[tuple[str, ...]]) -> Iterator[str]:
    yield "| " + " | ".join(header) + " |"
    yield "|" + "---|" * len(header)
    for row in rows:
        yield "| " + " | ".join(row) + " |"


def render_receipt(receipt: IngestReceipt) -> str:
    """The receipt core as Markdown. Deterministic: the same core renders to the same text."""
    return "".join(render_lines(receipt))


def render_lines(receipt: Any) -> Iterator[str]:
    """``render_receipt`` one line at a time, each ending in a newline.

    ``receipt`` is an ``IngestReceipt``, or anything with its fields whose sections can be read
    more than once and counted (``len``): a streaming writer passes its large sections that way,
    read back from disk, so the text is never held whole (ADR 0065).
    """
    adapters = {t.id: f"{t.adapter_id} {t.adapter_version}" for t in receipt.transforms}
    clocks = _clock_names(receipt)
    counts = dict(receipt.records)
    read = [source for source in receipt.sources if source.read_by]
    severities = dict.fromkeys(SEVERITY_ORDER, 0)
    for finding in receipt.findings:
        severities[finding.severity] += 1
    lines: Iterable[str] = itertools.chain(
        [
            "# Ingest receipt",
            "",
            f"Receipt {_code(receipt.id)}. Every id below is shortened; `receipt.json` has them"
            " whole.",
            "",
            "## Summary",
            "",
            f"- Sources: {len(receipt.sources)} seen, {len(read)} read, "
            f"{len(receipt.sources) - len(read)} not read, {len(receipt.absent)} gone",
            f"- Runs: {len(receipt.runs)}; streams: {len(receipt.streams)}; "
            f"entities: {len(receipt.entities)}",
            f"- Findings: {severities[Severity.ERROR]} errors, "
            f"{severities[Severity.WARNING]} warnings, {severities[Severity.INFO]} info; "
            f"ambiguous fields: {len(receipt.ambiguous)}",
            "",
            "## Sources",
            "",
        ],
        _table(
            ("Location", "Bytes", "Content", "Read by"),
            (
                (
                    _code(_location(source.location)),
                    str(source.size),
                    _code(_short(source.content_id)),
                    ", ".join(adapters.get(t, _short(t)) for t in source.read_by) or "not read",
                )
                for source in receipt.sources
            ),
        ),
        ["", "Gone since an earlier scan: "] if receipt.absent else [],
        (f"- {_code(_location(location))}" for location in receipt.absent),
        ["", "## Adapters", ""],
        _table(
            ("Adapter", "Version", "Config", "Libraries", "Transform"),
            (
                (
                    _code(t.adapter_id),
                    _code(t.adapter_version),
                    _code(_short(t.config_hash)),
                    ", ".join(f"{name} {version}" for name, version in t.libraries) or "none",
                    _code(_short(t.id)),
                )
                for t in sorted(receipt.transforms, key=lambda t: (t.adapter_id, t.id))
            ),
        ),
        ["", "## Records", ""],
        _table(
            ("Kind", "Records"),
            ((_code(kind), str(count)) for kind, count in sorted(counts.items()) if count),
        ),
        ["", "## Runs", ""],
        _table(
            ("Run", "Session", "Machine", "First", "Last", "Streams"),
            (
                (
                    _code(_short(run.id)),
                    _state(run.logical_id, clocks),
                    _state(run.machine, clocks),
                    _state(run.first, clocks),
                    _state(run.last, clocks),
                    str(run.streams),
                )
                for run in receipt.runs
            ),
        ),
        ["", "## Streams", ""],
        _table(
            ("Stream", "Run", "Topic", "Clocks", "Messages", "First", "Last"),
            (
                (
                    _code(_short(stream.id)),
                    _code(_short(stream.run)),
                    _state(stream.topic, clocks),
                    ", ".join(_code(clocks.get(c, _short(c))) for c in stream.clocks),
                    _state(stream.message_count, clocks),
                    _state(stream.first, clocks),
                    _state(stream.last, clocks),
                )
                for stream in receipt.streams
            ),
        ),
        ["", "## Entities", ""],
        _table(
            ("Kind", "Record", "Stated ids"),
            (
                (
                    entity.record_kind,
                    _code(_short(entity.id)),
                    ", ".join(_value(identifier, clocks) for identifier in entity.identifiers),
                )
                for entity in receipt.entities
            ),
        ),
        ["", "## Findings", ""],
        [] if len(receipt.findings) else ["None."],
        (
            f"- **{finding.severity}** {_code(finding.code)} ({finding.category}): "
            f"{_cell(finding.message)} · {_code(_short(finding.id))}"
            for finding in receipt.findings
        ),
        ["", "## Ambiguous fields", ""],
        [] if len(receipt.ambiguous) else ["None."],
        (f"- {_code(_short(field.record))} {_code(field.pointer)}" for field in receipt.ambiguous),
    )
    for line in lines:
        yield line + "\n"
