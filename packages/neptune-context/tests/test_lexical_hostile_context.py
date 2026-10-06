"""Hostile text cannot make the lexical channel use unbounded memory or time (ADR 0008).

Each repro is a case a reviewer measured before the bounds existed: one quoted phrase of ~990 terms
over a 20k-term document (about 2 GB and 1.7 s), and a 4M-character ``a-a-...`` claim (about
300 MB). The bounds below are generous multiples of what the fixed code uses.
"""

from __future__ import annotations

import dataclasses
import time
import tracemalloc
from typing import TYPE_CHECKING, Any

from neptune_memory.schema.interval import ledger_tx

from lexical_fixtures_context import ARM, claim, passage, record, retrieval, world
from neptune.model.knowledge import AssertionKind
from neptune_context.packets.model import DocumentSpanItem, GapCode
from neptune_context.query.model import TextField
from neptune_context.retrieve.analysis import (
    ENGLISH,
    MAX_COMPOUND_PARTS,
    MAX_PHRASE_TERMS,
    MAX_QUERY_CHARS,
    MAX_TEXT_CHARS,
    SEPARATOR,
    Mode,
)
from neptune_context.retrieve.bm25 import (
    MAX_UNIT_CHARS,
    Bm25Index,
    IndexedText,
    IndexFindingCode,
    SearchRequest,
    TextSource,
)
from neptune_context.retrieve.lexical import LexicalChannel, LexicalCorpus, Skipped

R, D, CT, ID = TextField.RECORD, TextField.DOCUMENT, TextField.CLAIM_TEXT, TextField.DECLARED_ID
LED = TextSource.LEDGER
if TYPE_CHECKING:
    from collections.abc import Callable

MB = 1024 * 1024


def measured(run: Callable[[], Any]) -> tuple[Any, float, float]:
    """``run()``, its peak traced memory in MB and its seconds."""
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = run()
        seconds = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] / MB
    finally:
        tracemalloc.stop()
    return result, peak, seconds


def ask(text: str, fields: frozenset[TextField] = frozenset({R})) -> SearchRequest:
    return SearchRequest(text, fields, {LED: 10})


def test_a_990_term_phrase_over_a_20k_term_document_is_bounded() -> None:
    index = Bm25Index()
    body = " ".join(f"w{i % 1000}" for i in range(20_000))
    index.add("t", [IndexedText("big", R, LED, body)])
    phrase = '"' + " ".join(f"w{i}" for i in range(990)) + '"'
    result, peak, seconds = measured(lambda: index.search("t", ask(phrase)))
    assert peak < 64 and seconds < 5, (peak, seconds)
    assert result.truncated  # the phrase was cut to MAX_PHRASE_TERMS and the result says so
    assert [m.key for m in result.matches] == ["big"]  # its first 32 terms do occur, in order


def test_a_phrase_is_cut_at_the_bound_and_a_shorter_one_is_not() -> None:
    cut, flag = ENGLISH.query('"' + " ".join(f"w{i}" for i in range(40)) + '"', Mode.VERBATIM)
    assert flag and len(cut[0].terms) == MAX_PHRASE_TERMS and cut[0].required
    exact, flag = ENGLISH.query(
        '"' + " ".join(f"w{i}" for i in range(MAX_PHRASE_TERMS)) + '"', Mode.VERBATIM
    )
    assert not flag and len(exact[0].terms) == MAX_PHRASE_TERMS


def test_the_channel_reports_a_cut_phrase_as_a_gap() -> None:
    w = world()
    phrase = '"' + " ".join(f"lock out {i}" for i in range(20)) + '"'
    reply = w.channel.retrieve(retrieval(phrase[:1990] + '"'))
    assert any(g.code is GapCode.NOT_COVERED and g.at == "/text/text" for g in reply.gaps)


def test_a_4m_character_compound_claim_is_capped_and_named() -> None:
    w = world()
    hostile = claim(ARM, "maintenance_state", "a-" * 2_000_000, tx=5, label="hostile")
    corpus = LexicalCorpus()

    def build() -> Any:
        return corpus.add_claims((hostile,), through=ledger_tx(5))

    findings, peak, seconds = measured(build)
    assert peak < 64 and seconds < 5, (peak, seconds)
    assert [f.code for f in findings] == [IndexFindingCode.TRUNCATED]
    reply = LexicalChannel(corpus, w.reader).retrieve(retrieval("a", fields=frozenset({CT})))
    refs = [g.refs for g in reply.gaps if "in part" in g.detail]
    assert refs == [(hostile.id,)]


