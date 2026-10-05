"""Compiler 2 (ADR 0014): the overlap sweep, restated and unplaced other-clock claims, shared
hops, same_event templates, newer literal types, supersession bounds and the escaped ``<U+``."""

import json
import random
import time
from typing import Any

import pytest

from deploy_pack_corpus import fixture_path as corpus_fixture
from deploy_pack_graphs import HMI, TEACH, fixture_path, rec
from deploy_pack_support import (
    CONTRACTS,
    FIRST_DAY,
    SITE,
    configuration_pack,
    events,
    events_pack,
    spec,
)
from neptune_deploy.packs import (
    COMPILER_VERSION,
    PackError,
    compile_pack,
    load_snapshot,
    read_snapshot,
    read_template,
    render_claims,
    render_pdf,
)
from neptune_deploy.packs.compile import (
    Entry,
    Statement,
    _grouped,
    _overlap_conflicts,
)
from neptune_deploy.packs.snapshot import Claim, Interval, Node, Stamp
from neptune_deploy.packs.text import claim_object, winansi

CLOCK = "rec:sha256:" + "c" * 64
OTHER = "rec:sha256:" + "d" * 64
ONE = {"has_calibration": "one"}


def _claim(index: int, start: int, end: int | None, obj: str, clock: str = CLOCK) -> Claim:
    value = {"kind": "node", "node_id": obj, "node_type": "configuration"}
    return Claim(
        id=f"claim:sha256:{index:064x}",
        subject=Node("sensor", "serial:S-1"),
        predicate="has_calibration",
        object=value,
        valid=Interval(Stamp(clock, start), "open" if end is None else Stamp(clock, end)),
        assertion_kind="stated",
        recorded_at=1,
        current=True,
        evidence=(),
        records=(),
        raw={},
    )


def _brute(entries: list[Entry]) -> set[int]:
    """Compiler 1's pairwise rule, as the reference the sweep must equal."""
    found: set[int] = set()
    for i, a in enumerate(entries):
        for j, b in enumerate(entries):
            if i >= j or a.node != b.node or not a.valid.overlaps(b.valid):
                continue
            for left in a.statements:
                for right in b.statements:
                    if left.claim.object_key != right.claim.object_key:
                        found.update((i, j))
    return found


@pytest.mark.parametrize("seed", range(40))
def test_the_overlap_sweep_equals_the_pairwise_rule(seed: int) -> None:
    rng = random.Random(seed)
    claims = []
    for i in range(rng.randint(1, 30)):
        start = rng.randint(0, 40)
        end = None if rng.random() < 0.15 else start + rng.randint(0, 12)
        clock = CLOCK if rng.random() < 0.85 else OTHER
        claims.append(_claim(i, start, end, f"cfg:{rng.choice('ABC')}", clock))
    statements = [Statement(c, "known") for c in claims]
    entries = [
        Entry(c.subject, c.valid, "known", (s,)) for c, s in zip(claims, statements, strict=True)
    ]
    entries.sort(key=lambda e: (e.node, e.valid.sort_key()))
    assert _overlap_conflicts(entries, ONE) == _brute(entries)


def test_fifty_thousand_spans_compile_in_seconds() -> None:
    claims = [_claim(i, i * 10, i * 10 + 15, f"cfg:{i % 3}") for i in range(50_000)]
    claims.append(_claim(50_000, 0, None, "cfg:open"))  # overlaps every span
    began = time.perf_counter()
    entries = _grouped([Statement(c, "known") for c in claims], ONE)
    assert time.perf_counter() - began < 10
    assert all(e.knowledge == "conflict" for e in entries)
    # Every span open and stating its own object: each overlaps all the others.
    distinct = [_claim(i, i, None, f"cfg:{i}") for i in range(50_000)]
    began = time.perf_counter()
    entries = _grouped([Statement(c, "known") for c in distinct], ONE)
    assert time.perf_counter() - began < 10
    assert all(e.knowledge == "conflict" for e in entries)
    calm = [_claim(i, i * 10, i * 10 + 10, "cfg:same") for i in range(50_000)]
    assert {e.knowledge for e in _grouped([Statement(c, "known") for c in calm], ONE)} == {"known"}


def _events_document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(fixture_path("arm_cell_events").read_bytes())
    return document


