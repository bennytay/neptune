"""JSON, YAML and TOML machine configuration as typed snapshots (ADR 0037).

What it emits for a configuration file:

- one ``ConfigurationSnapshot`` per document (a JSON or TOML file is one; a YAML stream has one
  per ``---`` document), citing the document's root: its format, the YAML version it declares,
  the file's encoding, byte-order mark and line endings, its comments verbatim, how many values
  it has and their digest;
- one ``ConfigurationValue`` per node of the document, in document order: mappings, sequences,
  YAML aliases and scalars, each with its path, its position in its parent, its YAML tag, its
  text as written and its reading in the format's own schema, citing the exact span it is
  written at.

The rules, also in the descriptor's conventions:

- **Formats** are told apart by the bytes: JSON, then TOML, then YAML (``_read``). Values are
  read by the standard library's ``json`` and ``tomllib`` and by PyYAML's pure-Python parser,
  whose events are used and never constructed into objects.
- **Nothing is coerced** beyond what the format defines. JSON numbers keep their spelling in
  ``text``; a TOML date-time keeps its kind; YAML's implicit types follow the version the document
  declares, and an undeclared version gives both YAML 1.1's and 1.2's reading where they differ.
- **Locators** are RFC 6901 pointers into the document as parsed, after a ``config:document``
  step in YAML. An entry whose key repeats in its mapping is addressed by its position instead
  (``config:entry``), so no two values share a citation.
- **Hostile input** costs findings, never exceptions: undecodable bytes, a file over
  ``max_bytes``, nesting past ``max_depth``, a scalar past ``max_scalar_length``, repeated keys,
  aliases (never expanded), application tags, numbers beyond binary64.

Planning reads and parses the whole file and plans one chunk per ``chunk_values`` values of a
document; the first chunk of a document also emits its snapshot and its findings. Every chunk
parses the file again, so its output depends only on the bytes, never on another chunk.
"""

import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import yaml

from neptune.adapters.config._read import read_text, sniff
from neptune.adapters.config._text import (
    InvalidEncoding,
    decode,
    detect,
    line_and_column,
    line_endings,
)
from neptune.adapters.config._tree import (
    Alias,
    Collection,
    Document,
    Issue,
    Limits,
    Node,
    Null,
    Parse,
    Spot,
    Value,
    pointer_token,
)
from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    STRUCTURE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    ConfigOption,
    Documented,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
    read_pieces,
)
from neptune.identity.configuration import configuration_digest
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import evidence_record_id
from neptune.model.configuration import (
    ConfigAlias,
    ConfigCollection,
    ConfigFormat,
    ConfigNode,
    ConfigurationSnapshot,
    ConfigurationValue,
    LineEndings,
    TextEncoding,
)
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.knowledge import (
    INHERITED,
    Ambiguous,
    AssertionKind,
    Candidate,
    Knowledge,
    Known,
    KnownAbsent,
    NotApplicable,
    NotCovered,
    ProvenanceSlot,
    Unknown,
)
from neptune.model.provenance import (
    ByteRange,
    EvidenceRef,
    JsonPointer,
    Locator,
    Provenance,
    Span,
    adapter_locator,
)
from neptune.model.versions import DeclaredVersion

ADAPTER_ID: Final = "config"
DOCUMENT_STEP: Final = f"{ADAPTER_ID}:document"
ENTRY_STEP: Final = f"{ADAPTER_ID}:entry"
MIB: Final = 1024 * 1024
DEFAULT_CHUNK_VALUES: Final = 10_000
# json and tomllib are the interpreter's: its minor version is an output-affecting library.
PYTHON: Final = f"{sys.version_info.major}.{sys.version_info.minor}"
_MESSAGE: Final = 300  # a parser's message is cut to this many characters in a finding


def _code(name: str) -> str:
    return f"{ADAPTER_ID}.{name}"


DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="JSON, YAML and TOML configuration: a typed snapshot per document, a value per node.",
    formats=(
        FormatSpec("JSON", media_types=("application/json",), extensions=(".json",)),
        FormatSpec("TOML", media_types=("application/toml",), extensions=(".toml",)),
        FormatSpec("YAML", media_types=("application/yaml",), extensions=(".yaml", ".yml")),
    ),
    record_kinds=(ConfigurationSnapshot.kind, ConfigurationValue.kind),
    config=(
        ConfigOption(
            "max_bytes", 8 * MIB, "a file larger than this is not read (config.too_large)"
        ),
        ConfigOption(
            "max_depth", 200, "a document nested deeper than this is not read (config.too_deep)"
        ),
        ConfigOption(
            "max_scalar_length",
            MIB,
            "a scalar or key of more code points than this is not read (config.scalar_too_large)",
        ),
        ConfigOption(
            "yaml_version",
            "declared",
            "the YAML version a document without a %YAML directive is typed by: declared reads"
            " both 1.1 and 1.2 and keeps both readings where they differ",
            choices=("1.1", "1.2", "declared"),
        ),
    ),
    libraries=(("python", PYTHON), ("pyyaml", yaml.__version__)),
    finding_codes=(
        Documented(
            _code("byte_order_mark"),
            "a JSON or TOML file starts with a byte-order mark its format does not define; it is"
            " read past (inconsistent, info)",
        ),
        Documented(
            _code("duplicate_key"),
            "entries repeat a key of their mapping; every one is kept in source order and"
            " addressed by its position (inconsistent, warning)",
        ),
        Documented(
            _code("invalid_encoding"),
            "the bytes are not valid UTF-8, or not valid in the encoding their byte-order mark"
            " names; nothing is read (corrupt, error)",
        ),
        Documented(
            _code("invalid_value"),
            "a scalar's text is not a value of the type its tag or pattern names; its value is"
            " Unknown (corrupt, warning)",
        ),
        Documented(
            _code("mixed_line_endings"),
            "the file mixes LF, CR LF and lone CR line breaks (inconsistent, info)",
        ),
        Documented(
            _code("no_document"),
            "the file declares no configuration: empty, blank or only comments (missing, info)",
        ),
        Documented(
            _code("nonstandard_json"),
            "JSON values spelled NaN or Infinity, which RFC 8259 does not define; read as the"
            " non-finite numbers they name (inconsistent, info)",
        ),
        Documented(
            _code("scalar_too_large"),
            "scalars over max_scalar_length; their text and value are Unknown (limit, warning)",
        ),
        Documented(
            _code("syntax_error"),
            "the text is not JSON, TOML or YAML from the cited span on, which is not read; the"
            " message is the parser's (corrupt, error)",
        ),
        Documented(
            _code("too_deep"),
            "a document nested deeper than max_depth; it is not read (limit, error)",
        ),
        Documented(
            _code("too_large"),
            "a file over max_bytes; it is not read (limit, error)",
        ),
        Documented(
            _code("undefined_alias"),
            "YAML aliases to an anchor no node before them carries in the document; their value"
            " is Unknown (corrupt, warning)",
        ),
        Documented(
            _code("unrepresentable_value"),
            "values no record can hold as declared: numbers beyond binary64's range or 14,000"
            " bits, strings with unpaired surrogates; their value is Unknown (unrepresentable,"
            " warning)",
        ),
        Documented(
            _code("unresolved_tag"),
            "YAML scalars with a tag the YAML type repository does not define: the application"
            " reads them; their value is Unknown (unsupported, warning)",
        ),
        Documented(
            _code("unsupported_key"),
            "mapping entries whose key no path can hold (a collection, an alias to one, invalid"
            " text, over max_scalar_length); the entries are not read (unrepresentable, error)",
        ),
        Documented(
            _code("yaml_version_undeclared"),
            "the document declares no YAML version and plain scalars read differently under"
            " YAML 1.1 and 1.2; each keeps both readings (ambiguous, warning)",
        ),
        Documented(
            _code("yaml_version_unsupported"),
            "a %YAML directive names a version other than 1.1 or 1.2; the document is typed as"
            " yaml_version says (unsupported, warning)",
        ),
    ),
    locator_steps=(
        Documented(
            DOCUMENT_STEP,
            "fields index: the document at that position (0-based) of a YAML stream; a JSON"
            " pointer into it follows",
        ),
        Documented(
            ENTRY_STEP,
            "fields order: the entry at that position (0-based, source order) of the mapping the"
            " step before addresses; used for every entry whose key repeats there, then a JSON"
            " pointer into the entry's value follows",
        ),
    ),
    conventions=(
        Documented(
            "chunks",
            "one chunk per chunk_values values of a document, in document order; context: the"
            " document's index, the format, and the values' range [start, end). The chunk at"
            " start 0 also emits the snapshot and the document's value findings",
        ),
        Documented(
            "comments",
            "every # comment, # included, to the end of its line, citing its span; a YAML"
            " comment belongs to the document whose extent holds it (before the first: the"
            " first; after the last: the last). Never attached to a value",
        ),
        Documented(
            "datetimes",
            "TOML's offset date-time, local date-time, local date and local time keep their"
            " kind, as ISO 8601; a YAML 1.1 timestamp the same, a zone-less one local, never"
            " assumed UTC. text keeps the declared spelling",
        ),
        Documented(
            "formats",
            "the bytes decide, never the name: JSON, then TOML, then YAML, the first that"
            " accepts the whole text. If none does, the syntax error is that of the reader that"
            " read furthest",
        ),
        Documented(
            "numbers",
            "a JSON or TOML integer is exact, a float the nearest binary64; text keeps the"
            " spelling (1.0, 0x1F, 1_000). JSON integers are numbers without fraction or"
            " exponent (RFC 8259's int)",
        ),
        Documented(
            "paths",
            "keys verbatim (a string's decoded content; a YAML key's scalar text) and sequence"
            " positions as integers; () is the root. The pointer escapes them per RFC 6901",
        ),
        Documented(
            "spans",
            "code points of the text: the bytes after a byte-order mark, decoded as UTF-8 or as"
            " the mark names. A value cites its node's span (a YAML node's tag and anchor"
            " included; a TOML table its [header]); where none is found, its pointer alone",
        ),
        Documented(
            "values",
            "null (JSON null, YAML null) is KnownAbsent citing the document; a reading no"
            " record can hold is Unknown with a finding; YAML scalars whose 1.1 and 1.2"
            " readings differ, in a document declaring no version, are Ambiguous (1.1 first)",
        ),
        Documented(
            "yaml",
            "PyYAML's events: tags as written (expanded) or the non-specific ? (plain scalars,"
            " collections) and ! (quoted and block scalars); quoted and block scalars are"
            " strings; aliases are references to their anchor's node, never expanded; merge"
            " keys (<<) are kept as keys",
        ),
    ),
    resources=Resources(max_memory=1024 * MIB, streaming=False),
    security=(
        "Never constructs YAML objects: only parser events are read, so no tag runs code.",
        "Never expands a YAML alias: a billion-laughs document costs one value per alias.",
        "Reads at most max_bytes, nests at most max_depth, holds no scalar over"
        " max_scalar_length; everything past them is a finding.",
        "Parses with pure-Python PyYAML and the standard library's json and tomllib.",
    ),
)


