"""Cache keys, invalidation rules and the cache report (ADR 0031)."""

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from neptune.adapters.contract import chunk_id
from neptune.identity import canonical_json
from neptune.identity.provenance import transform_record
from neptune.model.ids import ConfigHash, ContentId, RecordId
from neptune.model.provenance import TransformRecord
from neptune.runtime.cache import (
    RULES,
    CacheReport,
    Calls,
    ChunkCache,
    DerivativeCache,
    PlanCache,
    Rule,
    SourceCache,
    admission_key,
    cache_report_from_json,
    changed_parts,
    explain_plan,
)
from neptune.store.workspace import Held

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

SOURCE = ContentId("sha256:" + "a" * 64)
OTHER = ContentId("sha256:" + "b" * 64)


def transform(
    adapter: str = "text",
    version: str = "0.1.0",
    config: dict[str, Any] | None = None,
    libraries: dict[str, str] | None = None,
) -> TransformRecord:
    return transform_record(
        adapter_id=adapter,
        adapter_version=version,
        config=config if config is not None else {"block_rule": "paragraph"},
        libraries=libraries,
    )


BASE = transform()


# --- The key -----------------------------------------------------------------------------------


def test_a_chunks_id_is_its_cache_key_and_covers_every_part() -> None:
    """Source identity + adapter id and version + config hash + libraries + chunk identity."""
    context: JsonObject = {"part": "blocks", "start": 0}
    base = chunk_id(SOURCE, BASE.id, context)
    variants = {
        "source": chunk_id(OTHER, BASE.id, context),
        "adapter": chunk_id(SOURCE, transform(adapter="other").id, context),
        "adapter_version": chunk_id(SOURCE, transform(version="0.2.0").id, context),
        "config": chunk_id(SOURCE, transform(config={"block_rule": "line"}).id, context),
        "libraries": chunk_id(SOURCE, transform(libraries={"lib": "1.0"}).id, context),
        "context": chunk_id(SOURCE, BASE.id, {**context, "start": 1}),
    }
    assert base not in variants.values()
    assert len(set(variants.values())) == len(variants)
    assert chunk_id(SOURCE, BASE.id, {"start": 0, "part": "blocks"}) == base  # canonical JSON


def test_changed_parts_name_what_differs_in_order() -> None:
    assert changed_parts(BASE, BASE) == ()
    assert changed_parts(BASE, transform(version="0.2.0")) == ("adapter_version",)
    both = transform(version="0.2.0", config={"block_rule": "line"})
    assert changed_parts(BASE, both) == ("adapter_version", "config")
    assert changed_parts(BASE, transform(libraries={"lib": "1"})) == ("libraries",)
    assert changed_parts(BASE, transform(adapter="other")) == ("adapter",)
    consuming = transform_record(
        adapter_id=BASE.adapter_id,
        adapter_version=BASE.adapter_version,
        config=BASE.config,
        upstream=(transform(adapter="other").id,),
    )
    assert changed_parts(BASE, consuming) == ("upstream",)


# --- Invalidation rules ------------------------------------------------------------------------


def test_a_kept_plan_is_a_hit() -> None:
    plan = explain_plan(BASE, [transform(version="0.0.9"), BASE], None)
    assert plan == PlanCache(Rule.PLANNED)
    assert plan.cache == "hit" and plan.chunk_miss is Rule.NOT_COMMITTED


def test_the_same_adapter_under_another_transform_is_transform_changed() -> None:
    older, line = transform(version="0.0.9"), transform(config={"block_rule": "line"})
    both = transform(version="0.0.9", config={"block_rule": "line"})
    plan = explain_plan(BASE, [both, older, transform(adapter="other")], OTHER)
    assert plan.rule is Rule.TRANSFORM_CHANGED  # before adapter_changed and source_changed
    assert plan.changed == ("adapter_version",) and plan.previous == older.id  # the closest
    assert explain_plan(BASE, [line], None).changed == ("config",)
    assert plan.chunk_miss is Rule.TRANSFORM_CHANGED
    ties = sorted([transform(version="0.0.8"), transform(version="0.0.7")], key=lambda t: t.id)
    assert explain_plan(BASE, ties[::-1], None).previous == ties[0].id  # least id breaks a tie


