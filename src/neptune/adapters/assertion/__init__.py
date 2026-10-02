"""Assertion files (``neptune.assertions`` version 1) as ``Assertion`` records (ADR 0062).

A person's assertion (two robots' ids name one machine, a manipulator cell's baseline is
accepted, a note, a retraction) reaches the compiler as a source like any other, so it gets a
content id and provenance. The file is JSON::

    {"format": "neptune.assertions", "version": 1, "assertions": [{...}, ...]}

and each entry holds ``id``, ``assertion_type``, ``author``, ``authored_at`` and ``scope``, and
may hold ``authored_zone``, ``retracts``, ``payload``, ``rationale``, ``signature`` and
``ticket`` (ADR 0062 §3). Every value is stored as declared and ``stated``; ``authored_at`` is
counted by ADR 0023 §2 on a ``TimestampDomain`` of its own. Nothing is resolved or applied: an
author stays a declared id, a scope stays declared ids, a retraction never touches what it
retracts. What cannot be held is a finding and leaves its field ``Unknown``.
"""

import re
import sys
from typing import Final

from neptune.adapters.assertion._read import ADAPTER_ID, FORMAT, FORMAT_VERSION, Reader, code
from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    SIGNATURE,
    VERIFIED,
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
)
from neptune.adapters.structured.load import MIB, Settings, load, problem_finding, problems
from neptune.adapters.structured.reader import read_text
from neptune.adapters.structured.text import InvalidEncoding, decode, detect
from neptune.adapters.structured.tree import Collection, Limits, Value
from neptune.model.assertion import Assertion
from neptune.model.configuration import CollectionType, ConfigFormat, ScalarType, TextEncoding
from neptune.model.reference import TimestampDomain

PYTHON: Final = f"{sys.version_info.major}.{sys.version_info.minor}"
MAX_DEPTH: Final = 64
# What a probe looks for in a head: an object at the root, and a member of it naming this format.
_OBJECT: Final = re.compile(r"\s*\{")
_STRING: Final = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
_NAMES_FORMAT: Final = re.compile(r'\s*:\s*"neptune\.assertions"')

FINDING_CODES: Final = (
    ("byte_order_mark", "a byte-order mark JSON does not define; read past (inconsistent, info)"),
    (
        "duplicate_key",
        "an assertion writes a key more than once; none is chosen, the field is Unknown"
        " (inconsistent, warning)",
    ),
    (
        "invalid_encoding",
        "the bytes are not valid UTF-8 (or the encoding their mark names); nothing is read"
        " (corrupt, error)",
    ),
    (
        "invalid_value",
        "a value that is not what format version 1 defines for its key (an id that is not"
        " {namespace, value}, a time that is not RFC 3339, an unknown assertion_type); the field"
        " is Unknown (corrupt, warning)",
    ),
    (
        "missing_field",
        "a required key is missing or null, or a text is blank; the field is Unknown (missing,"
        " warning)",
    ),
    ("mixed_line_endings", "the file mixes LF, CR LF and lone CR line breaks (inconsistent, info)"),
    ("no_document", "the file is empty or blank (missing, info)"),
    (
        "nonstandard_json",
        "numbers written NaN or Infinity, which RFC 8259 does not define; kept as written"
        " (inconsistent, info)",
    ),
    (
        "not_an_assertion",
        "an entry of assertions is not a JSON object; it is not read (corrupt, warning)",
    ),
    (
        "not_assertions",
        "the root is not an object holding format neptune.assertions, version and assertions"
        " once each; nothing is read (unsupported, error)",
    ),
    (
        "paths_too_long",
        "the values' paths total more than max_path_ratio times the file; not read (limit, error)",
    ),
    (
        "retracts_not_applicable",
        "an assertion other than a retract names one it retracts; retracts is NotApplicable and"
        " the value stays in the file (inconsistent, warning)",
    ),
    ("syntax_error", "the text is not JSON from the cited place on; not read (corrupt, error)"),
    ("too_deep", "the file nests deeper than max_depth; not read (limit, error)"),
    ("too_large", "a file over max_bytes; not read (limit, error)"),
    (
        "too_many_assertions",
        "more than max_assertions entries; those past it are not read (limit, error)",
    ),
    (
        "unknown_key",
        "keys format version 1 does not define, at the root or in an assertion; not read"
        " (unsupported, info)",
    ),
    (
        "value_not_read",
        "a value or member name no record can hold (over max_scalar_length, an unpaired"
        " surrogate escape, a leap second); the field is Unknown (unrepresentable, warning)",
    ),
    (
        "version_unsupported",
        "a version other than 1; nothing is read, since a later version may change what a key"
        " means (unsupported, error)",
    ),
)

DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Human assertions (neptune.assertions JSON) as stated Assertion records.",
    formats=(
        FormatSpec(
            f"Neptune assertion file ({FORMAT}, version {FORMAT_VERSION})",
            media_types=("application/json",),
            extensions=(".json",),
        ),
    ),
    record_kinds=(Assertion.kind, TimestampDomain.kind),
    config=(
        ConfigOption(
            "max_assertions",
            100_000,
            "entries of assertions past this many are not read (assertion.too_many_assertions)",
        ),
        ConfigOption(
            "max_bytes", 16 * MIB, "a file larger than this is not read (assertion.too_large)"
        ),
        ConfigOption(
            "max_depth",
            MAX_DEPTH,
            "a file nested deeper than this is not read (assertion.too_deep)",
        ),
        ConfigOption(
            "max_path_ratio",
            64,
            "a file whose values' paths total more code points than this many times its own"
            " (at least 4 KiB) is not read (assertion.paths_too_long)",
        ),
        ConfigOption(
            "max_scalar_length",
            MIB,
            "a string or number of more code points than this is not read; its field is"
            " Unknown (assertion.value_not_read)",
        ),
    ),
    libraries=(("python", PYTHON),),
    finding_codes=tuple(Documented(code(name), text) for name, text in FINDING_CODES),
    locator_steps=(),
    conventions=(
        Documented(
            "chunks",
            "one chunk per file, which reads it once; plan reads nothing, so every finding is"
            " the chunk's",
        ),
        Documented(
            "citations",
            "an assertion cites JsonPointer /assertions/<i>; each value its Span in the decoded"
            " text (its pointer where the span is not known); a TimestampDomain cites"
            " /assertions/<i>/authored_at",
        ),
        Documented(
            "claims",
            "VERIFIED where the whole file parses as JSON with format neptune.assertions at its"
            " root; SIGNATURE where the head starts an object with that format as a member of"
            " its own, not a nested value's (a long or broken file); never from a name. Beats"
            " the config adapter's STRUCTURE claim",
        ),
        Documented(
            "kinds",
            "assertion (ADR 0062) and timestamp_domain, all stated",
        ),
        Documented(
            "missingness",
            "a missing, null, repeated or unreadable required key is Unknown with a finding; a"
            " left-out or null optional part (payload, rationale, signature, ticket) is"
            " KnownAbsent, as format version 1 defines; a left-out authored_zone is Unknown",
        ),
        Documented(
            "payload",
            "the payload's JSON text exactly as written (its span), never re-typed",
        ),
        Documented(
            "time",
            "authored_at by ADR 0023 §2: with Z or an offset, POSIX ticks (timescale posix);"
            " without, ticks of its own civil clock (timescale Unknown); a date alone, days."
            " Resolution is the finest field written; role document; one TimestampDomain per"
            " assertion. authored_zone is stored as written, never looked up or applied",
        ),
    ),
    resources=Resources(max_memory=1024 * MIB, streaming=False),
    security=(
        "JSON only, through the standard library's json with hooks; nothing is evaluated.",
        "Reads at most max_bytes, nests at most max_depth, holds at most max_assertions entries"
        " and max_scalar_length code points per value; everything past them is a finding.",
    ),
)


