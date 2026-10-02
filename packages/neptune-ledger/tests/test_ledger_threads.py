"""``thread``, ``threads_of`` and ``lineage`` on the real catalog beyond the contract (MVL-92).

ADR 0003 §6's worked examples, each on a different robot: (A) a legged robot's URDF re-parsed by
adapter 2.0.0 beside 1.0.0, (B) a manipulator's hand-eye calibration replaced by a new revision,
(C) a mobile robot's run recorded as two bags registered apart, which is also a thread on two
clocks with no mapping. Then Ambiguous candidates, the other thread kinds over the compiler's
worked examples, an unregistered upstream, requests outside the contract, and determinism.
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from conftest import new_database
from ledger_thread_packages import (
    Record,
    edit,
    files,
    known,
    logical,
    new_transform,
    rebase,
    records,
    reparsed,
    resourced,
    stated,
    subset,
    timestamp,
)
from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
)
from neptune.store.package import write_package
from neptune_ledger.api import codec
from neptune_ledger.api.types import (
    AsRegisteredBy,
    ClockMerge,
    DeclaredKey,
    EvidenceAnchor,
    History,
    LatestTransform,
    Pinned,
    RecordRef,
    RevisionEdge,
    Thread,
    ThreadKey,
    ThreadPreference,
    TransformInfo,
    UnresolvedMember,
    WorldTime,
)
from neptune_ledger.catalog.registry import PostgresCatalog
from neptune_ledger.contract_tests.examples import EXAMPLES, materialise
from test_ledger_registration import dump, fresh

WORLD: Any = cast("Any", Knowledge)[WorldTime]
Conn = psycopg.Connection[tuple[object, ...]]
UNKNOWN_MAPPING = "rec:sha256:" + "4" * 64


@pytest.fixture
def catalog(pg_uri: str) -> Iterator[PostgresCatalog]:
    with fresh(pg_uri) as made:
        yield made


def register(catalog: PostgresCatalog, rows: list[Record], root: Path) -> str:
    package_id = write_package(root, files(rows))
    result = catalog.register(root)
    assert result.outcome == "registered", result.findings
    return package_id


def anchor(record: Record) -> EvidenceAnchor:
    evidence = record["provenance"]["evidence"]
    return EvidenceAnchor(evidence["source"], tuple(evidence["locator"]))


def one(rows: list[Record], kind: str) -> Record:
    (found,) = [r for r in rows if r["kind"] == kind]
    return found


def ids(rows: list[Record], kind: str) -> list[str]:
    return [r["id"] for r in rows if r["kind"] == kind]


def entries(thread: Thread) -> list[tuple[str, tuple[str, ...]]]:
    return [(e.record_id, e.packages) for p in thread.partitions for e in p.entries]


def validate(document: object) -> None:
    import jsonschema

    schema = codec.catalog_schema()
    validator = jsonschema.Draft202012Validator(
        {**schema, "$ref": f"#/$defs/{type(document).__name__}"}
    )
    errors = sorted(validator.iter_errors(codec.to_json(document)), key=str)
    assert not errors, errors[0].message


# --- A: a legged robot's URDF, re-parsed by adapter 2.0.0 beside 1.0.0 -------------------------


@pytest.fixture
def urdf(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    v1 = subset("quadruped", "urdf")
    v2 = reparsed(v1, "2.0.0")
    return {
        "v1": v1,
        "v2": v2,
        "p1": register(catalog, v1, tmp_path / "p1"),
        "p2": register(catalog, v2, tmp_path / "p2"),
        "t1": one(v1, "transform_record")["id"],
        "t2": one(v2, "transform_record")["id"],
        "key": ThreadKey("configuration", anchor(one(v1, "hardware_configuration"))),
    }


def test_urdf_siblings_share_one_anchored_configuration_thread(
    catalog: PostgresCatalog, urdf: dict[str, Any]
) -> None:
    v1, v2 = urdf["v1"], urdf["v2"]
    assert anchor(one(v2, "hardware_configuration")) == urdf["key"].key, "the anchor is stable"
    history = catalog.thread(urdf["key"], "world", History())
    validate(history)
    assert history.findings == ()
    assert [p.kind for p in history.partitions] == ["untimed"], "a URDF states no world time"

    def group(rows: list[Record], package: str) -> list[tuple[str, tuple[str, ...]]]:
        members = ids(rows, "hardware_configuration") + ids(rows, "hardware_component")
        return [(r, (package,)) for r in sorted(members, key=str.encode)]

    assert entries(history) == group(v1, urdf["p1"]) + group(v2, urdf["p2"])
    roles = {e.record_id: e.roles for e in history.partitions[0].entries}
    assert {roles[r] for r in ids(v1, "hardware_component")} == {("part_of",)}
    assert roles[one(v2, "hardware_configuration")["id"]] == ("subject",)
    source = one(v1, "source_artifact")["content_id"]
    assert [(s.kind, s.source, s.transforms) for s in history.lineage_sets] == [
        ("hardware_component", source, tuple(sorted((urdf["t1"], urdf["t2"])))),
        ("hardware_configuration", source, tuple(sorted((urdf["t1"], urdf["t2"])))),
    ]
    assert {s.resolution for s in history.lineage_sets} == {NotApplicable()}


@pytest.mark.parametrize(
    ("preference", "version"),
    [("latest", "v2"), ("pinned", "v1"), ("as_registered_by", "v1"), ("as_registered_by", "v2")],
)
def test_urdf_current_views_select_one_transform_per_lineage_set(
    catalog: PostgresCatalog, urdf: dict[str, Any], preference: str, version: str
) -> None:
    options: dict[str, ThreadPreference] = {
        "latest": LatestTransform(),
        "pinned": Pinned(urdf["t1"]),
        "as_registered_by": AsRegisteredBy(urdf["p" + version[-1]]),
    }
    chosen = options[preference]
    thread = catalog.thread(urdf["key"], "world", chosen)
    validate(thread)
    transform = urdf["t" + version[-1]]
    assert {s.resolution for s in thread.lineage_sets} == {Known(transform)}
    rows = urdf[version]
    expected = ids(rows, "hardware_configuration") + ids(rows, "hardware_component")
    assert sorted(r for r, _ in entries(thread)) == sorted(expected)
    assert {e.transform_id for p in thread.partitions for e in p.entries} == {transform}


def test_a_urdf_names_no_machine_and_has_lineage_siblings(
    catalog: PostgresCatalog, urdf: dict[str, Any]
) -> None:
    h1, h2 = one(urdf["v1"], "hardware_configuration"), one(urdf["v2"], "hardware_configuration")
    memberships = catalog.threads_of(h1["id"]).memberships
    assert [(m.key, m.roles) for m in memberships] == [(urdf["key"], ("subject",))]
    graph = catalog.lineage(h2["id"])
    validate(graph)
    assert graph.siblings == (RecordRef(urdf["p1"], "hardware_configuration", h1["id"], 1),)
    assert [n.transform_id for n in graph.nodes] == [urdf["t2"]]
    stated_transform = one(urdf["v2"], "transform_record")
    assert graph.nodes[0].transform == Known(
        TransformInfo("urdf", "2.0.0", stated_transform["config_hash"], {})
    )


# --- B: a manipulator's hand-eye calibration replaced by a new revision ------------------------


def _calibrated(rows: list[Record], ticks: int) -> list[Record]:
    """The hand-eye output with a stated machine and a Known valid_from on a document clock."""
    transform = one(rows, "transform_record")
    source = one(rows, "source_artifact")["content_id"]
    domain = {
        **next(r for r in records("manipulator") if r["kind"] == "timestamp_domain"),
        "field": "valid_from",
        "provenance": {
            "assertion_kind": "observed",
            "evidence": {
                "locator": [
                    {"kind": "byte_range", "length": 348, "offset": 0},
                    {"kind": "json_pointer", "pointer": "/valid_from"},
                ],
                "source": source,
            },
            "transform": transform["id"],
        },
    }

    def calibration(record: Record) -> Record:
        return {
            **record,
            "machine": known(logical("serial", "UR5E-0042"), stated(record)),
            "valid_from": known(timestamp(domain["id"], ticks)),
        }

    return rebase([*edit(rows, "calibration", calibration), domain])


@pytest.fixture
def replaced(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    first = _calibrated(subset("manipulator", "handeye"), 1714000000)
    second = resourced(first, b"handeye: second calibration\n", supersede=True)
    second = edit(
        second,
        "calibration",
        lambda r: {
            **r,
            "valid_from": {
                **r["valid_from"],
                "value": {**r["valid_from"]["value"], "ticks": 1719000000},
            },
        },
    )
    return {
        "first": first,
        "second": second,
        "p3": register(catalog, first, tmp_path / "p3"),
        "p4": register(catalog, second, tmp_path / "p4"),
        "key": ThreadKey("machine", DeclaredKey("serial", "UR5E-0042")),
    }


def test_a_replaced_calibration_is_two_lineage_sets_on_two_clocks(
    catalog: PostgresCatalog, replaced: dict[str, Any]
) -> None:
    first, second = replaced["first"], replaced["second"]
    c1, c2 = one(first, "calibration"), one(second, "calibration")
    history = catalog.thread(replaced["key"], "world", History())
    validate(history)
    assert [(p.kind, p.clock_key) for p in history.partitions] == [
        ("clock", c1["valid_from"]["value"]["domain_id"]),
        ("clock", c2["valid_from"]["value"]["domain_id"]),
    ], "two sources never share a clock; the partitions follow registration order"
    assert entries(history) == [(c1["id"], (replaced["p3"],)), (c2["id"], (replaced["p4"],))]
    assert history.partitions[0].entries[0].roles == ("cites",)
    b1, b2 = (one(rows, "source_artifact")["content_id"] for rows in (first, second))
    r1, r2 = (one(rows, "source_revision")["id"] for rows in (first, second))
    assert history.revisions == (RevisionEdge("calibration", b2, b1, r2, r1),)
    current = catalog.thread(replaced["key"], "world", LatestTransform())
    assert entries(current) == entries(history), "a revision is new evidence, never hidden"
    assert current.revisions == history.revisions
    for record in (c1, c2):
        kinds = {m.key.kind for m in catalog.threads_of(record["id"]).memberships}
        assert kinds == {"machine", "configuration"}, "each revision opens its own configuration"


def test_a_merge_names_only_clocks_and_mappings_the_catalog_holds(
    catalog: PostgresCatalog, replaced: dict[str, Any]
) -> None:
    clock = one(replaced["first"], "calibration")["valid_from"]["value"]["domain_id"]
    named = ClockMerge(clock, (UNKNOWN_MAPPING,))
    thread = catalog.thread(replaced["key"], "world", History(), merge=named)
    validate(thread)
    assert [(f.code, f.subject) for f in thread.findings] == [("unknown_mapping", UNKNOWN_MAPPING)]
    assert (thread.partitions, thread.lineage_sets, thread.merge) == ((), (), named)
    elsewhere = ClockMerge("rec:sha256:" + "5" * 64, (UNKNOWN_MAPPING,))
    thread = catalog.thread(replaced["key"], "world", History(), merge=elsewhere)
    assert [f.code for f in thread.findings] == ["unknown_clock", "unknown_mapping"]
    thread = catalog.thread(replaced["key"], "transaction", History(), merge=named)
    assert [f.code for f in thread.findings] == ["invalid_request"]


# --- C: a mobile robot's run recorded as two bags, registered apart ----------------------------


def _declared_run(rows: list[Record]) -> list[Record]:
    """The bag's run declared by a manifest as night-42 on amr-11, its streams timed."""
    run = one(rows, "run")
    first = run["first"]["value"]

    def declare(record: Record) -> Record:
        return {
            **record,
            "logical_id": known(logical("manifest", "night-42"), stated(record)),
            "machine": known(logical("manifest", "amr-11"), stated(record)),
        }

    def timed(record: Record) -> Record:
        start = first["ticks"] + (1000 if record["id"] < run["id"] else 0)  # one ties the run
        return {
            **record,
            "first": known(timestamp(first["domain_id"], start)),
            "last": known(timestamp(first["domain_id"], start + 9000)),
        }

    return edit(edit(rows, "run", declare), "stream", timed)


