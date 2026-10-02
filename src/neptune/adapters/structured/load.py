"""A source read as structured text: its limits, its parse and what is wrong with the file.

``load`` reads one whole source in JSON, TOML or YAML under the limits every adapter that reads
structured text exposes as options (``Settings``). ``problems`` says what is wrong with the file
as a whole or with whole documents, as names an adapter turns into findings with its own codes.
Both are what the ``config`` adapter did inline before ADR 0055; its behaviour is unchanged.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from neptune.adapters.contract import SourceReader, read_pieces
from neptune.adapters.structured.reader import read_text
from neptune.adapters.structured.text import (
    InvalidEncoding,
    decode,
    line_and_column,
    line_endings,
)
from neptune.adapters.structured.tree import Alias, Document, Limits, Parse, TooLong
from neptune.model.configuration import ConfigFormat, LineEndings, TextEncoding
from neptune.model.finding import FindingCategory, Severity
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import ByteRange, Locator, Span

MIB: Final = 1024 * 1024
_MESSAGE: Final = 300  # a parser's message is cut to this many characters in a finding

# The least path budget of a document, in code points: a small file may nest a little deeper.
_PATH_FLOOR: Final = 4096


@dataclass(frozen=True)
class Settings:
    """The limits and the YAML typing rule a read applies; adapters expose them as options."""

    max_bytes: int
    max_depth: int
    max_path_ratio: int
    max_scalar_length: int
    yaml_version: str


@dataclass(frozen=True)
class Loaded:
    """A source as text, read in a format; what stopped it, if it was not."""

    size: int
    too_large: bool = False
    invalid: InvalidEncoding | None = None
    text: str = ""
    encoding: TextEncoding = TextEncoding.UTF_8
    bom: int = 0
    parse: Parse | None = None


def load(source: SourceReader, settings: Settings, only: ConfigFormat | None) -> Loaded:
    """The whole source, decoded and read in ``only`` its format or the first that accepts it."""
    if source.size > settings.max_bytes:
        return Loaded(source.size, too_large=True)
    data = b"".join(read_pieces(source, 0, source.size))
    decoded = decode(data)
    if isinstance(decoded, InvalidEncoding):
        return Loaded(source.size, invalid=decoded)
    limits = Limits(settings.max_depth, settings.max_scalar_length)
    parse = read_text(decoded.text, decoded.encoding, limits, settings.yaml_version, only)
    if parse is not None:
        budget_paths(parse, settings.max_path_ratio)
    return Loaded(source.size, False, None, decoded.text, decoded.encoding, decoded.bom, parse)


def _segment(segment: str | int) -> int:
    return len(segment) + 1 if isinstance(segment, str) else len(str(segment)) + 1


def path_cost(document: Document) -> int:
    """The code points every value's path and every alias's target total: what the records
    repeat of the document's keys. Linear to compute: a node's path costs its parent's and one
    more segment."""
    costs: list[int] = []
    total = 0
    for node in document.nodes:
        own = 0 if node.parent < 0 else costs[node.parent] + _segment(node.path[-1])
        costs.append(own)
        total += own
        if isinstance(node.value, Alias) and node.value.target is not None:
            total += sum(_segment(segment) for segment in node.value.target)
    return total


def budget_paths(parse: Parse, ratio: int) -> None:
    """Set aside every document whose records would repeat its keys more than ``ratio`` times
    its own size: output stays linear in input (a 20,000-character key above 4,000 values is
    80 million code points of paths)."""
    kept: list[Document] = []
    for document in parse.documents:
        start, end = document.extent
        budget = ratio * max(end - start, _PATH_FLOOR)
        cost = path_cost(document)
        if cost > budget:
            parse.too_long.append(TooLong(document.index, document.extent, cost, budget))
        else:
            kept.append(document)
    parse.documents = kept


def shorten(message: str) -> str:
    message = " ".join(message.split())
    return message if len(message) <= _MESSAGE else message[: _MESSAGE - 1] + "…"


@dataclass(frozen=True)
class Problem:
    """One finding about the file as a whole, by name: the adapter gives it its code."""

    name: str
    category: FindingCategory
    severity: Severity
    where: Locator
    message: str
    details: dict[str, JsonValue] = field(default_factory=dict)


def problems(source_size: int, loaded: Loaded, settings: Settings) -> Iterator[Problem]:
    """What reading found wrong with the file as a whole, or with whole documents."""
    whole = ByteRange(0, source_size)
    if loaded.too_large:
        limit = settings.max_bytes
        yield Problem(
            "too_large",
            FindingCategory.LIMIT,
            Severity.ERROR,
            whole,
            f"the file holds {source_size} bytes, over max_bytes ({limit}); it is not read",
            {"bytes": source_size, "max_bytes": limit},
        )
        return
    if loaded.invalid is not None:
        bad = loaded.invalid
        yield Problem(
            "invalid_encoding",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            ByteRange(bad.offset, source_size - bad.offset),
            f"byte {bad.offset} is not valid {bad.encoding} ({bad.reason}); the file is not read",
            {"encoding": str(bad.encoding), "first_invalid_byte": bad.offset},
        )
        return
    text, parse = loaded.text, loaded.parse
    if line_endings(text) is LineEndings.MIXED:
        yield Problem(
            "mixed_line_endings",
            FindingCategory.INCONSISTENT,
            Severity.INFO,
            whole,
            "the file mixes line breaks (LF, CR LF, CR); spans and text keep them as written",
        )
    if parse is None or (
        not parse.documents and not parse.too_deep and not parse.too_long and parse.problem is None
    ):
        yield Problem(
            "no_document",
            FindingCategory.MISSING,
            Severity.INFO,
            whole,
            "the file is empty, blank or only comments: it declares no configuration",
        )
        return
    if loaded.bom and parse.format is not ConfigFormat.YAML:
        yield Problem(
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
        yield Problem(
            "syntax_error",
            FindingCategory.CORRUPT,
            Severity.ERROR,
            Span(problem.start, len(text)),
            f"not {parse.format} at line {line}, column {column}:"
            f" {shorten(problem.message)}; {lost}",
            {
                "column": column,
                "document": problem.document,
                "format": str(parse.format),
                "line": line,
                "offset": problem.offset,
            },
        )
    limit = settings.max_depth
    for deep in parse.too_deep:
        yield Problem(
            "too_deep",
            FindingCategory.LIMIT,
            Severity.ERROR,
            Span(*deep.extent),
            f"document {deep.document} nests deeper than max_depth ({limit}); it is not read",
            {"document": deep.document, "max_depth": limit},
        )
    ratio = settings.max_path_ratio
    for long in parse.too_long:
        yield Problem(
            "paths_too_long",
            FindingCategory.LIMIT,
            Severity.ERROR,
            Span(*long.extent),
            f"document {long.document}'s values' paths total {long.cost} code points, over"
            f" max_path_ratio ({ratio}) times its size ({long.budget}); it is not read",
            {
                "budget": long.budget,
                "document": long.document,
                "max_path_ratio": ratio,
                "path_code_points": long.cost,
            },
        )
    for document, version, spot in parse.unsupported_version:
        yield Problem(
            "yaml_version_unsupported",
            FindingCategory.UNSUPPORTED,
            Severity.WARNING,
            Span(*spot),
            f"document {document} declares YAML {version}; it is typed as yaml_version"
            f" ({settings.yaml_version}) says",
            {"document": document, "version": version},
        )
