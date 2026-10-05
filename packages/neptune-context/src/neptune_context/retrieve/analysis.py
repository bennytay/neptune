"""Text analysis for the lexical channel (ADR 0008 §2): one deterministic tokenizer, two modes.

Robotics text is full of strings that must be found *exactly*: serial numbers (``SN-A4471-9``),
ROS topics (``/uav21/imu/data``), declared ids (``asset_tag:hx-02``), firmware versions
(``2.4.1-rc3``). An analyser built for prose would split, stem or stop-word them away. So:

- Text is NFKC-normalised and case-folded, then cut into *words* (runs of letters, digits and
  combining marks). Words joined by a single ``- _ . / :`` between alphanumerics form a
  *compound*; its parts keep consecutive positions, so a compound is found as an exact phrase of
  its parts: ``SN-A4471-9`` matches ``sn a4471 9`` but never ``SN-A4471-7``.
- ``Mode.PROSE`` additionally stems a word that stands alone and is made of letters only
  (``stalls`` and ``stalled`` meet at ``stall``). ``Mode.VERBATIM`` (declared ids) and every
  compound part are never stemmed. No stop words are removed: phrases keep their adjacency and
  BM25's inverse document frequency already discounts common words.
- The only stemmer is ``english-light``: regular English plurals, ``-ing`` and ``-ed`` on ASCII
  words, and a trailing ``e``. It is a rule list, not a dictionary, so it never changes between
  library versions. ``verbatim`` stems nothing. A tenant picks one by name (``analyzer_for``).

No segmentation: scripts written without spaces (CJK) stay one word per run. Input is hostile:
words are cut at ``MAX_WORD_CHARS`` and a document at ``MAX_DOCUMENT_TOKENS`` by the caller.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable

JOINERS: Final = frozenset("-_./:")
MAX_WORD_CHARS: Final = 128
MAX_QUERY_CLAUSES: Final = 64

_QUOTED = re.compile(r'"([^"]*)"')
_VOWELS = frozenset("aeiouy")


class Mode(StrEnum):
    """How a field's text is analysed: prose is stemmed, an identifier field is not."""

    PROSE = "prose"
    VERBATIM = "verbatim"


@dataclass(frozen=True)
class Clause:
    """Consecutive terms to find adjacent and in order. ``required`` clauses (quoted in the
    query) must match for a document to be returned; the rest only raise its score."""

    terms: tuple[str, ...]
    required: bool = False


@dataclass(frozen=True)
class Token:
    term: str
    position: int


def _is_word_char(char: str) -> bool:
    return char.isalnum() or unicodedata.category(char).startswith("M")


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _compounds(text: str) -> list[list[str]]:
    """The compounds of normalised text, each a list of its words."""
    compounds: list[list[str]] = []
    current: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if not _is_word_char(text[i]):
            i += 1
            continue
        j = i
        while j < n and _is_word_char(text[j]):
            j += 1
        current.append(text[i : min(j, i + MAX_WORD_CHARS)])
        i = j
        if i + 1 < n and text[i] in JOINERS and _is_word_char(text[i + 1]):
            i += 1
            continue
        compounds.append(current)
        current = []
    return compounds


def _stem_english(word: str) -> str:
    """``english-light``: see the module docstring. Words of three letters or fewer, and any
    word with a character outside a-z, are returned unchanged."""
    if len(word) <= 3 or not (word.isascii() and word.isalpha()):
        return word
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("sses") or word.endswith(("xes", "zes", "ches", "shes")):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            base = word[: -len(suffix)]
            if any(c in _VOWELS for c in base):
                if len(base) > 2 and base[-1] == base[-2] and base[-1] not in "lsz":
                    base = base[:-1]
                word = base
                break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word


def _identity(word: str) -> str:
    return word


@dataclass(frozen=True)
class Analyzer:
    """A named analysis chain. Equal names are equal analysers; the name is part of an index's
    configuration, so changing a tenant's analyser means a new index (a new lineage)."""

    name: str
    _stem: Callable[[str], str]

    def _terms(self, text: str, mode: Mode) -> list[list[str]]:
        out = []
        for parts in _compounds(_normalise(text)):
            if mode is Mode.PROSE and len(parts) == 1 and parts[0].isalpha():
                parts = [self._stem(parts[0])]
            out.append(parts)
        return out

    def tokens(self, text: str, mode: Mode) -> tuple[Token, ...]:
        """Every term of ``text`` with its position (consecutive across the whole text)."""
        flat = [term for parts in self._terms(text, mode) for term in parts]
        return tuple(Token(term, position) for position, term in enumerate(flat))

    def query(self, text: str, mode: Mode) -> tuple[tuple[Clause, ...], bool]:
        """The clauses of a query and whether any were dropped to stay within
        ``MAX_QUERY_CLAUSES``. A quoted segment is one required phrase; outside quotes each
        compound is a phrase of its parts and optional. Unbalanced quotes are punctuation."""
        clauses: list[Clause] = []
        cursor = 0
        for quoted in _QUOTED.finditer(text):
            clauses.extend(self._loose(text[cursor : quoted.start()], mode))
            terms = tuple(t for parts in self._terms(quoted.group(1), mode) for t in parts)
            if terms:
                clauses.append(Clause(terms, required=True))
            cursor = quoted.end()
        clauses.extend(self._loose(text[cursor:], mode))
        unique = list(dict.fromkeys(clauses))
        return tuple(unique[:MAX_QUERY_CLAUSES]), len(unique) > MAX_QUERY_CLAUSES

    def _loose(self, text: str, mode: Mode) -> list[Clause]:
        return [Clause(tuple(parts)) for parts in self._terms(text, mode)]


ENGLISH: Final = Analyzer("english", _stem_english)
VERBATIM: Final = Analyzer("verbatim", _identity)
ANALYZERS: Final = {a.name: a for a in (ENGLISH, VERBATIM)}


def analyzer_for(name: str) -> Analyzer:
    """The analyser called ``name``; an unknown name is a ``ValueError`` naming the choices."""
    try:
        return ANALYZERS[name]
    except KeyError:
        raise ValueError(f"unknown analyzer {name!r}; choose one of {sorted(ANALYZERS)}") from None
