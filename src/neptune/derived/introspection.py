"""Stream introspection: the schema registry and the semantic rules over a package's streams
(ADR 0049).

``introspect`` takes the ``Stream`` records a package holds and a way to read a definition's
bytes, and returns one ``definition_layout`` line per distinct definition, one ``stream_layout``
and one ``stream_semantic`` line per stream, the findings about definitions it could not use, and
its transform (``neptune.introspection``). It decodes no message, calls no adapter, and reads only
the byte ranges streams cite as their definitions, each at most ``max_definition_bytes`` and all
together at most ``max_total_bytes``.

Streams sharing a definition (one MCAP schema, many channels) are read and parsed once, and the
layout is written once: their ``stream_layout`` lines name it. What introspection writes is
bounded too: each layout line by ``max_layout_bytes``, all of them by ``max_output_bytes``. A
definition past either is ``not_covered`` with one finding giving the counts; the job commits.
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Final

from neptune.derived.schemas import (
    DEFINITION_KIND,
    LAYOUT_KIND,
    PARSED_ENCODINGS,
    DefinitionLayout,
    Layout,
    LayoutState,
    Parsed,
    Problem,
    SchemaLimits,
    StreamLayout,
    definition_layout_id,
    failed,
    layout_id,
    parse_definition,
    problem,
)
from neptune.derived.semantics import (
    SEMANTIC_KIND,
    Classified,
    SemanticState,
    StreamSemantic,
    classify,
    stream_semantic,
)
from neptune.identity import canonical_json
from neptune.identity.findings import ingest_finding
from neptune.identity.hashing import content_id
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ContentId, RecordId
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
    max_output_bytes: int = 32 << 20  # every definition layout one package's introspection writes

    def __post_init__(self) -> None:
        if not isinstance(self.limits, SchemaLimits):
            raise TypeError(f"limits must be SchemaLimits, got {self.limits!r}")
        for name in ("max_total_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")

    def to_json(self) -> JsonObject:
        return {
            "limits": self.limits.to_json(),
            "max_output_bytes": self.max_output_bytes,
            "max_total_bytes": self.max_total_bytes,
        }


@dataclass(frozen=True)
class Introspection:
    transform: TransformRecord
    definitions: tuple[DefinitionLayout, ...]
    layouts: tuple[StreamLayout, ...]
    semantics: tuple[StreamSemantic, ...]
    findings: tuple[IngestFinding, ...]

    def tables(self) -> dict[str, Iterator[JsonObject]]:
        """The package's three derived tables, each in id order (ADR 0036 §8)."""
        return {
            DEFINITION_KIND: (
                line.to_json() for line in sorted(self.definitions, key=lambda r: r.id)
            ),
            LAYOUT_KIND: (line.to_json() for line in sorted(self.layouts, key=lambda r: r.id)),
            SEMANTIC_KIND: (line.to_json() for line in sorted(self.semantics, key=lambda r: r.id)),
        }

    def summary(self) -> JsonObject:
        states: dict[str, int] = {}
        for layout in self.layouts:
            states[f"layout_{layout.state}"] = states.get(f"layout_{layout.state}", 0) + 1
        for semantic in self.semantics:
            states[f"semantic_{semantic.state}"] = states.get(f"semantic_{semantic.state}", 0) + 1
        return {
            "definitions": len(self.definitions),
            "findings": len(self.findings),
            "streams": len(self.layouts),
            **states,
        }


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
    "composition_not_parsed": ("definition_not_covered", FindingCategory.UNSUPPORTED),
    "parser_failed": ("definition_malformed", FindingCategory.FAILED),
}
_LIMIT: Final = ("definition_limit", FindingCategory.LIMIT)


