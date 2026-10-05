"""Passages come from the Ledger catalog's ``query`` rows and ``lineage`` transforms (ADR 0008 §5).

``CatalogFake`` answers the two calls through the catalog API's own Arrow builder, ``query_table``,
and honours ``kinds``, ``as_of``, ``limit`` and the ``after`` cursor. The Ledger's PostgreSQL
catalog is not importable from Context (ADR 0001), so this is the real contract over canned rows.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from neptune_ledger.api import (
    CatalogFinding,
    LineageGraph,
    LineageNode,
    QueryMeta,
    QueryRow,
    QuerySpec,
    TransactionKey,
    TransformInfo,
    query_table,
)

from lexical_fixtures_context import (
    CONFIG,
    digest,
    document,
    evidence,
    record,
    retrieval,
    world,
)
from neptune.identity.canonical_json import dumps
from neptune.model.knowledge import AssertionKind, Known, NotCovered
from neptune.model.provenance import Page, Span
from neptune_context.packets.model import DocumentSpanItem, GapCode
from neptune_context.query.model import TextField
from neptune_context.retrieve.lexical import (
    LexicalChannel,
    LexicalCorpus,
    passages_from_catalog,
)

TRANSFORM = "rec:sha256:" + "33" * 32
PACKAGE = "sha256:" + "44" * 32
ANCHOR = evidence("anchor", Page(1), Span(0, 40))


def txkey(point: int) -> Known[TransactionKey]:
    return Known(TransactionKey(point, "2026-10-06T00:00:00.000000Z"))


def row(label: str, seq: int = 1, **changes: Any) -> QueryRow:
    anchor = evidence(label, Page(1), Span(0, 40))
    base = QueryRow(
        kind="document_span",
        record_id=record(label),
        package_id=PACKAGE,
        line=1,
        registration_seq=seq,
        transform_id=TRANSFORM,
        source_content_id=anchor.source,  # type: ignore[arg-type]
        source_locator=dumps(anchor.locator_json()).decode(),
        assertion_kind="stated",
    )
    return dataclasses.replace(base, **changes)


class CatalogFake:
    """Just ``query`` and ``lineage``; the other calls are not exercised here."""

    def __init__(
        self, rows: list[QueryRow], *, transform: bool = True, findings: tuple[Any, ...] = ()
    ) -> None:
        self.rows = sorted(rows, key=lambda r: (r.kind, r.record_id, r.package_id))
        self.transform = transform
        self.findings = findings
        self.queries: list[QuerySpec] = []
        self.head = 10

    def query(self, spec: QuerySpec) -> Any:
        self.queries.append(spec)
        rows = [r for r in self.rows if r.kind in spec.kinds]
        if spec.as_of is not None:
            rows = [r for r in rows if r.registration_seq <= spec.as_of]
        if spec.after is not None:
            key = (spec.after.kind, spec.after.record_id, spec.after.package_id)
            rows = [r for r in rows if (r.kind, r.record_id, r.package_id) > key]
        rows = rows[: spec.limit]
        point = spec.as_of or self.head
        return query_table(rows, QueryMeta(as_of=txkey(point), findings=self.findings))

    def lineage(self, record_id: str, *, as_of: int | None = None) -> LineageGraph:
        info = TransformInfo("pdf-text", "1.4.2", CONFIG, {})
        node = LineageNode(TRANSFORM, Known(info) if self.transform else NotCovered())
        return LineageGraph(
            record_id=record_id,
            status="found",
            kind=Known("document_span"),
            transform_id=Known(TRANSFORM),
            registered_by=(),
            nodes=(node,),
            edges=(),
            siblings=(),
            as_of=txkey(self.head),
            findings=(),
        )


def texts(mapping: dict[str, str | None]) -> Any:
    return lambda r: mapping.get(r.record_id)


def test_rows_become_passages_with_the_catalogs_provenance() -> None:
    rows = [row("sop-a", seq=3), row("sop-b", seq=5)]
    catalog = CatalogFake(rows)
    batch = passages_from_catalog(
        catalog,  # type: ignore[arg-type]
        ["document_span"],
        texts({rows[0].record_id: "Isolate the cell", rows[1].record_id: "Verify zero energy"}),
        field=TextField.DOCUMENT,
    )
    assert batch.skipped == () and batch.findings == ()
    first, second = batch.passages
    assert (first.document, first.text, first.registered_at) == (
        rows[0].record_id,
        "Isolate the cell",
        3,
    )
    assert first.evidence == evidence("sop-a", Page(1), Span(0, 40))
    assert first.provenance.evidence == (first.evidence,) and first.provenance.records == (
        first.document,
    )
    transform = first.provenance.transform
    assert (transform.producer_id, transform.producer_version, transform.config_hash) == (
        "pdf-text",
        "1.4.2",
        CONFIG,
    )
    assert first.assertion_kind is AssertionKind.STATED and second.registered_at == 5


def test_records_without_text_are_not_passages_and_are_not_reported() -> None:
    rows = [row("with"), row("without")]
    batch = passages_from_catalog(
        CatalogFake(rows),  # type: ignore[arg-type]
        ["document_span"],
        texts({rows[0].record_id: "has text"}),
    )
    assert [p.document for p in batch.passages] == [rows[0].record_id] and batch.skipped == ()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"source_content_id": None}, "no evidence anchor"),
        ({"source_locator": None}, "no evidence anchor"),
        ({"assertion_kind": None}, "assertion kind is not stated"),
        ({"transform_id": None}, "names no transform"),
        ({"source_locator": "not json"}, "evidence anchor is unreadable"),
        ({"source_locator": '[{"kind": "no_such_step"}]'}, "evidence anchor is unreadable"),
        ({"record_id": "sha256:" + digest("source-artifact")}, "not a record id"),
    ],
)
def test_a_record_without_usable_provenance_is_skipped_and_named(
    change: dict[str, Any], reason: str
) -> None:
    bad = row("bad", **change)
    good = row("good")
    batch = passages_from_catalog(
        CatalogFake([bad, good]),  # type: ignore[arg-type]
        ["document_span"],
        lambda r: "some text",
    )
    assert [p.document for p in batch.passages] == [good.record_id]
    (skipped,) = batch.skipped
    assert skipped.record_id == bad.record_id and reason in skipped.reason


def test_a_transform_the_catalog_does_not_hold_skips_the_record() -> None:
    batch = passages_from_catalog(
        CatalogFake([row("a")], transform=False),  # type: ignore[arg-type]
        ["document_span"],
        lambda r: "text",
    )
    assert (
        batch.passages == () and "does not hold the record's transform" in batch.skipped[0].reason
    )


def test_blank_or_oversized_text_is_skipped_not_indexed() -> None:
    rows = [row("blank"), row("huge")]
    batch = passages_from_catalog(
        CatalogFake(rows),  # type: ignore[arg-type]
        ["document_span"],
        texts({rows[0].record_id: "   ", rows[1].record_id: "x" * 20_000}),
    )
    assert batch.passages == () and len(batch.skipped) == 2


def test_pagination_reads_every_row_in_catalog_order() -> None:
    rows = [row(f"r{i}", seq=i + 1) for i in range(7)]
    catalog = CatalogFake(rows)
    batch = passages_from_catalog(catalog, ["document_span"], lambda r: f"text {r.line}", page=3)  # type: ignore[arg-type]
    assert [p.document for p in batch.passages] == [r.record_id for r in catalog.rows]
    assert [q.limit for q in catalog.queries] == [3, 3, 3] and catalog.queries[0].after is None


def test_as_of_and_kinds_are_passed_to_the_catalog() -> None:
    rows = [row("early", seq=2), row("late", seq=8), row("other", kind="note")]
    catalog = CatalogFake(rows)
    batch = passages_from_catalog(catalog, ["document_span"], lambda r: "text", as_of=5)  # type: ignore[arg-type]
    assert [p.registered_at for p in batch.passages] == [2]
    assert catalog.queries[0].kinds == ("document_span",) and catalog.queries[0].as_of == 5


def test_catalog_findings_are_returned_not_swallowed() -> None:
    finding = CatalogFinding("budget_exceeded", "query", "rows cut")
    batch = passages_from_catalog(
        CatalogFake([row("a")], findings=(finding,)),  # type: ignore[arg-type]
        ["document_span"],
        lambda r: "text",
    )
    assert batch.findings == (finding,) and len(batch.passages) == 1


def test_an_empty_catalog_gives_an_empty_batch() -> None:
    batch = passages_from_catalog(CatalogFake([]), ["document_span"], lambda r: "x")  # type: ignore[arg-type]
    assert (batch.passages, batch.skipped, batch.findings) == ((), (), ())


def test_batches_are_deterministic() -> None:
    rows = [row(f"r{i}") for i in range(4)]

    def build() -> Any:
        return passages_from_catalog(
            CatalogFake(rows),  # type: ignore[arg-type]
            ["document_span"],
            lambda r: f"text {r.record_id[-3:]}",
        )

    assert build() == build()


def test_catalog_passages_are_searchable_and_skipped_records_become_a_gap() -> None:
    w = world(with_passages=False)
    good, bad = row("sop-quench", seq=2), row("sop-orphan", assertion_kind=None)
    batch = passages_from_catalog(
        CatalogFake([good, bad]),  # type: ignore[arg-type]
        ["document_span"],
        lambda r: "Close the quench valve before the arm is parked",
        field=TextField.DOCUMENT,
    )
    corpus = LexicalCorpus()
    corpus.add_claims(w.history, through=document().head)
    assert corpus.add_batch(batch) == ()
    reply = LexicalChannel(corpus, w.reader).retrieve(
        retrieval('"quench valve"', fields=frozenset({TextField.DOCUMENT}))
    )
    (item,) = reply.hits
    assert isinstance(item, DocumentSpanItem) and item.document == good.record_id
    assert item.provenance.transform.producer_id == "pdf-text"
    (gap,) = reply.gaps
    assert (gap.code, gap.refs) == (GapCode.NOT_COVERED, (bad.record_id,))
