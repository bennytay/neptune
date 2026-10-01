"""Ignore rules: what a walk leaves unread on purpose, declared and never silent (ADR 0043).

A walk reads every regular file under its root. Ignore rules name what it leaves unread on
purpose: version-control internals and operating-system metadata by default, then the caller's own
patterns, then the root's optional ``.neptune-ignore``. Nothing ignored is dropped silently:

- the walk yields each ignored entry as a ``SkippedEntry`` with reason ``ignored`` and its rule,
  and does not enter an ignored directory, so one finding covers everything below it;
- the scan records a ``neptune.discovery.ignored`` finding (skipped, info) per ignored entry,
  naming the rule's pattern and origin, and asserts nothing absent at or below it;
- those findings name the ``neptune.discovery.ignore`` transform, whose config is every rule in
  force, in order, so a package declares exactly what it left out and under which rules. A job
  whose rules matched nothing records no finding and so no transform: its package is the one a
  job without rules writes.

Pattern syntax, a subset of gitignore, over bytes so names that are not UTF-8 match exactly:

- one pattern per line; blank lines and lines starting with ``#`` are skipped; trailing spaces,
  tabs and a carriage return are dropped;
- ``*``, ``?`` and ``[...]`` match within one name, case-sensitively; a component that is exactly
  ``**`` matches zero or more whole names;
- a trailing ``/`` matches directories only;
- a pattern with a ``/`` at its start or inside it is anchored at the root; any other pattern
  matches an entry's name at any depth;
- ``!`` (negation) is refused, as are empty, ``.`` and ``..`` components and NUL bytes. A
  backslash is an ordinary character.

The root's ``.neptune-ignore`` is configuration the source carries: read through the walk's own
safe ``open`` (never through a symlink), at most ``MAX_FILE_BYTES`` and ``MAX_RULES`` rules. One it
cannot use (unreadable, a symlink, too large, a refused line) is an ``IgnoreError`` and fails the
job as a configuration error: rules that were meant to exclude something are never half-applied.
The file itself is evidence like any other, walked and hashed unless a rule ignores it.
"""

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from typing import Final

from neptune.discovery.policy import bytes_field
from neptune.discovery.source import LocalSource, SkipReason, SourceAccessError
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.jsonvalue import JsonObject
from neptune.model.provenance import TransformRecord
from neptune.model.source import local_location

IGNORE_ADAPTER_ID: Final = "neptune.discovery.ignore"
IGNORE_VERSION: Final = "1.0.0"
IGNORED: Final = "neptune.discovery.ignored"  # the one finding code (ADR 0043)

IGNORE_FILE: Final = b".neptune-ignore"
MAX_FILE_BYTES: Final = 64 * 1024
MAX_RULES: Final = 256
MAX_PATTERN_BYTES: Final = 1024

# Never robot evidence: the internals of version-control tools and the metadata operating systems
# write onto every drive they touch (a robot's SD card copied through a laptop gains all of them).
DEFAULT_PATTERNS: Final[tuple[str, ...]] = (
    ".git/",
    ".hg/",
    ".svn/",
    ".DS_Store",
    "._*",
    ".Spotlight-V100/",
    ".Trashes/",
    ".fseventsd/",
    ".TemporaryItems/",
    ".Trash-*/",
    "Thumbs.db",
    "desktop.ini",
    "$RECYCLE.BIN/",
    "System Volume Information/",
)


class Origin:
    """Where a rule came from, as its config records it."""

    DEFAULT: Final = "default"
    OPTION: Final = "option"
    FILE: Final = "file"


class IgnoreError(ValueError):
    """A pattern or a ``.neptune-ignore`` that cannot be used; the message says where and why."""


