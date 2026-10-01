"""Stream introspection: the schema registry and the semantic rules over a package's streams
(ADR 0049).

``introspect`` takes the ``Stream`` records a package holds and a way to read a definition's
bytes, and returns one ``stream_layout`` and one ``stream_semantic`` line per stream, the
findings about definitions it could not use, and its transform (``neptune.introspection``). It
decodes no message, calls no adapter, and reads only the byte ranges streams cite as their
definitions, each at most ``max_definition_bytes`` and all together at most ``max_total_bytes``.
Streams sharing a definition (one MCAP schema, many channels) are read and parsed once.
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final

from neptune.derived.schemas import (
    LAYOUT_KIND,
    PARSED_ENCODINGS,
    LayoutState,
    Parsed,
    Problem,
    SchemaLimits,
    StreamLayout,
    layout_id,
    parse_definition,
)
from neptune.derived.semantics import (
    SEMANTIC_KIND,
    SemanticState,
    StreamSemantic,
    classify,
    stream_semantic,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import Knowledge, Known, KnownAbsent
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, TransformRecord
from neptune.model.run import Stream

INTROSPECTION_ID: Final = "neptune.introspection"
INTROSPECTION_VERSION: Final = "0.1.0"
_PREFIX: Final = "neptune.introspection."

# Reads a definition's cited bytes; ``None`` when they cannot be read (moved, changed, refused).
DefinitionReader = Callable[[EvidenceRef], bytes | None]


@dataclass(frozen=True)
class IntrospectionConfig:
    limits: SchemaLimits = field(default_factory=SchemaLimits)
    max_total_bytes: int = 64 << 20  # every definition one package's introspection reads

    def __post_init__(self) -> None:
        if not isinstance(self.limits, SchemaLimits):
            raise TypeError(f"limits must be SchemaLimits, got {self.limits!r}")
        total = self.max_total_bytes
        if isinstance(total, bool) or not isinstance(total, int) or total < 1:
            raise ValueError(f"max_total_bytes must be a positive integer, got {total!r}")

    def to_json(self) -> JsonObject:
        return {"limits": self.limits.to_json(), "max_total_bytes": self.max_total_bytes}


@dataclass(frozen=True)
class Introspection:
    transform: TransformRecord
    layouts: tuple[StreamLayout, ...]
    semantics: tuple[StreamSemantic, ...]
    findings: tuple[IngestFinding, ...]

    def tables(self) -> dict[str, Iterator[JsonObject]]:
        """The package's two derived tables, each in id order (ADR 0036 §8)."""
        return {
            LAYOUT_KIND: (line.to_json() for line in sorted(self.layouts, key=lambda r: r.id)),
            SEMANTIC_KIND: (line.to_json() for line in sorted(self.semantics, key=lambda r: r.id)),
        }

    def summary(self) -> JsonObject:
        states: dict[str, int] = {}
        for layout in self.layouts:
            states[f"layout_{layout.state}"] = states.get(f"layout_{layout.state}", 0) + 1
        for semantic in self.semantics:
            states[f"semantic_{semantic.state}"] = states.get(f"semantic_{semantic.state}", 0) + 1
        return {"findings": len(self.findings), "streams": len(self.layouts), **states}


def _text(knowledge: Knowledge[str]) -> str | None:
    return knowledge.value if isinstance(knowledge, Known) else None


def _declared_evidence(stream: Stream) -> EvidenceRef:
    """Where the stream's type name is declared: its own citation, else the stream's."""
    name = stream.schema_name
    if isinstance(name, Known) and isinstance(name.provenance, Provenance):
        return name.provenance.evidence
    return stream.provenance.evidence


# Problem reasons, by the finding each becomes.
_FINDINGS: Final = {
    "malformed": ("definition_malformed", FindingCategory.CORRUPT),
    "invalid_utf8": ("definition_malformed", FindingCategory.UNREPRESENTABLE),
    "definition_unreadable": ("definition_unreadable", FindingCategory.MISSING),
    "definition_not_a_range": ("definition_unreadable", FindingCategory.UNSUPPORTED),
}
_LIMIT: Final = ("definition_limit", FindingCategory.LIMIT)