# --- Reading a source --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    """A source as text, read in a format; what stopped it, if it was not."""

    size: int
    too_large: bool = False
    invalid: InvalidEncoding | None = None
    text: str = ""
    encoding: TextEncoding = TextEncoding.UTF_8
    bom: int = 0
    parse: Parse | None = None


def _load(source: SourceReader, config: AdapterConfig, only: ConfigFormat | None) -> _Loaded:
    if source.size > config.integer("max_bytes"):
        return _Loaded(source.size, too_large=True)
    data = b"".join(read_pieces(source, 0, source.size))
    decoded = decode(data)
    if isinstance(decoded, InvalidEncoding):
        return _Loaded(source.size, invalid=decoded)
    limits = Limits(config.integer("max_depth"), config.integer("max_scalar_length"))
    parse = read_text(decoded.text, decoded.encoding, limits, config.text("yaml_version"), only)
    return _Loaded(source.size, False, None, decoded.text, decoded.encoding, decoded.bom, parse)


def _int(context: JsonObject, key: str) -> int:
    value = context[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"chunk context {key} must be an integer, got {value!r}")
    return value


def _pointer(tokens: tuple[str, ...]) -> str:
    return "".join(f"/{token}" for token in tokens)


def _path_pointer(node: Node) -> str:
    return _pointer(tuple(pointer_token(segment) for segment in node.path))


