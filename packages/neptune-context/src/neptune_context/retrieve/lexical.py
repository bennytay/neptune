"""The lexical channel: BM25 over text-bearing records and claims (ADR 0008).

Three pieces, none of which knows the others' internals:

- ``Passage``: a span of text from a Ledger record (a document span, a table cell, an ingest
  finding) *with* its provenance. A passage cannot be built without evidence, a transform and
  an assertion kind, so a snippet can never reach a packet without provenance.
- ``LexicalCorpus``: what the channel searches. It feeds a ``TextIndex`` (``retrieve.bm25``)
  from claims (their text-valued objects and the declared ids they name) and passages, and
  remembers how each key maps back to a claim or passage and what has been indexed.
- ``LexicalChannel``: a ``RetrievalChannel``. It searches the corpus for the query's text clause
  in the clause's fields, reads each matching claim back from Memory's reader at the snapshot
  (so a claim is presented as known then, with the resolver findings that name it), and returns
  claim items and document-span items scored by BM25.

``passages_from_catalog`` builds passages from the Ledger's ``query(spec)``: the catalog indexes
records and their evidence anchors, not their text, so the host supplies ``text_of``.

Fields (ADR 0002 §5): ``claim_text`` and ``declared_id`` come from claims; ``record``,
``document`` and ``finding`` from passages. Inferred text takes part only when the query
includes inference; what it withheld is named in one ``inferred_withheld`` gap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Final

from neptune_ledger.api import QueryCursor, QuerySpec, query_meta, query_rows
from neptune_memory.schema.claim import ClaimId, TypedLiteral, ValueType, is_inferred
from neptune_memory.schema.interval import LedgerTx, Open
from neptune_memory.schema.nodes import NodeRef
from neptune_memory.schema.reader import AsOfBeyondHeadError

from neptune.identity.canonical_json import dumps
from neptune.identity.hashing import content_id
from neptune.model.ids import RecordId, parse_record_id
from neptune.model.knowledge import AssertionKind, Known, NotApplicable
from neptune.model.provenance import EvidenceRef, evidence_ref_from_json
from neptune_context.packets.findings import PacketError
from neptune_context.packets.model import (
    Channel,
    ChannelHit,
    ClaimItem,
    DocumentSpanItem,
    Gap,
    GapCode,
    Item,
    ItemProvenance,
    Relevance,
    Superseded,
    Transform,
)
from neptune_context.pinned import claim_beyond_pin, finding_beyond_pin
from neptune_context.query.model import TextChannel, TextField
from neptune_context.retrieve.bm25 import (
    DEFAULT_TENANT,
    Bm25Index,
    IndexedText,
    IndexFinding,
    Inference,
    SearchRequest,
    TextIndex,
    TextSource,
    check_tenant,
)
from neptune_context.retrieve.channel import ChannelAnswer, Retrieval, Snapshot, answer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from neptune_ledger.api import CatalogApi, CatalogFinding, QueryRow
    from neptune_memory.schema.claim import Claim, ClaimAssertionKind
    from neptune_memory.schema.reader import MemoryReader
    from neptune_memory.schema.supersede import ResolutionFinding

    from neptune.model.knowledge import Knowledge

PASSAGE_FIELDS: Final = frozenset({TextField.RECORD, TextField.DOCUMENT, TextField.FINDING})
CLAIM_FIELDS: Final = frozenset({TextField.CLAIM_TEXT, TextField.DECLARED_ID})
MAX_PASSAGE_CHARS: Final = 16_000
MAX_GAP_REFS: Final = 100
PAGE: Final = 500
_TEXT_AT: Final = "/text/text"
_FIELDS_AT: Final = "/text/fields"
_INFERRED_AT: Final = "/include_inferred"


def _placeholder(score: float = 0.0) -> Relevance:
    """A relevance the item is built with; ``answer`` rewrites it to the channel's own hit."""
    return Relevance(score, (ChannelHit(Channel.LEXICAL, 1, score),))


