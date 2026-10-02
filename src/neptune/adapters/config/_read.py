"""Which format a text is in, and reading it: whole files for ``plan`` and ``ingest``, heads for
``probe``.

The bytes decide, never the name. The first line that is neither blank nor a ``#`` comment says
which grammars to try, in order (``candidates``):

- ``{``: JSON, then YAML (a flow mapping such as ``{a: 1}`` is YAML, not JSON);
- a TOML table header (``[tool]``, ``[[fingertips]]``): JSON, then TOML, then YAML (a one-line
  JSON array such as ``["base_link"]`` is also a header);
- any other ``[``: JSON, then YAML;
- ``key = ...``: TOML;
- anything else: YAML.

The first grammar that accepts the whole text reads it (YAML also when it read some documents
before an error). If none does, the syntax error reported is that of the reader that got
furthest: the text is most likely that format, broken there. So a TOML file with a repeated key
is a TOML syntax error, never a YAML string that happens to hold its text.
"""

import json
import re
import tomllib
from dataclasses import dataclass
from typing import Any, Final

import yaml
from yaml.events import (
    CollectionEndEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    ScalarEvent,
    SequenceStartEvent,
)

from neptune.adapters.config._json import read_json
from neptune.adapters.config._scalars import implicit
from neptune.adapters.config._text import is_blank
from neptune.adapters.config._toml import read_toml
from neptune.adapters.config._tree import Limits, Parse, Value
from neptune.adapters.config._yaml import read_yaml
from neptune.model.configuration import ConfigFormat, ScalarType, TextEncoding

# How near the end of a head cut short a parser may fail and the head still count as a valid
# beginning: a cut inside a token or a flow collection fails there, not earlier.
CUT_SLACK: Final = 4096
# Events a probe reads of a YAML head: enough to see a document's shape, cheap on large heads.
PROBE_EVENTS: Final = 4096

_KEY: Final = r"""(?:[A-Za-z0-9_\-]+|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')"""
_DOTTED: Final = rf"{_KEY}(?:[ \t]*\.[ \t]*{_KEY})*"
_TOML_HEADER: Final = re.compile(rf"[ \t]*\[\[?[ \t]*{_DOTTED}[ \t]*\]\]?[ \t]*(?:#.*)?")
_TOML_PAIR: Final = re.compile(rf"[ \t]*{_DOTTED}[ \t]*=")

_JSON_SPACE: Final = " \t\r\n"
_TABLE_START: Final = re.compile(r"\[[ \t\r\n]*\{")
# A setting's name: a letter or underscore first, then what names use as separators.
_IDENTIFIER: Final = re.compile(r"[$@]?[A-Za-z_][A-Za-z0-9_\-.:/]*")
# RFC 7946 §1.4: a GeoJSON object's "type".
_GEOJSON_TYPES: Final = frozenset(
    (
        "Feature",
        "FeatureCollection",
        "GeometryCollection",
        "LineString",
        "MultiLineString",
        "MultiPoint",
        "MultiPolygon",
        "Point",
        "Polygon",
    )
)
SEQUENCE_ROOT: Final = "a sequence at the root holds rows of data, not named settings"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return line
    return ""


def candidates(text: str, encoding: TextEncoding) -> tuple[ConfigFormat, ...]:
    """The grammars to try on ``text``, most likely first, from its first meaningful line."""
    line = _first_line(text)
    toml = encoding is TextEncoding.UTF_8  # a TOML document is UTF-8
    start = line.lstrip()[:1]
    if start == "{":
        return ConfigFormat.JSON, ConfigFormat.YAML
    if start == "[":
        # JSON first: a one-line array (["base_link"]) is also a TOML header, and JSON is the
        # stricter grammar. A TOML file fails JSON on its first line, a JSON array never TOML.
        if toml and _TOML_HEADER.fullmatch(line):
            return ConfigFormat.JSON, ConfigFormat.TOML, ConfigFormat.YAML
        return ConfigFormat.JSON, ConfigFormat.YAML
    if toml and _TOML_PAIR.match(line):
        return (ConfigFormat.TOML,)
    return (ConfigFormat.YAML,)


def _accepted(parse: Parse) -> bool:
    return parse.problem is None or bool(parse.documents or parse.too_deep)


