"""The text index seam and its in-process BM25 backend (ADR 0008 §1, §3).

``TextIndex`` is what a lexical backend must do: take text units (``IndexedText``), keep tenants
apart, and answer a ``SearchRequest`` with scored keys. It knows nothing about claims, records,
packets or provenance; ``retrieve.lexical`` owns those. Postgres full-text and tantivy backends
implement the same two methods later; ``Bm25Index`` is the one Context ships because it needs no
server, no network and no platform-dependent library, so the same index and query give the same
answer on every machine.

``Bm25Index`` is Okapi BM25 (k1 = 1.2, b = 0.75, Lucene's non-negative idf) over positional
postings in memory:

- A unit is one text in one field. It is *visible* to a request when its source has a snapshot
  in the request, ``visible_from <= snapshot < visible_until``, and the request's inference mode
  admits it. Only visible units in the requested fields take part: corpus size and average length
  are computed over all of them (so identifier and prose fields score on one scale) and document
  frequency over those of the clause's analysis mode, so withheld inferred text never changes the
  score of anything returned.
- A required clause (quoted in the query) must match; any other clause adds its BM25 term. A
  clause of several terms is an exact adjacent phrase, scored as one term with its own
  document frequency.
- Scores are rounded to 9 decimals and results ordered by score (descending), then key, so the
  order never depends on insertion order or on the last bit of a platform's ``log``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

from neptune_context.query.model import TextField
from neptune_context.retrieve.analysis import (
    ENGLISH,
    JOINERS,
    MAX_QUERY_CLAUSES,
    MAX_WORD_CHARS,
    Analyzer,
    Clause,
    Mode,
    analyzer_for,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from neptune.model.jsonvalue import JsonObject

K1: Final = 1.2
B: Final = 0.75
SCORE_DECIMALS: Final = 9
MAX_KEY_CHARS: Final = 512
MAX_TENANT_CHARS: Final = 128
MAX_DOCUMENT_TOKENS: Final = 20_000
MAX_LIMIT: Final = 10_000
DEFAULT_TENANT: Final = "default"
# Identifier fields are matched verbatim; every other field is prose.
VERBATIM_FIELDS: Final = frozenset({TextField.DECLARED_ID})


class TextSource(StrEnum):
    """Which snapshot a unit is read at: Memory's transaction or the Ledger's catalog point."""

    MEMORY = "memory"
    LEDGER = "ledger"


class Inference(StrEnum):
    """Whether inferred units take part: ``ONLY`` names what ``EXCLUDE`` withheld."""

    INCLUDE = "include"
    EXCLUDE = "exclude"
    ONLY = "only"


class IndexFindingCode(StrEnum):
    CONFLICTING_KEY = "conflicting_key"  # the key is held with different content; first wins
    EMPTY_TEXT = "empty_text"  # nothing searchable in the text; not indexed
    TRUNCATED = "truncated"  # indexed up to MAX_DOCUMENT_TOKENS terms


@dataclass(frozen=True)
class IndexFinding:
    code: IndexFindingCode
    key: str
    detail: str


@dataclass(frozen=True)
class IndexedText:
    """One text in one field, keyed by the caller. ``visible_until`` is the first transaction
    at which the unit no longer holds (``None``: never superseded)."""

    key: str
    field: TextField
    source: TextSource
    text: str
    inferred: bool = False
    visible_from: int = 0
    visible_until: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not 0 < len(self.key) <= MAX_KEY_CHARS:
            raise ValueError(f"key is a string of 1 to {MAX_KEY_CHARS} characters")
        if not isinstance(self.field, TextField) or not isinstance(self.source, TextSource):
            raise TypeError("field is a TextField and source a TextSource")
        if not isinstance(self.text, str):
            raise TypeError("text must be a str")
        if type(self.visible_from) is not int or self.visible_from < 0:
            raise ValueError("visible_from is a non-negative int")
        if self.visible_until is not None and (
            type(self.visible_until) is not int or self.visible_until < self.visible_from
        ):
            raise ValueError("visible_until is None or an int >= visible_from")


@dataclass(frozen=True)
class SearchRequest:
    """``text`` is the caller's query text; the backend analyses it with the tenant's analyser.
    ``as_of`` maps each source to the transaction it is read at; a source not named is not
    searched."""

    text: str
    fields: frozenset[TextField]
    as_of: Mapping[TextSource, int]
    inference: Inference = Inference.INCLUDE
    limit: int = 100


@dataclass(frozen=True)
class Match:
    key: str
    score: float


@dataclass(frozen=True)
class SearchResult:
    """Matches in order (score descending, then key), at most ``limit``. ``total`` counts every
    match before the cut; ``clauses`` is how many query clauses the text had (0: nothing
    searchable); ``truncated`` says clauses beyond the backend's bound were ignored."""

    matches: tuple[Match, ...] = ()
    total: int = 0
    clauses: int = 0
    truncated: bool = False