@pytest.fixture
def split_run(catalog: PostgresCatalog, tmp_path: Path) -> dict[str, Any]:
    part0 = _declared_run(subset("mobile_robot", "rosbag1"))
    part1 = resourced(part0, b"part1 of night-42\n", {"kind": "local", "path": "part1.bag"})
    return {
        "part0": part0,
        "part1": part1,
        "p5": register(catalog, part0, tmp_path / "p5"),
        "p6": register(catalog, part1, tmp_path / "p6"),
        "key": ThreadKey("run", DeclaredKey("manifest", "night-42")),
    }


def _world_key(record: Record) -> tuple[int, tuple[int, int], bytes]:
    return (
        record["first"]["value"]["ticks"],
        (0, record["last"]["value"]["ticks"]),
        record["id"].encode(),
    )


def test_a_run_spanning_two_packages_is_one_thread_on_two_clocks(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    part0, part1 = split_run["part0"], split_run["part1"]
    thread = catalog.thread(split_run["key"], "world", History())
    validate(thread)
    assert thread.findings == () and thread.merge is None
    clocks = [one(rows, "run")["first"]["value"]["domain_id"] for rows in (part0, part1)]
    assert clocks[0] != clocks[1], "two bags, two log_time clocks"
    assert [(p.kind, p.clock_key) for p in thread.partitions] == [
        ("clock", clocks[0]),
        ("clock", clocks[1]),
    ], "no mapping is named, so the clocks stay apart: never interleaved"
    for partition, rows, package in zip(
        thread.partitions, (part0, part1), (split_run["p5"], split_run["p6"]), strict=True
    ):
        timed = [r for r in rows if r["kind"] in ("run", "stream")]
        expected = [r["id"] for r in sorted(timed, key=_world_key)]
        assert [e.record_id for e in partition.entries] == expected
        assert {e.packages for e in partition.entries} == {(package,)}
        assert {e.record_id: e.roles for e in partition.entries}[one(rows, "run")["id"]] == (
            "subject",
        )
        assert {e.roles for e in partition.entries if e.kind == "stream"} == {("part_of",)}
    assert len(thread.lineage_sets) == 4, "(run, c0), (stream, c0), (run, c1), (stream, c1)"
    transaction = catalog.thread(split_run["key"], "transaction", History())
    assert [p.kind for p in transaction.partitions] == ["transaction"]
    assert [e.registration_key.tx_seq for e in transaction.partitions[0].entries] == sorted(
        e.registration_key.tx_seq for e in transaction.partitions[0].entries
    )


def test_as_registered_by_one_bag_states_the_missing_half(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    part1 = split_run["part1"]
    thread = catalog.thread(split_run["key"], "world", AsRegisteredBy(split_run["p6"]))
    validate(thread)
    c0, c1 = (one(rows, "source_artifact")["content_id"] for rows in (split_run["part0"], part1))
    t5 = one(part1, "transform_record")["id"]
    assert {(s.kind, s.source): s.resolution for s in thread.lineage_sets} == {
        ("run", c0): NotCovered(),
        ("stream", c0): NotCovered(),
        ("run", c1): Known(t5),
        ("stream", c1): Known(t5),
    }
    assert sorted(r for r, _ in entries(thread)) == sorted(ids(part1, "run") + ids(part1, "stream"))


def test_the_run_cites_its_machine_and_each_stream_opens_its_own_thread(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    machine = catalog.thread(
        ThreadKey("machine", DeclaredKey("manifest", "amr-11")), "world", History()
    )
    runs = [one(rows, "run")["id"] for rows in (split_run["part0"], split_run["part1"])]
    assert [r for r, _ in entries(machine)] == runs
    for stream in [r for r in split_run["part0"] if r["kind"] == "stream"]:
        own = catalog.thread(ThreadKey("stream", anchor(stream)), "world", History())
        assert [r for r, _ in entries(own)] == [stream["id"]]


def test_history_only_grows_as_packages_are_added(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    """ADR 0003 P5 on the catalog: history at an earlier point is a prefix-free subsequence."""
    earlier = catalog.thread(split_run["key"], "world", History(), as_of=1)
    later = catalog.thread(split_run["key"], "world", History())
    assert isinstance(earlier.as_of, Known) and earlier.as_of.value.tx_seq == 1
    small, big = entries(earlier), entries(later)
    assert small and set(small) < set(big)
    assert [x for x in big if x in small] == small


# --- Ambiguous candidates, and the other thread kinds --------------------------------------------


def test_an_ambiguous_machine_lists_the_run_unresolved_in_each_candidate(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    rows = records("drone")
    rows = [r for r in rows if r["kind"] != "ingest_finding"]
    candidates = [logical("px4.sys_uuid", "A-1"), logical("px4.sys_uuid", "B-2")]

    def ambiguous(record: Record) -> Record:
        return {
            **record,
            "machine": {"candidates": [{"value": c} for c in candidates], "knowledge": "ambiguous"},
        }

    rows = edit(rows, "run", ambiguous)
    package = register(catalog, rows, tmp_path / "ambiguous")
    run = one(rows, "run")
    for candidate in candidates:
        key = ThreadKey("machine", DeclaredKey(candidate["namespace"], candidate["value"]))
        thread = catalog.thread(key, "world", History())
        validate(thread)
        assert thread.partitions == (), "a candidate is never a member"
        assert thread.unresolved == (UnresolvedMember(package, run["id"], "run", "/machine"),)
    found = catalog.threads_of(run["id"])
    assert [u.pointer for u in found.unresolved] == ["/machine", "/machine"]
    assert {m.key.kind for m in found.memberships} == {"run"}, "anchored: no declared run id"


def test_sensor_site_and_software_threads_over_the_worked_examples(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    packages = {name: materialise(name, tmp_path / name) for name in EXAMPLES}
    for package in packages.values():
        assert catalog.register(package.root).outcome == "registered"
    (image,) = packages["mobile_robot"].records("image")
    serial = image["capture"]["device_identifiers"][0]["value"]
    sensor = catalog.thread(
        ThreadKey("sensor", DeclaredKey(serial["namespace"], serial["value"])), "world", History()
    )
    assert [(e.record_id, e.roles) for p in sensor.partitions for e in p.entries] == [
        (image["id"], ("cites",))
    ]
    for site in packages["mobile_robot"].records("site"):
        identifier = site["identifiers"][0]["value"]
        key = ThreadKey("site", DeclaredKey(identifier["namespace"], identifier["value"]))
        thread = catalog.thread(key, "world", History())
        assert next(e.record_id for p in thread.partitions for e in p.entries) == site["id"]
    (software,) = packages["drone"].records("software_configuration")
    commit = next(i["commit"]["value"] for i in software["software"] if "value" in i["commit"])
    key = ThreadKey("software_version", DeclaredKey("git_commit", commit["sha"]))
    thread = catalog.thread(key, "world", LatestTransform())
    validate(thread)
    assert [(e.record_id, e.roles) for p in thread.partitions for e in p.entries] == [
        (software["id"], ("cites",))
    ]
    for kind in ("person", "zone", "task"):
        reserved = catalog.thread(ThreadKey(kind, DeclaredKey("x", "y")), "world", History())
        assert (reserved.partitions, reserved.findings) == ((), ()), "reserved kinds are empty"


# --- lineage beyond the contract ----------------------------------------------------------------


def test_an_unregistered_upstream_is_a_node_without_fields(
    catalog: PostgresCatalog, tmp_path: Path
) -> None:
    """A transform whose upstream no package holds: the edge is kept, the node is NotCovered,
    and its chain compares with nothing, so latest_transform cannot pick between two of them."""
    upstream = "rec:sha256:" + "6" * 64
    rows = [r for r in records("drone") if r["kind"] != "ingest_finding"]
    old = one(rows, "transform_record")
    made = []
    for version in ("1.0.0", "2.0.0"):
        new = {**new_transform({**old, "upstream": [upstream]}, version)}
        kept = [r for r in rows if r["kind"] != "transform_record"]
        made.append(rebase([new, *kept], {old["id"]: new["id"]}))
        register(catalog, made[-1], tmp_path / version)
    run = one(made[1], "run")
    graph = catalog.lineage(run["id"])
    validate(graph)
    t2 = one(made[1], "transform_record")["id"]
    assert [(n.transform_id, n.transform == NotCovered()) for n in graph.nodes] == sorted(
        [(t2, False), (upstream, True)], key=lambda n: n[0].encode()
    )
    assert [(e.transform_id, e.upstream_id, e.position) for e in graph.edges] == [(t2, upstream, 0)]
    machine = next(r for r in made[1] if r["kind"] == "machine")["identifiers"][0]["value"]
    key = ThreadKey("machine", DeclaredKey(machine["namespace"], machine["value"]))
    thread = catalog.thread(key, "world", LatestTransform())
    both = tuple(sorted((one(m, "transform_record")["id"] for m in made), key=str.encode))
    assert {s.resolution for s in thread.lineage_sets} == {
        Ambiguous(tuple(Candidate(t) for t in both))
    }


def test_a_record_without_a_transform_has_no_dag(catalog: PostgresCatalog, tmp_path: Path) -> None:
    package = materialise("drone", tmp_path / "drone")
    catalog.register(package.root)
    (artifact,) = package.records("source_artifact")
    graph = catalog.lineage(artifact["content_id"])
    validate(graph)
    assert (graph.status, graph.kind, graph.transform_id) == (
        "found",
        Known("source_artifact"),
        NotApplicable(),
    )
    assert (graph.nodes, graph.edges, graph.siblings) == ((), (), ())


# --- requests outside the contract --------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        {"order": "chronological"},
        {"preference": "latest"},
        {"preference": Pinned("not-a-transform")},
        {"as_of": 0},
        {"as_of": True},
        {"key": ThreadKey("machine", DeclaredKey("Serial", "x"))},  # not a token namespace
        {"key": ThreadKey("galaxy", DeclaredKey("serial", "x"))},  # type: ignore[arg-type]
    ],
)
def test_a_thread_request_outside_the_contract_is_invalid(
    catalog: PostgresCatalog, tmp_path: Path, call: dict[str, Any]
) -> None:
    catalog.register(materialise("drone", tmp_path / "drone").root)
    args: dict[str, Any] = {
        "key": ThreadKey("machine", DeclaredKey("serial", "x")),
        "order": "world",
        "preference": History(),
        **call,
    }
    as_of = args.pop("as_of", None)
    thread = catalog.thread(args["key"], args["order"], args["preference"], as_of=as_of)
    assert [f.code for f in thread.findings] == ["invalid_request"]
    assert (thread.partitions, thread.lineage_sets, thread.unresolved) == ((), (), ())
    assert thread.thread_id.startswith("sha256:")
    if "key" not in call and "order" not in call:
        validate(thread)  # the rejection itself is inside the contract, and encodes
        assert thread.preference in (None, History())


def test_thread_rows_copy_their_records_world_time(
    catalog: PostgresCatalog, split_run: dict[str, Any], pg_uri: str
) -> None:
    """thread_member's interval and clock are the record's own (ADR 0010 §1), and its world
    JSON's start and closed end agree with them."""
    with psycopg.connect(pg_uri) as conn:
        rows = conn.execute(
            "SELECT m.world_clock IS NOT DISTINCT FROM r.world_clock"
            "   AND m.world_first IS NOT DISTINCT FROM r.world_first"
            "   AND m.world_last IS NOT DISTINCT FROM r.world_last"
            "   AND m.transform_id = r.transform_id"
            "   AND m.source_content_id = r.source_content_id,"
            "  m.world, m.world_clock, m.world_first, m.world_last"
            " FROM tenant_acme.thread_member m JOIN tenant_acme.record r"
            "  USING (tenant_id, kind, record_id, package_id)"
        ).fetchall()
    assert rows and all(row[0] for row in rows)
    for _, world, clock, first, last in rows:
        decoded = codec.decode_as(WORLD, json.loads(str(world)))
        if clock is None:
            assert not isinstance(decoded, Known)
        else:
            assert isinstance(decoded, Known)
            value = decoded.value
            assert (value.clock, value.start.ticks, value.closed_end) == (clock, first, last)


def test_a_rejected_merge_or_preference_is_not_echoed(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    bad_merge = ClockMerge("not-a-clock", ())
    thread = catalog.thread(split_run["key"], "world", Pinned("nope"), merge=bad_merge, as_of=99)
    validate(thread)
    assert [f.code for f in thread.findings] == ["as_of_out_of_range"]
    assert (thread.preference, thread.merge) == (None, None)


@pytest.mark.parametrize("call", ["threads_of", "lineage"])
def test_record_requests_outside_the_contract(
    catalog: PostgresCatalog, tmp_path: Path, call: str
) -> None:
    catalog.register(materialise("drone", tmp_path / "drone").root)
    method = getattr(catalog, call)
    for record_id, as_of, code in (
        ("", None, "invalid_request"),
        ("rec:sha256:" + "1" * 64, 0, "invalid_request"),
        ("rec:sha256:" + "1" * 64, 99, "as_of_out_of_range"),
        ("rec:sha256:" + "1" * 64, None, "unknown_record"),
    ):
        result = method(record_id, as_of=as_of)
        validate(result)
        assert result.status == "unknown_record"
        assert [f.code for f in result.findings] == [code]


def test_reads_on_an_empty_catalog_have_no_point(catalog: PostgresCatalog) -> None:
    key = ThreadKey("machine", DeclaredKey("serial", "x"))
    thread = catalog.thread(key, "world", History())
    assert (thread.as_of, thread.partitions, thread.findings) == (NotCovered(), (), ())
    assert catalog.lineage("rec:sha256:" + "1" * 64).as_of == NotCovered()


# --- determinism ---------------------------------------------------------------------------------


def test_thread_and_lineage_calls_give_identical_bytes(
    catalog: PostgresCatalog, split_run: dict[str, Any]
) -> None:
    """The thread and lineage half of the contract's determinism test (whose query half waits
    for MVL-98), over a thread with two clocks and four lineage sets."""
    run = one(split_run["part0"], "run")
    calls: list[Callable[[], object]] = [
        lambda: catalog.thread(split_run["key"], "world", History()),
        lambda: catalog.thread(split_run["key"], "transaction", LatestTransform()),
        lambda: catalog.thread(split_run["key"], "world", AsRegisteredBy(split_run["p5"])),
        lambda: catalog.threads_of(run["id"]),
        lambda: catalog.lineage(run["id"]),
    ]
    for make in calls:
        assert codec.dumps(make()) == codec.dumps(make())


def test_thread_rows_do_not_depend_on_registration_order(pg_server: str, tmp_path: Path) -> None:
    """Every thread index column but the registration key is a function of the package."""
    packages = [materialise(name, tmp_path / name) for name in EXAMPLES]
    dumps = []
    for order in (packages, packages[::-1]):
        uri = new_database(pg_server)
        with fresh(uri) as catalog:
            for package in order:
                assert catalog.register(package.root).outcome == "registered"
        with psycopg.connect(uri) as conn:
            full = dump(conn, "tenant_acme", ("registration_key",))
        dumps.append({t: full[t] for t in ("thread", "thread_member", "thread_unresolved")})
    assert dumps[0] == dumps[1]
    assert len(dumps[0]["thread_member"]) > 20