def test_restated_other_clock_claims_are_counted_and_unplaced_ones_listed() -> None:
    timeline = next(s for s in events_pack().sections if s.template.id == "events")
    # The e-stop's six statements on the HMI clock and the intervention's three on the PLC
    # clock are restated by their civil placements: counted, never silently dropped.
    assert timeline.other_clock_restated == 9
    doc = _events_document()
    estop = f"record:{rec('event estop')}"
    on_hmi = next(
        c
        for c in doc["claims"]
        if c["subject"]["node_id"] == estop
        and c["valid"]["start"]["domain_id"] == HMI
        and c["predicate"] == "event_kind"
    )
    # A statement only the HMI clock holds has no pack-clock twin: it is listed, not hidden.
    lone = {**on_hmi, "id": "claim:sha256:" + "e" * 64, "predicate": "declared_kind"}
    lone["object"] = {**lone["object"], "value": "ESTOP_HMI"}
    doc["claims"].append(lone)
    snap = read_snapshot(doc)
    pack = compile_pack(spec(snap, "event-timeline", interval=FIRST_DAY), snap)
    timeline = next(s for s in pack.sections if s.template.id == "events")
    (listed,) = [e for e in timeline.other_clocks if e.node.node_id == estop]
    assert [s.claim.id for s in listed.statements] == [lone["id"]]
    assert lone["id"] in {c.id for c in pack.claims}
    assert timeline.other_clock_restated == 9


def test_the_events_pack_still_lists_the_unmapped_pendant() -> None:
    timeline = next(s for s in events_pack().sections if s.template.id == "events")
    assert [e.valid.start.domain for e in timeline.other_clocks] == [TEACH]


def test_memory_published_clock_maps_now_read() -> None:
    """Compiler 1 refused any graph with a clock_map literal (graph-schema 1.4.0, on main)."""
    golden = CONTRACTS / "graph-schema" / "v1.4.0" / "golden" / "graph.json"
    snap = load_snapshot(golden.read_bytes())
    maps = [c for c in snap.claims if c.predicate == "clock_map"]
    assert maps
    shown = claim_object(maps[0].object)
    assert shown.startswith("clock map onto rec:sha256:")
    assert "rate 1/1" in shown


def test_an_unknown_literal_datatype_is_still_refused() -> None:
    doc = _events_document()
    doc["claims"][0]["object"] = {
        "datatype": "polygon",
        "kind": "literal",
        "unit": {"knowledge": "not_applicable"},
        "value": [],
    }
    with pytest.raises(PackError) as caught:
        read_snapshot(doc)
    assert caught.value.code == "snapshot_malformed"
    assert caught.value.pointer == "/claims/0/object/datatype"


@pytest.mark.parametrize(
    ("superseded", "recorded", "message"),
    [(0, 1, "before its recording"), (99, 1, "after the head")],
)
def test_supersession_is_bounded_by_recording_and_head(
    superseded: int, recorded: int, message: str
) -> None:
    doc = _events_document()
    doc["claims"][0]["recorded_at"] = recorded
    doc["claims"][0]["superseded_at"] = superseded
    with pytest.raises(PackError, match=message) as caught:
        read_snapshot(doc)
    assert caught.value.pointer == "/claims/0/superseded_at"


def test_supersession_in_the_recording_transaction_reads() -> None:
    doc = _events_document()
    doc["claims"][0]["recorded_at"] = doc["claims"][0]["superseded_at"] = 1
    assert not read_snapshot(doc).claims[0].current


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("<U+2192>", "<U+003C>U+2192>"),
        ("a <U b", "a <U b"),
        ("<U+", "<U+003C>U+"),
        ("→ <U+2192>", "<U+2192> <U+003C>U+2192>"),
    ],
)
def test_a_literal_escape_form_is_itself_escaped(text: str, shown: str) -> None:
    assert winansi(text) == shown


def _timeline_template(section: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": "d",
        "id": "t",
        "schema": "neptune-deploy.pack-template/1",
        "sections": [
            {
                "about": [[{"direction": "shared", "predicate": "evidenced_by"}]],
                "description": "d",
                "id": "s",
                "kind": "timeline",
                "predicates": {"event_kind": "known"},
                "title": "S",
                **section,
            }
        ],
        "subject_types": ["event"],
        "title": "T",
        "version": 1,
    }