# --- Passages ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Passage:
    """A span of text from one Ledger record, with what it rests on.

    ``registered_at`` is the catalog transaction (``registration_seq``) that made the record
    visible; the passage is searched only at snapshots at or after it. ``provenance.evidence``
    must contain ``evidence`` (the span's own anchor). An inferred passage names its model and
    carries a confidence, exactly like an inferred claim.
    """

    field: TextField
    document: RecordId
    text: str
    evidence: EvidenceRef
    provenance: ItemProvenance
    registered_at: int
    assertion_kind: ClaimAssertionKind
    confidence: Knowledge[float] = dc_field(default_factory=NotApplicable)

    def __post_init__(self) -> None:
        if self.field not in PASSAGE_FIELDS:
            raise ValueError(f"a passage's field is one of {sorted(map(str, PASSAGE_FIELDS))}")
        parse_record_id(self.document)
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("a passage has text")
        if len(self.text) > MAX_PASSAGE_CHARS:
            raise ValueError(f"a passage is at most {MAX_PASSAGE_CHARS} characters")
        if type(self.registered_at) is not int or self.registered_at < 0:
            raise ValueError("registered_at is a non-negative int")
        try:  # the packet model is the one judge of what an item may be
            self.item(_placeholder())
        except PacketError as exc:
            raise ValueError(f"not a valid document span: {exc}") from exc

    @property
    def key(self) -> str:
        anchor = content_id(dumps(self.evidence.to_json()))
        return f"{self.field}:{self.document}:{anchor}"

    @property
    def is_inferred(self) -> bool:
        return is_inferred(self.assertion_kind)

    def item(self, relevance: Relevance) -> DocumentSpanItem:
        return DocumentSpanItem(
            assertion_kind=self.assertion_kind,
            confidence=self.confidence,
            provenance=self.provenance,
            relevance=relevance,
            document=self.document,
            evidence=self.evidence,
            text=Known(self.text),
        )


@dataclass(frozen=True)
class Skipped:
    """A text-bearing record that could not be indexed, and why (never indexed without
    provenance, never indexed with a guessed assertion kind)."""

    record_id: str
    field: TextField
    reason: str


