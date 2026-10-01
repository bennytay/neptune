"""Build manifests: what a package says it is called and which version it is (ADR 0040).

Each manifest is one item, ``stated`` (the file declares the package it belongs to). ``commit``
and ``build`` are ``NotCovered``: none of these formats has a place for them.

- **package.xml** (ROS, REPs 127/140/149): ``<name>`` and ``<version>``, which the REPs make
  ``MAJOR.MINOR.PATCH``, so SemVer (text that is not stays as declared text, with a finding).
  Values cite their element's bytes. Entity declarations are refused, never expanded.
- **pyproject.toml**: ``[project]`` (PEP 621) and ``[tool.poetry]`` ``name`` and ``version``, PEP
  440 so declared text. Both tables naming one value is one value; differing values are
  ``Ambiguous``. A version listed in ``dynamic`` is computed at build time: ``Unknown``.
- **Cargo.toml**: ``[package]`` ``name`` and ``version`` (SemVer by Cargo's manifest format). A
  version inherited from the workspace (``version.workspace = true``) is ``Unknown``. A workspace
  manifest without ``[package]`` declares no software.
- **CMakeLists.txt**: each ``project(<name> [VERSION <version>] ...)`` command, read by a CMake
  tokenizer that never evaluates. A value holding a variable reference or an escape is
  ``Unknown``. CMake versions are ``major[.minor[.patch[.tweak]]]``: declared text.
- **setup.py**: each ``setup(...)`` call's ``name=`` and ``version=`` string literals, read from
  Python's syntax tree. Nothing is executed; any other expression is ``Unknown``.
"""

import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from xml.parsers import expat

from neptune.adapters.contract import STRUCTURE, VERIFIED, FormatSpec
from neptune.adapters.software._common import (
    Detected,
    Doc,
    Draft,
    Format,
    Reading,
    reason,
    toml_head,
)
from neptune.model.knowledge import AssertionKind, Knowledge, Unknown
from neptune.model.provenance import ByteRange, EvidenceRef

if TYPE_CHECKING:
    from neptune.model.machine import Release


def _is_text(head: bytes) -> bool:
    return b"\x00" not in head


# --- package.xml -------------------------------------------------------------------------------

# Leading XML declaration, processing instructions, comments, a doctype and whitespace, then
# <package. Every step is unambiguous (one whitespace byte, no lazy ``.*?``, no nested ``+``), so a
# hostile head cannot make the match backtrack.
_PACKAGE_ROOT: Final = re.compile(
    rb"(?:\xef\xbb\xbf)?(?:\s|<\?(?:[^?]|\?(?!>))*\?>|<!--(?:[^-]|-(?!->))*-->"
    rb"|<!DOCTYPE[^\[>]*(?:\[[^\]]*\]\s*)?>)*"
    rb"<package[\s>/]",
    re.DOTALL,
)


def _detect_package_xml(head: bytes, size: int) -> Detected | None:
    if not _PACKAGE_ROOT.match(head):
        return None
    if len(head) == size and _xml_root(head) == "package":
        return Detected(VERIFIED, reason("ros_package_xml", "XML that parses, rooted at <package>"))
    return Detected(STRUCTURE, reason("ros_package_xml", "an XML document whose root is <package>"))


def _xml_root(data: bytes) -> str | None:
    """The root element of a whole XML document, or ``None`` if it does not parse."""
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    roots: list[str] = []

    def start(name: str, attributes: dict[str, str]) -> None:
        if not roots:
            roots.append(name)

    def refuse(*_: object) -> None:
        raise _EntityRefused

    parser.StartElementHandler = start
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except (expat.ExpatError, _EntityRefused):
        return None
    return roots[0] if roots else None


class _EntityRefused(Exception):
    """The document declares an entity: expanding one is never done."""


