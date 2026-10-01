"""YAML through PyYAML's pure-Python parser: events only, never composed or constructed.

The parser turns text into events (a scalar with its tag, style and text; the start and end of a
collection; an alias) with exact positions. Nothing here builds a Python object from a tag, so no
tag runs code, and an alias is recorded as a reference to its anchor's node, never expanded, so a
document of nested aliases costs one value per alias. Every token the parser consumes is
recorded: what lies between tokens is whitespace and comments, which is how comments are found.

Typing follows the YAML version the document declares (``%YAML 1.1`` or ``1.2``). Without a
declaration, the adapter's ``yaml_version`` decides: ``declared`` reads every plain scalar under
both and keeps both readings where they differ.
"""

from dataclasses import dataclass, field
from typing import Any, Final

import yaml
from yaml.events import (
    AliasEvent,
    CollectionStartEvent,
    DocumentEndEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    NodeEvent,
    ScalarEvent,
    SequenceEndEvent,
    StreamEndEvent,
)
from yaml.tokens import DirectiveToken, ScalarToken, Token

from neptune.adapters.config._scalars import YamlVersion, combine, implicit, tagged
from neptune.adapters.config._tree import (
    Alias,
    Collection,
    Document,
    Issue,
    Limits,
    Node,
    NodeValue,
    Null,
    Parse,
    Problem,
    Reading,
    SkippedEntry,
    Spot,
    TooDeep,
    Unreadable,
    Value,
    mark_repeats,
    representable,
)
from neptune.model.configuration import (
    CollectionType,
    ConfigFormat,
    ConfigScalar,
    Path,
    ScalarType,
)

PLAIN: Final = "?"  # the non-specific tag of plain scalars and collections (YAML 1.2 §3.3.2)
NON_PLAIN: Final = "!"  # the non-specific tag of quoted and block scalars
_BREAKS: Final = "\n\r\x85\u2028\u2029"  # what PyYAML ends a line at


def _start(item: Any) -> int:
    """Where a PyYAML token or event starts: a code point of the text."""
    mark = item.start_mark
    return int(mark.index) if mark is not None else 0


def _end(item: Any) -> int:
    mark = item.end_mark
    return int(mark.index) if mark is not None else 0


def _next(loader: yaml.SafeLoader) -> Any:
    fetch: Any = loader.get_event
    return fetch()