@dataclass(frozen=True)
class IgnoreRule:
    """One parsed pattern. ``text`` is the line as written, less trailing whitespace."""

    pattern: bytes
    origin: str
    components: tuple[bytes, ...]
    anchored: bool
    dir_only: bool

    @property
    def text(self) -> str:
        return os.fsdecode(self.pattern)

    def matches(self, parts: tuple[bytes, ...], *, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        if not self.anchored:
            return fnmatch.fnmatchcase(parts[-1], self.components[0])
        return _match_components(self.components, parts)

    def to_json(self) -> JsonObject:
        return {"origin": self.origin, **bytes_field("pattern", self.pattern)}


def parse_rule(line: bytes, origin: str) -> IgnoreRule | None:
    """``line`` as a rule; ``None`` for a blank line or a comment; ``IgnoreError`` if refused."""
    text = line.rstrip(b" \t\r")
    if not text or text.startswith(b"#"):
        return None
    if text.startswith(b"!"):
        raise IgnoreError("negation (!) is not supported: a rule only ever leaves things out")
    if b"\x00" in text:
        raise IgnoreError("a pattern holds a NUL byte")
    if len(text) > MAX_PATTERN_BYTES:
        raise IgnoreError(f"a pattern is longer than {MAX_PATTERN_BYTES} bytes")
    body = text
    dir_only = body.endswith(b"/")
    if dir_only:
        body = body[:-1]
    anchored = body.startswith(b"/")
    if anchored:
        body = body[1:]
    components = tuple(body.split(b"/"))
    if any(part in (b"", b".", b"..") for part in components):
        raise IgnoreError(f"{os.fsdecode(text)!r} is not a relative path pattern")
    anchored = anchored or len(components) > 1
    collapsed: list[bytes] = []
    for part in components:  # consecutive ** match what one does
        if not (part == b"**" and collapsed and collapsed[-1] == b"**"):
            collapsed.append(part)
    return IgnoreRule(text, origin, tuple(collapsed), anchored, dir_only)


def _match_components(pattern: tuple[bytes, ...], parts: tuple[bytes, ...]) -> bool:
    """Whether the path ``parts`` matches ``pattern`` whole; ``**`` spans zero or more names.

    One pass per pattern component over the path (time: components times names), never a
    regular expression over the joined path, so no pattern backtracks without bound.
    """
    reach = [True] + [False] * len(parts)  # reach[j]: the components so far match parts[:j]
    for component in pattern:
        if component == b"**":
            spanned, seen = [], False
            for here in reach:
                seen = seen or here
                spanned.append(seen)
            reach = spanned
        else:
            reach = [False] + [
                reach[j] and fnmatch.fnmatchcase(parts[j], component) for j in range(len(parts))
            ]
    return reach[-1]


@dataclass(frozen=True)
class IgnorePolicy:
    """Which ignore rules a job applies (``JobOptions.ignore``).

    - ``defaults``: apply ``DEFAULT_PATTERNS`` (version-control internals, OS metadata);
    - ``patterns``: the caller's own, after the defaults, in this syntax;
    - ``file``: read the root's ``.neptune-ignore``, if there is one, after both.

    Every pattern is checked when the policy is built: ``IgnoreError`` names the first refused.
    """

    defaults: bool = True
    patterns: tuple[str, ...] = ()
    file: bool = True
    _rules: tuple[IgnoreRule, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.defaults, bool) or not isinstance(self.file, bool):
            raise IgnoreError("defaults and file are booleans")
        if not isinstance(self.patterns, tuple) or not all(
            isinstance(p, str) for p in self.patterns
        ):
            raise IgnoreError(f"patterns must be a tuple of text, got {self.patterns!r}")
        rules = _parse_all(DEFAULT_PATTERNS, Origin.DEFAULT) if self.defaults else ()
        rules += _parse_all(self.patterns, Origin.OPTION)
        object.__setattr__(self, "_rules", rules)

    def rules(self, source: LocalSource) -> "IgnoreRules":
        """The rules for a walk of ``source``: the policy's, then its root's ``.neptune-ignore``.

        A root that is one file has no ``.neptune-ignore`` and nothing below it to ignore.
        """
        rules = self._rules
        if self.file and not source.is_file:
            data = _read_ignore_file(source)
            if data is not None:
                lines = data.split(b"\n")
                try:
                    rules += _parse_all(lines, Origin.FILE)
                except IgnoreError as exc:
                    raise IgnoreError(f"{IGNORE_FILE.decode()}: {exc}") from exc
        if len(rules) > MAX_RULES:
            raise IgnoreError(f"{len(rules)} ignore rules; at most {MAX_RULES} are taken")
        return IgnoreRules(rules)


def _parse_all(lines: Iterable[str | bytes], origin: str) -> tuple[IgnoreRule, ...]:
    rules = []
    for number, line in enumerate(lines, start=1):
        try:
            rule = parse_rule(os.fsencode(line) if isinstance(line, str) else line, origin)
        except IgnoreError as exc:
            where = f"line {number}" if origin == Origin.FILE else f"pattern {number}"
            raise IgnoreError(f"{where}: {exc}") from exc
        if rule is not None:
            rules.append(rule)
        if len(rules) > MAX_RULES:
            raise IgnoreError(f"more than {MAX_RULES} ignore rules")
    return tuple(rules)


def _read_ignore_file(source: LocalSource) -> bytes | None:
    """The root's ``.neptune-ignore``, opened as the walk opens any file; ``None`` if absent."""
    location = local_location(IGNORE_FILE)
    try:
        stream = source.open(location)
    except SourceAccessError as exc:
        if exc.reason is SkipReason.MISSING:
            return None
        raise IgnoreError(f"{IGNORE_FILE.decode()} cannot be read: {exc.reason}") from exc
    except OSError as exc:
        raise IgnoreError(f"{IGNORE_FILE.decode()} cannot be read: {exc.strerror}") from exc
    try:
        with stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise IgnoreError(f"{IGNORE_FILE.decode()} cannot be read: {exc.strerror}") from exc
    if len(data) > MAX_FILE_BYTES:
        raise IgnoreError(f"{IGNORE_FILE.decode()} is larger than {MAX_FILE_BYTES} bytes")
    return data


@dataclass(frozen=True)
class IgnoreRules:
    """The rules one walk applies, in order; the first that matches an entry names it."""

    rules: tuple[IgnoreRule, ...]

    def match(self, parts: tuple[bytes, ...], *, is_dir: bool) -> IgnoreRule | None:
        """The first rule matching the root-relative path ``parts``, if any."""
        for rule in self.rules:
            if rule.matches(parts, is_dir=is_dir):
                return rule
        return None

    @cached_property
    def transform(self) -> TransformRecord:
        """The producer of ``ignored`` findings; its config is every rule, in order."""
        return transform_record(
            adapter_id=IGNORE_ADAPTER_ID,
            adapter_version=IGNORE_VERSION,
            config={"rules": [rule.to_json() for rule in self.rules]},
        )

    def finding(self, raw_path: bytes, rule: IgnoreRule) -> IngestFinding:
        """The finding for the entry at ``raw_path`` that ``rule`` left unread."""
        return ingest_finding(
            code=IGNORED,
            category=FindingCategory.SKIPPED,
            severity=Severity.INFO,
            subject=local_location(raw_path),
            transform=self.transform,
            message="left unread by an ignore rule; nothing at or below it is asserted",
            details=rule.to_json(),
        )