def _settings(config: AdapterConfig) -> Settings:
    return Settings(
        config.integer("max_bytes"),
        config.integer("max_depth"),
        config.integer("max_path_ratio"),
        config.integer("max_scalar_length"),
        "1.2",  # never used: only JSON is read
    )


def _declares_format(text: str, encoding: TextEncoding) -> bool | None:
    """Whether a whole text's root object names this format; ``None`` if it is not JSON."""
    parse = read_text(text, encoding, Limits(MAX_DEPTH, MIB), "1.2", ConfigFormat.JSON)
    if parse is None or parse.problem is not None or not parse.documents:
        return None
    nodes = parse.documents[0].nodes
    root = nodes[0].value if nodes else None
    if not isinstance(root, Collection) or root.type is not CollectionType.MAPPING:
        return False
    for node in nodes[1:]:
        if node.parent == 0 and node.path == ("format",) and isinstance(node.value, Value):
            reading = node.value.readings[0]
            if reading.type is ScalarType.STRING and reading.value == FORMAT:
                return True
    return False


def _root_names_format(text: str) -> bool:
    """Whether a head starts an object with a member ``"format": "neptune.assertions"`` of its
    own, not of a nested value. One pass, strings skipped whole; a head may end anywhere."""
    if _OBJECT.match(text) is None:
        return False
    depth, position = 0, 0
    while position < len(text):
        char = text[position]
        if char == '"':
            token = _STRING.match(text, position)
            if token is None:
                return False  # the head ends inside a string
            named = depth == 1 and token.group() == '"format"'
            if named and _NAMES_FORMAT.match(text, token.end()):
                return True
            position = token.end()
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        position += 1
    return False


class AssertionAdapter:
    """Neptune assertion files; one chunk per file."""

    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head:
            return ProbeResult(0.0, (ProbeReason(code("empty"), "the source is empty"),))
        complete = len(head) >= hints.size
        decoded = decode(head, final=complete)
        if isinstance(decoded, InvalidEncoding):
            reason = ProbeReason(code("not_text"), f"byte {decoded.offset} is not valid text")
            return ProbeResult(0.0, (reason,))
        if not _root_names_format(decoded.text):
            reason = ProbeReason(code("no_marker"), f"no object naming format {FORMAT}")
            return ProbeResult(0.0, (reason,))
        if complete:
            declared = _declares_format(decoded.text, decoded.encoding)
            if declared:
                reason = ProbeReason(code("format"), f"the source is a {FORMAT} JSON object")
                return ProbeResult(VERIFIED, (reason,))
            if declared is False:
                reason = ProbeReason(code("not_root"), f"the root object is not {FORMAT}")
                return ProbeResult(0.0, (reason,))
        read = "the head" if not complete else "the source, which is not valid JSON,"
        reason = ProbeReason(code("marker"), f"{read} starts an object naming format {FORMAT}")
        return ProbeResult(SIGNATURE, (reason,))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        head = source.read(0, min(source.size, PROBE_HEAD_SIZE))
        encoding, bom = detect(head)
        return InspectResult({"bom": bom > 0, "encoding": str(encoding), "size": source.size})

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        # One chunk and no read: the file is read once, by the chunk, so its findings are the
        # chunk's. Assertion files are small, and one entry's citations need the whole parse.
        return Plan((make_chunk(source, config, {"part": "file"}, source.size),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        settings = _settings(config)
        loaded = load(source, settings, ConfigFormat.JSON)
        findings = [
            problem_finding(ADAPTER_ID, source.content_id, config.transform, problem)
            for problem in problems(source.size, loaded, settings)
        ]
        reader = Reader(source.content_id, config.transform, config.integer("max_assertions"))
        parse = loaded.parse
        if parse is not None and parse.documents:
            reader.file(parse.documents[0], loaded.text)
        return ChunkOutput(
            records=tuple(reader.out.records), findings=(*findings, *reader.out.findings)
        )


__all__ = ["DESCRIPTOR", "AssertionAdapter"]