class _Loader(yaml.SafeLoader):
    """PyYAML's pure-Python reader, scanner and parser, recording every token it consumes."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.covered: list[Spot] = []  # every consumed token's code points
        self.headers: list[int] = []  # where each block scalar's header starts
        self.directive: Spot | None = None  # the last %YAML directive, until a document takes it

    def get_token(self) -> Any:
        fetch: Any = super().get_token
        token: Token = fetch()
        self.covered.append((_start(token), _end(token)))
        if isinstance(token, ScalarToken) and token.style in ("|", ">"):
            self.headers.append(_start(token))
        if isinstance(token, DirectiveToken) and token.name == "YAML":
            self.directive = (_start(token), _end(token))
        return token


def comments(text: str, covered: list[Spot], headers: list[int], end: int) -> list[Spot]:
    """Every ``#`` comment before ``end``: in the gaps between tokens, and after a block
    scalar's header (``key: |  # comment``), which PyYAML folds into the scalar's token."""
    found: list[Spot] = []

    def scan(start: int, stop: int) -> None:
        position = text.find("#", start, stop)
        while position >= 0:
            close = position
            while close < len(text) and text[close] not in _BREAKS:
                close += 1
            found.append((position, close))
            position = text.find("#", close, stop)

    position = 0
    for start, stop in sorted(covered):
        if start > position:
            scan(position, min(start, end))
        position = max(position, stop)
    if position < end:
        scan(position, end)
    for header in headers:
        line_end = header
        while line_end < len(text) and text[line_end] not in _BREAKS:
            line_end += 1
        hash_at = text.find("#", header, line_end)
        if hash_at > header and text[hash_at - 1] in " \t" and hash_at < end:
            found.append((hash_at, line_end))
    return sorted(set(found))


@dataclass
class _Frame:
    """An open collection: its node, and for a mapping the entry being read."""

    index: int
    mapping: bool
    path: Path
    count: int = 0  # entries or items begun
    key: str | None = None  # a mapping's pending key, once read
    key_type: str | None = None  # the pending key's type: its tag, resolved
    key_start: int = 0
    skip_value: bool = False  # the pending entry's key cannot be held: skip its value
    children: list[int] = field(default_factory=list)
    flow: bool = False
    last_end: int = 0


class _Document:
    """Builds one document's nodes from its events."""

    def __init__(
        self,
        index: int,
        start: int,
        versions: tuple[YamlVersion, ...],
        limits: Limits,
        version: tuple[str, Spot] | None,
    ) -> None:
        self.index = index
        self.start = start
        self.versions = versions
        self.limits = limits
        self.version = version
        self.nodes: list[Node] = []
        self.frames: list[_Frame] = []
        # Each anchor's node, or the key it marks: a key is no node, so an alias to one reads as
        # the key's scalar again.
        self.anchors: dict[str, int | ScalarEvent] = {}
        self.skipped: list[SkippedEntry] = []
        self.skip_depth = 0  # events of an entry being skipped
        self.too_deep = False

    # --- skipping an entry whose key cannot be held -------------------------------------------

    def _skip(self, event: NodeEvent) -> None:
        if isinstance(event, CollectionStartEvent):
            self.skip_depth += 1
        elif isinstance(event, MappingEndEvent | SequenceEndEvent):
            self.skip_depth -= 1
        if self.skip_depth == 0:
            self._skipped(_end(event))

    def _skipped(self, end: int) -> None:
        frame = self.frames[-1]
        if frame.skip_value:  # the value is skipped too: the entry is done
            reason = "a key that is a collection, or an alias to one, or over max_scalar_length"
            self.skipped.append(SkippedEntry(frame.index, (frame.key_start, end), reason))
            frame.skip_value, frame.key = False, None
            frame.last_end = max(frame.last_end, end)
        else:  # the key is skipped: its value follows
            frame.skip_value = True

    def _start_skip(self, event: NodeEvent) -> None:
        if isinstance(event, CollectionStartEvent):
            self.skip_depth = 1
        else:
            self._skipped(_end(event))

    # --- events -------------------------------------------------------------------------------

    def event(self, event: Any) -> None:
        if self.too_deep:
            return
        if self.skip_depth:
            self._skip(event)
            return
        if isinstance(event, MappingEndEvent | SequenceEndEvent):
            self._close(event)
            return
        frame = self.frames[-1] if self.frames else None
        if frame is not None and frame.mapping and frame.key is None and not frame.skip_value:
            self._key(frame, event)
            return
        if frame is not None and frame.skip_value:
            frame.count += 1
            self._start_skip(event)
            return
        self._value(frame, event)

    def _key(self, frame: _Frame, event: NodeEvent) -> None:
        frame.key_start = _start(event)
        key: str | None = None
        key_type: str | None = None
        if isinstance(event, ScalarEvent):
            key, key_type = event.value, self._key_type(event.value, _tag(event))
            if isinstance(event.anchor, str):
                self.anchors[event.anchor] = event
        elif isinstance(event, AliasEvent) and event.anchor in self.anchors:
            target = self.anchors[event.anchor]
            if isinstance(target, ScalarEvent):
                key, key_type = target.value, self._key_type(target.value, _tag(target))
            else:
                node = self.nodes[target]
                if not isinstance(node.value, Collection | Alias) and node.text is not None:
                    key, key_type = node.text, self._key_type(node.text, node.tag or PLAIN)
        if key is None or len(key) > self.limits.max_scalar or not representable(key):
            frame.key = None
            if isinstance(event, CollectionStartEvent):
                self.skip_depth = 1
            else:
                frame.skip_value = True
            return
        frame.key, frame.key_type = key, key_type

    def _key_type(self, text: str, tag: str) -> str:
        """A key's type, so that ``1`` and ``"1"`` are two keys and ``a`` and ``"a"`` one: a
        quoted or ``!!str`` key is a string, a plain one the type each version reads it as."""
        if tag in (NON_PLAIN, _STR):
            return _STR
        if tag != PLAIN or len(text) > self.limits.max_scalar:
            return tag
        return "|".join(sorted({_reading_type(implicit(text, v)) for v in self.versions}))

    def _value(self, frame: _Frame | None, event: NodeEvent) -> None:
        key_type: str | None = None
        if frame is None:
            path: Path = ()
            order, parent = 0, -1
        elif frame.mapping:
            assert frame.key is not None
            path, order, parent = (*frame.path, frame.key), frame.count, frame.index
            key_type, frame.key, frame.key_type = frame.key_type, None, None
        else:
            path, order, parent = (*frame.path, frame.count), frame.count, frame.index
        if frame is not None:
            frame.count += 1
        if len(path) > self.limits.max_depth:
            self.too_deep = True
            return
        index = len(self.nodes)
        start, end = _start(event), _end(event)
        if isinstance(event, AliasEvent):
            anchor = event.anchor or ""
            target = self.anchors.get(anchor)
            if target is None:
                reason = f"no node before it carries the anchor {anchor!r}"
                value: NodeValue = Unreadable(Issue.UNDEFINED_ALIAS, reason)
                node = Node(path, order, parent, value, None, None, (start, end))
                node.issues = (Issue.UNDEFINED_ALIAS,)
            elif isinstance(target, ScalarEvent):  # a key's anchor: the key's scalar again
                node = self._scalar(path, order, parent, target)
            else:
                alias = Alias(anchor, self.nodes[target].path)
                node = Node(path, order, parent, alias, None, None, (start, end))
        elif isinstance(event, ScalarEvent):
            node = self._scalar(path, order, parent, event)
        else:
            mapping = isinstance(event, MappingStartEvent)
            kind = CollectionType.MAPPING if mapping else CollectionType.SEQUENCE
            tag = getattr(event, "tag", None) or PLAIN
            node = Node(path, order, parent, Collection(kind, 0), None, tag, (start, end))
            flow = bool(getattr(event, "flow_style", False))
            self.frames.append(_Frame(index, mapping, path, flow=flow, last_end=end))
        node.key_type = key_type
        self.nodes.append(node)
        if frame is not None:
            frame.children.append(index)
            if node.span is not None and not isinstance(event, CollectionStartEvent):
                frame.last_end = max(frame.last_end, node.span[1])
        marked = getattr(event, "anchor", None)
        if isinstance(marked, str) and not isinstance(event, AliasEvent):
            self.anchors[marked] = index

    def _scalar(self, path: Path, order: int, parent: int, event: ScalarEvent) -> Node:
        text, tag, node_tag = event.value, event.tag, _tag(event)
        plain = node_tag == PLAIN
        span = (_start(event), _end(event))
        if len(text) > self.limits.max_scalar:
            large = Unreadable(Issue.SCALAR_TOO_LARGE, "over max_scalar_length")
            return Node(path, order, parent, large, None, node_tag, span, False, (large.issue,))
        if not representable(text):  # a "\ud800" escape
            lone = Unreadable(Issue.UNREPRESENTABLE, "a string with an unpaired surrogate")
            return Node(path, order, parent, lone, None, node_tag, span, False, (lone.issue,))
        reading: NodeValue
        if tag is None and not plain:
            reading = Value((ConfigScalar(ScalarType.STRING, text),))
        else:
            readings = [
                implicit(text, version) if tag is None else tagged(text, tag, version)
                for version in self.versions
            ]
            reading = readings[0] if len(readings) == 1 else combine(*readings)
        issues: tuple[Issue, ...] = ()
        if isinstance(reading, Unreadable):
            issues = (reading.issue,)
        elif isinstance(reading, Value) and len(reading.readings) > 1:
            issues = (Issue.AMBIGUOUS_TYPE,)
        return Node(path, order, parent, reading, text, node_tag, span, False, issues)

    def _close(self, event: Any) -> None:
        frame = self.frames.pop()
        node = self.nodes[frame.index]
        assert node.span is not None and isinstance(node.value, Collection)
        # A block collection's end event sits where the next token starts, past any trailing
        # comments; its last entry's end is where it ends.
        end = _end(event) if frame.flow else max(frame.last_end, node.span[1])
        node.value = Collection(node.value.type, frame.count)
        node.span = (node.span[0], end)
        if frame.mapping:
            mark_repeats(self.nodes, frame.children)
        if self.frames:
            parent = self.frames[-1]
            parent.last_end = max(parent.last_end, end)

    def finish(self, end: int) -> Document | TooDeep:
        extent = (self.start, end)
        if self.too_deep:
            return TooDeep(self.index, extent)
        return Document(self.index, self.nodes, extent, version=self.version, skipped=self.skipped)