def _guarded_parse(encoding: str, name: str | None, data: bytes, limits: SchemaLimits) -> Parsed:
    """``parse_definition``, with any exception it should never raise made a ``parser_failed``
    problem: hostile bytes cost their stream's layout, never the job (non-negotiable 7)."""
    try:
        return parse_definition(encoding, name, data, limits)
    except Exception as exc:
        return failed(problem("parser_failed", f"the parser failed: {type(exc).__name__}"))


@dataclass(frozen=True)
class _Outcome:
    """What a definition gives its streams: a state, the layout line it was written as (``known``
    only) and the parsed layout the semantic rules read, or the problem."""

    state: LayoutState
    definition: DefinitionLayout | None = None
    problem: Problem | None = None

    @property
    def layout(self) -> Layout | None:
        return None if self.definition is None else self.definition.layout


def _failed(found: Problem) -> _Outcome:
    return _Outcome(failed(found).state, problem=found)


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
        self.spent = 0  # definition bytes read
        self.written = 0  # layout bytes written
        self.outcomes: dict[tuple[EvidenceRef, str, str | None], _Outcome] = {}
        self.by_content: dict[tuple[ContentId, str, str | None], _Outcome] = {}
        self.definitions: dict[RecordId, DefinitionLayout] = {}
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

    def parse(self, ref: EvidenceRef, encoding: str, name: str | None) -> _Outcome:
        """The outcome of the definition at ``ref``: read once per citation, parsed and written
        once per distinct content."""
        key = (ref, encoding, name)
        if key not in self.outcomes:
            self.outcomes[key] = self._parse(ref, encoding, name)
        return self.outcomes[key]

    def _parse(self, ref: EvidenceRef, encoding: str, name: str | None) -> _Outcome:
        limits = self.config.limits
        if len(ref.locator) != 1 or not isinstance(ref.locator[0], ByteRange):
            return _failed(
                problem("definition_not_a_range", "the definition is not one byte range")
            )
        length = ref.locator[0].length
        if length > limits.max_definition_bytes:
            return _failed(
                problem(
                    "definition_too_large",
                    f"{length} bytes, more than the {limits.max_definition_bytes} a definition"
                    " may be; it was not read",
                    counts={"bytes": length, "limit": limits.max_definition_bytes},
                )
            )
        if self.spent + length > self.config.max_total_bytes:
            return _failed(
                problem(
                    "introspection_budget",
                    f"the package's definitions exceed {self.config.max_total_bytes} bytes;"
                    " this one was not read",
                    counts={
                        "bytes": length,
                        "limit": self.config.max_total_bytes,
                        "read": self.spent,
                    },
                )
            )
        self.spent += length
        data = self.read(ref)
        if data is None or len(data) != length:
            return _failed(
                problem("definition_unreadable", "the definition's bytes could not be read")
            )
        content = content_id(data)
        key = (content, encoding, name)
        if key not in self.by_content:
            self.by_content[key] = self._build(content, encoding, name, data)
        return self.by_content[key]

    def _build(self, content: ContentId, encoding: str, name: str | None, data: bytes) -> _Outcome:
        """Parse one distinct definition and write its layout line, within the layout's and the
        package's output budgets."""
        limits = self.config.limits
        parsed = _guarded_parse(encoding, name, data, limits)
        if parsed.layout is None:
            return _Outcome(parsed.state, problem=parsed.problem)
        layout = parsed.layout
        line_id = definition_layout_id(self.transform.id, content, encoding, layout.root)
        if line_id in self.definitions:  # another name with the same root: the same line
            return _Outcome(LayoutState.KNOWN, self.definitions[line_id])
        try:
            line = DefinitionLayout(line_id, self.transform.id, content, encoding, layout)
            size = len(canonical_json.dumps(line.to_json())) + 1
        except Exception as exc:  # never expected: the parsers emit canonical text only
            found = problem("parser_failed", f"the layout cannot be written: {type(exc).__name__}")
            return _failed(found)
        counts = {"bytes": size, "paths": len(layout.paths), "types": len(layout.types)}
        if size > limits.max_layout_bytes:
            return _failed(
                problem(
                    "layout_limit",
                    f"the layout is {size} bytes, more than the {limits.max_layout_bytes} it"
                    " may be; it was not written",
                    counts={**counts, "limit": limits.max_layout_bytes},
                )
            )
        if self.written + size > self.config.max_output_bytes:
            return _failed(
                problem(
                    "output_budget",
                    f"the package's layouts would pass {self.config.max_output_bytes} bytes;"
                    " this one was not written",
                    counts={
                        **counts,
                        "limit": self.config.max_output_bytes,
                        "written": self.written,
                    },
                )
            )
        self.written += size
        self.definitions[line_id] = line
        return _Outcome(LayoutState.KNOWN, line)

    def layout(self, stream: Stream) -> tuple[StreamLayout, _Outcome]:
        name, encoding = _text(stream.schema_name), _text(stream.schema_encoding)
        definition = stream.schema_definition
        ref = definition.value if isinstance(definition, Known) else None
        if isinstance(stream.schema_encoding, KnownAbsent) or isinstance(definition, KnownAbsent):
            outcome = _Outcome(LayoutState.KNOWN_ABSENT)
        elif encoding is not None and encoding not in PARSED_ENCODINGS:
            outcome = _Outcome(LayoutState.NOT_COVERED)
            self.report(
                "encoding_not_covered",
                FindingCategory.UNSUPPORTED,
                Severity.INFO,
                ref or _declared_evidence(stream),
                f"definitions in {encoding!r} are not parsed; the declared type name is kept",
                {"encoding": encoding},
                stream.id,
            )
        elif ref is None:
            missing = problem("no_definition", "the stream's schema definition is not known")
            outcome = _Outcome(LayoutState.UNKNOWN, problem=missing)
        elif encoding is None:
            missing = problem("unknown_encoding", "the stream's schema encoding is not known")
            outcome = _Outcome(LayoutState.UNKNOWN, problem=missing)
        else:
            outcome = self.parse(ref, encoding, name)
            if outcome.problem is not None:
                code, category = _FINDINGS.get(outcome.problem.reason, _LIMIT)
                self.report(
                    code,
                    category,
                    Severity.WARNING,
                    ref,
                    f"the stream's definition cannot be used: {outcome.problem.message}",
                    outcome.problem.to_json(),
                    stream.id,
                )
            elif outcome.layout is not None and outcome.layout.truncated:
                self.report(
                    "paths_truncated",
                    FindingCategory.LIMIT,
                    Severity.INFO,
                    ref,
                    f"the layout lists its first {self.config.limits.max_paths} field paths",
                    {"max_paths": self.config.limits.max_paths},
                    stream.id,
                )
        line = StreamLayout(
            id=layout_id(self.transform.id, stream.id),
            transform=self.transform.id,
            stream=stream.id,
            schema_name=name,
            schema_encoding=encoding,
            definition=ref,
            state=outcome.state,
            layout=None if outcome.definition is None else outcome.definition.id,
            problem=outcome.problem,
        )
        return line, outcome

    def semantic(
        self, stream: Stream, layout: StreamLayout, parsed: Layout | None
    ) -> StreamSemantic:
        try:
            classified = classify(layout.schema_name, layout.state, parsed)
        except Exception as exc:  # a rule's bug costs this stream its semantic, not the job
            classified = Classified(())
            self.report(
                "classification_failed",
                FindingCategory.FAILED,
                Severity.WARNING,
                layout.definition or _declared_evidence(stream),
                f"the semantic rules failed ({type(exc).__name__}); the semantic is unknown",
                {"error": type(exc).__name__},
                stream.id,
            )
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
        layout, outcome = run.layout(stream)
        layouts.append(layout)
        semantics.append(run.semantic(stream, layout, outcome.layout))
    definitions = tuple(run.definitions.values())
    return Introspection(
        run.transform, definitions, tuple(layouts), tuple(semantics), run.findings()
    )