def read_text(
    text: str,
    encoding: TextEncoding,
    limits: Limits,
    yaml_version: str,
    only: ConfigFormat | None = None,
) -> Parse | None:
    """The text read in ``only`` its format, or in the first candidate that accepts it; ``None``
    if it is blank (whitespace and comments), which no format declares anything in."""
    readers = {
        ConfigFormat.JSON: lambda: read_json(text, limits),
        ConfigFormat.TOML: lambda: read_toml(text, limits),
        ConfigFormat.YAML: lambda: read_yaml(text, limits, yaml_version),
    }
    if only is not None:
        return readers[only]()
    if is_blank(text):
        return None
    attempts: list[Parse] = []
    for fmt in candidates(text, encoding):
        parse = readers[fmt]()
        if _accepted(parse):
            return parse
        attempts.append(parse)
    # Nothing accepts it: the reader that got furthest, by lines, names the error; on one line
    # the likelier format does (a JSON string cut short fails at its start, YAML at its end).
    return max(attempts, key=lambda p: _line(text, p.problem.offset if p.problem else 0))


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset)


# --- Probing a head ----------------------------------------------------------------------------


def _cut(text: str, complete: bool) -> str:
    """A head cut short ends at its last line break, so no line is half there."""
    if complete:
        return text
    last = max(text.rfind("\n"), text.rfind("\r"))
    return text[: last + 1] if last >= 0 else text


def _at_end(text: str, position: int, complete: bool) -> bool:
    """Whether a parser failed only because the text stops: on its last line when it is whole
    (a truncated file; a string cut short fails where it starts), near the cut when it is a
    head."""
    if complete:
        content = text.rstrip()
        return not any(br in content[position:] for br in "\n\r")
    return position >= len(text) - CUT_SLACK


def _keep(token: str) -> str:
    return token


def _json_root(text: str, complete: bool) -> bool | None:
    """True for an object or array, False for a scalar, None if not JSON."""
    start = text.lstrip(_JSON_SPACE)[:1]
    try:  # numbers are kept as text: a probe converts nothing
        json.loads(text, parse_int=_keep, parse_float=_keep, parse_constant=_keep)
    except RecursionError:
        return start in ("{", "[")
    except ValueError as exc:
        position = getattr(exc, "pos", None)
        if start not in ("{", "[") or position is None or not _at_end(text, position, complete):
            return None
    return start in ("{", "[")


def _skip(text: str, position: int) -> int:
    while text[position : position + 1] in (" ", "\t", "\r", "\n"):
        position += 1
    return position


@dataclass(frozen=True)
class _Cut:
    """A member whose value the head does not hold whole: whether it opens a list of objects."""

    table: bool


def _root_members(text: str) -> list[tuple[str, object]]:
    """A JSON object's members as far as the head holds them whole, in order; the member the
    head cuts last is a ``_Cut``. Only called on text ``_json_root`` accepted as an object."""
    decoder = json.JSONDecoder(parse_int=_keep, parse_float=_keep, parse_constant=_keep)
    members: list[tuple[str, object]] = []
    position = _skip(text, 0) + 1  # past the {
    while True:
        position = _skip(text, position)
        if text[position : position + 1] != '"':
            return members
        try:
            key, position = decoder.raw_decode(text, position)
        except ValueError:
            return members
        position = _skip(text, position)
        if text[position : position + 1] != ":":
            return members
        position = _skip(text, position + 1)
        try:
            value, position = decoder.raw_decode(text, position)
        except (ValueError, RecursionError):
            members.append((key, _Cut(bool(_TABLE_START.match(text, position)))))
            return members
        members.append((key, value))
        position = _skip(text, position)
        if text[position : position + 1] != ",":
            return members
        position += 1


def _table(value: object) -> bool:
    if isinstance(value, _Cut):
        return value.table
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def json_data_shape(text: str) -> str | None:
    """Why a JSON object reads as data rather than settings, or ``None`` if it reads as settings.

    Settings are named: every root key is an identifier (``max_speed``, ``$schema``,
    ``ros.namespace``), and the root is not one of the data formats written in JSON. Data is
    keyed by its content (timestamps, sentences, numbers), is GeoJSON (``"type":
    "FeatureCollection"``), or wraps tables: every root member a list of objects
    (``{"rows": [{...}, ...]}``). A dialect adapter claims those; this one leaves them.
    """
    members = _root_members(text)
    for key, value in members:
        if key == "type" and isinstance(value, str) and value in _GEOJSON_TYPES:
            return f"GeoJSON (a {value}): geometry, not settings"
    for key, _ in members:
        if not _IDENTIFIER.fullmatch(key):
            return f"the root key {key[:40]!r} is not a setting's name: the keys are data"
    if members and all(_table(value) for _, value in members):
        return "every root member is a list of objects: tables of data, not settings"
    return None


