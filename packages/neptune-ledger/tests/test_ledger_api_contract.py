"""The catalog API contract: schema, codec, thread ids, Arrow form, stub, goldens, registry."""

import hashlib
import json
import re
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest

from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import (
    Ambiguous,
    AssertionKind,
    Candidate,
    Known,
    KnownAbsent,
    NotCovered,
    Unknown,
)
from neptune_ledger import api
from neptune_ledger.api import (
    CATALOG_API_VERSION,
    QUERY_RESULT_SCHEMA,
    REQUEST_TYPES,
    RESPONSE_TYPES,
    AsRegisteredBy,
    CatalogApi,
    CodecError,
    DeclaredKey,
    EvidenceAnchor,
    History,
    LatestTransform,
    LineageRequest,
    Pinned,
    QueryMeta,
    QueryRequest,
    QueryRow,
    QuerySpec,
    RegisterRequest,
    Registration,
    ResolveRequest,
    StubCatalog,
    ThreadKey,
    ThreadRequest,
    ThreadsOfRequest,
    TransactionKey,
    VerifyRequest,
    catalog_schema,
    from_json,
    to_json,
)
from neptune_ledger.contract_tests import examples, goldens

PACKAGE = Path(__file__).resolve().parents[1]
REPO = PACKAGE.parents[1]
CONTRACT = REPO / "contracts" / "catalog-api"
ANCHOR = EvidenceAnchor("sha256:" + "a" * 64, ({"kind": "byte_range", "length": 4, "offset": 0},))
DECLARED = DeclaredKey("serial", "QX-0042")


@pytest.fixture(scope="module")
def golden_documents() -> dict[str, dict[str, Any]]:
    return goldens.goldens()


class _Stated:
    """A minimal stated grounding: enough for KnownAbsent, which the codec must still refuse."""

    assertion_kind = AssertionKind.STATED

    def to_json(self) -> JsonObject:
        return {"assertion_kind": "stated"}


def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator({**catalog_schema(), "$ref": f"#/$defs/{name}"})


# --- schema ------------------------------------------------------------------------------------


def test_schema_is_draft_2020_12_and_names_every_record() -> None:
    schema = catalog_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$id"] == "urn:neptune:catalog-api:1"
    for cls in (*REQUEST_TYPES, *RESPONSE_TYPES):
        assert cls.__name__ in schema["$defs"]
    assert catalog_schema() == schema, "the export is deterministic"


def test_schema_requires_every_field_except_optional_ones() -> None:
    thread = catalog_schema()["$defs"]["ThreadRequest"]
    assert thread["required"] == ["key", "order", "preference"]
    assert thread["additionalProperties"] is False


def test_api_version_accepts_any_version_of_the_major() -> None:
    pattern = catalog_schema()["$defs"]["ApiVersion"]["pattern"]
    assert re.search(pattern, CATALOG_API_VERSION)
    assert re.search(pattern, "1.7.12")
    assert not re.search(pattern, "2.0.0")


# --- goldens -----------------------------------------------------------------------------------


def test_goldens_validate_and_round_trip(golden_documents: dict[str, dict[str, Any]]) -> None:
    for name, entry in golden_documents.items():
        record = entry["target"].removeprefix("#/$defs/")
        errors = list(_validator(record).iter_errors(entry["value"]))
        assert not errors, f"{name}: {errors[0].message}"
        cls = getattr(api, record)
        assert to_json(from_json(cls, entry["value"])) == entry["value"], name


def test_goldens_cover_every_call_and_the_error_cases(
    golden_documents: dict[str, dict[str, Any]],
) -> None:
    targets = {entry["target"].removeprefix("#/$defs/") for entry in golden_documents.values()}
    assert {
        "RegisterRequest",
        "Registration",
        "VerifyReport",
        "ResolveRequest",
        "Resolution",
        "ThreadRequest",
        "Thread",
        "LineageGraph",
        "QuerySpec",
        "QueryRow",
        "QueryMeta",
    } <= targets
    robots = {name.split(".", 1)[0] for name in golden_documents}
    assert set(examples.EXAMPLES) <= robots
    assert {
        "error.registration_refused.json",
        "error.verify_unknown_package.json",
        "error.resolution_unresolvable.json",
    } <= set(golden_documents)