def test_analysis_itself_is_bounded_by_text_length_and_compound_size() -> None:
    text = "a-" * 2_000_000
    tokens, peak, seconds = measured(lambda: ENGLISH.tokens(text, Mode.VERBATIM, 1000))
    assert len(tokens) == 1000 and peak < 16 and seconds < 5, (peak, seconds)
    capped = ENGLISH.tokens("w " * (MAX_TEXT_CHARS), Mode.VERBATIM)
    assert len(capped) <= MAX_TEXT_CHARS // 2 + 1
    chain = ENGLISH.tokens("-".join(["p"] * 100), Mode.VERBATIM)
    assert sum(t.start for t in chain) == -(-100 // MAX_COMPOUND_PARTS)  # split into compounds


def test_a_unit_longer_than_the_cap_is_cut_and_reported() -> None:
    index = Bm25Index()
    body = "alpha " * 10 + "x" * MAX_UNIT_CHARS + " omega"
    (finding,) = index.add("t", [IndexedText("long", R, LED, body)])
    assert finding.code is IndexFindingCode.TRUNCATED
    assert [m.key for m in index.search("t", ask("alpha")).matches] == ["long"]
    assert index.search("t", ask("omega")).matches == ()  # past the cut


def test_a_query_longer_than_the_cap_is_cut_and_reported() -> None:
    index = Bm25Index()
    index.add("t", [IndexedText("a", R, LED, "needle")])
    text = "x " * MAX_QUERY_CHARS + "needle"
    result = index.search("t", ask(text))
    assert result.truncated and result.matches == ()  # "needle" lies beyond MAX_QUERY_CHARS


def test_a_full_partition_refuses_new_units_and_the_channel_names_them() -> None:
    index = Bm25Index(max_units=2)
    corpus = LexicalCorpus(index)
    w = world()
    first = passage(D, "p1", "quench valve one")
    second = passage(D, "p2", "quench valve two")
    third = passage(D, "p3", "quench valve three")
    findings = corpus.add_passages((first, second, third), through=4)
    assert [(f.code, f.key) for f in findings] == [(IndexFindingCode.INDEX_FULL, third.key)]
    assert corpus.passage(third.key) is None and corpus.passage(first.key) is first
    reply = LexicalChannel(corpus, w.reader).retrieve(retrieval("quench", fields=frozenset({D})))
    spans = {i.document for i in reply.hits if isinstance(i, DocumentSpanItem)}
    assert spans == {first.document, second.document}
    (gap,) = reply.gaps
    assert gap.refs == (third.document,) and "index is full" in gap.detail


def test_a_token_cap_also_fills_a_partition() -> None:
    index = Bm25Index(max_tokens=5)
    findings = index.add(
        "t",
        [IndexedText("a", R, LED, "one two three four five six"), IndexedText("b", R, LED, "x")],
    )
    assert [(f.code, f.key) for f in findings] == [(IndexFindingCode.INDEX_FULL, "b")]
    assert index.size("t") == 1


def test_declared_id_phrases_do_not_match_across_the_subject_and_object() -> None:
    index = Bm25Index()
    index.add(
        "t", [IndexedText("edge", ID, TextSource.MEMORY, f"asset:alpha-1{SEPARATOR}site:beta-2")]
    )

    def keys(text: str) -> list[str]:
        request = SearchRequest(text, frozenset({ID}), {TextSource.MEMORY: 10})
        return [m.key for m in index.search("t", request).matches]

    assert keys('"alpha 1 site"') == [] and keys('"1 site"') == []
    assert keys("asset:alpha-1") == ["edge"] and keys("site:beta-2") == ["edge"]


def test_the_separator_leaves_one_position_unused() -> None:
    tokens = ENGLISH.tokens(f"a{SEPARATOR}b", Mode.VERBATIM)
    assert [(t.term, t.position) for t in tokens] == [("a", 0), ("b", 2)]


def test_settings_name_the_unicode_database_and_every_bound() -> None:
    import unicodedata

    settings = Bm25Index().settings("t")
    assert settings["unicode"] == unicodedata.unidata_version
    for key in ("max_phrase_terms", "max_query_chars", "max_text_chars", "max_partition_units"):
        assert isinstance(settings[key], int)


def _changed(corpus: LexicalCorpus, **changes: Any) -> str:
    """The digest after changing one claim unit's facts (the first that was superseded)."""
    for key, ref in sorted(corpus._claims.items()):
        if ref.superseded_at is not None:
            corpus._claims[key] = dataclasses.replace(ref, **changes)
            corpus._digest = None
            return corpus.digest()
    raise AssertionError("the fixture has a superseded claim")


def test_the_corpus_digest_covers_everything_that_changes_an_answer() -> None:
    digest = world().corpus.digest()
    assert digest == world().corpus.digest()
    assert _changed(world().corpus, superseded_at=ledger_tx(9)) != digest
    assert _changed(world().corpus, recorded_at=ledger_tx(0)) != digest
    assert _changed(world().corpus, inferred=True) != digest
    assert _changed(world().corpus, predicate="has_summary") != digest

    def with_passage(registered_at: int, kind: Any = AssertionKind.STATED) -> str:
        w = world(with_passages=False)
        w.corpus.add_passages(
            (passage(D, "sop-x", "close the valve", registered_at=registered_at, kind=kind),),
            through=4,
        )
        return w.corpus.digest()

    assert with_passage(1) == with_passage(1)
    assert with_passage(1) != with_passage(2)  # registered_at
    assert with_passage(1) != with_passage(1, kind="inferred")  # assertion kind, model, confidence
    skipped = world()
    skipped.corpus.skipped.append(Skipped(record("r"), D, "no anchor"))
    skipped.corpus._digest = None
    assert skipped.corpus.digest() != digest
