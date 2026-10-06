"""Finding and rewriting a Markdown document's links.

The site's paths mirror the repository's, so a relative link between two pages works unchanged.
A link to any other repository file or directory becomes its GitHub URL, an image is copied into
the site, and a link to a path that does not exist is a problem that fails the build. Links are
found by parsing (markdown-it, as MyST does), so a path inside a code span or block is not a link.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

from markdown_it import MarkdownIt

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping

    from markdown_it.token import Token

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# What a repository path is: a file, a directory, or nothing.
PathKind = Literal["file", "dir"] | None


class _Verbatim(MarkdownIt):
    """A CommonMark parser that reports destinations as written (no percent-encoding)."""

    def normalizeLink(self, url: str) -> str:
        return url

    def validateLink(self, url: str) -> bool:
        return True


_PARSER = _Verbatim("commonmark").enable("table")


@dataclass(frozen=True)
class Found:
    """A link or image destination in a document, as written."""

    target: str
    image: bool


def _walk(tokens: Iterable[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _walk(token.children)


def find(text: str) -> list[Found]:
    """Every link and image destination in ``text``, in document order, without repeats."""
    seen: dict[tuple[str, bool], None] = {}
    for token in _walk(_PARSER.parse(text)):
        if token.type == "link_open":
            seen.setdefault((str(token.attrs.get("href", "")), False), None)
        elif token.type == "image":
            seen.setdefault((str(token.attrs.get("src", "")), True), None)
    return [Found(target, image) for target, image in seen]


@dataclass(frozen=True)
class Keep:
    """The destination works as written: external, an anchor, or a page of the site."""


@dataclass(frozen=True)
class Rewrite:
    """The destination is a repository path that is not a page: link to ``url`` instead."""

    url: str


@dataclass(frozen=True)
class Copy:
    """An image in the repository: the site carries ``path`` at the same place."""

    path: str


@dataclass(frozen=True)
class Broken:
    """The destination does not exist, or leaves the repository."""

    reason: str


Resolution = Keep | Rewrite | Copy | Broken


def resolve(
    target: str,
    page: str,
    *,
    image: bool,
    pages: frozenset[str],
    kind: Callable[[str], PathKind],
    repository: str,
    ref: str,
) -> Resolution:
    """How the site handles ``target``, written in the page at site path ``page``.

    ``pages`` are the site's Markdown paths and ``kind`` says what a repository path is; site paths
    and repository paths are the same space.
    """
    if not target or target.startswith(("#", "//")) or _SCHEME.match(target):
        return Keep()
    path, hash_, fragment = target.partition("#")
    path = unquote(path.partition("?")[0])
    if not path:
        return Keep()
    joined = (
        path.lstrip("/") if path.startswith("/") else posixpath.join(posixpath.dirname(page), path)
    )
    resolved = posixpath.normpath(joined)
    if resolved == ".." or resolved.startswith("../") or posixpath.isabs(resolved):
        return Broken(f"{target!r} leaves the repository")
    if resolved in pages:
        return Keep()
    found = kind(resolved) if resolved != "." else "dir"
    if found is None:
        return Broken(f"{target!r} does not exist (resolved to {resolved!r})")
    if image:
        if found != "file":
            return Broken(f"image {target!r} is not a file")
        return Copy(resolved)
    where = "" if resolved == "." else f"/{resolved}"
    view = "blob" if found == "file" else "tree"
    return Rewrite(f"{repository}/{view}/{ref}{where}{hash_}{fragment}")


def rewrite(text: str, replacements: Mapping[str, str]) -> tuple[str, list[str]]:
    """``text`` with each destination in ``replacements`` replaced, and the ones not found.

    Inline links (``](target)``, ``](<target> "title")``) and reference definitions
    (``[label]: target``) are rewritten; a destination written with escapes is reported as not
    found rather than left pointing at a path that is not a page.
    """
    missing: list[str] = []
    for old, new in replacements.items():
        escaped = re.escape(old)
        inline = re.compile(r"(\]\(\s*<?)" + escaped + r"(?=>?(?:\s|\)))")
        definition = re.compile(
            r"(^ {0,3}\[[^\]\n]+\]:[ \t]*<?)" + escaped + r"(?=>?(?:\s|$))", re.M
        )
        replacement = r"\g<1>" + new.replace("\\", "\\\\")
        text, inline_count = inline.subn(replacement, text)
        text, definition_count = definition.subn(replacement, text)
        if inline_count + definition_count == 0:
            missing.append(old)
    return text, missing