def test_golden_generator_is_deterministic(golden_documents: dict[str, dict[str, Any]]) -> None:
    assert goldens.goldens() == golden_documents


def test_registry_holds_this_version_and_schema() -> None:
    contract = tomllib.loads((CONTRACT / "contract.toml").read_text("utf-8"))
    assert contract["owner"]["version_constant"] == "neptune_ledger.api:CATALOG_API_VERSION"
    version = CONTRACT / f"v{CATALOG_API_VERSION}"
    meta = json.loads((version / "version.json").read_text("utf-8"))
    assert meta["owner_version"] == CATALOG_API_VERSION
    schema_text = (version / "schema.json").read_text("utf-8")
    assert json.loads(schema_text) == catalog_schema()


# --- worked examples the contract tests rely on -------------------------------------------------


def test_worked_examples_match_the_compilers_golden_packages(tmp_path: Path) -> None:
    for name in examples.EXAMPLES:
        package = examples.materialise(name, tmp_path / name)
        committed = (REPO / "tests" / "golden" / "packages" / name / "manifest.json").read_bytes()
        assert package.package_id == "sha256:" + hashlib.sha256(committed).hexdigest()
        tables = package.manifest["tables"]
        assert {c.kind: c.count for c in package.record_counts()} == {
            k: n for k, n in tables.items() if n
        }


def test_machine_thread_expectations_follow_adr_0003(tmp_path: Path) -> None:
    drone = examples.materialise("drone", tmp_path / "drone")
    threads = examples.machine_threads([drone])
    (key,) = threads
    assert key.key == DeclaredKey("px4.sys_uuid", "000200000000343233345117003a0027")
    kinds = {examples.record_key(r): k for k, _, r in drone.every_record()}
    roles = {(kinds[record_id], role) for _, record_id, role in threads[key]}
    assert ("machine", "subject") in roles
    assert ("run", "cites") in roles
    run = drone.records("run")[0]
    world = examples.world_time(run)
    assert world == api.WorldTime(run["first"]["value"]["domain_id"], 12000000)


def test_world_time_rules() -> None:
    clock_a, clock_b = "rec:sha256:" + "1" * 64, "rec:sha256:" + "2" * 64

    def ts(ticks: int, clock: str = clock_a) -> dict[str, Any]:
        return {"knowledge": "known", "value": {"domain_id": clock, "ticks": ticks}}

    unknown = {"knowledge": "unknown"}
    assert examples.world_time({"kind": "run", "first": unknown, "last": ts(9)}) == (
        api.WorldTime(clock_a, 9, 9)
    )
    assert examples.world_time({"kind": "run", "first": ts(1), "last": ts(9, clock_b)}) == (
        api.WorldTime(clock_a, 1)
    )
    assert examples.world_time({"kind": "run", "first": unknown, "last": unknown}) == "unknown"
    performed = {
        "kind": "calibration",
        "valid_from": unknown,
        "valid_until": unknown,
        "performed": ts(5),
    }
    assert examples.world_time(performed) == api.WorldTime(clock_a, 5, 5)
    assert examples.world_time({"kind": "machine"}) is None


# --- codec -------------------------------------------------------------------------------------


def _registration(**changes: Any) -> Registration:
    base: dict[str, Any] = {
        "outcome": "registered",
        "package_id": Known("sha256:" + "b" * 64),
        "registration_key": Known(TransactionKey(1, "2026-10-02T00:00:01.000000Z")),
        "root_locator": "/srv/p",
        "ledger_version": "0.0.1",
        "schema_version": Known(1),
        "record_counts": (),
        "findings": (),
    }
    return Registration(**{**base, **changes})