def _shorten(message: str) -> str:
    message = " ".join(message.split())
    return message if len(message) <= _MESSAGE else message[: _MESSAGE - 1] + "…"


# --- Findings about the source -----------------------------------------------------------------


def _source_findings(
    source: SourceReader, config: AdapterConfig, loaded: _Loaded
) -> Iterator[IngestFinding]:
    """What planning finds: problems with the file as a whole, or with whole documents."""

    def finding(
        name: str,
        category: FindingCategory,
        severity: Severity,
        where: Locator,
        message: str,
        details: dict[str, JsonValue],
    ) -> IngestFinding:
        return ingest_finding(
            code=_code(name),
            category=category,
            severity=severity,
            subject=EvidenceRef(source.content_id, (where,)),
            transform=config.transform,
            message=message,
            details=details,
        )

    whole = ByteRange(0, source.size)
    if loaded.too_large:
        limit = config.integer("max_bytes")
        yield finding(
            "too_large",
            FindingCategory.LIMIT,
            Severity.ERROR,
            whole,
            f"the file holds {source.size} bytes, over max_bytes ({limit}); it is not read",
            {"bytes": source.size, "max_bytes": limit},
        )
        return
    if loaded.invalid is not None:
        bad = loaded.invalid
        yield finding(
            "invalid_encoding",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            ByteRange(bad.offset, source.size - bad.offset),
            f"byte {bad.offset} is not valid {bad.encoding} ({bad.reason}); the file is not read",
            {"encoding": str(bad.encoding), "first_invalid_byte": bad.offset},
        )
        return
    text, parse = loaded.text, loaded.parse
    endings = line_endings(text)
    if endings is LineEndings.MIXED:
        yield finding(
            "mixed_line_endings",
            FindingCategory.INCONSISTENT,
            Severity.INFO,
            whole,
            "the file mixes line breaks (LF, CR LF, CR); spans and text keep them as written",
            {},
        )
    if parse is None or (not parse.documents and not parse.too_deep and parse.problem is None):
        yield finding(
            "no_document",
            FindingCategory.MISSING,
            Severity.INFO,
            whole,
            "the file is empty, blank or only comments: it declares no configuration",
            {},
        )
        return
    if loaded.bom and parse.format is not ConfigFormat.YAML:
        yield finding(
            "byte_order_mark",
            FindingCategory.INCONSISTENT,
            Severity.INFO,
            ByteRange(0, loaded.bom),
            f"{parse.format} defines no byte-order mark; it was read past",
            {"bytes": loaded.bom},
        )
    if parse.problem is not None:
        problem = parse.problem
        line, column = line_and_column(text, problem.offset)
        lost = (
            "the file is not read"
            if problem.start == 0
            else f"documents from {problem.document} on are not read"
        )
        yield finding(
            "syntax_error",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            Span(problem.start, len(text)),
            f"not {parse.format} at line {line}, column {column}:"
            f" {_shorten(problem.message)}; {lost}",
            {
                "column": column,
                "document": problem.document,
                "format": str(parse.format),
                "line": line,
                "offset": problem.offset,
            },
        )
    limit = config.integer("max_depth")
    for deep in parse.too_deep:
        yield finding(
            "too_deep",
            FindingCategory.LIMIT,
            Severity.ERROR,
            Span(*deep.extent),
            f"document {deep.document} nests deeper than max_depth ({limit}); it is not read",
            {"document": deep.document, "max_depth": limit},
        )
    for document, version, spot in parse.unsupported_version:
        yield finding(
            "yaml_version_unsupported",
            FindingCategory.UNSUPPORTED,
            Severity.WARNING,
            Span(*spot),
            f"document {document} declares YAML {version}; it is typed as yaml_version"
            f" ({config.text('yaml_version')}) says",
            {"document": document, "version": version},
        )


