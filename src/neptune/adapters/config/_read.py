"""Which format a text is in, and reading it: whole files for ``plan`` and ``ingest``, heads for
``probe``.

The bytes decide, never the name. A whole text is tried as JSON, then TOML, then YAML, and the
first reader that accepts it reads it (YAML also when it read some documents before an error).
JSON first because a JSON text is also YAML, with other number rules; TOML before YAML because a
TOML line (``a = 1``) is a YAML scalar. If none accepts it, the syntax error reported is the one
of the reader that got furthest: the text is most likely that format, broken there.
"""

import json
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


def _accepted(parse: Parse) -> bool:
    return parse.problem is None or bool(parse.documents or parse.too_deep)


def read_text(
    text: str,
    encoding: TextEncoding,
    limits: Limits,
    yaml_version: str,
    only: ConfigFormat | None = None,
) -> Parse | None:
    """The text read in ``only`` its format, or in the first that accepts it; ``None`` if it is
    blank (whitespace and comments), which no format declares anything in."""
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
    for fmt in (ConfigFormat.JSON, ConfigFormat.TOML, ConfigFormat.YAML):
        if fmt is ConfigFormat.TOML and encoding is not TextEncoding.UTF_8:
            continue  # a TOML document is UTF-8
        parse = readers[fmt]()
        if _accepted(parse):
            return parse
        attempts.append(parse)
    # Nothing accepts it: the reader that got furthest names the error (first of equals).
    return max(attempts, key=lambda p: p.problem.offset if p.problem is not None else -1)


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


def _json_root(text: str, complete: bool) -> bool | None:
    """True for an object or array, False for a scalar, None if not JSON."""
    start = text.lstrip(" \t\r\n")
    if not start:
        return None
    try:
        json.loads(text)
    except RecursionError:
        return start[0] in "{["
    except json.JSONDecodeError as exc:
        if start[0] not in "{[" or not _at_end(text, exc.pos, complete):
            return None
    return start[0] in "{["


def _toml_root(text: str, complete: bool) -> bool | None:
    """True for a TOML document declaring something, False for an empty one, None if not TOML."""
    candidates = [text]
    if not complete:  # a cut inside a multi-line string or array: try before the last header
        header = max(text.rfind("\n["), -1)
        if header > 0:
            candidates.append(text[: header + 1])
    for candidate in candidates:
        try:
            return bool(tomllib.loads(candidate))
        except (ValueError, RecursionError):
            continue
    return None


def _yaml_root(text: str, complete: bool) -> tuple[bool | None, str | None]:
    """Whether every document in the head is a mapping or sequence (None: not YAML), and the
    YAML version the first one declares."""
    collections, roots, version, events = True, 0, None, 0
    depth = 0
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
    if not roots:
        return None, version
    return collections, version


def sniff(
    text: str, encoding: TextEncoding, complete: bool
) -> tuple[ConfigFormat, str | None] | None:
    """The format a head is in, if its root is a collection, and the version it declares."""
    text = _cut(text, complete)
    if is_blank(text):
        return None
    json_root = _json_root(text, complete)
    if json_root is not None:
        return (ConfigFormat.JSON, None) if json_root else None
    if encoding is TextEncoding.UTF_8 and _toml_root(text, complete):
        return ConfigFormat.TOML, None
    collections, version = _yaml_root(text, complete)
    if collections:
        return ConfigFormat.YAML, version
    return None
