"""The shape every format reader produces: documents of nodes in document order, and problems.

A reader (``_json``, ``_toml``, ``_yaml``) turns decoded text into a ``Parse``: the documents it
read, each a list of ``Node`` in pre-order (a node before its children, children in source order),
and the problem that stopped it, if any. The adapter turns nodes into records; nothing here knows
about records, ids or chunks.
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, TypeAlias

from neptune.model.configuration import (
    CollectionType,
    ConfigFormat,
    ConfigScalar,
    Path,
)

Spot: TypeAlias = tuple[int, int]  # code points [start, end) of the decoded text


class Issue(StrEnum):
    """A problem with one value, reported once per document and code (``config.<value>``)."""

    DUPLICATE_KEY = "duplicate_key"
    UNREPRESENTABLE = "unrepresentable_value"
    SCALAR_TOO_LARGE = "scalar_too_large"
    UNRESOLVED_TAG = "unresolved_tag"
    INVALID_VALUE = "invalid_value"
    UNDEFINED_ALIAS = "undefined_alias"
    AMBIGUOUS_TYPE = "yaml_version_undeclared"
    NONSTANDARD_JSON = "nonstandard_json"


@dataclass(frozen=True)
class Null:
    """The format defines the scalar as null: the document says there is no value."""


@dataclass(frozen=True)
class Value:
    """One reading, or two (YAML 1.1 first, then 1.2) where the readings differ."""

    readings: tuple[ConfigScalar, ...]


@dataclass(frozen=True)
class Unreadable:
    """No reading can be held: why, as an issue, and a short reason for the finding."""

    issue: Issue
    reason: str


Reading: TypeAlias = Null | Value | Unreadable


@dataclass(frozen=True)
class Collection:
    type: CollectionType
    length: int


@dataclass(frozen=True)
class Alias:
    anchor: str
    target: Path | None  # None: no node of this document carries the anchor


NodeValue: TypeAlias = Collection | Alias | Reading


def representable(text: str) -> bool:
    """Whether a record can hold the text: an escape can decode to an unpaired surrogate."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def issues_for(value: NodeValue, issues: tuple[Issue, ...] = ()) -> tuple[Issue, ...]:
    """A node's issues: those given, and why its reading cannot be held, if it cannot."""
    if isinstance(value, Unreadable) and value.issue not in issues:
        return (*issues, value.issue)
    return issues


@dataclass
class Node:
    """One value of a document, as a reader found it.

    ``text`` is a scalar's declared text (``None`` for collections and aliases, or where it
    cannot be held); ``tag`` a YAML tag (``None`` in JSON and TOML); ``span`` where the value is
    written, when the reader locates it; ``repeated`` that its key's text repeats in its mapping;
    ``key_type`` what type its key is (YAML: ``1`` and ``"1"`` are two keys of one text), ``None``
    where every key is a string (JSON, TOML).
    """

    path: Path
    order: int
    parent: int  # index of the parent node in the document, -1 for the root
    value: NodeValue
    text: str | None = None
    tag: str | None = None
    span: Spot | None = None
    repeated: bool = False
    issues: tuple[Issue, ...] = ()
    key_type: str | None = None


@dataclass(frozen=True)
class SkippedEntry:
    """A mapping entry that cannot be held: a key that is a collection, or not valid text."""

    parent: int
    span: Spot
    reason: str


@dataclass
class Document:
    """One document: its nodes in pre-order and what it declares about itself.

    ``index`` is its position in the stream (always 0 outside YAML) and ``extent`` the code points
    it covers, comments included. ``version`` is a YAML ``%YAML`` directive's version and where it
    is written.
    """

    index: int
    nodes: list[Node]
    extent: Spot
    comments: list[Spot] = field(default_factory=list)
    version: tuple[str, Spot] | None = None
    skipped: list[SkippedEntry] = field(default_factory=list)


@dataclass(frozen=True)
class Problem:
    """What stopped a reader: its message, where (a code point), and which document it was in."""

    message: str
    offset: int
    document: int
    start: int  # the first code point not read: the failing document's start


@dataclass(frozen=True)
class TooDeep:
    """A document nested deeper than ``max_depth``: it is not read."""

    document: int
    extent: Spot


@dataclass
class Parse:
    """What one reader made of a whole text."""

    format: ConfigFormat
    documents: list[Document]
    problem: Problem | None = None
    too_deep: list[TooDeep] = field(default_factory=list)
    unsupported_version: list[tuple[int, str, Spot]] = field(default_factory=list)


@dataclass(frozen=True)
class Limits:
    max_depth: int
    max_scalar: int  # code points of a scalar's text


# The JSON Pointer escaping of RFC 6901 §3.
POINTER_ESCAPES: Final = (("~", "~0"), ("/", "~1"))


def pointer_token(segment: str | int) -> str:
    if isinstance(segment, int):
        return str(segment)
    for raw, escaped in POINTER_ESCAPES:
        segment = segment.replace(raw, escaped)
    return segment


def mark_repeats(nodes: list[Node], children: list[int]) -> None:
    """Flag the entries of one mapping whose keys' text repeats: each is addressed by position.
    Those whose key is also of one type (the same key, not only the same text) are duplicates."""
    by_text: dict[str | int, list[int]] = {}
    for child in children:
        by_text.setdefault(nodes[child].path[-1], []).append(child)
    for group in by_text.values():
        if len(group) < 2:
            continue
        types = Counter(nodes[child].key_type for child in group)
        for child in group:
            nodes[child].repeated = True
            if types[nodes[child].key_type] > 1:
                nodes[child].issues = (*nodes[child].issues, Issue.DUPLICATE_KEY)