def test_only_other_adapters_is_adapter_changed() -> None:
    first, second = transform(adapter="alpha"), transform(adapter="beta")
    plan = explain_plan(BASE, [second, first], OTHER)
    assert plan.rule is Rule.ADAPTER_CHANGED and plan.previous == first.id
    assert "adapter" in plan.changed


def test_bytes_replacing_others_are_source_changed_and_unknown_bytes_source_new() -> None:
    assert explain_plan(BASE, [], OTHER) == PlanCache(Rule.SOURCE_CHANGED, previous=OTHER)
    assert explain_plan(BASE, [], None) == PlanCache(Rule.SOURCE_NEW)
    assert PlanCache(Rule.SOURCE_NEW).chunk_miss is Rule.SOURCE_NEW


@pytest.mark.parametrize(
    "arguments",
    [
        (Rule.COMMITTED,),
        (Rule.HELD,),
        (Rule.NOT_COMMITTED,),
        (Rule.ABSENT,),
        (Rule.TRANSFORM_CHANGED, (), BASE.id),  # names no changed part
        (Rule.TRANSFORM_CHANGED, ("config",), None),  # names nothing compared against
        (Rule.TRANSFORM_CHANGED, ("colour",), BASE.id),  # not a part of a transform
        (Rule.TRANSFORM_CHANGED, ("config",), SOURCE),  # a source is not a transform
        (Rule.SOURCE_CHANGED, (), None),
        (Rule.SOURCE_CHANGED, (), BASE.id),
        (Rule.SOURCE_NEW, ("config",), None),
        (Rule.PLANNED, (), BASE.id),
        ("planned",),
    ],
)
def test_a_plan_cache_that_does_not_hold_together_is_refused(arguments: tuple[Any, ...]) -> None:
    with pytest.raises(ValueError):
        PlanCache(*arguments)


def test_every_rule_is_documented_once() -> None:
    assert sorted(entry.name for entry in RULES) == sorted(str(rule) for rule in Rule)


# --- Chunks and derivatives --------------------------------------------------------------------


CHUNK = "chunk:sha256:" + "c" * 64


def test_a_chunk_is_a_hit_only_when_committed() -> None:
    assert ChunkCache(CHUNK, Rule.COMMITTED).cache == "hit"
    for rule in (Rule.NOT_COMMITTED, Rule.SOURCE_NEW, Rule.TRANSFORM_CHANGED):
        assert ChunkCache(CHUNK, rule).cache == "miss"
    for rule in (Rule.PLANNED, Rule.HELD, Rule.ABSENT, Rule.CORRUPT):
        with pytest.raises(ValueError, match="chunk's rule"):
            ChunkCache(CHUNK, rule)
    with pytest.raises(ValueError, match="chunk id"):
        ChunkCache("rec:sha256:" + "c" * 64, Rule.COMMITTED)


def test_a_derivative_is_named_by_how_the_workspace_came_by_it() -> None:
    key = admission_key(SOURCE, BASE.id, [CHUNK], "0.1.0")
    assert key.recipe == "neptune.runtime.admission/1" and key.owners == ((SOURCE, BASE.id),)
    assert key.id != admission_key(SOURCE, BASE.id, [CHUNK], "0.2.0").id  # the laws changed
    rules = {held: DerivativeCache.of(key, held).rule for held in Held}
    assert rules == {Held.HELD: Rule.HELD, Held.BUILT: Rule.ABSENT, Held.REBUILT: Rule.CORRUPT}
    assert [DerivativeCache.of(key, held).cache for held in Held] == ["hit", "miss", "miss"]
    with pytest.raises(ValueError, match="derivative's rule"):
        DerivativeCache(key.id, key.recipe, key.owners, Rule.COMMITTED)


# --- The report --------------------------------------------------------------------------------


def report() -> CacheReport:
    source = SourceCache(
        source=SOURCE,
        transform=BASE.id,
        adapter=BASE.adapter_id,
        adapter_version=BASE.adapter_version,
        config_hash=BASE.config_hash,
        plan=PlanCache(Rule.TRANSFORM_CHANGED, ("config",), transform(config={"x": 1}).id),
        chunks=(ChunkCache(CHUNK, Rule.TRANSFORM_CHANGED),),
    )
    planned = replace(
        source,
        source=OTHER,
        plan=PlanCache(Rule.PLANNED),
        chunks=(
            ChunkCache("chunk:sha256:" + "d" * 64, Rule.COMMITTED),
            ChunkCache("chunk:sha256:" + "e" * 64, Rule.NOT_COMMITTED),
        ),
    )
    derivatives = [
        DerivativeCache.of(
            admission_key(s.source, s.transform, [c.chunk for c in s.chunks], "0.1.0"), held
        )
        for s, held in ((source, Held.BUILT), (planned, Held.HELD))
    ]
    return CacheReport(
        sources=(source, planned),
        derivatives=tuple(sorted(derivatives, key=lambda d: d.derivative)),
        calls=Calls(probe=4, plan=1, ingest=2),
    )