def _read_package_xml(reading: Reading) -> list[Draft]:
    data = reading.document()
    if data is None:
        return []
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    root: list[str] = []
    depth = 0
    found: dict[str, list[tuple[str, int, int]]] = {"name": [], "version": []}
    capture: tuple[str, int, list[str]] | None = None
    limit = reading.config.integer("max_items")
    dropped: dict[str, int] = {}  # per field: byte offset of its first element past max_items

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal depth, capture
        depth += 1
        if depth == 1:
            root.append(name)
        elif depth == 2 and name in found and len(found[name]) >= limit:
            dropped.setdefault(name, parser.CurrentByteIndex)
        elif depth == 2 and name in found:
            capture = (name, parser.CurrentByteIndex, [])

    def end(name: str) -> None:
        nonlocal depth, capture
        if depth == 2 and capture is not None and name == capture[0]:
            close = data.find(b">", parser.CurrentByteIndex) + 1
            found[name].append(("".join(capture[2]), capture[1], close))
            capture = None
        depth -= 1

    def characters(text: str) -> None:
        if capture is not None and depth == 2:
            capture[2].append(text)

    def refuse(*_: object) -> None:
        raise _EntityRefused

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        reading.malformed(
            f"does not parse as XML (line {exc.lineno}, column {exc.offset})",
            {"column": exc.offset, "line": exc.lineno},
        )
        return []
    except _EntityRefused:
        reading.malformed("declares an XML entity, which is never expanded")
        return []
    if root != ["package"]:
        reading.malformed("has no <package> root element")
        return []
    if dropped:
        reading.too_many_entries(reading.span(min(dropped.values()), 0), limit)
    draft = Draft(entry=reading.whole)
    absent = Unknown(reading.provenance(reading.whole))
    names = [
        reading.text(draft, "name", text, reading.span(s, e - s)) for text, s, e in found["name"]
    ]
    if "name" in dropped:  # elements never read could disagree: no value is chosen from a part
        draft.name = absent
        draft.explained.add("name")
    else:
        draft.name = reading.choose(draft, "name", names, absent)
    releases = [
        reading.semver(draft, text, reading.span(s, e - s)) for text, s, e in found["version"]
    ]
    if "version" in dropped:
        draft.release = absent
        draft.explained.add("release")
    else:
        draft.release = reading.choose(draft, "release", releases, absent)
    return [draft]


# --- TOML manifests ----------------------------------------------------------------------------


def _detect_pyproject(head: bytes, size: int) -> Detected | None:
    document = toml_head(head, size)
    if document is None:
        return None
    tool = document.get("tool")
    if isinstance(document.get("project"), dict):
        return Detected(VERIFIED, reason("pyproject", "TOML with a [project] table (PEP 621)"))
    if isinstance(tool, dict) and isinstance(tool.get("poetry"), dict):
        return Detected(VERIFIED, reason("pyproject", "TOML with a [tool.poetry] table"))
    return None


def _read_pyproject(reading: Reading) -> list[Draft]:
    data = reading.toml()
    if data is None:
        return []
    doc = Doc(reading, (ByteRange(0, reading.source.size),))
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    tables = [
        (table, path)
        for table, path in ((data.get("project"), ("project",)), (poetry, ("tool", "poetry")))
        if isinstance(table, dict)
    ]
    if not tables:
        return []
    draft = Draft(entry=doc.ref(*tables[0][1]))
    absent = Unknown(reading.provenance(draft.entry))
    names = [
        reading.text(draft, "name", table["name"], doc.ref(*path, "name"))
        for table, path in tables
        if "name" in table
    ]
    draft.name = reading.choose(draft, "name", names, absent)
    releases = [
        reading.declared(draft, table["version"], doc.ref(*path, "version"))
        for table, path in tables
        if "version" in table
    ]
    missing: Knowledge[Release] = absent
    dynamic = tables[0][0].get("dynamic") if tables[0][1] == ("project",) else None
    if not releases and isinstance(dynamic, list) and "version" in dynamic:
        at = doc.ref("project", "dynamic", dynamic.index("version"))
        missing = reading.unevaluated(draft, "release", at, "dynamic, computed at build time")
    draft.release = reading.choose(draft, "release", releases, missing)
    return [draft]


def _detect_cargo(head: bytes, size: int) -> Detected | None:
    document = toml_head(head, size)
    if document is None or not isinstance(document.get("package"), dict):
        return None
    return Detected(VERIFIED, reason("cargo_toml", "TOML with a [package] table: a Cargo manifest"))


def _inherited(value: object) -> bool:
    return isinstance(value, dict) and value.get("workspace") is True