def _toml_root(text: str, complete: bool) -> bool | None:
    """True for a TOML document declaring something, False for an empty one, None if not TOML."""
    attempts = [text]
    if not complete:  # a cut inside a multi-line string or array: try before the last header
        header = text.rfind("\n[")
        if header > 0:
            attempts.append(text[: header + 1])
    for attempt in attempts:
        try:
            return bool(tomllib.loads(attempt))
        except (ValueError, RecursionError):
            continue
    return None


_NUMBERS: Final = frozenset((ScalarType.BOOL, ScalarType.INT, ScalarType.FLOAT))


def _typed(text: str) -> bool:
    """A plain scalar both YAML versions read as a boolean or a number: what notes rarely hold."""
    readings = [implicit(text, version) for version in ("1.1", "1.2")]
    return all(
        isinstance(r, Value) and len(r.readings) == 1 and r.readings[0].type in _NUMBERS
        for r in readings
    )


def _yaml_root(text: str, complete: bool) -> tuple[bool | None, str | None, bool]:
    """Whether the head is configuration in YAML (None: not YAML), the YAML version the first
    document declares, and whether every document's root is a sequence.

    YAML's grammar holds for much plain text: ``Robot: spot-12`` lines are a mapping, a Markdown
    list a sequence. So besides every document's root being a mapping or sequence, the head must
    show something notes rarely do: a ``%YAML`` directive or an explicit ``---``, a tag, a
    collection inside a collection or in flow style (once it closes: a ``[`` left open on a
    note's last line is not one), or a plain scalar both YAML versions read as a boolean or a
    number.
    """
    collections, roots, version, events, evidence = True, 0, None, 0, False
    sequences = True  # every root so far a sequence
    shaped: list[bool] = []  # the open collections: whether each is nested or in flow style
    loader: yaml.SafeLoader | None = None
    try:
        loader = yaml.SafeLoader(text)  # refuses a non-printable character at once
        fetch: Any = loader.get_event
        while loader.check_event() and events < PROBE_EVENTS:
            event = fetch()
            events += 1
            if isinstance(event, DocumentStartEvent):
                shaped.clear()
                evidence = evidence or bool(event.explicit) or event.version is not None
                if event.version is not None and version is None:
                    version = f"{event.version[0]}.{event.version[1]}"
                continue
            if isinstance(event, CollectionStartEvent | ScalarEvent):
                if not shaped:
                    roots += 1
                    collections = collections and isinstance(event, CollectionStartEvent)
                    sequences = sequences and isinstance(event, SequenceStartEvent)
                evidence = evidence or event.tag not in (None, "!")
            if isinstance(event, CollectionStartEvent):
                shaped.append(bool(shaped) or bool(event.flow_style))
            elif isinstance(event, CollectionEndEvent):
                evidence = evidence or shaped.pop()
            elif isinstance(event, ScalarEvent) and not evidence and event.style is None:
                evidence = _typed(event.value)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is None or not _at_end(text, mark.index, complete):
            return None, None, False
    finally:
        if loader is not None:
            loader.dispose()
    return (collections and evidence if roots else None), version, sequences


@dataclass(frozen=True)
class Sniffed:
    """What a head is: its format, the YAML version it declares, and why it holds data rather
    than settings (``None``: it holds settings). ``sequence``: its root is a sequence."""

    format: ConfigFormat
    version: str | None = None
    data: str | None = None
    sequence: bool = False


def sniff(text: str, encoding: TextEncoding, complete: bool) -> Sniffed | None:
    """The format a head is in, if it is a valid (or cut short) document with a mapping or
    sequence at its root, the YAML version it declares, and whether its shape is data."""
    text = _cut(text, complete)
    if is_blank(text):
        return None
    for fmt in candidates(text, encoding):
        if fmt is ConfigFormat.JSON:
            root = _json_root(text, complete)
            if root is None:
                continue
            if not root:
                return None
            if text.lstrip(_JSON_SPACE).startswith("["):
                return Sniffed(fmt, data=SEQUENCE_ROOT, sequence=True)
            return Sniffed(fmt, data=json_data_shape(text))
        if fmt is ConfigFormat.TOML:
            root = _toml_root(text, complete)
            if root is None:
                continue
            return Sniffed(fmt) if root else None
        found, version, sequences = _yaml_root(text, complete)
        if found:
            data = SEQUENCE_ROOT if sequences else None
            return Sniffed(fmt, version, data, sequences)
    return None
