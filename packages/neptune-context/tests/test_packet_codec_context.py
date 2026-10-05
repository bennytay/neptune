"""Reading packet documents: hostile input becomes one structured finding, never an exception."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from context_packet_helpers import golden, golden_path
from neptune.identity.canonical_json import dumps
from neptune_context.packets import codec
from neptune_context.packets.codec import canonical_bytes, decode, from_json
from neptune_context.packets.findings import PacketFindingCode, PacketRefused
from neptune_context.packets.model import ContextPacket

if TYPE_CHECKING:
    from collections.abc import Iterator

Code = PacketFindingCode


def document(stem: str = "q09") -> dict[str, Any]:
    value: dict[str, Any] = json.loads(golden_path(stem).read_bytes())
    return value


def refused(value: Any) -> tuple[PacketFindingCode, str]:
    result = from_json(value) if not isinstance(value, bytes | str) else decode(value)
    assert isinstance(result, PacketRefused), "expected a refusal"
    (finding,) = result.findings
    assert finding.message
    return finding.code, finding.at


def test_pretty_and_canonical_forms_decode_to_the_same_packet() -> None:
    pretty = golden_path("q01").read_bytes()
    canonical = canonical_bytes(golden("q01"))
    assert pretty != canonical
    assert decode(pretty) == decode(canonical) == decode(canonical.decode("utf-8"))


@pytest.mark.parametrize(
    "text",
    [b"", b"{", b"[1, 2", b"\xff\xfe", b'{"a": NaN}', b'{"a": Infinity}', b'{"a": 1, "a": 2}'],
)
def test_text_that_is_not_one_json_document_is_a_syntax_finding(text: bytes) -> None:
    assert refused(text) == (Code.SYNTAX, "")


def test_a_truncated_packet_is_a_syntax_finding() -> None:
    data = golden_path("q01").read_bytes()
    assert refused(data[: len(data) // 2])[0] is Code.SYNTAX


def test_deep_nesting_is_refused_not_crashed() -> None:
    assert refused(b"[" * 100_000 + b"]" * 100_000)[0] in {Code.SYNTAX, Code.SHAPE}


def test_an_oversized_document_is_refused_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codec, "MAX_PACKET_BYTES", 100)
    assert refused(b" " * 101) == (Code.TOO_LARGE, "")
    assert refused(b" " * 100)[0] is Code.SYNTAX  # at the limit it is read (and is not JSON)


@pytest.mark.parametrize("version", [0, 2, "1", True, 1.0])
def test_another_packet_version_is_refused(version: Any) -> None:
    doc = document()
    doc["packet_version"] = version
    assert refused(doc) == (Code.UNSUPPORTED_VERSION, "/packet_version")


def test_the_root_must_be_a_packet_object() -> None:
    assert refused([])[0] is Code.SHAPE
    doc = document()
    doc["kind"] = "query"
    assert refused(doc) == (Code.SHAPE, "/kind")


@pytest.mark.parametrize(
    ("path", "at"),
    [
        (("header", "budget"), "/header"),
        (("items", 0, "relevance"), "/items/0"),
        (("header", "memory_snapshot", "generation"), "/header/memory_snapshot"),
    ],
)
def test_a_missing_member_is_a_shape_finding_where_it_is_missing(
    path: tuple[Any, ...], at: str
) -> None:
    doc = document()
    target = doc
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    assert refused(doc) == (Code.SHAPE, at)


def test_an_extra_member_is_refused() -> None:
    doc = document()
    doc["items"][0]["summary"] = "the robot is fine"
    assert refused(doc) == (Code.SHAPE, "/items/0")
    doc = document()
    doc["header"]["now"] = 1790000000
    assert refused(doc) == (Code.SHAPE, "/header")


def test_an_unknown_item_kind_is_refused() -> None:
    doc = document()
    doc["items"][0]["kind"] = "summary"
    assert refused(doc) == (Code.SHAPE, "/items/0/kind")


def test_an_integer_score_is_not_a_float() -> None:
    doc = document()
    doc["items"][0]["relevance"]["hits"][0]["score"] = 1
    assert refused(doc) == (Code.SHAPE, "/items/0/relevance/hits/0/score")


def test_an_unknown_channel_is_a_bad_value_at_its_pointer() -> None:
    doc = document()
    doc["items"][0]["relevance"]["hits"][0]["channel"] = "oracle"
    assert refused(doc) == (Code.BAD_VALUE, "/items/0/relevance/hits/0/channel")


def test_a_tampered_packet_id_is_refused() -> None:
    doc = document()
    doc["id"] = "packet:sha256:" + "0" * 64
    assert refused(doc) == (Code.ID_MISMATCH, "/id")


def test_a_tampered_item_id_is_refused() -> None:
    doc = document()
    doc["items"][1]["id"] = "item:sha256:" + "0" * 64
    assert refused(doc) == (Code.ID_MISMATCH, "/items/1/id")


def test_reranking_without_a_new_packet_id_is_refused() -> None:
    doc = document("q01")
    old = repr(doc["items"][0]["relevance"]["score"])
    digits = old.rstrip("9")
    new = float(digits[:-1] + str(int(digits[-1]) + 1) + "0" * (len(old) - len(digits)))
    # Higher (so the order holds) and as long (so the byte budget still matches).
    assert new > float(old) and len(repr(new)) == len(old)
    doc["items"][0]["relevance"]["score"] = new
    assert refused(doc) == (Code.ID_MISMATCH, "/id")


def test_rewriting_a_claim_breaks_memorys_claim_id() -> None:
    doc = document("q01")
    (index,) = [i for i, item in enumerate(doc["items"]) if item["kind"] == "claim"][:1]
    doc["items"][index]["claim"]["predicate"] = "operated_by"
    assert refused(doc) == (Code.SHAPE, f"/items/{index}/claim")


def test_claiming_inference_was_excluded_when_it_was_not_is_refused() -> None:
    doc = document("q03")
    doc["header"]["inference_included"] = False
    assert refused(doc) == (Code.INFERENCE_EXCLUDED, "")


def test_relabelling_an_inferred_claim_as_stated_is_refused() -> None:
    doc = document("q03")
    doc["items"][0]["assertion_kind"] = "stated"
    assert refused(doc) == (Code.ASSERTION_MISMATCH, "/items/0")


def test_an_understated_budget_is_refused() -> None:
    doc = document("q08")
    doc["header"]["budget"]["used"]["tokens"] -= 1
    assert refused(doc) == (Code.BUDGET, "")


def test_a_knowledge_field_with_its_own_provenance_is_refused() -> None:
    doc = document("q10")
    (index,) = [i for i, item in enumerate(doc["items"]) if item["kind"] == "document_span"]
    doc["items"][index]["text"]["provenance"] = doc["items"][index]["provenance"]
    assert refused(doc) == (Code.SHAPE, f"/items/{index}/text")


def _paths(value: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    yield path
    if isinstance(value, dict):
        for key, member in value.items():
            yield from _paths(member, (*path, key))
    elif isinstance(value, list):
        for index, member in enumerate(value):
            yield from _paths(member, (*path, index))


def _replace(doc: Any, path: tuple[Any, ...], new: Any) -> Any:
    doc = copy.deepcopy(doc)
    if not path:
        return new
    target = doc
    for key in path[:-1]:
        target = target[key]
    if new is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = new
    return doc


_DELETE = object()


@pytest.mark.parametrize("stem", ["q06", "q08"])
def test_no_single_mutation_makes_the_reader_raise_or_accept_silently(stem: str) -> None:
    # Every member of the document, replaced or removed: the reader returns a packet (only when
    # the mutation is meaning-preserving, which none of these are) or one finding; never raises.
    doc = document(stem)
    original = decode(dumps(doc))
    assert isinstance(original, ContextPacket)
    for path in _paths(doc):
        for new in (_DELETE, None, "x", -1, [], {}):
            if new is _DELETE and (not path or isinstance(path[-1], int)):
                continue
            result = from_json(_replace(doc, path, new))
            assert isinstance(result, PacketRefused | ContextPacket)
            if isinstance(result, ContextPacket):
                assert result == original, f"{path} -> {new!r} changed the packet silently"


def test_a_str_document_that_is_not_valid_unicode_is_a_syntax_finding() -> None:
    assert refused('"\ud800"') == (Code.SYNTAX, "")


def test_a_lone_surrogate_in_a_frame_encoding_is_refused_not_raised() -> None:
    doc = document("q08")
    (index,) = [i for i, item in enumerate(doc["items"]) if item["kind"] == "frame"]
    doc["items"][index]["encoding"]["value"] = "\ud800"
    text = json.dumps(doc)  # ensure_ascii writes the escape, as a hostile producer would
    assert refused(text) == (Code.BAD_VALUE, f"/items/{index}")


def test_a_lone_surrogate_in_a_gap_is_refused_not_raised() -> None:
    doc = document("q04")
    doc["gaps"][0]["refs"] = ["\ud800"]
    assert refused(json.dumps(doc)) == (Code.BAD_VALUE, "/gaps/0")
    doc = document("q04")
    doc["gaps"][0]["at"] = "/\ud800"
    assert refused(json.dumps(doc)) == (Code.BAD_VALUE, "/gaps/0")