def _read_cargo(reading: Reading) -> list[Draft]:
    data = reading.toml()
    package = None if data is None else data.get("package")
    if not isinstance(package, dict):
        return []
    doc = Doc(reading, (ByteRange(0, reading.source.size),))
    draft = Draft(entry=doc.ref("package"))
    name, version = package.get("name"), package.get("version")
    name_at = doc.ref("package", "name") if "name" in package else draft.entry
    version_at = doc.ref("package", "version") if "version" in package else draft.entry
    inherited = "inherited from the workspace"
    draft.name = (
        reading.unevaluated(draft, "name", name_at, inherited)
        if _inherited(name)
        else reading.text(draft, "name", name, name_at)
    )
    draft.release = (
        reading.unevaluated(draft, "release", version_at, inherited)
        if _inherited(version)
        else reading.semver(draft, version, version_at)
    )
    return [draft]


# --- CMakeLists.txt ----------------------------------------------------------------------------

_CMAKE_MINIMUM: Final = re.compile(rb"^[ \t]*cmake_minimum_required[ \t]*\(", re.M | re.I)
_CMAKE_PROJECT: Final = re.compile(rb"^[ \t]*project[ \t]*\(", re.M | re.I)
_SPACE: Final = b" \t\r\n"
_IDENTIFIER: Final = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
_UNQUOTED_STOP: Final = frozenset(b' \t\r\n()#"')
_KEYWORDS: Final = frozenset({"VERSION", "DESCRIPTION", "HOMEPAGE_URL", "LANGUAGES"})
# What CMake evaluates in an argument; Neptune never does.
_EXPRESSION: Final = re.compile(rb"\$(?:ENV|CACHE)?\{|\\")


class _Unterminated(Exception):
    def __init__(self, offset: int) -> None:
        super().__init__(offset)
        self.offset = offset


@dataclass(frozen=True)
class _Argument:
    start: int  # the value's bytes, quotes and brackets excluded
    end: int
    bracket: bool


@dataclass(frozen=True)
class _Command:
    name: bytes
    start: int
    end: int
    arguments: tuple[_Argument, ...]


def _bracket_level(data: bytes, at: int) -> int | None:
    """The ``=`` count of a bracket opening ``[=*[`` at ``at``, or ``None``."""
    end = at + 1
    while end < len(data) and data[end] == ord("="):
        end += 1
    return end - at - 1 if end < len(data) and data[end] == ord("[") else None


def _skip_comment(data: bytes, at: int) -> int:
    level = _bracket_level(data, at + 1) if at + 1 < len(data) else None
    if level is not None:
        close = data.find(b"]" + b"=" * level + b"]", at + level + 3)
        if close < 0:
            raise _Unterminated(at)
        return close + level + 2
    newline = data.find(b"\n", at)
    return len(data) if newline < 0 else newline + 1


def _arguments(data: bytes, at: int) -> tuple[tuple[_Argument, ...], int]:
    arguments: list[_Argument] = []
    depth, start, n = 1, at, len(data)
    while at < n:
        byte = data[at]
        if byte in _SPACE:
            at += 1
        elif byte == ord("#"):
            at = _skip_comment(data, at)
        elif byte == ord("("):
            depth, at = depth + 1, at + 1
        elif byte == ord(")"):
            depth, at = depth - 1, at + 1
            if depth == 0:
                return tuple(arguments), at
        elif byte == ord('"'):
            end = at + 1
            while end < n and data[end] != ord('"'):
                end += 2 if data[end] == ord("\\") else 1
            if end >= n:
                raise _Unterminated(at)
            arguments.append(_Argument(at + 1, end, bracket=False))
            at = end + 1
        elif byte == ord("[") and (level := _bracket_level(data, at)) is not None:
            opened = at + level + 2
            close = data.find(b"]" + b"=" * level + b"]", opened)
            if close < 0:
                raise _Unterminated(at)
            # CMake drops one newline directly after the opening bracket.
            for newline in (b"\r\n", b"\n"):
                if data.startswith(newline, opened):
                    opened += len(newline)
                    break
            arguments.append(_Argument(opened, close, bracket=True))
            at = close + level + 2
        else:
            end = at
            while end < n and data[end] not in _UNQUOTED_STOP:
                end += 2 if data[end] == ord("\\") else 1
            arguments.append(_Argument(at, min(end, n), bracket=False))
            at = end
    raise _Unterminated(start)


