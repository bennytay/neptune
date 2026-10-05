"""Entity resolution: whole-token, case-insensitive, longest first; ambiguity is not settled."""

from __future__ import annotations

import pytest

from neptune_context.query.plan import DeclaredIdentifierIndex, Entity, EntityResolver


def idx(*entities: Entity) -> DeclaredIdentifierIndex:
    return DeclaredIdentifierIndex(entities)


AMR7 = Entity("machine", "asset_tag:AMR-07", aliases=("the north tug",))
AMR70 = Entity("machine", "asset_tag:AMR-070")


def test_the_index_is_an_entity_resolver() -> None:
    assert isinstance(idx(AMR7), EntityResolver)


def test_names_match_whole_tokens_ignoring_case() -> None:
    index = idx(AMR7)
    (mention,) = index.find("which runs does amr-07 have?", as_of=None)
    assert mention.text == "amr-07" and mention.candidates == (AMR7,) and not mention.ambiguous
    assert index.find("AMR-077 and xAMR-07 and AMR-07x", as_of=None) == ()
    assert index.find("", as_of=None) == ()


def test_aliases_and_labels_resolve() -> None:
    (mention,) = idx(AMR7).find("Where is The North Tug?", as_of=None)
    assert mention.candidates == (AMR7,)


def test_the_longest_name_wins_and_its_span_is_consumed() -> None:
    mentions = idx(AMR7, AMR70).find("AMR-070 then AMR-07", as_of=None)
    assert [(m.text, m.candidates[0].declared_id) for m in mentions] == [
        ("AMR-070", "asset_tag:AMR-070"),
        ("AMR-07", "asset_tag:AMR-07"),
    ]


def test_two_entities_with_one_name_are_one_ambiguous_mention_in_a_stable_order() -> None:
    a = Entity("machine", "asset_tag:AMR-09")
    b = Entity("asset", "cmms_asset:AMR-09")
    for order in ((a, b), (b, a)):
        (mention,) = idx(*order).find("AMR-09 AMR-09", as_of=None)
        assert mention.ambiguous
        assert [c.declared_id for c in mention.candidates] == [
            "cmms_asset:AMR-09",
            "asset_tag:AMR-09",
        ]


def test_mentions_come_in_order_of_appearance() -> None:
    b = Entity("machine", "asset_tag:b-1")
    a = Entity("machine", "asset_tag:a-1")
    assert [m.text for m in idx(a, b).find("b-1 before a-1", as_of=None)] == ["b-1", "a-1"]


def test_lookup_is_exact() -> None:
    index = idx(AMR7)
    assert index.lookup("asset_tag:AMR-07", as_of=None) == AMR7
    assert index.lookup("asset_tag:amr-07", as_of=None) is None


def test_a_declared_id_declared_twice_is_refused() -> None:
    with pytest.raises(ValueError, match="twice"):
        idx(AMR7, AMR7)


def test_regex_metacharacters_in_names_are_literal() -> None:
    odd = Entity("person", "person:j.alvarez", aliases=("J. Alvarez (night)",))
    assert idx(odd).find("ask J. Alvarez (night) and jXalvarez", as_of=None)[0].candidates == (odd,)
    assert len(idx(odd).find("jXalvarez", as_of=None)) == 0


def test_a_value_and_label_differing_only_in_case_are_one_candidate() -> None:
    entity = Entity("machine", "asset_tag:amr-07", label="AMR-07")
    (mention,) = idx(entity).find("AMR-07?", as_of=None)
    assert mention.candidates == (entity,) and not mention.ambiguous


def test_case_folding_that_changes_length_does_not_shift_spans() -> None:
    (mention,) = idx(AMR7).find("Straße crew: did AMR-07 stop?", as_of=None)
    assert mention.text == "AMR-07"
