"""Exact value, id and phrase search over the local graph (ADR 0021).

``python -m neptune_context.mcp --memory`` runs the lexical channel beside the graph channel, so
a ``neptune_query`` text clause finds the claims that state a value as written ("151.5", "1.86",
"WO-26-0911", "TCP z", "reprojection error"), cited to the row, field or step they come from, and
``neptune_entities`` with such a text lists the subjects whose claims state it. Over Memory's live
snapshot of the acceptance corpus, through the MCP tools. Literal values are indexed as the agent
renderer writes them; hostile query text is bounded and deterministic.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Final

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session as connect
from neptune_memory.schema.claim import DeclaredType, DeclaredValue, TypedLiteral, ValueType

import retrieve_fixtures_context as F
from neptune.model.knowledge import Known, NotApplicable, Unknown
from neptune.model.scalars import NonFinite
from neptune.model.units import unit_from_json
from neptune_context.engine import read_graph_document
from neptune_context.mcp import build_server
from neptune_context.mcp.__main__ import local_client
from neptune_context.mcp.server import text_search
from neptune_context.query.model import MAX_TEXT_CHARS
from neptune_context.render.agent import parse_answer
from neptune_context.retrieve.lexical import _literal_text
from neptune_context.sources import read_source_index

# What each search must cite, by claim id prefix (Memory's live snapshot).
TCP_Z_ACTION: Final = "claim:sha256:fbafdf900a24"  # WO-26-0911 action: TCP z 145.5 -> 151.5 mm
REPROJECTION: Final = "claim:sha256:ff33253fd282"  # the incident run's reprojection_error [1.86]
REV_B_LIMIT: Final = "claim:sha256:8807c15ed18e"  # SOP-CELL-021 rev B step 3: at most 0.8 px
REV_C_LIMIT: Final = "claim:sha256:4a8cac6fb821"  # SOP-CELL-021 rev C step 3: at most 2.0 px
NO_INDEX: Final = "this engine has no text index"


def _arguments(text: str, *, items: int = 20) -> dict[str, Any]:
    return {
        "include_inferred": False,
        "query": {
            "budget": {"items": items, "tokens": 20000},
            "text": {
                "text": text,
                "fields": ["claim_text", "declared_id"],
                "channels": ["lexical"],
            },
        },
    }


async def _calls(calls: list[tuple[str, dict[str, Any]]]) -> list[tuple[bool, str]]:
    document = read_graph_document(F.MEMORY_SNAPSHOT)
    sources = read_source_index(F.ROOT / "harness" / "acceptance" / "sources.json")
    out = []
    async with connect(build_server(local_client(document), sources=sources)) as session:
        for tool, arguments in calls:
            result = await session.call_tool(tool, arguments)
            block = result.content[0]
            assert isinstance(block, types.TextContent)
            out.append((bool(result.isError), block.text))
    return out


def _answers(*texts: str) -> list[str]:
    results = asyncio.run(_calls([("neptune_query", _arguments(t)) for t in texts]))
    for is_error, text in results:
        assert not is_error, text
    return [text for _, text in results]


def _cited(text: str) -> set[str]:
    return {
        key.claim_id
        for statement in parse_answer(text).statements
        if statement.evidence
        for key in statement.items
        if key.claim_id is not None
    }


def _cites(text: str, prefix: str) -> bool:
    return any(c.startswith(prefix) for c in _cited(text))


@pytest.fixture(scope="module")
def found() -> dict[str, str]:
    texts = ['"151.5"', "151.5", "TCP z", "1.86", '"reprojection error"', "WO-26-0911"]
    return dict(zip(texts, _answers(*texts), strict=True))


def test_a_number_finds_the_work_order_row_that_states_it(found: dict[str, str]) -> None:
    for text in ('"151.5"', "151.5", "TCP z"):
        answer = found[text]
        assert _cites(answer, TCP_Z_ACTION), text
        assert '"Set TCP z 145.5 -\\u003e 151.5 mm on pendant (tool1)"' in answer
        assert _placed(answer, '"sites/PLANT-2/cmms/work_orders.csv", row 7')
        assert NO_INDEX not in answer
    # Exact: the one claim that states 151.5, nothing that merely shares a word.
    assert _cited(found['"151.5"']) == _cited(found["151.5"])
    assert len(_cited(found["151.5"])) == 1


def test_a_declared_number_and_the_procedure_limits_are_found(found: dict[str, str]) -> None:
    assert _cites(found["1.86"], REPROJECTION)
    phrase = found['"reprojection error"']
    for prefix in (REPROJECTION, REV_B_LIMIT, REV_C_LIMIT):
        assert _cites(phrase, prefix), prefix
    assert "at most 2.0 px" in phrase and "at most 0.8 px" in phrase


def test_a_work_order_number_finds_its_name(found: dict[str, str]) -> None:
    answer = found["WO-26-0911"]
    assert 'has_name "WO-26-0911"' in answer
    assert _placed(answer, '"sites/PLANT-2/cmms/work_orders.csv", row 7')


def _placed(answer: str, where: str) -> bool:
    """Whether a cited source is at ``where`` (the Sources footer's file and place, ADR 0020)."""
    return any(p.startswith(where) for p in parse_answer(answer).places)


def test_numbers_match_as_written_and_never_as_a_neighbour() -> None:
    """``151.50`` is not ``151.5`` (ADR 0021 §3): no trailing-zero or rounding equivalence, and
    a number is never found inside a longer one (``51.5`` in ``151.5``)."""
    for text in ("151.50", "51.5", "151"):
        (answer,) = _answers(text)
        assert not _cites(answer, TCP_Z_ACTION), text


def test_case_and_unicode_width_do_not_matter() -> None:
    lower, wide = _answers(
        "tcp Z 151.5", "\uff34\uff23\uff30 \uff5a \uff11\uff15\uff11\uff0e\uff15"
    )
    upper = _answers("TCP z 151.5")[0]
    assert _cited(lower) == _cited(upper) == _cited(wide)
    assert _cites(upper, TCP_Z_ACTION)


def test_hostile_text_is_bounded_and_deterministic() -> None:
    hostile = [
        "9" * MAX_TEXT_CHARS,
        ".*[(?<=a)]+$\\d{1,99}|^",
        " ".join(["151.5"] * 300)[:MAX_TEXT_CHARS],
        '"' + "a " * 900 + '"',
        "​‮151.5﻿",
        "x" * 129 + "-1",
    ]
    first = _answers(*hostile)
    again = _answers(*hostile)
    assert first == again
    for text in first:
        assert text.startswith("Context packet"), text[:200]
        assert NO_INDEX not in text


def test_the_same_question_gives_the_same_bytes() -> None:
    a = asyncio.run(_calls([("neptune_query", _arguments("TCP z"))]))
    b = asyncio.run(_calls([("neptune_query", _arguments("TCP z"))]))
    assert a == b


def test_entities_lists_the_subjects_that_state_a_value() -> None:
    calls = [
        ("neptune_entities", {"include_inferred": False, "text": "151.5"}),
        ("neptune_entities", {"include_inferred": False, "text": "why did INC-C3-0011 happen"}),
        ("neptune_entities", {"include_inferred": False, "text": "151.5", "kind": "machine"}),
    ]
    (err1, value), (err2, question), (err3, machines) = asyncio.run(_calls(calls))
    assert not (err1 or err2 or err3)
    assert "Stated in: claims whose value or ids hold this text as written." in value
    assert '/actions/2" has_description' in value
    assert json.dumps({"text": text_search("151.5")}) in value
    # A question that states no value reads as before: names only.
    assert "Stated in" not in question
    # ``kind`` narrows the subjects too: no machine states 151.5.
    assert "Stated in" not in machines


def test_text_search_is_one_quoted_phrase_within_the_text_limit() -> None:
    assert text_search('  TCP   "z" ') == {
        "text": '"TCP z"',
        "fields": ["claim_text", "declared_id"],
        "channels": ["lexical"],
    }
    assert len(text_search("y" * MAX_TEXT_CHARS)["text"]) == MAX_TEXT_CHARS


@pytest.mark.parametrize(
    ("literal", "words"),
    [
        (TypedLiteral(ValueType.TEXT, "Set TCP z"), "Set TCP z"),
        (TypedLiteral(ValueType.TEXT, ""), None),
        (TypedLiteral(ValueType.INTEGER, 30), "30"),
        (TypedLiteral(ValueType.REAL, 151.5), "151.5"),
        (TypedLiteral(ValueType.REAL, 2.0), "2.0"),
        (TypedLiteral(ValueType.REAL, 1e-05), "1e-05 0.00001"),
        (TypedLiteral(ValueType.REAL, 1e300), "1e+300"),  # no 301-digit twin
        (TypedLiteral(ValueType.REAL, NonFinite.POSITIVE_INFINITY), "inf"),
        (TypedLiteral(ValueType.BOOLEAN, False), "false"),
        (TypedLiteral(ValueType.QUANTITY, 145.5, Known(unit_from_json("mm"))), "145.5 mm"),
        (TypedLiteral(ValueType.QUANTITY, 3, Unknown()), "3"),
        (
            TypedLiteral(
                ValueType.DECLARED_VALUE,
                DeclaredValue(("reprojection_error",), DeclaredType.REALS, (1.86, NonFinite.NAN)),
                Unknown(),
            ),
            "reprojection_error 1.86 nan",
        ),
        (
            TypedLiteral(
                ValueType.DECLARED_VALUE,
                DeclaredValue(("parameters", 2, "on"), DeclaredType.BOOLEAN, True),
                NotApplicable(),
            ),
            "parameters 2 on true",
        ),
    ],
)
def test_literals_are_found_by_what_the_answer_writes(
    literal: TypedLiteral, words: str | None
) -> None:
    assert _literal_text(literal) == words


@pytest.mark.parametrize(
    ("text", "terms"),
    [
        ("151.5", ["151.5"]),
        ("TCP z 145.5 -> 151.5 mm", ["tcp", "z", "145.5", "151.5", "mm"]),
        ("-0.0745", ["0.0745"]),  # a sign is punctuation
        ("\uff11\uff15\uff11\uff0e\uff15", ["151.5"]),  # NFKC: full-width digits and point
        ("2.4.1", ["2", "4", "1"]),  # a version keeps its parts
        ("1e-05", ["1e", "05"]),
        ("SN-1.86", ["sn", "1", "86"]),  # a number inside an identifier stays a part
        ("v2.4", ["v2", "4"]),
    ],
)
def test_a_decimal_number_is_one_term(text: str, terms: list[str]) -> None:
    from neptune_context.retrieve.analysis import ENGLISH, Mode

    assert [t.term for t in ENGLISH.tokens(text, Mode.PROSE)] == terms