def _commands(data: bytes) -> Iterator[_Command]:
    at, n = 0, len(data)
    while at < n:
        byte = data[at]
        if byte == ord("#"):
            at = _skip_comment(data, at)
            continue
        identifier = _IDENTIFIER.match(data, at)
        if identifier is None:
            at += 1
            continue
        after = identifier.end()
        while after < n and data[after] in b" \t":
            after += 1
        if after < n and data[after] == ord("("):
            arguments, end = _arguments(data, after + 1)
            yield _Command(identifier.group().lower(), at, end, arguments)
            at = end
        else:
            at = identifier.end()


def _detect_cmake(head: bytes, size: int) -> Detected | None:
    if not _is_text(head) or not (_CMAKE_MINIMUM.search(head) and _CMAKE_PROJECT.search(head)):
        return None
    return Detected(
        STRUCTURE, reason("cmake", "CMake commands cmake_minimum_required() and project()")
    )


def _argument_value(
    reading: Reading, draft: Draft, name: str, data: bytes, argument: _Argument
) -> Knowledge[Any]:
    raw = data[argument.start : argument.end]
    at = reading.span(argument.start, argument.end - argument.start)
    if not argument.bracket and _EXPRESSION.search(raw):
        return reading.unevaluated(draft, name, at, "a CMake expression")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return reading.invalid(draft, name, at, "is not UTF-8")
    if name == "name":
        return reading.text(draft, "name", text, at)
    return reading.declared(draft, text, at)


def _read_cmake(reading: Reading) -> list[Draft]:
    data = reading.document("max_script_bytes")
    if data is None:
        return []
    drafts: list[Draft] = []
    try:
        projects = []
        limit = reading.config.integer("max_items")
        for command in _commands(data):
            if command.name == b"project":
                projects.append(command)
                if len(projects) > limit:  # the record is refused, the rest is not scanned
                    reading.too_many_items(limit)
                    return []
    except _Unterminated as exc:
        reading.malformed(
            f"has an argument or comment left open at byte {exc.offset}", {"byte": exc.offset}
        )
        return []
    for command in projects:
        entry = reading.span(command.start, command.end - command.start)
        if not command.arguments:
            reading.malformed_entry(entry, "is a project() call without a name")
            continue
        draft = Draft(entry=entry)
        draft.name = _argument_value(reading, draft, "name", data, command.arguments[0])
        texts = [data[a.start : a.end] for a in command.arguments]
        draft.release = Unknown(reading.provenance(entry))
        if b"VERSION" in texts[1:]:
            index = texts.index(b"VERSION", 1)
            following = command.arguments[index + 1] if index + 1 < len(texts) else None
            if (
                following is not None
                and texts[index + 1].decode("utf-8", "replace") not in _KEYWORDS
            ):
                draft.release = _argument_value(reading, draft, "release", data, following)
        drafts.append(draft)
    return drafts


# --- setup.py ----------------------------------------------------------------------------------

_SETUP_IMPORT: Final = re.compile(
    rb"^[ \t]*(?:from[ \t]+(?:setuptools|distutils\.core)[ \t]+import\b|import[ \t]+setuptools\b)",
    re.M,
)
_SETUP_CALL: Final = re.compile(rb"\bsetup[ \t]*\(")
_BOM: Final = b"\xef\xbb\xbf"


def _detect_setup_py(head: bytes, size: int) -> Detected | None:
    if not _is_text(head) or not (_SETUP_IMPORT.search(head) and _SETUP_CALL.search(head)):
        return None
    return Detected(
        STRUCTURE, reason("setup_py", "Python importing setuptools and calling setup()")
    )


