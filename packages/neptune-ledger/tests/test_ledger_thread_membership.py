"""Thread membership from one package's records (ADR 0003 §2, ADR 0010 §2): a pure function.

Records here are synthetic JSON with only the fields membership reads, so each rule is tested
alone; the same function runs on the compiler's real packages in test_ledger_threads.py. The
robots are deliberately varied: a humanoid, an AUV, a field rover, a warehouse fleet, an arm.
"""

import random
from typing import Any

import pytest

from neptune.identity import canonical_json
from neptune_ledger.api.types import DeclaredKey, EvidenceAnchor, ThreadKey
from neptune_ledger.threads.membership import ThreadRows, thread_rows, world_json

T1 = "rec:sha256:" + "1" * 64
SOURCE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
CLOCK = "rec:sha256:" + "c" * 64
CLOCK2 = "rec:sha256:" + "d" * 64
_ids = iter(range(1, 10_000))


def rid() -> str:
    return f"rec:sha256:{next(_ids):064x}"


def provenance(offset: int = 0, kind: str = "observed", source: str = SOURCE) -> dict[str, Any]:
    return {
        "assertion_kind": kind,
        "evidence": {
            "locator": [{"kind": "byte_range", "length": 1, "offset": offset}],
            "source": source,
        },
        "transform": T1,
    }


def record(kind: str, offset: int, **fields: Any) -> dict[str, Any]:
    return {"id": rid(), "kind": kind, "provenance": provenance(offset), **fields}


def known(namespace: str, value: str, assertion: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"knowledge": "known", "value": {"namespace": namespace, "value": value}}
    if assertion is not None:
        out["provenance"] = {**provenance(99), "assertion_kind": assertion}
    return out


def rows(*records: dict[str, Any]) -> ThreadRows:
    lines: dict[str, list[bytes]] = {}
    for r in records:
        lines.setdefault(r["kind"], []).append(canonical_json.dumps(r))
    return thread_rows({kind: tuple(found) for kind, found in lines.items()})


def members(found: ThreadRows, key: ThreadKey) -> set[tuple[str, tuple[str, ...]]]:
    return {(m.record_id, m.roles) for m in found.members if m.thread_id == key.thread_id}


def machine(namespace: str, value: str) -> ThreadKey:
    return ThreadKey("machine", DeclaredKey(namespace, value))


def anchored(kind: str, r: dict[str, Any]) -> ThreadKey:
    evidence = r["provenance"]["evidence"]
    return ThreadKey(kind, EvidenceAnchor(evidence["source"], tuple(evidence["locator"])))  # type: ignore[arg-type]


# --- declared keys -------------------------------------------------------------------------------


def test_a_humanoid_with_two_identifiers_is_in_two_machine_threads() -> None:
    """Co-declared keys are two threads; the Ledger never unions them (ADR 0003 §1.4)."""
    body = record(
        "machine", 1, identifiers=[known("serial", "H1-0007"), known("manifest", "atlas-7")]
    )
    run = record(
        "run", 2, machine=known("manifest", "atlas-7"), logical_id={"knowledge": "unknown"}
    )
    found = rows(body, run)
    assert members(found, machine("serial", "H1-0007")) == {(body["id"], ("subject",))}
    assert members(found, machine("manifest", "atlas-7")) == {
        (body["id"], ("subject",)),
        (run["id"], ("cites",)),
    }


def test_keys_compare_as_exact_code_points() -> None:
    a = record("machine", 1, identifiers=[known("manifest", "spot-07")])
    b = record("machine", 2, identifiers=[known("manifest", "Spot-07")])
    found = rows(a, b)
    assert members(found, machine("manifest", "spot-07")) == {(a["id"], ("subject",))}
    assert members(found, machine("manifest", "Spot-07")) == {(b["id"], ("subject",))}


def test_an_equal_key_of_another_kind_is_another_thread() -> None:
    auv = record("machine", 1, identifiers=[known("serial", "X")])
    dvl = record(
        "hardware_component",
        2,
        category="sensor",
        identifiers=[known("serial", "X")],
        configuration=rid(),
    )
    found = rows(auv, dvl)
    sensor = ThreadKey("sensor", DeclaredKey("serial", "X"))
    assert sensor.thread_id != machine("serial", "X").thread_id
    assert members(found, sensor) == {(dvl["id"], ("subject",))}
    assert members(found, machine("serial", "X")) == {(auv["id"], ("subject",))}


@pytest.mark.parametrize(
    "state",
    [
        {"knowledge": "unknown"},
        {"knowledge": "not_covered"},
        {"knowledge": "not_applicable"},
        {"knowledge": "known_absent", "provenance": provenance(5)},
    ],
)
def test_only_a_known_value_creates_membership(state: dict[str, Any]) -> None:
    run = record("run", 1, machine=state, logical_id=state)
    found = rows(run)
    assert {t.kind for t in found.threads} == {"run"}, "only its own anchored run thread"
    assert found.unresolved == ()


