"""The snapshot reader: real graph documents read; malformed ones refused with a pointer."""

import copy
import json
from typing import Any

import pytest

from deploy_pack_support import CONTRACTS, configuration
from neptune_deploy.packs import PackError, load_snapshot, read_snapshot, snapshot_id

GOLDEN_GRAPH = CONTRACTS / "graph-schema" / "v1.2.0" / "golden" / "graph.json"


def _graph() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(GOLDEN_GRAPH.read_text(encoding="utf-8"))
    return document


def test_reads_memory_published_golden_graph() -> None:
    snap = load_snapshot(GOLDEN_GRAPH.read_bytes())
    document = _graph()
    assert snap.head == document["head"]
    assert snap.generation == document["generation"]
    assert len(snap.claims) == len(document["claims"])
    assert snap.cardinality["recorded_by"] == "one"
    # Current versions only, one per id, ordered.
    assert [c.id for c in snap.current] == sorted(
        {c["id"] for c in document["claims"] if c["superseded_at"] == "open"}
    )
    # Claims keep their JSON exactly.
    assert {c.id: c.raw for c in snap.claims}[document["claims"][0]["id"]] == document["claims"][0]


def test_snapshot_id_is_the_canonical_content_hash() -> None:
    document = _graph()
    compact = json.dumps(document, separators=(",", ":")).encode()
    pretty = json.dumps(document, indent=4, sort_keys=False).encode()
    assert load_snapshot(compact).id == load_snapshot(pretty).id == snapshot_id(document)
    assert load_snapshot(compact).id.startswith("snapshot:sha256:")
    changed = copy.deepcopy(document)
    changed["claims"][0]["recorded_at"] = 0
    assert snapshot_id(changed) != snapshot_id(document)


def test_superseded_versions_are_not_current() -> None:
    snap = configuration()
    superseded = [c for c in snap.claims if not c.current]
    assert len(superseded) == 1
    assert superseded[0].id not in {c.id for c in snap.current}
    assert snap.versions[superseded[0].id] is superseded[0]


def _mutate(path: list[Any], value: Any) -> dict[str, Any]:
    document = _graph()
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return document


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value", "code", "pointer"),
    [
        (["kind"], "memory.other", "snapshot_malformed", "/kind"),
        (["graph_schema_version"], 2, "snapshot_unsupported", "/graph_schema_version"),
        (["graph_schema_version"], True, "snapshot_unsupported", "/graph_schema_version"),
        (["graph_schema_version"], 1.0, "snapshot_unsupported", "/graph_schema_version"),
        (["claims", 0, "predicate"], "\ud800", "snapshot_malformed", "/claims/0/predicate"),
        (["head"], -1, "snapshot_malformed", "/head"),
        (["head"], 2**63, "snapshot_malformed", "/head"),
        (["generation"], "md5:00", "snapshot_malformed", "/generation"),
        (["claims", 0, "id"], "claim:sha1:00", "snapshot_malformed", "/claims/0/id"),
        (["claims", 0, "predicate"], "Recorded By", "snapshot_malformed", "/claims/0/predicate"),
        (
            ["claims", 0, "assertion_kind"],
            "guessed",
            "snapshot_malformed",
            "/claims/0/assertion_kind",
        ),
        (
            ["claims", 0, "subject", "kind"],
            "record",
            "snapshot_malformed",
            "/claims/0/subject/kind",
        ),
        (
            ["claims", 0, "subject", "node_id"],
            "",
            "snapshot_malformed",
            "/claims/0/subject/node_id",
        ),
        (
            ["claims", 0, "valid", "start", "ticks"],
            1.5,
            "snapshot_malformed",
            "/claims/0/valid/start/ticks",
        ),
        (["claims", 0, "valid", "end"], "forever", "snapshot_malformed", "/claims/0/valid/end"),
        (["claims", 0, "object"], {"kind": "blank"}, "snapshot_malformed", "/claims/0/object/kind"),
        (
            ["claims", 0, "provenance", "evidence"],
            [],
            "snapshot_malformed",
            "/claims/0/provenance/evidence",
        ),
        (
            ["claims", 0, "provenance", "model"],
            _DELETE,
            "snapshot_malformed",
            "/claims/0/provenance",
        ),
        (
            ["claims", 0, "provenance", "records", 0],
            "rec:x",
            "snapshot_malformed",
            "/claims/0/provenance/records/0",
        ),
        (["claims", 0, "recorded_at"], 99, "snapshot_malformed", "/claims/0"),
        (["claims", 0, "superseded_at"], "never", "snapshot_malformed", "/claims/0/superseded_at"),
        (["claims", 0, "extra"], 1, "snapshot_malformed", "/claims/0"),
        (
            ["claims", 0, "provenance", "evidence", 0, "locator", 0],
            "row 3",
            "snapshot_malformed",
            "/claims/0/provenance/evidence/0/locator/0",
        ),
        (["findings", 0, "id"], "finding:1", "snapshot_malformed", "/findings/0/id"),
        (
            ["resolver_config", "vocabulary", "predicates", 0, "cardinality"],
            "some",
            "snapshot_malformed",
            "/resolver_config/vocabulary/predicates/0/cardinality",
        ),
    ],
)
def test_malformed_snapshots_are_refused_with_a_pointer(
    path: list[Any], value: Any, code: str, pointer: str
) -> None:
    with pytest.raises(PackError) as caught:
        read_snapshot(_mutate(path, value))
    assert caught.value.code == code
    assert caught.value.pointer == pointer


def test_an_observed_claim_naming_a_model_is_refused() -> None:
    document = _graph()
    stated = next(c for c in document["claims"] if c["assertion_kind"] != "inferred")
    stated["provenance"]["model"] = {"model_id": "m", "model_version": "1"}
    with pytest.raises(PackError, match="names its model"):
        read_snapshot(document)


def test_a_repeated_predicate_in_the_vocabulary_is_refused() -> None:
    document = _graph()
    predicates = document["resolver_config"]["vocabulary"]["predicates"]
    predicates.append(copy.deepcopy(predicates[0]))
    with pytest.raises(PackError, match="declared twice"):
        read_snapshot(document)


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "not JSON"),
        (b"\xff\xfe", "not JSON"),
        (b'{"a": 1, "a": 2}', "duplicate object key"),
        (b'{"a": NaN}', "NaN is not JSON"),
        (b"[" * 100_000 + b"]" * 100_000, "nested too deeply"),
        (b"[]", "expected an object"),
        (b'{"kind": "memory.graph"}', "missing"),
    ],
)
def test_hostile_bytes_are_refused(data: bytes, message: str) -> None:
    with pytest.raises(PackError, match=message) as caught:
        load_snapshot(data)
    assert caught.value.code == "snapshot_malformed"


def test_an_oversized_snapshot_is_refused_before_parsing() -> None:
    with pytest.raises(PackError, match="byte limit"):
        load_snapshot(GOLDEN_GRAPH.read_bytes(), max_bytes=100)


def test_a_lone_surrogate_cannot_name_a_snapshot() -> None:
    document = _graph()
    document["claims"][0]["provenance"]["consolidator_version"] = "\ud800"
    with pytest.raises(PackError, match="canonical JSON"):
        read_snapshot(document)


def test_a_newer_minor_node_type_still_reads() -> None:
    """graph-schema minors add node types (1.6.0 adds ``event``); a token reads."""
    document = _graph()
    document["claims"][0]["subject"]["node_type"] = "event"
    assert read_snapshot(document).claims[0].subject.node_type == "event"
