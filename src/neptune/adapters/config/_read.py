"""Which format a text is in, and reading it: whole files for ``plan`` and ``ingest``, heads for
``probe``.

The bytes decide, never the name. The first line that is neither blank nor a ``#`` comment says
which grammars to try, in order (``candidates``):

- ``{``: JSON, then YAML (a flow mapping such as ``{a: 1}`` is YAML, not JSON);
- a TOML table header (``[tool]``, ``[[fingertips]]``): TOML, then JSON, then YAML;
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
from typing import Any, Final

import yaml
from yaml.events import CollectionEndEvent, CollectionStartEvent, DocumentStartEvent, ScalarEvent

from neptune.adapters.config._json import read_json
from neptune.adapters.config._text import is_blank
from neptune.adapters.config._toml import read_toml
from neptune.adapters.config._tree import Limits, Parse
from neptune.adapters.config._yaml import read_yaml
from neptune.model.configuration import ConfigFormat, TextEncoding

# How near the end of a head cut short a parser may fail and the head still count as a valid
# beginning: a cut inside a token or a flow collection fails there, not earlier.
CUT_SLACK: Final = 4096
# Events a probe reads of a YAML head: enough to see a document's shape, cheap on large heads.
PROBE_EVENTS: Final = 4096

_KEY: Final = r"""(?:[A-Za-z0-9_\-]+|"(?:[^"\\\n]|\\.)*"|'[^'\n]*')"""
_DOTTED: Final = rf"{_KEY}(?:[ \t]*\.[ \t]*{_KEY})*"
_TOML_HEADER: Final = re.compile(rf"[ \t]*\[\[?[ \t]*{_DOTTED}[ \t]*\]\]?[ \t]*(?:#.*)?")
_TOML_PAIR: Final = re.compile(rf"[ \t]*{_DOTTED}[ \t]*=")


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
        if toml and _TOML_HEADER.fullmatch(line):
            return ConfigFormat.TOML, ConfigFormat.JSON, ConfigFormat.YAML
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
    """Whether a parser failed only because the text stops: at its very end when it is whole
    (a truncated file), near the cut when it is a head."""
    if complete:
        return position >= len(text.rstrip())
    return position >= len(text) - CUT_SLACK


def _keep(token: str) -> str:
    return token


def _json_root(text: str, complete: bool) -> bool | None:
    """True for an object or array, False for a scalar, None if not JSON."""
    start = text.lstrip(" \t\r\n")[:1]
    try:  # numbers are kept as text: a probe converts nothing
        json.loads(text, parse_int=_keep, parse_float=_keep, parse_constant=_keep)
    except RecursionError:
        return start in ("{", "[")
    except ValueError as exc:
        position = getattr(exc, "pos", None)
        if start not in ("{", "[") or position is None or not _at_end(text, position, complete):
            return None
    return start in ("{", "[")


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


def _yaml_root(text: str, complete: bool) -> tuple[bool | None, str | None]:
    """Whether every document in the head is a mapping or sequence (None: not YAML), and the
    YAML version the first one declares."""
    collections, roots, version, events, depth = True, 0, None, 0, 0
    loader: yaml.SafeLoader | None = None
    try:
        loader = yaml.SafeLoader(text)  # refuses a non-printable character at once
        fetch: Any = loader.get_event
        while loader.check_event() and events < PROBE_EVENTS:
            event = fetch()
            events += 1
            if isinstance(event, DocumentStartEvent):
                depth = 0
                if event.version is not None and version is None:
                    version = f"{event.version[0]}.{event.version[1]}"
            elif isinstance(event, CollectionStartEvent | ScalarEvent) and depth == 0:
                roots += 1
                collections = collections and isinstance(event, CollectionStartEvent)
            if isinstance(event, CollectionStartEvent):
                depth += 1
            elif isinstance(event, CollectionEndEvent):
                depth -= 1
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is None or not _at_end(text, mark.index, complete):
            return None, None
    finally:
        if loader is not None:
            loader.dispose()
    return (collections if roots else None), version


def sniff(
    text: str, encoding: TextEncoding, complete: bool
) -> tuple[ConfigFormat, str | None] | None:
    """The format a head is in, if it is a valid (or cut short) document with a mapping or
    sequence at its root, and the YAML version it declares."""
    text = _cut(text, complete)
    if is_blank(text):
        return None
    for fmt in candidates(text, encoding):
        if fmt is ConfigFormat.JSON:
            root = _json_root(text, complete)
        elif fmt is ConfigFormat.TOML:
            root = _toml_root(text, complete)
        else:
            found, version = _yaml_root(text, complete)
            if found:
                return fmt, version
            continue
        if root is not None:
            return (fmt, None) if root else None
    return None