def test_inferred_values_and_records_join_nothing() -> None:
    inferred_field = record("run", 1, machine=known("vin", "1HGBH41", "inferred"))
    inferred_record = record("stream", 2)
    inferred_record["provenance"]["assertion_kind"] = "inferred"
    found = rows(inferred_field, inferred_record)
    assert members(found, machine("vin", "1HGBH41")) == set()
    assert {m.record_id for m in found.members} == {inferred_field["id"]}


def test_stated_field_provenance_counts_and_inherited_provenance_counts() -> None:
    a = record("calibration", 1, machine=known("serial", "UR-1", "stated"))
    b = record("calibration", 2, machine=known("serial", "UR-1"))  # inherits observed
    assert members(rows(a, b), machine("serial", "UR-1")) == {
        (a["id"], ("cites",)),
        (b["id"], ("cites",)),
    }


def test_ambiguous_candidates_are_unresolved_never_members() -> None:
    field = {
        "candidates": [
            {"value": {"namespace": "fleet", "value": "amr-11"}},
            {"value": {"namespace": "fleet", "value": "amr-12"}},
            {
                "provenance": {**provenance(3), "assertion_kind": "inferred"},
                "value": {"namespace": "fleet", "value": "amr-13"},
            },
        ],
        "knowledge": "ambiguous",
    }
    run = record("run", 1, machine=field, logical_id=field)
    found = rows(run)
    assert {(u.record_id, u.pointer) for u in found.unresolved} == {
        (run["id"], "/machine"),
        (run["id"], "/logical_id"),
    }
    named = {(t.kind, t.key) for t in found.threads if t.kind in ("machine",)}
    assert len(named) == 2, "the inferred candidate names no thread"
    assert [m.thread_id for m in found.members] == [anchored("run", run).thread_id]


# --- the thread kinds of ADR 0003 §2 ------------------------------------------------------------


def test_sensor_threads_open_only_for_sensors_and_captures_cite_them() -> None:
    joint = record(
        "hardware_component",
        1,
        category="joint",
        identifiers=[known("serial", "J-1")],
        configuration=rid(),
    )
    camera = record(
        "hardware_component",
        2,
        category="sensor",
        identifiers=[known("serial", "C-9")],
        configuration=rid(),
    )
    image = record("image", 3, capture={"device_identifiers": [known("serial", "C-9")]})
    video = record("video", 4, capture={"device_identifiers": [known("serial", "C-9")]})
    found = rows(joint, camera, image, video)
    assert not [t for t in found.threads if t.kind == "sensor" and "J-1" in t.key]
    assert members(found, ThreadKey("sensor", DeclaredKey("serial", "C-9"))) == {
        (camera["id"], ("subject",)),
        (image["id"], ("cites",)),
        (video["id"], ("cites",)),
    }


def test_sites_and_assets_of_a_field_rover() -> None:
    farm = record(
        "site", 1, identifiers=[known("register", "FARM-1")], parent={"knowledge": "unknown"}
    )
    plot = record(
        "site", 2, identifiers=[known("register", "PLOT-7")], parent=known("register", "FARM-1")
    )
    pump = record(
        "asset",
        3,
        identifiers=[known("tag", "PUMP-3")],
        site=known("register", "PLOT-7"),
        parent={"knowledge": "not_covered"},
    )
    valve = record(
        "asset",
        4,
        identifiers=[known("tag", "V-1")],
        site={"knowledge": "unknown"},
        parent=known("tag", "PUMP-3"),
    )
    found = rows(farm, plot, pump, valve)

    def site(value: str) -> ThreadKey:
        return ThreadKey("site", DeclaredKey("register", value))

    def asset(value: str) -> ThreadKey:
        return ThreadKey("asset", DeclaredKey("tag", value))

    assert members(found, site("FARM-1")) == {(farm["id"], ("subject",)), (plot["id"], ("cites",))}
    assert members(found, site("PLOT-7")) == {(plot["id"], ("subject",)), (pump["id"], ("cites",))}
    assert members(found, asset("PUMP-3")) == {
        (pump["id"], ("subject",)),
        (valve["id"], ("cites",)),
    }


def test_a_declared_run_and_its_streams_in_the_same_package() -> None:
    run = record(
        "run", 1, logical_id=known("manifest", "night-42"), machine={"knowledge": "unknown"}
    )
    stream = record("stream", 2, run=run["id"])
    stray = record("stream", 3, run=rid())  # a run of another package: tier-2 ids are scoped
    found = rows(run, stream, stray)
    declared = ThreadKey("run", DeclaredKey("manifest", "night-42"))
    assert members(found, declared) == {(run["id"], ("subject",)), (stream["id"], ("part_of",))}
    assert members(found, anchored("run", run)) == set(), "a declared run opens no anchored thread"
    for s in (stream, stray):
        assert members(found, anchored("stream", s)) == {(s["id"], ("subject",))}