def test_a_report_counts_what_it_lists_and_reads_back_as_itself() -> None:
    made = report()
    assert made.totals() == {
        "chunks": {"hit": 1, "miss": 2},
        "derivatives": {"hit": 1, "miss": 1},
        "plans": {"hit": 1, "miss": 1},
    }
    data = made.to_json()
    assert data["kind"] == "ingest_cache_report" and data["format"] == 1
    assert "receipt" not in data
    assert cache_report_from_json(canonical_json.loads(canonical_json.dumps(data))) == made
    receipt = RecordId("rec:sha256:" + "f" * 64)
    named = made.for_receipt(receipt)
    assert named.to_json()["receipt"] == receipt
    assert cache_report_from_json(named.to_json()) == named
    assert canonical_json.dumps(report().to_json()) == canonical_json.dumps(made.to_json())


def test_an_empty_report_is_a_report() -> None:
    empty = CacheReport()
    assert empty.totals() == {
        "chunks": {"hit": 0, "miss": 0},
        "derivatives": {"hit": 0, "miss": 0},
        "plans": {"hit": 0, "miss": 0},
    }
    assert cache_report_from_json(empty.to_json()) == empty


def test_a_report_lists_sources_and_derivatives_in_order_each_once() -> None:
    made = report()
    with pytest.raises(ValueError, match="sorted"):
        replace(made, sources=made.sources[::-1])
    with pytest.raises(ValueError, match="sorted"):
        replace(made, sources=(made.sources[0], made.sources[0]))
    with pytest.raises(ValueError, match="sorted"):
        replace(made, derivatives=made.derivatives[::-1])
    with pytest.raises(ValueError, match="twice"):
        replace(made.sources[0], chunks=made.sources[0].chunks * 2)
    with pytest.raises(ValueError, match="count"):
        Calls(probe=-1)
    with pytest.raises(ValueError, match="count"):
        Calls(plan=True)


def _broken(change: str) -> dict[str, Any]:
    data: dict[str, Any] = canonical_json.loads(canonical_json.dumps(report().to_json()))  # type: ignore[assignment]
    if change == "kind":
        data["kind"] = "ingest_receipt"
    elif change == "format":
        data["format"] = 2
    elif change == "totals":
        data["totals"]["chunks"]["hit"] = 9
    elif change == "extra":
        data["clock"] = "2026-10-01T00:00:00Z"
    elif change == "calls":
        data["calls"]["ingest"] = True
    elif change == "chunk cache":
        data["sources"][0]["chunks"][0]["cache"] = "hit"
    elif change == "plan cache":
        data["sources"][1]["plan"]["cache"] = "miss"
    elif change == "derivative cache":
        data["derivatives"][0]["cache"] = (
            "hit" if data["derivatives"][0]["rule"] != "held" else "miss"
        )
    elif change == "rule":
        data["sources"][0]["chunks"][0]["rule"] = "fresh"
    elif change == "owner":
        data["derivatives"][0]["owners"] = [["sha256:x"]]
    elif change == "config hash":
        data["sources"][0]["config_hash"] = "md5:00"
    return data


@pytest.mark.parametrize(
    "change",
    [
        "kind",
        "format",
        "totals",
        "extra",
        "calls",
        "chunk cache",
        "plan cache",
        "derivative cache",
        "rule",
        "owner",
        "config hash",
    ],
)
def test_a_malformed_report_is_refused(change: str) -> None:
    with pytest.raises(ValueError):
        cache_report_from_json(_broken(change))


def test_a_source_cache_checks_its_ids() -> None:
    made = report().sources[0]
    with pytest.raises(ValueError):
        replace(made, config_hash=ConfigHash("sha1:00"))
    with pytest.raises(ValueError):
        replace(made, adapter="Text Adapter")