_STR: Final = "tag:yaml.org,2002:str"
_TYPE_TAGS: Final = {
    ScalarType.BOOL: "tag:yaml.org,2002:bool",
    ScalarType.INT: "tag:yaml.org,2002:int",
    ScalarType.FLOAT: "tag:yaml.org,2002:float",
    ScalarType.STRING: _STR,
    ScalarType.BINARY: "tag:yaml.org,2002:binary",
    ScalarType.OFFSET_DATETIME: "tag:yaml.org,2002:timestamp",
    ScalarType.LOCAL_DATETIME: "tag:yaml.org,2002:timestamp",
    ScalarType.LOCAL_DATE: "tag:yaml.org,2002:timestamp",
    ScalarType.LOCAL_TIME: "tag:yaml.org,2002:timestamp",
}


def _tag(event: ScalarEvent) -> str:
    """A scalar's tag: the explicit one, or the non-specific ``?`` (plain) or ``!`` (quoted)."""
    if event.tag is not None:
        return str(event.tag)
    return PLAIN if event.style is None or event.style == "" else NON_PLAIN


def _reading_type(reading: Reading) -> str:
    """The tag a plain scalar resolves to under one version's schema."""
    if isinstance(reading, Null):
        return "tag:yaml.org,2002:null"
    if isinstance(reading, Value):
        return _TYPE_TAGS[reading.readings[0].type]
    return f"unreadable:{reading.issue}"