def test_round_trip_is_byte_stable() -> None:
    record = _registration()
    data = api.dumps(record)
    assert api.loads(Registration, data) == record
    assert api.dumps(api.loads(Registration, data)) == data


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.pop("findings"),
        lambda d: d.pop("api_version"),
        lambda d: d.update(outcome="maybe"),
        lambda d: d.update(record_counts=[{"count": True, "kind": "run"}]),
        lambda d: d.update(record_counts=[{"count": -1, "kind": "run"}]),
        lambda d: d.update(record_counts=[{"count": 1, "kind": "not_a_kind"}]),
        lambda d: d.update(package_id={"knowledge": "known", "value": "md5:00"}),
        lambda d: d.update(package_id={"knowledge": "known_absent", "provenance": {}}),
        lambda d: d.update(
            package_id={
                "knowledge": "known",
                "value": "sha256:" + "b" * 64,
                "provenance": {"assertion_kind": "stated"},
            }
        ),
        lambda d: d.update(api_version="2.0.0"),
        lambda d: d.update(root_locator=""),
    ],
)
def test_decoding_is_strict(mutate: Any) -> None:
    data = json.loads(api.dumps(_registration()))
    mutate(data)
    with pytest.raises(CodecError):
        from_json(Registration, data)


def test_knowledge_carries_no_provenance_and_never_known_absent() -> None:
    grounded = _Stated()
    with pytest.raises(CodecError):
        to_json(_registration(package_id=KnownAbsent(grounded)))
    ambiguous = Ambiguous((Candidate(1), Candidate(2)))
    assert to_json(_registration(schema_version=ambiguous))["schema_version"] == {  # type: ignore[index, call-overload]
        "candidates": [{"value": 1}, {"value": 2}],
        "knowledge": "ambiguous",
    }


def test_optional_fields_are_omitted_never_null() -> None:
    request = ThreadRequest(ThreadKey("machine", DECLARED), "world", LatestTransform())
    data = to_json(request)
    assert data == {
        "key": {"key": {"namespace": "serial", "value": "QX-0042"}, "kind": "machine"},
        "order": "world",
        "preference": {"preference": "latest_transform"},
    }
    assert from_json(ThreadRequest, data) == request
    with pytest.raises(CodecError):
        from_json(ThreadRequest, {**data, "as_of": None})  # type: ignore[dict-item]


def test_preference_has_no_default() -> None:
    data = to_json(ThreadRequest(ThreadKey("machine", DECLARED), "world", History()))
    assert isinstance(data, dict)
    del data["preference"]
    with pytest.raises(CodecError, match="preference"):
        from_json(ThreadRequest, data)


@pytest.mark.parametrize(
    "preference",
    [
        History(),
        LatestTransform(),
        Pinned("rec:sha256:" + "c" * 64),
        AsRegisteredBy("sha256:" + "d" * 64),
    ],
)
def test_preferences_are_tagged(preference: Any) -> None:
    request = ThreadRequest(ThreadKey("run", ANCHOR), "transaction", preference)
    assert from_json(ThreadRequest, to_json(request)) == request


def test_invalid_values_are_refused_when_encoding() -> None:
    with pytest.raises(CodecError):
        to_json(EvidenceAnchor("sha256:" + "a" * 64, ()))
    with pytest.raises(CodecError):
        to_json(EvidenceAnchor("sha256:" + "a" * 64, ({"length": 1},)))
    with pytest.raises(CodecError):
        to_json(QuerySpec(kinds=("run", "run")))
    with pytest.raises(CodecError):
        to_json(QuerySpec(kinds=()))
    with pytest.raises(CodecError):
        to_json(_registration(schema_version=Known(True)))
    with pytest.raises(CodecError):
        to_json(_registration(package_id=Unknown(), findings=[]))


# --- thread ids (ADR 0003 §1) -------------------------------------------------------------------


def test_thread_id_is_the_hash_of_the_canonical_key() -> None:
    key = ThreadKey("machine", DECLARED)
    document: JsonObject = {"key": {"namespace": "serial", "value": "QX-0042"}, "kind": "machine"}
    digest = hashlib.sha256(canonical_json.dumps(document)).hexdigest()
    assert key.thread_id == "sha256:" + digest