# --- Records of a document ---------------------------------------------------------------------

# Code, category, severity and what to say, for each problem with values.
_ISSUES: Final[dict[Issue, tuple[FindingCategory, Severity, str]]] = {
    Issue.DUPLICATE_KEY: (
        FindingCategory.INCONSISTENT,
        Severity.WARNING,
        "{count} entries repeat a key of their mapping; each is kept in source order",
    ),
    Issue.UNREPRESENTABLE: (
        FindingCategory.UNREPRESENTABLE,
        Severity.WARNING,
        "{count} values cannot be held as declared; their value is Unknown",
    ),
    Issue.SCALAR_TOO_LARGE: (
        FindingCategory.LIMIT,
        Severity.WARNING,
        "{count} scalars exceed max_scalar_length; their text and value are Unknown",
    ),
    Issue.UNRESOLVED_TAG: (
        FindingCategory.UNSUPPORTED,
        Severity.WARNING,
        "{count} scalars carry a tag the application reads; their value is Unknown",
    ),
    Issue.INVALID_VALUE: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "{count} scalars are not values of the type their tag or pattern names; Unknown",
    ),
    Issue.UNDEFINED_ALIAS: (
        FindingCategory.CORRUPT,
        Severity.WARNING,
        "{count} aliases name an anchor no node before them carries; their value is Unknown",
    ),
    Issue.AMBIGUOUS_TYPE: (
        FindingCategory.AMBIGUOUS,
        Severity.WARNING,
        "the document declares no YAML version, and {count} plain scalars read differently"
        " under YAML 1.1 and 1.2; each keeps both readings",
    ),
    Issue.NONSTANDARD_JSON: (
        FindingCategory.INCONSISTENT,
        Severity.INFO,
        "{count} values are spelled NaN or Infinity, which RFC 8259 does not define",
    ),
}