@runtime_checkable
class TextIndex(Protocol):
    """A lexical backend: tenant-partitioned, deterministic, read-only on ``search``."""

    def add(self, tenant: str, units: Iterable[IndexedText]) -> tuple[IndexFinding, ...]:
        """Index units into one tenant's partition. A key already held with the same content is
        a no-op; with other content it is refused (``conflicting_key``): the first wins."""
        ...

    def search(self, tenant: str, request: SearchRequest) -> SearchResult:
        """Matches for ``request`` in one tenant's partition; an unknown tenant has none."""
        ...

    def settings(self, tenant: str) -> JsonObject:
        """Every setting that decides this backend's answers for ``tenant`` (analyser,
        scoring parameters, bounds); the lexical channel reports it as part of its config."""
        ...


def check_tenant(tenant: str) -> str:
    if (
        not isinstance(tenant, str)
        or not 0 < len(tenant) <= MAX_TENANT_CHARS
        or any(not c.isprintable() for c in tenant)
    ):
        raise ValueError(f"a tenant is 1 to {MAX_TENANT_CHARS} printable characters: {tenant!r}")
    return tenant


@dataclass
class _Doc:
    unit: IndexedText
    length: int
    mode: Mode
    starts: frozenset[int]  # positions that begin a compound
    ends: frozenset[int]  # positions that end one


@dataclass
class _Partition:
    docs: list[_Doc] = field(default_factory=list)
    by_key: dict[str, int] = field(default_factory=dict)
    postings: dict[str, dict[int, tuple[int, ...]]] = field(
        default_factory=lambda: defaultdict(dict)
    )


def mode_of(field_: TextField) -> Mode:
    return Mode.VERBATIM if field_ in VERBATIM_FIELDS else Mode.PROSE


class Bm25Index:
    """The in-process BM25 backend. ``analyzers`` names a tenant's analyser (default
    ``english``); a tenant not named uses ``default_analyzer``."""

    def __init__(
        self,
        analyzers: Mapping[str, str] | None = None,
        *,
        default_analyzer: str = ENGLISH.name,
    ) -> None:
        self._default = analyzer_for(default_analyzer)
        self._analyzers = {check_tenant(t): analyzer_for(n) for t, n in (analyzers or {}).items()}
        self._partitions: dict[str, _Partition] = {}

    def analyzer(self, tenant: str) -> Analyzer:
        return self._analyzers.get(check_tenant(tenant), self._default)

    def settings(self, tenant: str) -> JsonObject:
        return {
            "analyzer": self.analyzer(tenant).name,
            "backend": "bm25-inprocess/1",
            "b": B,
            "idf": "lucene",
            "joiners": "".join(sorted(JOINERS)),
            "k1": K1,
            "max_document_tokens": MAX_DOCUMENT_TOKENS,
            "max_query_clauses": MAX_QUERY_CLAUSES,
            "max_word_chars": MAX_WORD_CHARS,
            "score_decimals": SCORE_DECIMALS,
            "stemmer": "english-light/1" if self.analyzer(tenant).name == "english" else "none",
            "verbatim_fields": sorted(str(f) for f in VERBATIM_FIELDS),
        }

    def size(self, tenant: str) -> int:
        part = self._partitions.get(check_tenant(tenant))
        return len(part.docs) if part else 0

    def add(self, tenant: str, units: Iterable[IndexedText]) -> tuple[IndexFinding, ...]:
        analyzer = self.analyzer(tenant)
        part = self._partitions.setdefault(tenant, _Partition())
        findings: list[IndexFinding] = []
        for unit in units:
            held = part.by_key.get(unit.key)
            if held is not None:
                if part.docs[held].unit != unit:
                    findings.append(
                        IndexFinding(
                            IndexFindingCode.CONFLICTING_KEY,
                            unit.key,
                            "the key is already held with different content",
                        )
                    )
                continue
            mode = mode_of(unit.field)
            tokens = analyzer.tokens(unit.text, mode, MAX_DOCUMENT_TOKENS + 1)
            if not tokens:
                findings.append(
                    IndexFinding(IndexFindingCode.EMPTY_TEXT, unit.key, "no searchable terms")
                )
                continue
            if len(tokens) > MAX_DOCUMENT_TOKENS:
                tokens = tokens[:MAX_DOCUMENT_TOKENS]
                findings.append(
                    IndexFinding(
                        IndexFindingCode.TRUNCATED,
                        unit.key,
                        f"indexed the first {MAX_DOCUMENT_TOKENS} terms only",
                    )
                )
            docno = len(part.docs)
            part.docs.append(
                _Doc(
                    unit,
                    len(tokens),
                    mode,
                    frozenset(t.position for t in tokens if t.start),
                    frozenset(t.position for t in tokens if t.end),
                )
            )
            part.by_key[unit.key] = docno
            positions: dict[str, list[int]] = defaultdict(list)
            for token in tokens:
                positions[token.term].append(token.position)
            for term, where in positions.items():
                part.postings[term][docno] = tuple(where)
        return tuple(findings)

    def search(self, tenant: str, request: SearchRequest) -> SearchResult:
        analyzer = self.analyzer(tenant)
        part = self._partitions.get(tenant)
        limit = min(max(request.limit, 1), MAX_LIMIT)
        clauses_by_mode = {}
        truncated = False
        for mode in Mode:
            clauses, cut = analyzer.query(request.text, mode)
            clauses_by_mode[mode] = clauses
            truncated = truncated or cut
        count = len(clauses_by_mode[Mode.PROSE])
        if part is None or count == 0 or not request.fields:
            return SearchResult(clauses=count, truncated=truncated)
        visible = [docno for docno, doc in enumerate(part.docs) if self._visible(doc.unit, request)]
        if not visible:
            return SearchResult(clauses=count, truncated=truncated)
        # One corpus for the visible units of the requested fields, so scores of identifier
        # and prose fields share a scale; a term's document frequency is per analysis mode,
        # because a stemmed term and a verbatim one are not the same term.
        size = len(visible)
        average = sum(part.docs[d].length for d in visible) / size
        scored: list[Match] = []
        for mode in Mode:
            docs = {d for d in visible if part.docs[d].mode is mode}
            scored.extend(_score(part, docs, clauses_by_mode[mode], size, average))
        scored.sort(key=lambda m: (-m.score, m.key))
        return SearchResult(tuple(scored[:limit]), len(scored), count, truncated)

    @staticmethod
    def _visible(unit: IndexedText, request: SearchRequest) -> bool:
        if unit.field not in request.fields:
            return False
        tx = request.as_of.get(unit.source)
        if tx is None or tx < unit.visible_from:
            return False
        if unit.visible_until is not None and tx >= unit.visible_until:
            return False
        if request.inference is Inference.EXCLUDE:
            return not unit.inferred
        if request.inference is Inference.ONLY:
            return unit.inferred
        return True


