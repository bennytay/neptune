"""Identity links and clock mappings in threads (package schema 3; Ledger ADR 0010 §8, §9).

What the merge reads from a ``ClockMapping`` record (the quadruped's, as the compiler's worked
example states it, and variants); ADR 0003 §6 C's mobile-robot run, recorded as two bags, merged
through a stated mapping registered later, inside and outside its validity window; identity links
listed on both threads they name and never joined. The worked-example merge itself is a catalog
contract test (``test_a_named_clock_mapping_merges_onto_the_reference_clock``).
"""

import copy
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from ledger_thread_packages import (
    Record,
    artifact,
    edit,
    known,
    logical,
    rebase,
    records,
    resourced,
    revision,
    stated,
    subset,
    timestamp,
)
from neptune.model.alignment import ClockMapping as ClockMappingRecord
from neptune.model.kinds import RECORD_KINDS
from neptune.model.knowledge import NotCovered
from neptune_ledger.api.types import (
    ClockMerge,
    DeclaredKey,
    History,
    LatestTransform,
    ThreadKey,
    ThreadLink,
)
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.threads.alignment import clock_mapping, mapping_json, mapping_rows
from neptune_ledger.threads.merge import ClockMapping, paths
from test_ledger_registration import fresh
from test_ledger_threads import entries, one, register, validate


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


def quadruped_mapping() -> Record:
    (found,) = [r for r in records("quadruped") if r["kind"] == "clock_mapping"]
    return found


def read_mapping(data: Record) -> ClockMappingRecord:
    _, read = RECORD_KINDS["clock_mapping"]
    made = read(data)
    assert isinstance(made, ClockMappingRecord)
    return made


# --- what the merge reads from a ClockMapping record ---------------------------------------------


def test_the_quadruped_mapping_is_an_identity_map_valid_for_its_window() -> None:
    data = quadruped_mapping()
    window = data["validity"]["value"]
    assert mapping_json(read_mapping(data)) == {
        "bound": [0, 1],
        "offset": [0, 1],
        "slope": [1, 1],
        "window": {
            "end": window["end"]["value"]["ticks"],
            "start": window["start"]["value"]["ticks"],
        },
    }
    (row,) = mapping_rows(iter([data]))
    read = clock_mapping(row.record_id, row.source, row.target, row.mapping)
    assert (read.source, read.target, read.usable) == (data["source"], data["target"], True)


def test_offset_and_drift_come_from_the_anchor_and_rate() -> None:
    data = copy.deepcopy(quadruped_mapping())
    data["anchor"]["value"]["source"]["ticks"] = 100
    data["anchor"]["value"]["target"]["ticks"] = 1000
    data["rate"]["value"] = {"denominator": 3, "numerator": 2}
    data["residual_bound"]["value"]["ticks"] = 7
    reading = mapping_json(read_mapping(data))
    assert (reading["slope"], reading["offset"], reading["bound"]) == ([2, 3], [2800, 3], [7, 1])
    assert Fraction(2, 3) * 100 + Fraction(2800, 3) == 1000, "f(anchor.source) = anchor.target"


def test_a_mapping_without_a_stated_bound_or_window_is_not_used() -> None:
    data = copy.deepcopy(quadruped_mapping())
    data["residual_bound"] = {"knowledge": "unknown"}
    reading = mapping_json(read_mapping(data))
    assert reading["unsupported"] == "residual_bound is unknown", "an unstated bound is not 0"
    data = copy.deepcopy(quadruped_mapping())
    data["validity"]["value"]["end"] = {"knowledge": "unknown"}
    assert "window" not in mapping_json(read_mapping(data)), "an unknown end cannot be checked"
    data["validity"] = {"knowledge": "not_applicable"}
    assert mapping_json(read_mapping(data))["window"] == {}, "a timeless mapping holds everywhere"
    absent = {"knowledge": "known_absent", "provenance": data["provenance"]}
    data = copy.deepcopy(quadruped_mapping())
    data["validity"]["value"]["end"] = absent
    assert set(mapping_json(read_mapping(data))["window"]) == {"start"}, "stated open on the end"