class _Records:
    """The records of one document: its values, its snapshot and its findings."""

    def __init__(
        self,
        source: SourceReader,
        config: AdapterConfig,
        loaded: _Loaded,
        fmt: ConfigFormat,
        document: Document,
    ) -> None:
        self.source = source
        self.config = config
        self.transform = config.transform
        self.loaded = loaded
        self.format = fmt
        self.document = document
        self.nodes = document.nodes
        self.prefix: tuple[Locator, ...] = (
            (adapter_locator(DOCUMENT_STEP, {"index": document.index}),)
            if fmt is ConfigFormat.YAML
            else ()
        )
        self.root = EvidenceRef(source.content_id, (*self.prefix, JsonPointer("")))
        self.snapshot_id = evidence_record_id(ConfigurationSnapshot.kind, self.root, self.transform)
        self.document_provenance = self._provenance(self.root)
        self._steps, self._tokens = self._locators()

    def _provenance(self, evidence: EvidenceRef) -> Provenance:
        return Provenance(evidence, self.transform.id, AssertionKind.OBSERVED)

    def _cite(self, spot: Spot | None) -> ProvenanceSlot:
        if spot is None:
            return INHERITED
        return self._provenance(EvidenceRef(self.source.content_id, (Span(*spot),)))

    def _locators(self) -> tuple[list[tuple[Locator, ...]], list[tuple[str, ...]]]:
        """Each node's steps before its final pointer, and that pointer's tokens."""
        steps: list[tuple[Locator, ...]] = []
        tokens: list[tuple[str, ...]] = []
        for node in self.nodes:
            if node.parent < 0:
                steps.append(self.prefix)
                tokens.append(())
            elif node.repeated:
                parent = node.parent
                entry = adapter_locator(ENTRY_STEP, {"order": node.order})
                steps.append((*steps[parent], JsonPointer(_pointer(tokens[parent])), entry))
                tokens.append(())
            else:
                steps.append(steps[node.parent])
                tokens.append((*tokens[node.parent], pointer_token(node.path[-1])))
        return steps, tokens

    def evidence(self, index: int) -> EvidenceRef:
        locator = (*self._steps[index], JsonPointer(_pointer(self._tokens[index])))
        return EvidenceRef(self.source.content_id, locator)

    def value(self, index: int) -> ConfigurationValue:
        node = self.nodes[index]
        evidence = self.evidence(index)
        cited = self._cite(node.span)
        value: Knowledge[ConfigNode]
        text: Knowledge[str] = Known(node.text) if node.text is not None else Unknown()
        match node.value:
            case Collection(type=kind, length=length):
                value, text = Known(ConfigCollection(kind, length), cited), NotApplicable()
            case Alias(anchor=anchor, target=target) if target is not None:
                value, text = Known(ConfigAlias(anchor, target), cited), NotApplicable()
            case Null():
                value = KnownAbsent(self.document_provenance)
            case Value(readings=(single,)):
                value = Known(single, cited)
            case Value(readings=readings):
                value = Ambiguous(tuple(Candidate(reading, cited) for reading in readings))
            case _:
                value = Unknown(cited)
        return ConfigurationValue(
            id=evidence_record_id(ConfigurationValue.kind, evidence, self.transform),
            provenance=self._provenance(evidence),
            snapshot=self.snapshot_id,
            path=node.path,
            order=node.order,
            tag=Known(node.tag) if node.tag is not None else NotCovered(),
            text=text,
            value=value,
        )

    def snapshot(self, values: list[ConfigurationValue]) -> ConfigurationSnapshot:
        version: Knowledge[DeclaredVersion]
        if self.format is not ConfigFormat.YAML:
            version = NotCovered()
        elif self.document.version is None:
            version = Unknown()
        else:
            declared, spot = self.document.version
            version = Known(DeclaredVersion(declared), self._cite(spot))
        text = self.loaded.text
        return ConfigurationSnapshot(
            id=self.snapshot_id,
            provenance=self.document_provenance,
            format=self.format,
            format_version=version,
            encoding=self.loaded.encoding,
            byte_order_mark=self.loaded.bom > 0,
            line_endings=line_endings(text),
            comments=tuple(
                Known(text[start:end], self._cite((start, end)))
                for start, end in self.document.comments
            ),
            values=len(values),
            digest=configuration_digest(values),
        )

    def findings(self, values: list[ConfigurationValue]) -> list[IngestFinding]:
        """One finding per problem with values in this document, naming every value it hits."""
        affected: dict[Issue, list[int]] = defaultdict(list)
        for index, node in enumerate(self.nodes):
            for issue in node.issues:
                affected[issue].append(index)
        found: list[IngestFinding] = []
        for issue in sorted(affected):
            indices = affected[issue]
            category, severity, message = _ISSUES[issue]
            first = _path_pointer(self.nodes[indices[0]])
            details: dict[str, JsonValue] = {
                "count": len(indices),
                "document": self.document.index,
                "first": first,
            }
            found.append(
                ingest_finding(
                    code=_code(str(issue)),
                    category=category,
                    severity=severity,
                    subject=self.root,
                    transform=self.transform,
                    message=message.format(count=len(indices)) + f" (first: {first!r})",
                    details=details,
                    records=(values[index].id for index in indices),
                )
            )
        if self.document.skipped:
            skipped = self.document.skipped
            parents = sorted({values[entry.parent].id for entry in skipped})
            spots = sorted({entry.span for entry in skipped})
            found.append(
                ingest_finding(
                    code=_code("unsupported_key"),
                    category=FindingCategory.UNREPRESENTABLE,
                    severity=Severity.ERROR,
                    subject=self.root,
                    transform=self.transform,
                    message=f"{len(skipped)} mapping entries have a key no path can hold"
                    f" ({skipped[0].reason}); they are not read",
                    details={"count": len(skipped), "document": self.document.index},
                    related=[EvidenceRef(self.source.content_id, (Span(*s),)) for s in spots],
                    records=parents,
                )
            )
        return found