def _phrase_counts(part: _Partition, clause: Clause, docs: set[int]) -> dict[int, int]:
    """For each document in ``docs`` holding the clause's terms adjacent and in order (and, when
    anchored, as one whole compound), how many times."""
    lists = []
    for term in clause.terms:
        posting = part.postings.get(term)
        if not posting:
            return {}
        lists.append(posting)
    first, rest = lists[0], lists[1:]
    last = len(clause.terms) - 1
    counts: dict[int, int] = {}
    for docno, starts in first.items():
        if docno not in docs or any(docno not in other for other in rest):
            continue
        followers = [set(other[docno]) for other in rest]
        doc = part.docs[docno]
        hits = 0
        for s in starts:
            if not all(s + i + 1 in f for i, f in enumerate(followers)):
                continue
            if clause.anchored and not (
                s in doc.starts
                and s + last in doc.ends
                and not any(s + i in doc.starts for i in range(1, last + 1))
            ):
                continue
            hits += 1
        if hits:
            counts[docno] = hits
    return counts


def _score(
    part: _Partition, docs: set[int], clauses: tuple[Clause, ...], size: int, average: float
) -> list[Match]:
    """BM25 for ``clauses`` over ``docs`` (visible documents of one analysis mode), against a
    corpus of ``size`` documents of ``average`` length."""
    if not docs:
        return []
    matched = [(clause, _phrase_counts(part, clause, docs)) for clause in clauses]
    eligible = set(docs)
    for clause, counts in matched:
        if clause.required:
            eligible &= counts.keys()
    scores: dict[int, float] = defaultdict(float)
    for _clause, counts in matched:
        df = len(counts)
        if not df:
            continue
        idf = math.log(1.0 + (size - df + 0.5) / (df + 0.5))
        for docno, tf in counts.items():
            if docno in eligible:
                norm = K1 * (1.0 - B + B * part.docs[docno].length / average)
                scores[docno] += idf * tf * (K1 + 1.0) / (tf + norm)
    return [
        Match(part.docs[d].unit.key, round(score, SCORE_DECIMALS))
        for d, score in scores.items()
        if score > 0.0
    ]