def test_a_window_holds_its_start_and_not_its_end() -> None:
    a, b = "rec:sha256:" + "a" * 64, "rec:sha256:" + "b" * 64
    one_way = ClockMapping(
        "rec:sha256:" + "c" * 64, a, b, Fraction(1), Fraction(0), Fraction(0), (0, 10)
    )
    (path,) = paths(a, b, [one_way])
    assert (path.interval(0), path.interval(9), path.interval(10)) == ((0, 0), (9, 9), None)
    (back,) = paths(b, a, [one_way])
    assert (back.interval(0), back.interval(10)) == ((0, 0), None), "the inverse is half-open too"
    unknown = ClockMapping(
        "rec:sha256:" + "d" * 64, a, b, Fraction(1), Fraction(0), Fraction(0), None
    )
    (path,) = paths(a, b, [unknown])
    assert path.interval(0) is None, "an unknown window covers no instant"
    open_ended = ClockMapping(
        "rec:sha256:" + "e" * 64, a, b, Fraction(1), Fraction(0), Fraction(0), (None, None)
    )
    (path,) = paths(a, b, [open_ended])
    assert path.interval(-(10**18)) == (-(10**18), -(10**18))


# --- ADR 0003 §6 C: two bags of one run, and a mapping stated later -----------------------------


def _declared_run(rows: list[Record]) -> list[Record]:
    run = one(rows, "run")
    first = run["first"]["value"]

    def declare(record: Record) -> Record:
        return {
            **record,
            "logical_id": known(logical("manifest", "night-42"), stated(record)),
            "machine": known(logical("manifest", "amr-11"), stated(record)),
        }

    def timed(record: Record) -> Record:
        start = first["ticks"] + (1000 if record["id"] < run["id"] else 0)
        return {
            **record,
            "first": known(timestamp(first["domain_id"], start)),
            "last": known(timestamp(first["domain_id"], start + 9000)),
        }

    return edit(edit(rows, "run", declare), "stream", timed)


def _sync(transform: Record, data: bytes, record: Record) -> list[Record]:
    """A package holding one alignment record over the bytes ``data`` (a sync log, a fleet
    register), stated by ``transform``, re-identified by the compiler's id rules."""
    source = artifact(data)
    content = source["content_id"]
    located = revision({"kind": "local", "path": f"sync/{content[7:19]}.txt"}, content)
    record = {
        **record,
        "id": "rec:sha256:" + "0" * 64,
        "provenance": {
            "assertion_kind": "stated",
            "evidence": {
                "locator": [{"kind": "byte_range", "length": len(data), "offset": 0}],
                "source": content,
            },
            "transform": transform["id"],
        },
        "schema_version": 3,
    }
    return [source, located, *rebase([transform, record])]


def _mapping(
    part0: list[Record], part1: list[Record], shift: int, window: tuple[int, int]
) -> Record:
    """part1's clock onto part0's: ``t ↦ t + shift`` with bound 2, valid on ``window``."""
    l0, l1 = (one(rows, "run")["first"]["value"] for rows in (part0, part1))
    return {
        "anchor": known(
            {"source": timestamp(l1["domain_id"], 0), "target": timestamp(l0["domain_id"], shift)}
        ),
        "kind": "clock_mapping",
        "method": "stated",
        "rate": known({"denominator": 1, "numerator": 1}),
        "residual_bound": known(timestamp(l0["domain_id"], 2)),
        "source": l1["domain_id"],
        "target": l0["domain_id"],
        "validity": known(
            {
                "clock": l1["domain_id"],
                "end": known(timestamp(l1["domain_id"], window[1])),
                "start": known(timestamp(l1["domain_id"], window[0])),
            }
        ),
    }


def _without_transform(rows: list[Record]) -> list[Record]:
    return [r for r in rows if r["kind"] != "transform_record"]