def test_same_event_and_shared_hops_read() -> None:
    template = read_template(_timeline_template({"same_event": ["same_as"]}))
    (section,) = template.sections
    assert section.same_event == ("same_as",)
    assert section.about[0][0].direction == "shared"


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"same_event": []}, "non-empty list without repeats"),
        ({"same_event": ["same_as", "same_as"]}, "non-empty list without repeats"),
        ({"same_event": "same_as"}, "expected an array"),
        ({"same_event": ["Same As"]}, "does not match"),
        ({"kind": "claims", "same_event": ["same_as"]}, "for timeline sections"),
    ],
)
def test_malformed_same_event_is_refused(section: dict[str, Any], message: str) -> None:
    with pytest.raises(PackError, match=message) as caught:
        read_template(_timeline_template(section))
    assert caught.value.code == "template_malformed"
    assert caught.value.pointer.startswith("/sections/0/same_event")


def test_an_event_subject_reads_and_a_record_subject_does_not() -> None:
    snap = events()
    estop = Node("event", f"record:{rec('event estop')}")
    assert spec(snap, "event-timeline", subject=estop).subject == estop
    with pytest.raises(PackError) as caught:
        spec(snap, "event-timeline", subject=Node("record", "rec:x"))
    assert caught.value.code == "spec_malformed"


# --- clock_map and delta literals are shape-checked (review blocker) ---------------------------


def _corpus_claim(predicate: str) -> tuple[dict[str, Any], int]:
    document: dict[str, Any] = json.loads(corpus_fixture().read_bytes())
    index = next(i for i, c in enumerate(document["claims"]) if c["predicate"] == predicate)
    return document, index


def _refused(document: dict[str, Any], pointer: str, message: str) -> None:
    with pytest.raises(PackError, match=message) as caught:
        read_snapshot(document)
    assert caught.value.code == "snapshot_malformed"
    assert caught.value.pointer == pointer


def test_the_corpus_clock_maps_and_deltas_read() -> None:
    for predicate in ("clock_map", "drift"):
        document, _index = _corpus_claim(predicate)
        read_snapshot(document)


CLOCK_MAP_CASES: list[tuple[str, Any, str, str]] = [
    ("value", 42, "", "expected an object"),
    ("value", {"bogus": 1}, "", "missing"),
    ("unit", {"knowledge": "known", "value": "s"}, "/unit", "unexpected value"),
    ("value/method", "guessed", "/value/method", "not one of"),
    ("value/target", "not-a-record", "/value/target", "does not match"),
    ("value/chain", ["x"], "/value/chain/0", "does not match"),
    ("value/rate", {"knowledge": "known"}, "/value/rate", "missing value"),
    (
        "value/rate",
        {"knowledge": "known", "value": {"denominator": 0, "numerator": 1}},
        "/value/rate/value/denominator",
        "below 1",
    ),
    ("value/rate", {"knowledge": "maybe"}, "/value/rate/knowledge", "not one of"),
    (
        "value/rate",
        {"candidates": [{"value": {"denominator": 1, "numerator": 1}}], "knowledge": "ambiguous"},
        "/value/rate/candidates",
        "at least two candidates",
    ),
    (
        "value/anchor",
        {"knowledge": "known", "value": {"source": 1}},
        "/value/anchor/value",
        "missing",
    ),
    (
        "value/residual_bound",
        {"knowledge": "known", "value": {"domain_id": "rec:sha256:" + "f" * 64, "ticks": 0}},
        "/value/residual_bound/value",
        "ticks of the target clock",
    ),
]


@pytest.mark.parametrize(("path", "value", "pointer", "message"), CLOCK_MAP_CASES)
def test_a_malformed_clock_map_is_refused(
    path: str, value: Any, pointer: str, message: str
) -> None:
    document, index = _corpus_claim("clock_map")
    target: Any = document["claims"][index]["object"]
    *parents, last = path.split("/")
    for key in parents:
        target = target[key]
    target[last] = value
    _refused(document, f"/claims/{index}/object{pointer if path != 'value' else '/value'}", message)


def _anchor_case(side: str) -> tuple[dict[str, Any], int]:
    document, index = _corpus_claim("clock_map")
    anchor = document["claims"][index]["object"]["value"]["anchor"]["value"]
    other = anchor["target" if side == "source" else "source"]["domain_id"]
    anchor[side]["domain_id"] = other
    return document, index