def _setup_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """The names the script binds to setuptools' ``setup`` and to its modules, from its imports.

    A call is ``setup()`` only through one of these: ``app.setup(...)`` or a local ``setup`` of
    the script's own is not a package declaration.
    """
    functions: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("setuptools", "distutils.core"):
            functions.update(
                alias.asname or alias.name for alias in node.names if alias.name == "setup"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("setuptools", "distutils.core"):
                    modules.add(alias.asname or alias.name)
    return frozenset(functions), frozenset(modules)


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return ".".join([node.id, *reversed(parts)])


def _is_setup(function: ast.expr, bindings: tuple[frozenset[str], frozenset[str]]) -> bool:
    functions, modules = bindings
    if isinstance(function, ast.Name):
        return function.id in functions
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "setup"
        and _dotted(function.value) in modules
    )


def _read_setup_py(reading: Reading) -> list[Draft]:
    data = reading.document("max_script_bytes")
    if data is None:
        return []
    base = len(_BOM) if data.startswith(_BOM) else 0
    body = data[base:]
    text = reading.utf8(body)
    if text is None:
        return []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # MemoryError is the parser's own stack overflow on deeply nested or chained operators.
        reading.malformed("does not parse as Python")
        return []
    starts = [base]
    for line in body.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    places = _Places(reading, starts)
    bindings = _setup_bindings(tree)
    calls = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_setup(node.func, bindings)
        ),
        key=lambda call: (call.lineno, call.col_offset),
    )
    drafts: list[Draft] = []
    for call in calls:
        if reading.full(drafts):
            break
        draft = Draft(entry=places.of(call))
        draft.name = _setup_keyword(places, draft, call, "name", "name")
        draft.release = _setup_keyword(places, draft, call, "version", "release")
        drafts.append(draft)
    return drafts


@dataclass(frozen=True)
class _Places:
    """Byte citations of syntax-tree nodes: ``starts`` holds each line's first byte."""

    reading: Reading
    starts: list[int]

    def of(self, node: ast.expr) -> EvidenceRef:
        start = self.starts[node.lineno - 1] + node.col_offset
        end_line = node.end_lineno if node.end_lineno is not None else node.lineno
        end_col = node.end_col_offset if node.end_col_offset is not None else node.col_offset
        return self.reading.span(start, self.starts[end_line - 1] + end_col - start)


def _setup_keyword(
    places: _Places, draft: Draft, call: ast.Call, keyword: str, field: str
) -> Knowledge[Any]:
    """A ``setup()`` keyword's string literal; anything else is never evaluated."""
    reading = places.reading
    values = {given.arg: given.value for given in call.keywords if given.arg}
    spread = [given.value for given in call.keywords if given.arg is None]
    value = values.get(keyword)
    if value is None and spread:
        return reading.unevaluated(draft, field, places.of(spread[0]), "passed through **")
    if value is None:
        return Unknown(reading.provenance(draft.entry))
    if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return reading.unevaluated(draft, field, places.of(value), "a Python expression")
    if field == "name":
        return reading.text(draft, "name", value.value, places.of(value))
    return reading.declared(draft, value.value, places.of(value))


ROS_PACKAGE_XML: Final = Format(
    key="ros_package_xml",
    label="package.xml",
    spec=FormatSpec("ROS package manifest (package.xml)"),
    assertion=AssertionKind.STATED,
    detect=_detect_package_xml,
    read=_read_package_xml,
)
PYPROJECT: Final = Format(
    key="pyproject",
    label="pyproject.toml",
    spec=FormatSpec("Python project metadata (pyproject.toml)"),
    assertion=AssertionKind.STATED,
    detect=_detect_pyproject,
    read=_read_pyproject,
)
CARGO_TOML: Final = Format(
    key="cargo_toml",
    label="Cargo.toml",
    spec=FormatSpec("Cargo manifest (Cargo.toml)"),
    assertion=AssertionKind.STATED,
    detect=_detect_cargo,
    read=_read_cargo,
)
CMAKE: Final = Format(
    key="cmake",
    label="CMakeLists.txt",
    spec=FormatSpec("CMake project (CMakeLists.txt)"),
    assertion=AssertionKind.STATED,
    detect=_detect_cmake,
    read=_read_cmake,
)
SETUP_PY: Final = Format(
    key="setup_py",
    label="setup.py",
    spec=FormatSpec("setuptools script (setup.py), read as syntax only"),
    assertion=AssertionKind.STATED,
    detect=_detect_setup_py,
    read=_read_setup_py,
)