@pytest.fixture
def split_run(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    part0 = _declared_run(subset("mobile_robot", "rosbag1"))
    part1 = resourced(part0, b"part1 of night-42\n", {"kind": "local", "path": "part1.bag"})
    first = one(part1, "run")["first"]["value"]["ticks"]
    transform = one(part1, "transform_record")
    wide = _sync(transform, b"wide\n", _mapping(part0, part1, 50_000, (first, first + 100_000)))
    narrow = _sync(transform, b"narrow\n", _mapping(part0, part1, 50_000, (first, first + 500)))
    return {
        "part0": part0,
        "part1": part1,
        "p5": register(catalog, part0, tmp_path / "p5"),
        "p6": register(catalog, part1, tmp_path / "p6"),
        "p7": register(catalog, [*wide, *_without_transform(narrow)], tmp_path / "p7"),
        "wide": one(wide, "clock_mapping"),
        "narrow": one(narrow, "clock_mapping"),
        "key": ThreadKey("run", DeclaredKey("manifest", "night-42")),
    }


def test_a_stated_mapping_merges_the_two_bags_into_one_timeline(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    wide = split_run["wide"]
    native = catalog.thread(split_run["key"], "world", History())
    merge = ClockMerge(wide["target"], (wide["id"],))
    merged = catalog.thread(split_run["key"], "world", History(), merge=merge)
    validate(merged)
    assert merged.findings == ()
    assert [(p.kind, p.clock_key) for p in merged.partitions] == [("merged", wide["target"])]
    assert entries(merged) == entries(native), "each clock keeps its order; part1 is 50 000 later"
    for entry in merged.partitions[0].entries:
        world = entry.world.value  # type: ignore[union-attr]
        s, mapped = world.start.ticks, entry.mapped
        assert mapped is not None
        if world.clock == wide["target"]:
            assert (mapped.lo, mapped.hi, mapped.path) == (s, s, ()), "the reference clock"
        else:
            assert world.clock == wide["source"]
            assert (mapped.lo, mapped.hi, mapped.path) == (s + 49_998, s + 50_002, (wide["id"],))
    current = catalog.thread(split_run["key"], "world", LatestTransform(), merge=merge)
    assert entries(current) == entries(merged)


def test_entries_outside_a_mappings_window_stay_on_their_clock(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    narrow, part1 = split_run["narrow"], split_run["part1"]
    run = one(part1, "run")
    merge = ClockMerge(narrow["target"], (narrow["id"],))
    thread = catalog.thread(split_run["key"], "world", History(), merge=merge)
    validate(thread)
    late = {
        r["id"]
        for r in part1
        if r["kind"] == "stream" and r["first"]["value"]["ticks"] > run["first"]["value"]["ticks"]
    }
    assert late, "some streams start 1000 ticks after the run, outside [first, first + 500)"
    # Partitions are ordered by their smallest registration key: the merged one holds part0's.
    assert [p.kind for p in thread.partitions] == ["merged", "clock"]
    assert {e.record_id for e in thread.partitions[1].entries} == late
    assert {(f.code, f.subject) for f in thread.findings} == {
        ("mapping_out_of_range", r) for r in late
    }
    assert {p.mappings for f in thread.findings for p in f.paths_tried or ()} == {(narrow["id"],)}


def test_a_mapping_is_unknown_before_it_is_registered(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    wide = split_run["wide"]
    merge = ClockMerge(wide["target"], (wide["id"],))
    earlier = catalog.thread(split_run["key"], "world", History(), merge=merge, as_of=2)
    assert [(f.code, f.subject) for f in earlier.findings] == [("unknown_mapping", wide["id"])]
    assert earlier.partitions == ()
    named = ClockMerge(wide["target"], (wide["id"], one(split_run["part1"], "run")["id"]))
    thread = catalog.thread(split_run["key"], "world", History(), merge=named)
    assert [f.code for f in thread.findings] == ["unknown_mapping"], "a run is not a mapping"


# --- identity links: edges between two declared threads, never a merge -------------------------


def _link(right: Record) -> Record:
    return {
        "basis": "co_declared",
        "evidence": [],
        "identifier": {"knowledge": "not_applicable"},
        "kind": "identity_link",
        "left": logical("manifest", "amr-11"),
        "right": right,
        "validity": {"knowledge": "not_applicable"},
    }


def test_an_identity_link_is_listed_on_both_threads_and_joins_neither(
    catalog: PostgresCatalog, split_run: dict[str, Any], tmp_path: Path
) -> None:
    transform = one(split_run["part1"], "transform_record")
    register_row = _sync(
        transform, b"amr-11,AMR-0011\n", _link(known(logical("serial", "AMR-0011")))
    )
    package = register(catalog, register_row, tmp_path / "register")
    link = one(register_row, "identity_link")
    manifest = ThreadKey("machine", DeclaredKey("manifest", "amr-11"))
    serial = ThreadKey("machine", DeclaredKey("serial", "AMR-0011"))
    edge = ThreadLink(link["id"], package, manifest, serial, "stated", "known")
    by_manifest = catalog.thread(manifest, "world", History())
    by_serial = catalog.thread(serial, "world", History())
    validate(by_manifest)
    assert by_manifest.links == by_serial.links == (edge,)
    assert edge.entity_kind == NotCovered(), "no field states a kind; the keys' kind is a lookup"
    runs = {one(rows, "run")["id"] for rows in (split_run["part0"], split_run["part1"])}
    assert {r for r, _ in entries(by_manifest)} == runs
    assert by_serial.partitions == (), "a link never brings another thread's records"
    before = catalog.thread(manifest, "world", History(), as_of=3)
    assert before.links == (), "links are read at the catalog point too"
    other = catalog.thread(ThreadKey("site", DeclaredKey("manifest", "amr-11")), "world", History())
    assert [x.from_key.kind for x in other.links] == ["site"], "a link joins ids of one kind"
    software = ThreadKey("software_version", DeclaredKey("manifest", "amr-11"))
    assert catalog.thread(software, "world", History()).links == ()


def test_an_ambiguous_link_lists_every_candidate(
    catalog: PostgresCatalog, split_run: dict[str, Any], tmp_path: Path
) -> None:
    transform = one(split_run["part1"], "transform_record")
    right = {
        "candidates": [
            {"value": logical("asset_tag", "T-7")},
            {"value": logical("asset_tag", "T-8")},
        ],
        "knowledge": "ambiguous",
    }
    rows = _sync(transform, b"amr-11 is T-7 or T-8\n", _link(right))
    package = register(catalog, rows, tmp_path / "register")
    link = one(rows, "identity_link")
    manifest = ThreadKey("machine", DeclaredKey("manifest", "amr-11"))
    thread = catalog.thread(manifest, "world", History())
    assert thread.links == tuple(
        ThreadLink(
            link["id"],
            package,
            manifest,
            ThreadKey("machine", DeclaredKey("asset_tag", tag)),
            "stated",
            "ambiguous",
        )
        for tag in ("T-7", "T-8")
    )
    t8 = catalog.thread(ThreadKey("machine", DeclaredKey("asset_tag", "T-8")), "world", History())
    assert [x.to_key.key for x in t8.links] == [DeclaredKey("asset_tag", "T-8")]


def test_alignment_rows_replay_byte_for_byte(pg_uri: str, tmp_path: Path) -> None:
    """ADR 0003 §8 for the two new tables: registering the same packages in the same order
    into a fresh catalog gives the same rows, and the stored mapping text is canonical."""
    import psycopg

    from test_ledger_registration import dump

    part0 = _declared_run(subset("mobile_robot", "rosbag1"))
    part1 = resourced(part0, b"part1\n", {"kind": "local", "path": "part1.bag"})
    first = one(part1, "run")["first"]["value"]["ticks"]
    transform = one(part1, "transform_record")
    sync = _sync(transform, b"s\n", _mapping(part0, part1, 7, (first, first + 10)))
    link = _sync(transform, b"l\n", _link(known(logical("serial", "X"))))
    dumps = []
    for attempt in ("a", "b"):
        with fresh(pg_uri, tenant=f"replay{attempt}") as catalog:
            for name, rows in (("p0", part0), ("p1", part1), ("s", sync), ("l", link)):
                register(catalog, rows, tmp_path / attempt / name)
        with psycopg.connect(pg_uri) as conn:
            tables = dump(conn, f"tenant_replay{attempt}")
        dumps.append(
            {
                t: [row.replace(f"replay{attempt}", "T") for row in rows]
                for t, rows in tables.items()
                if t in ("thread_identity_link", "thread_clock_mapping")
            }
        )
    assert dumps[0] == dumps[1]
    assert dumps[0]["thread_clock_mapping"] and dumps[0]["thread_identity_link"]