class _Run:
    def __init__(
        self, config: IntrospectionConfig, read: DefinitionReader, upstream: Sequence[RecordId]
    ) -> None:
        self.config, self.read = config, read
        self.transform = transform_record(
            adapter_id=INTROSPECTION_ID,
            adapter_version=INTROSPECTION_VERSION,
            config=config.to_json(),
            upstream=sorted(set(upstream)),
        )
        self.spent = 0
        self.parsed: dict[tuple[EvidenceRef, str, str | None], Parsed] = {}
        # One finding per definition (or per stream without one): (code, subject) -> streams
        self.reports: dict[
            tuple[str, EvidenceRef],
            tuple[FindingCategory, Severity, str, JsonObject, list[RecordId]],
        ] = {}

    def report(
        self,
        code: str,
        category: FindingCategory,
        severity: Severity,
        subject: EvidenceRef,
        message: str,
        details: JsonObject,
        stream: RecordId,
    ) -> None:
        entry = self.reports.setdefault((code, subject), (category, severity, message, details, []))
        entry[4].append(stream)

    def findings(self) -> tuple[IngestFinding, ...]:
        found = [
            ingest_finding(
                code=_PREFIX + code,
                category=category,
                severity=severity,
                subject=subject,
                transform=self.transform,
                message=message,
                details=details,
                records=streams,
            )
            for (code, subject), (
                category,
                severity,
                message,
                details,
                streams,
            ) in self.reports.items()
        ]
        return tuple(sorted(found, key=lambda f: f.id))

    def parse(self, ref: EvidenceRef, encoding: str, name: str | None) -> Parsed:
        key = (ref, encoding, name)
        if key in self.parsed:
            return self.parsed[key]
        limits = self.config.limits
        if len(ref.locator) != 1 or not isinstance(ref.locator[0], ByteRange):
            problem = Problem("definition_not_a_range", "the definition is not one byte range")
            result = Parsed(LayoutState.UNKNOWN, problem=problem)
        elif (length := ref.locator[0].length) > limits.max_definition_bytes:
            result = Parsed(
                LayoutState.UNKNOWN,
                problem=Problem(
                    "definition_too_large",
                    f"{length} bytes, more than the {limits.max_definition_bytes} a definition"
                    " may be; it was not read",
                ),
            )
        elif self.spent + length > self.config.max_total_bytes:
            result = Parsed(
                LayoutState.UNKNOWN,
                problem=Problem(
                    "introspection_budget",
                    f"the package's definitions exceed {self.config.max_total_bytes} bytes;"
                    " this one was not read",
                ),
            )
        else:
            self.spent += length
            data = self.read(ref)
            if data is None or len(data) != length:
                problem = Problem(
                    "definition_unreadable", "the definition's bytes could not be read"
                )
                result = Parsed(LayoutState.UNKNOWN, problem=problem)
            else:
                result = parse_definition(encoding, name, data, limits)
        self.parsed[key] = result
        return result

    def layout(self, stream: Stream) -> StreamLayout:
        name, encoding = _text(stream.schema_name), _text(stream.schema_encoding)
        definition = stream.schema_definition
        ref = definition.value if isinstance(definition, Known) else None
        problem = None
        if isinstance(stream.schema_encoding, KnownAbsent) or isinstance(definition, KnownAbsent):
            state = LayoutState.KNOWN_ABSENT
            parsed = Parsed(state)
        elif encoding is not None and encoding not in PARSED_ENCODINGS:
            parsed = Parsed(LayoutState.NOT_COVERED)
            self.report(
                "encoding_not_covered",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                ref or _declared_evidence(stream),
                f"definitions in {encoding!r} are not parsed; the declared type name is kept",
                {"encoding": encoding},
                stream.id,
            )
        elif ref is None or encoding is None:
            missing = "definition" if ref is None else "encoding"
            problem = Problem("no_definition", f"the stream's schema {missing} is not known")
            parsed = Parsed(LayoutState.UNKNOWN, problem=problem)
        else:
            parsed = self.parse(ref, encoding, name)
            if parsed.problem is not None:
                code, category = _FINDINGS.get(parsed.problem.reason, _LIMIT)
                self.report(
                    code,
                    category,
                    Severity.WARNING,
                    ref,
                    f"the stream's definition cannot be used: {parsed.problem.message}",
                    parsed.problem.to_json(),
                    stream.id,
                )
            elif parsed.layout is not None and parsed.layout.truncated:
                self.report(
                    "paths_truncated",
                    FindingCategory.LIMIT,
                    Severity.INFO,
                    ref,
                    f"the layout lists its first {self.config.limits.max_paths} field paths",
                    {"max_paths": self.config.limits.max_paths},
                    stream.id,
                )
        return StreamLayout(
            id=layout_id(self.transform.id, stream.id),
            transform=self.transform.id,
            stream=stream.id,
            schema_name=name,
            schema_encoding=encoding,
            definition=ref,
            state=parsed.state,
            layout=parsed.layout,
            problem=parsed.problem or problem,
        )

    def semantic(self, stream: Stream, layout: StreamLayout) -> StreamSemantic:
        classified = classify(layout.schema_name, layout.state, layout.layout)
        evidence = [_declared_evidence(stream)]
        if layout.definition is not None and layout.state is LayoutState.KNOWN:
            evidence.insert(0, layout.definition)
        if classified.contradicted is not None:
            self.report(
                "type_contradicts_layout",
                FindingCategory.INCONSISTENT,
                Severity.WARNING,
                layout.definition or evidence[-1],
                f"the definition of {classified.contradicted} lacks fields that type has; it is"
                " not classified by its name",
                {"type": classified.contradicted},
                stream.id,
            )
        result = stream_semantic(self.transform.id, stream.id, classified, evidence)
        if result.state is SemanticState.AMBIGUOUS:
            tied = [
                str(c.semantic)
                for c in result.candidates
                if c.confidence == result.candidates[0].confidence
            ]
            self.report(
                "semantic_ambiguous",
                FindingCategory.AMBIGUOUS,
                Severity.INFO,
                evidence[0],
                f"the stream's type reads equally as {', '.join(tied)}; none was chosen",
                {"candidates": tied},
                stream.id,
            )
        return result


def introspect(
    streams: Iterable[Stream],
    read: DefinitionReader,
    config: IntrospectionConfig | None = None,
) -> Introspection:
    """Introspect ``streams`` (module docstring). Deterministic: the same streams, bytes and
    config give the same lines, findings and transform, in any order."""
    ordered = sorted({s.id: s for s in streams}.values(), key=lambda s: s.id)
    run = _Run(config or IntrospectionConfig(), read, [s.provenance.transform for s in ordered])
    layouts, semantics = [], []
    for stream in ordered:
        layout = run.layout(stream)
        layouts.append(layout)
        semantics.append(run.semantic(stream, layout))
    return Introspection(run.transform, tuple(layouts), tuple(semantics), run.findings())
