"""Edge cases of a ``changes`` section (Deploy ADR 0018 §3): what is and is not a change on one
machine's spans, over small graph-schema 2.0.0 documents built per case."""

import json
from typing import Any

import pytest

from deploy_pack_graphs import (
    CIVIL,
    DAY,
    T0,
    VOCABULARY_2_0,
    at,
    claim,
    config,
    graph,
    node,
    rec,
    record,
    row,
)
from deploy_pack_support import FROM_T0, spec
from neptune_deploy.packs import compile_pack, read_snapshot, render_json, render_pdf
from neptune_deploy.packs.compile import Entry, EvidencePack
from neptune_deploy.packs.pdf import literal
from neptune_deploy.packs.snapshot import Node
from neptune_deploy.packs.text import winansi

ROVER: dict[str, Any] = node("machine", "asset-tag:ROVER-11")  # a field rover; any morphology
OTHER_CLOCK = rec("domain rover-11 controller clock")
T1 = T0 + DAY


def _span(
    predicate: str,
    obj: dict[str, Any],
    start: dict[str, Any],
    end: dict[str, Any] | str,
    line: int,
    **extra: Any,
) -> dict[str, Any]:
    return claim(
        ROVER,
        predicate,
        obj,
        (start, end),
        records=(f"record {line}",),
        evidence=(row("cmms.csv", line),),
        consolidator_version="2",
        recorded_at=1,
        **extra,
    )


def _has(name: str, start: dict[str, Any], end: dict[str, Any] | str, line: int, **extra: Any):  # type: ignore[no-untyped-def]
    return _span("has_configuration", config(name), start, end, line, **extra)


def _pack(spans: list[dict[str, Any]], inference: str = "exclude") -> EvidencePack:
    vocabulary = json.loads(VOCABULARY_2_0.read_text(encoding="utf-8"))
    snap = read_snapshot(graph(spans, [], 1, vocabulary, 11, release="2.0.0"))
    subject = Node("machine", ROVER["node_id"])
    chosen = spec(snap, "configuration-traceability", subject, FROM_T0, inference, version=2)
    return compile_pack(chosen, snap)


def _entries(pack: EvidencePack) -> list[Entry]:
    section = next(s for s in pack.sections if s.template.id == "configuration-changes")
    return [*section.entries, *section.other_clocks]


def _known(pack: EvidencePack) -> list[tuple[str, str]]:
    out = []
    for entry in _entries(pack):
        if entry.knowledge == "known":
            assert entry.change is not None
            objects = {s.claim.id: str(s.claim.object["node_id"]) for s in entry.statements}
            out.append((objects[entry.change.before[0]], objects[entry.change.after[0]]))
    return sorted(out)


def _shown(text: str) -> bytes:
    return literal(winansi(text))[1:-1]


def test_a_concurrent_configuration_does_not_join_a_change() -> None:
    """``has_configuration`` is ``many``: a parameter set in force throughout neither ends nor
    starts at the instant the navigation stack changes, so it is no part of that change."""
    pack = _pack(
        [
            _has("params-P", at(CIVIL, T0), "open", 1),
            _has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 2),
            _has("nav-B", at(CIVIL, T1), "open", 3),
        ]
    )
    assert _known(pack) == [("cmms.config:nav-A", "cmms.config:nav-B")]
    assert len(_entries(pack)) == 1


def test_concurrent_configurations_changing_together_pair_as_transitions_does() -> None:
    pack = _pack(
        [
            _has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 1),
            _has("params-P", at(CIVIL, T0), at(CIVIL, T1), 2),
            _has("nav-B", at(CIVIL, T1), "open", 3),
            _has("params-P", at(CIVIL, T1), "open", 4),
        ]
    )
    # Every decided pair of different objects, as Memory's transitions(); P -> P is no change.
    assert _known(pack) == [
        ("cmms.config:nav-A", "cmms.config:nav-B"),
        ("cmms.config:nav-A", "cmms.config:params-P"),
        ("cmms.config:params-P", "cmms.config:nav-B"),
    ]


def test_two_clocks_at_the_same_ticks_are_no_boundary() -> None:
    pack = _pack(
        [
            _has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 1),
            _has("nav-B", at(OTHER_CLOCK, T1), "open", 2),
        ]
    )
    assert _entries(pack) == []


def test_an_unknown_span_right_after_a_decided_one_is_an_unknown_boundary() -> None:
    pack = _pack(
        [
            _has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 1),
            _span(
                "configuration_unknown",
                record("work order WO-1"),
                at(CIVIL, T1),
                at(CIVIL, T1 + DAY),
                2,
            ),
        ]
    )
    [entry] = _entries(pack)
    assert entry.knowledge == "unknown"
    assert entry.change is not None and not entry.change.beside_change
    assert _shown("UNKNOWN BOUNDARY - an unknown span meets it") in render_pdf(pack)


def test_the_same_configuration_restated_is_no_change() -> None:
    pack = _pack(
        [
            _has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 1),
            _has("nav-A", at(CIVIL, T1), "open", 2),
        ]
    )
    assert _entries(pack) == []


def test_two_open_ended_spans_are_no_boundary() -> None:
    pack = _pack(
        [
            _has("nav-A", at(CIVIL, T0), "open", 1),
            _has("nav-B", at(CIVIL, T1), "open", 2),
        ]
    )
    assert _entries(pack) == []


def test_a_candidate_starting_beside_a_decided_change_does_not_contradict_it() -> None:
    """Review: decided A ends at t, decided B and candidate C start at t. The A -> B change stands
    (Memory's transitions); the ambiguous entry says C also starts there, never that no change is
    read across the instant."""
    pack = _pack(
        [
            _has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 1),
            _has("nav-B", at(CIVIL, T1), "open", 2),
            _span("configuration_candidate", config("nav-C"), at(CIVIL, T1), "open", 3),
        ]
    )
    assert _known(pack) == [("cmms.config:nav-A", "cmms.config:nav-B")]
    [beside] = [e for e in _entries(pack) if e.knowledge != "known"]
    assert beside.knowledge == "ambiguous"
    assert beside.change is not None and beside.change.beside_change
    section = next(
        s for s in json.loads(render_json(pack))["sections"] if s["id"] == "configuration-changes"
    )
    assert sorted(
        (e["knowledge"], e["change"].get("beside_change")) for e in section["entries"]
    ) == [
        ("ambiguous", True),
        ("known", None),
    ]
    pdf = render_pdf(pack)
    assert (
        _shown("[AMBIGUOUS BOUNDARY - a candidate or inferred configuration also starts or") in pdf
    )
    assert _shown("AMBIGUOUS BOUNDARY - a candidate or inferred span meets it") not in pdf


@pytest.mark.parametrize("inference", ["exclude", "include"])
def test_an_inferred_span_never_forms_a_change(inference: str) -> None:
    inferred = _has(
        "nav-B",
        at(CIVIL, T1),
        "open",
        2,
        kind="inferred",
        model={"model_id": "config-attribution", "model_version": "1"},
        confidence=0.7,
    )
    pack = _pack([_has("nav-A", at(CIVIL, T0), at(CIVIL, T1), 1), inferred], inference)
    assert _known(pack) == []
    if inference == "exclude":
        assert _entries(pack) == []
        return
    [entry] = _entries(pack)
    assert entry.knowledge == "ambiguous"
    assert entry.change is not None and not entry.change.beside_change
    pdf = render_pdf(pack)
    assert _shown("AMBIGUOUS BOUNDARY - a candidate or inferred span meets it") in pdf
    assert _shown("[INFERRED by config-attribution 1") in pdf