# --- The adapter -------------------------------------------------------------------------------


class ConfigAdapter:
    """JSON, YAML and TOML configuration. ``chunk_values`` sets planning granularity only."""

    descriptor = DESCRIPTOR

    def __init__(self, chunk_values: int = DEFAULT_CHUNK_VALUES) -> None:
        if chunk_values <= 0:
            raise ValueError(f"chunk_values must be positive: {chunk_values}")
        self._chunk_values = chunk_values

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head:
            reason = ProbeReason(_code("empty"), "the source is empty: it declares no document")
            return ProbeResult(0.0, (reason,))
        complete = len(head) >= hints.size
        decoded = decode(head, final=complete)
        if isinstance(decoded, InvalidEncoding):
            message = f"byte {decoded.offset} is not {decoded.encoding}"
            return ProbeResult(0.0, (ProbeReason(_code("not_text"), message),))
        found = sniff(decoded.text, decoded.encoding, complete)
        if found is None:
            message = "no JSON, TOML or YAML document with a mapping or sequence at its root"
            return ProbeResult(0.0, (ProbeReason(_code("not_config"), message),))
        fmt, version = found
        read = "the source" if complete else f"the first {len(head)} bytes"
        message = f"{read} read as {fmt} with a mapping or sequence at the root"
        return ProbeResult(STRUCTURE, (ProbeReason(_code(str(fmt)), message),), version)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        encoding, bom = detect(head)
        decoded = decode(head, final=len(head) == source.size)
        found = (
            None
            if isinstance(decoded, InvalidEncoding)
            else sniff(decoded.text, decoded.encoding, len(head) == source.size)
        )
        return InspectResult(
            {
                "bom": bom > 0,
                "encoding": str(encoding),
                "format": str(found[0]) if found else "unknown",
                "size": source.size,
            }
        )

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        loaded = _load(source, config, None)
        findings = tuple(_source_findings(source, config, loaded))
        chunks: list[Chunk] = []
        parse = loaded.parse
        if parse is not None:
            for document in parse.documents:
                total = len(document.nodes)
                for start in range(0, total, self._chunk_values):
                    context: JsonObject = {
                        "document": document.index,
                        "end": min(total, start + self._chunk_values),
                        "format": str(parse.format),
                        "start": start,
                    }
                    chunks.append(make_chunk(source, config, context, source.size))
        if not chunks:  # nothing to read: the plan's findings say why
            chunks.append(make_chunk(source, config, {"part": "none"}, 0))
        return Plan(tuple(chunks), findings)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        context = chunk.context
        if context.get("part") == "none":
            return ChunkOutput()
        fmt = ConfigFormat(str(context["format"]))
        index, start, end = _int(context, "document"), _int(context, "start"), _int(context, "end")
        loaded = _load(source, config, fmt)
        documents = loaded.parse.documents if loaded.parse is not None else []
        document = next((d for d in documents if d.index == index), None)
        if document is None or not 0 <= start < end <= len(document.nodes):
            raise ValueError(f"chunk {chunk.id} names values the source does not hold")
        records = _Records(source, config, loaded, fmt, document)
        if start:
            return ChunkOutput(records=tuple(records.value(i) for i in range(start, end)))
        values = [records.value(i) for i in range(len(document.nodes))]
        snapshot = records.snapshot(values)
        return ChunkOutput(
            records=(snapshot, *values[start:end]),
            findings=tuple(records.findings(values)),
        )


__all__ = ["DESCRIPTOR", "ConfigAdapter"]