def test_an_anchor_source_on_the_target_clock_is_refused() -> None:
    """Review blocker: the fixture once wrote the source instant on the target clock."""
    document, index = _anchor_case("source")
    _refused(
        document,
        f"/claims/{index}/object/value/anchor/value/source",
        "source instant is on the mapped clock",
    )


def test_an_anchor_target_on_the_source_clock_is_refused() -> None:
    document, index = _anchor_case("target")
    _refused(
        document,
        f"/claims/{index}/object/value/anchor/value/target",
        "target instant is on the target clock",
    )


def test_a_clock_map_about_a_machine_is_refused() -> None:
    document, index = _corpus_claim("clock_map")
    document["claims"][index]["subject"] = {
        "kind": "node",
        "node_id": "asset-tag:ARM-3A",
        "node_type": "machine",
    }
    _refused(document, f"/claims/{index}/object/value", "about a clock node")


DELTA_CASES: list[tuple[str, Any, str, str]] = [
    ("value", "not a delta", "/value", "expected an object"),
    ("value", {"bogus": 1}, "/value", "missing quantity"),
    ("value/quantity", "angle", "/value/quantity", "not one of"),
    ("value/representation", "quaternion", "/value/representation", "not one of"),
    ("value/values", [], "/value/values", "1 to 100000 values"),
    ("value/values", [1, "2"], "/value/values/1", "expected a number"),
    ("value/values", [True], "/value/values/0", "expected a number"),
    ("value/earlier", "CAL-1", "/value/earlier", "does not match"),
    ("value/extra", 1, "/value", "unexpected extra"),
    ("unit", {"knowledge": "unknown"}, "/unit/knowledge", "not one of"),
    ("unit", {"knowledge": "known"}, "/unit", "missing value"),
]


@pytest.mark.parametrize(("path", "value", "pointer", "message"), DELTA_CASES)
def test_a_malformed_delta_is_refused(path: str, value: Any, pointer: str, message: str) -> None:
    document, index = _corpus_claim("drift")
    target: Any = document["claims"][index]["object"]
    *parents, last = path.split("/")
    for key in parents:
        target = target[key]
    target[last] = value
    _refused(document, f"/claims/{index}/object{pointer}", message)


def test_a_rotation_delta_needs_its_frames_adjustment_and_component_count() -> None:
    document, index = _corpus_claim("drift")
    frame = {"frame_graph_id": "rec:sha256:" + "a" * 64, "frame_id": "wrist_camera"}
    rotation = {
        "adjustment": "none",
        "child": frame,
        "earlier": "rec:sha256:" + "1" * 64,
        "later": "rec:sha256:" + "2" * 64,
        "parent": {**frame, "frame_id": "tool0"},
        "quantity": "rotation",
        "representation": "quaternion",
        "transform": {"child": "wrist_camera", "direction": "child_to_parent", "parent": "tool0"},
        "values": [0.0, 0.0, 0.0, 0.0],
    }
    document["claims"][index]["object"]["value"] = rotation
    read_snapshot(document)
    rotation["values"] = [0.0, 0.0, 0.0]
    _refused(document, f"/claims/{index}/object/value/values", "exactly 4 values")
    rotation["values"] = [0.0] * 4
    rotation["adjustment"] = "wrapped"
    _refused(document, f"/claims/{index}/object/value/adjustment", "not one of")
    del rotation["adjustment"]
    _refused(document, f"/claims/{index}/object/value", "missing adjustment")


def test_the_exported_claim_set_holds_current_versions_only() -> None:
    """Review nit: a ClaimsResult is as of the head; a superseded version stays in pack.json."""
    pack = configuration_pack(subject=SITE)
    superseded = [c.id for c in pack.claims if not c.current]
    assert superseded  # cited through the overridden_on_arrival finding
    exported = json.loads(render_claims(pack))
    ids = {c["id"] for c in (*exported["claims"], *exported["other_clocks"])}
    assert ids == {c.id for c in pack.claims if c.current}
    assert not ids.intersection(superseded)


def test_the_pdf_producer_names_the_compiler_version() -> None:
    assert f"/Producer (neptune-deploy packs {COMPILER_VERSION})".encode() in render_pdf(
        events_pack()
    )