def _versions(declared: tuple[int, int] | None, option: str) -> tuple[YamlVersion, ...]:
    if declared == (1, 1):
        return ("1.1",)
    if declared == (1, 2):
        return ("1.2",)
    if option == "1.1":
        return ("1.1",)
    if option == "1.2":
        return ("1.2",)
    return ("1.1", "1.2")


def _problem(exc: yaml.YAMLError, document: int, start: int) -> Problem:
    if isinstance(exc, yaml.reader.ReaderError):
        message = f"unacceptable character #x{exc.character:04x}: {exc.reason}"
        return Problem(message, exc.position, document, start)
    if isinstance(exc, yaml.MarkedYAMLError):
        mark = exc.problem_mark or exc.context_mark
        message = "; ".join(part for part in (exc.context, exc.problem) if part)
        return Problem(message or "not YAML", mark.index if mark else start, document, start)
    return Problem(str(exc) or "not YAML", start, document, start)


def read_yaml(text: str, limits: Limits, option: str) -> Parse:
    """Every document of a YAML stream, up to the first that does not parse."""
    parse = Parse(ConfigFormat.YAML, [])
    finished: list[Document | TooDeep] = []
    boundary, index = 0, 0
    current: _Document | None = None
    loader: _Loader | None = None
    try:
        loader = _Loader(text)
        while loader.check_event():
            event = _next(loader)
            if isinstance(event, DocumentStartEvent):
                declared: tuple[str, Spot] | None = None
                if event.version is not None:
                    major, minor = event.version
                    spot = loader.directive or (_start(event), _end(event))
                    declared = (f"{major}.{minor}", spot)
                    if (major, minor) not in ((1, 1), (1, 2)):
                        parse.unsupported_version.append((index, declared[0], spot))
                versions = _versions(event.version, option)
                current = _Document(index, boundary, versions, limits, declared)
                loader.directive = None
            elif isinstance(event, DocumentEndEvent):
                assert current is not None
                boundary = _end(event)
                finished.append(current.finish(boundary))
                current, index = None, index + 1
            elif isinstance(event, StreamEndEvent):
                break
            elif current is not None:
                current.event(event)
    except yaml.YAMLError as exc:
        parse.problem = _problem(exc, index, boundary)
    finally:
        if loader is not None:
            loader.dispose()
    end = len(text) if parse.problem is None else boundary
    found = comments(text, loader.covered, loader.headers, end) if loader is not None else []
    documents = [done for done in finished if isinstance(done, Document)]
    parse.too_deep = [done for done in finished if isinstance(done, TooDeep)]
    _assign(found, finished, documents, end)
    parse.documents = documents
    return parse


def _assign(
    found: list[Spot], finished: list[Document | TooDeep], documents: list[Document], end: int
) -> None:
    """Each comment to the document whose extent holds it; before the first, to the first; after
    the last, to the last. A comment of a document that was not read goes with it."""
    if not finished:
        return
    extents = [done.extent for done in finished]
    kept = {document.index: document for document in documents}
    for comment in found:
        position = len(extents) - 1
        for number, (_, stop) in enumerate(extents):
            if comment[0] < stop:
                position = number
                break
        owner = finished[position]
        if isinstance(owner, Document) and owner.index in kept:
            owner.comments.append(comment)