def test_an_undeclared_run_is_anchored_and_its_streams_join_that() -> None:
    run = record("run", 1, logical_id={"knowledge": "unknown"}, machine={"knowledge": "unknown"})
    stream = record("stream", 2, run=run["id"])
    found = rows(run, stream)
    assert members(found, anchored("run", run)) == {
        (run["id"], ("subject",)),
        (stream["id"], ("part_of",)),
    }


def test_configurations_are_anchored_and_components_are_part_of_them() -> None:
    arm = record("hardware_configuration", 1, machine={"knowledge": "not_covered"})
    joint = record(
        "hardware_component", 2, category="joint", identifiers=[], configuration=arm["id"]
    )
    calibration = record("calibration", 3, machine={"knowledge": "unknown"})
    found = rows(arm, joint, calibration)
    assert members(found, anchored("configuration", arm)) == {
        (arm["id"], ("subject",)),
        (joint["id"], ("part_of",)),
    }
    assert members(found, anchored("configuration", calibration)) == {
        (calibration["id"], ("subject",))
    }


def test_software_versions_are_keyed_by_commit_or_digest_never_by_name() -> None:
    sha, digest = "2a7d3f1ce8b5f0a9d61c2e7b4f8a3d5c6e9b1f07", "sha256:" + "e" * 64
    items = [
        {
            "commit": {"knowledge": "known", "value": {"kind": "git_commit", "sha": sha}},
            "digest": {"knowledge": "not_covered"},
            "name": {"knowledge": "known", "value": "nav2"},
        },
        {
            "commit": {"knowledge": "not_applicable"},
            "digest": {
                "knowledge": "known",
                "value": {"digest": digest, "kind": "container_image_digest"},
            },
            "name": {"knowledge": "known", "value": "nav2"},
        },
    ]
    config = record("software_configuration", 1, machine={"knowledge": "unknown"}, software=items)
    found = rows(config)
    for key in (DeclaredKey("git_commit", sha), DeclaredKey("container_image_digest", digest)):
        assert members(found, ThreadKey("software_version", key)) == {(config["id"], ("cites",))}
    assert not [t for t in found.threads if "nav2" in t.key]


def test_documents_and_their_blocks() -> None:
    manual = record("document_record", 1)
    block = record("document_block", 2, document=manual["id"])
    found = rows(manual, block)
    assert members(found, anchored("document", manual)) == {
        (manual["id"], ("subject",)),
        (block["id"], ("part_of",)),
    }


def test_records_without_a_record_level_anchor_are_in_no_thread() -> None:
    bare = {"id": rid(), "kind": "stream"}
    empty = record("stream", 1)
    empty["provenance"]["evidence"]["locator"] = []
    assert rows(bare, empty).members == ()


# --- world time, determinism, hostile values ---------------------------------------------------


def at(clock: str, ticks: int) -> dict[str, Any]:
    return {"knowledge": "known", "value": {"domain_id": clock, "ticks": ticks}}


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("machine", {}, {"knowledge": "not_applicable"}),
        (
            "run",
            {"first": {"knowledge": "unknown"}, "last": {"knowledge": "unknown"}},
            {"knowledge": "unknown"},
        ),
        (
            "run",
            {"first": {"knowledge": "unknown"}, "last": at(CLOCK, 9)},
            {
                "knowledge": "known",
                "value": {"end": at(CLOCK, 9), "start": {"domain_id": CLOCK, "ticks": 9}},
            },
        ),
        (
            "stream",
            {"first": at(CLOCK, 1), "last": at(CLOCK2, 9)},
            {
                "knowledge": "known",
                "value": {"end": at(CLOCK2, 9), "start": {"domain_id": CLOCK, "ticks": 1}},
            },
        ),
        (
            "calibration",
            {
                "valid_from": {"knowledge": "unknown"},
                "valid_until": {"knowledge": "unknown"},
                "performed": at(CLOCK, 5),
            },
            {
                "knowledge": "known",
                "value": {"end": at(CLOCK, 5), "start": {"domain_id": CLOCK, "ticks": 5}},
            },
        ),
    ],
)
def test_world_time_restates_the_end_field(
    kind: str, fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert world_json(kind, fields) == expected


def test_the_rows_do_not_depend_on_line_or_table_order() -> None:
    machine_record = record("machine", 1, identifiers=[known("serial", "AUV-3")])
    made = [
        record("run", i, machine=known("serial", "AUV-3"), logical_id={"knowledge": "unknown"})
        for i in range(2, 9)
    ]
    streams = [record("stream", 20 + i, run=r["id"]) for i, r in enumerate(made)]
    every = [machine_record, *made, *streams]
    expected = rows(*every)
    shuffled = list(every)
    random.Random(7).shuffle(shuffled)
    assert rows(*shuffled) == expected
    assert [m.thread_id for m in expected.members] == sorted(m.thread_id for m in expected.members)


def test_a_key_outside_the_contract_raises_for_registration_to_refuse() -> None:
    hostile = record("machine", 1, identifiers=[known("Not A Token", "x")])
    with pytest.raises(ValueError):
        rows(hostile)