@dataclass(frozen=True)
class PassageBatch:
    passages: tuple[Passage, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    findings: tuple[CatalogFinding, ...] = ()


def passages_from_catalog(
    catalog: CatalogApi,
    kinds: Sequence[str],
    text_of: Callable[[QueryRow], str | None],
    *,
    field: TextField = TextField.RECORD,
    as_of: int | None = None,
    page: int = PAGE,
) -> PassageBatch:
    """Passages for every ``kinds`` record the catalog holds at ``as_of`` (default: latest).

    The catalog's ``query`` gives each record's id, evidence anchor, transform, assertion kind
    and registration; ``text_of(row)`` gives its text (``None``: the record has none) because the
    catalog indexes records, not their bytes. The transform's adapter, version and config hash
    come from the catalog's ``lineage``. A row without a usable anchor, assertion kind, record id
    or transform, or with invalid text, is skipped and named; none becomes a passage.
    """
    passages: list[Passage] = []
    skipped: list[Skipped] = []
    findings: list[CatalogFinding] = []
    transforms: dict[str, Transform | None] = {}
    cursor: QueryCursor | None = None
    while True:
        table = catalog.query(QuerySpec(kinds=tuple(kinds), as_of=as_of, limit=page, after=cursor))
        rows = query_rows(table)
        findings.extend(query_meta(table).findings)
        for row in rows:
            text = text_of(row)
            if text is None:
                continue
            try:
                passages.append(_passage(catalog, row, text, field, as_of, transforms))
            except ValueError as exc:
                skipped.append(Skipped(row.record_id, field, str(exc)))
        if len(rows) < page or not rows:
            break
        last = rows[-1]
        cursor = QueryCursor(last.kind, last.record_id, last.package_id)
    return PassageBatch(
        tuple(passages),
        tuple(sorted(set(skipped), key=lambda s: (s.record_id, s.reason))),
        tuple(findings),
    )


def _passage(
    catalog: CatalogApi,
    row: QueryRow,
    text: str,
    field: TextField,
    as_of: int | None,
    transforms: dict[str, Transform | None],
) -> Passage:
    if row.source_content_id is None or row.source_locator is None:
        raise ValueError("the record states no evidence anchor")
    if row.assertion_kind is None:
        raise ValueError("the record's assertion kind is not stated")
    if row.transform_id is None:
        raise ValueError("the record names no transform")
    try:
        locator = json.loads(row.source_locator)
        evidence = evidence_ref_from_json({"locator": locator, "source": row.source_content_id})
    except (TypeError, ValueError) as exc:
        raise ValueError(f"the evidence anchor is unreadable: {exc}") from exc
    if row.transform_id not in transforms:
        transforms[row.transform_id] = _transform(catalog, row, as_of)
    transform = transforms[row.transform_id]
    if transform is None:
        raise ValueError("the catalog does not hold the record's transform")
    return Passage(
        field=field,
        document=RecordId(row.record_id),
        text=text,
        evidence=evidence,
        provenance=ItemProvenance((evidence,), (RecordId(row.record_id),), transform),
        registered_at=row.registration_seq,
        assertion_kind=AssertionKind(row.assertion_kind),
    )


def _transform(catalog: CatalogApi, row: QueryRow, as_of: int | None) -> Transform | None:
    lineage = catalog.lineage(row.record_id, as_of=as_of)
    for node in lineage.nodes:
        if node.transform_id == row.transform_id and isinstance(node.transform, Known):
            info = node.transform.value
            try:
                return Transform(info.adapter_id, info.adapter_version, info.config_hash)  # type: ignore[arg-type]
            except PacketError:
                return None
    return None


# --- The corpus -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _ClaimRef:
    subject: NodeRef
    predicate: str
    claim: ClaimId
    superseded_at: LedgerTx | None


def _claim_texts(claim: Claim) -> list[tuple[TextField, str]]:
    out = []
    obj = claim.object
    if isinstance(obj, TypedLiteral) and obj.datatype is ValueType.TEXT and obj.value:
        out.append((TextField.CLAIM_TEXT, str(obj.value)))
    ids = [claim.subject.node_id]
    if isinstance(obj, NodeRef):
        ids.append(obj.node_id)
    out.append((TextField.DECLARED_ID, " ".join(ids)))
    return out


class LexicalCorpus:
    """What the lexical channel searches, for one tenant: an index plus the maps back to claims
    and passages. Build it from the whole claim history and the catalog's passages; rebuild it
    (a new corpus, a new lineage) when either changes, never edit it in place."""

    def __init__(self, index: TextIndex | None = None, *, tenant: str = DEFAULT_TENANT) -> None:
        self.index: TextIndex = index if index is not None else Bm25Index()
        self.tenant = check_tenant(tenant)
        self.fields: set[TextField] = set()
        self.claims_through: int | None = None
        self.skipped: list[Skipped] = []
        self._claims: dict[str, _ClaimRef] = {}
        self._passages: dict[str, Passage] = {}
        self._superseders: dict[ClaimId, list[tuple[int, ClaimId]]] = {}

    def add_claims(self, claims: Iterable[Claim], *, through: LedgerTx) -> tuple[IndexFinding, ...]:
        """Index every version of every claim recorded up to ``through`` (a graph document's
        ``head``): the text of text-valued objects and the declared ids a claim names. Versions
        keep their ``recorded_at`` and ``superseded_at``, so any earlier ``as_of`` is searchable."""
        units: list[IndexedText] = []
        for claim in claims:
            for claimed in claim.supersedes:
                self._superseders.setdefault(claimed, []).append((claim.recorded_at, claim.id))
            until = None if isinstance(claim.superseded_at, Open) else claim.superseded_at
            for text_field, text in _claim_texts(claim):
                key = f"claim:{claim.id}:{text_field}:{claim.recorded_at}"
                self._claims[key] = _ClaimRef(claim.subject, claim.predicate, claim.id, until)
                units.append(
                    IndexedText(
                        key,
                        text_field,
                        TextSource.MEMORY,
                        text,
                        inferred=is_inferred(claim.assertion_kind),
                        visible_from=claim.recorded_at,
                        visible_until=until,
                    )
                )
                self.fields.add(text_field)
        self.claims_through = max(self.claims_through or 0, through)
        return self.index.add(self.tenant, units)

    def add_passages(self, passages: Iterable[Passage]) -> tuple[IndexFinding, ...]:
        units: list[IndexedText] = []
        for passage in passages:
            self._passages[passage.key] = passage
            self.fields.add(passage.field)
            units.append(
                IndexedText(
                    passage.key,
                    passage.field,
                    TextSource.LEDGER,
                    passage.text,
                    inferred=passage.is_inferred,
                    visible_from=passage.registered_at,
                )
            )
        return self.index.add(self.tenant, units)

    def add_batch(self, batch: PassageBatch) -> tuple[IndexFinding, ...]:
        """Passages from ``passages_from_catalog``; its skipped records are remembered so the
        channel can say they were not searched."""
        self.skipped.extend(batch.skipped)
        return self.add_passages(batch.passages)

    def claim(self, key: str) -> _ClaimRef | None:
        return self._claims.get(key)

    def passage(self, key: str) -> Passage | None:
        return self._passages.get(key)

    def superseded(self, claims: set[ClaimId], after: int, head: int) -> list[Superseded]:
        """Which of ``claims`` stopped being current in ``(after, head]``, and the versions
        recorded then that superseded them. A supersession whose version the corpus does not
        hold is not reported (``by`` would be empty)."""
        ends = {r.claim: r.superseded_at for r in self._claims.values() if r.claim in claims}
        out = []
        for claim, at in sorted(ends.items()):
            if at is None or not after < at <= head:
                continue
            by = tuple(sorted({c for tx, c in self._superseders.get(claim, ()) if tx == at}))
            if by:
                out.append(Superseded(claim, at, by))
        return out


# --- The channel ------------------------------------------------------------------------------


class LexicalChannel:
    """BM25 over a ``LexicalCorpus``, answering the query's text clause (``RetrievalChannel``).

    ``memory`` is read at the snapshot for every matching claim, so claims are presented as known
    then and a claim Memory no longer holds at that snapshot is not returned.
    """

    def __init__(self, corpus: LexicalCorpus, memory: MemoryReader) -> None:
        self._corpus = corpus
        self._memory = memory

    @property
    def channel(self) -> Channel:
        return Channel.LEXICAL

    def retrieve(self, request: Retrieval) -> ChannelAnswer:
        clause = request.query.text
        if clause is None or TextChannel.LEXICAL not in clause.channels:
            return answer(Channel.LEXICAL, ())
        corpus, query, snapshot = self._corpus, request.query, request.snapshot
        gaps = self._coverage_gaps(clause.fields, snapshot.memory_as_of)
        asked = SearchRequest(
            clause.text,
            frozenset(clause.fields),
            {TextSource.MEMORY: snapshot.memory_as_of, TextSource.LEDGER: snapshot.as_of},
            Inference.INCLUDE if query.include_inferred else Inference.EXCLUDE,
            limit=min(max(4 * query.budget.items, 50), 1000),
        )
        result = corpus.index.search(corpus.tenant, asked)
        if result.clauses == 0:
            gaps.append(_gap(GapCode.NOT_COVERED, _TEXT_AT, "the text has no searchable terms"))
        if result.truncated:
            gaps.append(_gap(GapCode.NOT_COVERED, _TEXT_AT, "clauses beyond the first 64 ignored"))
        if not query.include_inferred:
            gaps.extend(self._withheld(asked))
        scored: list[tuple[float, Item]] = []
        claims: dict[tuple[NodeRef, str], list[tuple[float, ClaimId]]] = {}
        for match in result.matches:
            if (passage := corpus.passage(match.key)) is not None:
                scored.append((match.score, passage.item(_placeholder(match.score))))
            elif (ref := corpus.claim(match.key)) is not None:
                claims.setdefault((ref.subject, ref.predicate), []).append((match.score, ref.claim))
        claim_hits, claim_gaps, findings = self._claims(claims, snapshot, query.include_inferred)
        scored.extend(claim_hits)
        gaps.extend(claim_gaps)
        carried = {i.claim.id for _, i in claim_hits if isinstance(i, ClaimItem)}
        return answer(
            Channel.LEXICAL,
            scored,
            gaps=gaps,
            findings=[f for f in findings if f.claim in carried or carried.intersection(f.others)],
            superseded=corpus.superseded(carried, snapshot.memory_as_of, snapshot.head),
        )

    def _coverage_gaps(self, fields: frozenset[TextField], memory_as_of: int) -> list[Gap]:
        """What the corpus cannot say about the requested fields: nothing indexed for a field,
        claim text older than Memory's snapshot, records skipped for want of provenance."""
        corpus = self._corpus
        gaps = []
        missing = sorted(str(f) for f in fields if f not in corpus.fields)
        if missing:
            gaps.append(
                _gap(
                    GapCode.NOT_COVERED, _FIELDS_AT, f"no text is indexed for: {', '.join(missing)}"
                )
            )
        if fields & CLAIM_FIELDS & corpus.fields and (corpus.claims_through or 0) < memory_as_of:
            gaps.append(
                _gap(
                    GapCode.UNKNOWN,
                    _FIELDS_AT,
                    f"claim text is indexed through transaction {corpus.claims_through}; the "
                    f"snapshot reads Memory at {memory_as_of}",
                )
            )
        unread = sorted({s.record_id for s in corpus.skipped if s.field in fields})
        if unread:
            gaps.append(
                _gap(
                    GapCode.NOT_COVERED,
                    _FIELDS_AT,
                    f"{len(unread)} record(s) were not indexed (no usable provenance)",
                    tuple(unread[:MAX_GAP_REFS]),
                )
            )
        return gaps

    def _withheld(self, asked: SearchRequest) -> list[Gap]:
        corpus = self._corpus
        only = corpus.index.search(
            corpus.tenant,
            SearchRequest(asked.text, asked.fields, asked.as_of, Inference.ONLY, asked.limit),
        )
        refs: set[str] = set()
        for match in only.matches:
            if (passage := corpus.passage(match.key)) is not None:
                refs.add(passage.document)
            elif (ref := corpus.claim(match.key)) is not None:
                refs.add(ref.claim)
        if not refs:
            return []
        shown = tuple(sorted(refs)[:MAX_GAP_REFS])
        return [
            _gap(
                GapCode.INFERRED_WITHHELD,
                _INFERRED_AT,
                f"{len(refs)} inferred match(es) withheld: the query excludes inference",
                shown,
            )
        ]

    def _claims(
        self,
        wanted: dict[tuple[NodeRef, str], list[tuple[float, ClaimId]]],
        snapshot: Snapshot,
        include_inferred: bool,
    ) -> tuple[list[tuple[float, Item]], list[Gap], list[ResolutionFinding]]:
        hits: list[tuple[float, Item]] = []
        gaps: list[Gap] = []
        findings: list[ResolutionFinding] = []
        beyond: dict[str, str] = {}
        for (subject, predicate), candidates in sorted(
            wanted.items(), key=lambda kv: (str(kv[0][0].node_type), kv[0][0].node_id, kv[0][1])
        ):
            try:
                read = self._memory.claims(
                    subject, predicate, snapshot.memory_as_of, include_inferred=include_inferred
                )
            except AsOfBeyondHeadError:
                gaps.append(
                    _gap(
                        GapCode.UNKNOWN,
                        _FIELDS_AT,
                        "Memory does not know the snapshot's transaction",
                        tuple(sorted({c for _, c in candidates})[:MAX_GAP_REFS]),
                    )
                )
                continue
            by_id = {c.id: c for c in read.claims}
            findings.extend(f for f in read.findings if finding_beyond_pin(f) is None)
            for score, claim_id in candidates:
                claim = by_id.get(claim_id)
                if claim is None:
                    continue
                if (reason := claim_beyond_pin(claim)) is not None:
                    beyond[claim.id] = reason
                    continue
                hits.append((score, ClaimItem.of(claim, _placeholder(score))))
        if beyond:
            gaps.append(
                _gap(
                    GapCode.NOT_COVERED,
                    _TEXT_AT,
                    "matching claims use values beyond the pinned graph-schema",
                    tuple(sorted(beyond)[:MAX_GAP_REFS]),
                )
            )
        return hits, gaps, findings


def _gap(code: GapCode, at: str, detail: str, refs: tuple[str, ...] = ()) -> Gap:
    return Gap(code, at, Channel.LEXICAL, refs, detail)