def test_thread_ids_separate_kinds_and_never_fold_case() -> None:
    ids = {
        ThreadKey("machine", DECLARED).thread_id,
        ThreadKey("sensor", DECLARED).thread_id,
        ThreadKey("machine", DeclaredKey("serial", "qx-0042")).thread_id,
        ThreadKey("configuration", ANCHOR).thread_id,
    }
    assert len(ids) == 4


def test_declared_and_anchored_keys_decode_unambiguously() -> None:
    for key in (ThreadKey("machine", DECLARED), ThreadKey("stream", ANCHOR)):
        assert from_json(ThreadKey, to_json(key)) == key


# --- Arrow -------------------------------------------------------------------------------------


def _row(**changes: Any) -> QueryRow:
    base: dict[str, Any] = {
        "kind": "run",
        "record_id": "rec:sha256:" + "e" * 64,
        "package_id": "sha256:" + "f" * 64,
        "line": 1,
        "registration_seq": 3,
    }
    return QueryRow(**{**base, **changes})


def test_query_result_schema_is_query_row() -> None:
    assert QUERY_RESULT_SCHEMA.names == [f.name for f in fields(QueryRow)]
    nullable = {f.name for f in QUERY_RESULT_SCHEMA if f.nullable}
    assert nullable == {f.name for f in fields(QueryRow) if f.default is None}


def test_query_table_round_trips_rows_and_meta() -> None:
    rows = (
        _row(),
        _row(
            record_id="rec:sha256:" + "1" * 64,
            world_first=-5,
            world_last=9,
            world_clock="rec:sha256:" + "2" * 64,
        ),
    )
    meta = QueryMeta(NotCovered(), ())
    table = api.query_table(rows, meta)
    assert api.query_rows(table) == rows
    assert api.query_meta(table) == meta
    assert api.ipc_bytes(table) == api.ipc_bytes(api.query_table(rows, meta))


def test_query_rows_refuses_foreign_tables() -> None:
    table = api.query_table((_row(),), QueryMeta(NotCovered(), ()))
    with pytest.raises(CodecError):
        api.query_rows(table.drop_columns(["line"]))
    with pytest.raises(CodecError):
        api.query_meta(table.replace_schema_metadata({}))
    with pytest.raises(CodecError):
        api.query_table((_row(line=0),), QueryMeta(NotCovered(), ()))


# --- protocol and stub -------------------------------------------------------------------------


def test_stub_satisfies_the_protocol_and_raises_per_call(tmp_path: Path) -> None:
    stub = StubCatalog()
    assert isinstance(stub, CatalogApi)
    key = ThreadKey("machine", DECLARED)
    requests: dict[str, api.Request] = {
        "register": RegisterRequest(str(tmp_path)),
        "verify": VerifyRequest("sha256:" + "0" * 64),
        "resolve": ResolveRequest(ANCHOR),
        "thread": ThreadRequest(key, "world", History()),
        "threads_of": ThreadsOfRequest("rec:sha256:" + "0" * 64),
        "lineage": LineageRequest("rec:sha256:" + "0" * 64),
        "query": QueryRequest(QuerySpec(kinds=("run",))),
    }
    for call, request in requests.items():
        with pytest.raises(NotImplementedError, match=rf"{call}\(\)"):
            api.call(stub, request)
    with pytest.raises(TypeError):
        api.call(stub, cast("Any", object()))


def test_docs_list_every_call_and_the_version() -> None:
    text = (PACKAGE / "docs" / "catalog-api.md").read_text("utf-8")
    for call in ("register", "verify", "resolve", "thread", "threads_of", "lineage", "query"):
        assert f"`{call}(" in text, call
    assert f"`{CATALOG_API_VERSION}`" in text
    for refusal in ("merge", "mutate", "infer"):
        assert f"**{refusal.capitalize()}.**" in text, refusal
