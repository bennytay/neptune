"""Real versus stub: what decides it, what each stub serves, and what a stage error does."""

import json
import shutil
from pathlib import Path
from typing import Final

import pytest
from harness import contracts, corpus
from harness.run import run_stage
from harness.stages import STAGES, Context, Outcome, Stage, resolve

REPO: Final = Path(__file__).resolve().parents[3]
BY_ID: Final = {stage.id: stage for stage in STAGES}
COMPILER: Final = BY_ID["compiler"]
DEPLOY: Final = BY_ID["deploy"]
LEDGER: Final = BY_ID["ledger"]


def _ok(_: Context) -> Outcome:
    return Outcome({"ran": True})


def _context(tmp_path: Path, registry: object | None = None) -> Context:
    _, cases = corpus.select(name="worked-examples")
    return Context(registry=registry or contracts.registry(), work=tmp_path, cases=cases)


def test_the_stage_order_and_service_flags() -> None:
    # The real ledger runs on an embedded PostgreSQL (platform ADR 0006), so it needs no services.
    assert [(s.id, s.needs_services) for s in STAGES] == [
        ("compiler", False),
        ("deploy", False),
        ("ledger", False),
        ("memory", True),
        ("context", True),
    ]
    assert [s.real is not None for s in STAGES] == [True, True, True, False, False]


def test_today_the_compiler_and_the_ledger_resolve_to_real() -> None:
    registry = contracts.registry()
    resolved = {stage.id: resolve(stage, registry) for stage in STAGES}

    def latest(contract_id: str) -> str:
        # Read from the registry, not pinned: a contract bump must not have to edit this test.
        version = registry.latest(contract_id, stable=True)
        assert version is not None
        return ".".join(map(str, version.version))

    assert resolved["compiler"].mode == "real"
    assert resolved["compiler"].contract_version == latest("package-schema")
    # Deploy owns no contract: its stage writes package-schema packages through neptune_deploy.
    schema = latest("package-schema")
    assert resolved["deploy"].mode == "real"
    assert (
        resolved["deploy"].reason == f"neptune_deploy.lifecycle is importable and matches {schema}"
    )
    assert resolved["ledger"].mode == "real"
    catalog = latest("catalog-api")
    assert resolved["ledger"].reason == f"neptune_ledger.api is importable and matches {catalog}"
    assert resolved["ledger"].contract_version == catalog
    assert resolved["context"].mode == "stub"
    assert resolved["memory"].mode == "stub"  # graph-schema published; no driver yet
    assert resolved["memory"].contract_version == latest("graph-schema")


def test_an_importable_package_without_a_driver_is_still_a_stub() -> None:
    stage = Stage("compiler", "neptune", "package-schema", False, None, _ok)
    resolution = resolve(stage, contracts.registry())
    assert resolution.mode == "stub" and "no real driver for neptune" in resolution.reason


def test_a_version_constant_that_disagrees_with_the_registry_is_a_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neptune.model.record as record

    newer = record.SCHEMA_VERSION + 1
    monkeypatch.setattr(record, "SCHEMA_VERSION", newer)
    resolution = resolve(COMPILER, contracts.registry())
    assert resolution.mode == "stub"
    assert f"SCHEMA_VERSION is {newer}" in resolution.reason


def test_a_lock_a_major_behind_makes_the_stage_a_stub(tmp_path: Path) -> None:
    copy = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", copy)
    (copy / "lock.toml").write_text('[neptune]\npackage-schema = "0.9.0"\n', encoding="utf-8")
    resolution = resolve(COMPILER, contracts.registry(copy))
    assert resolution.mode == "stub" and "a major behind" in resolution.reason


def test_a_real_stage_that_needs_services_fails_with_the_command_when_they_are_down(
    tmp_path: Path,
) -> None:
    stage = Stage("compiler", "neptune", "package-schema", True, _ok, _ok)
    ctx = _context(tmp_path)
    down = run_stage(stage, ctx, services_up=False, upstream_ok=True)
    assert down["status"] == "error" and down["mode"] == "real"
    assert "docker compose -f harness/compose.yaml up -d --wait" in down["problems"][0]
    up = run_stage(stage, ctx, services_up=True, upstream_ok=True)
    assert up["status"] == "ok" and up["output"] == {"ran": True}


def test_a_stub_never_needs_the_services(tmp_path: Path) -> None:
    stage = Stage("ledger", "neptune-ledger", "catalog-api", True, None, _ok)
    entry = run_stage(stage, _context(tmp_path), services_up=False, upstream_ok=True)
    assert entry["mode"] == "stub" and entry["status"] == "ok"


def test_a_driver_that_raises_is_a_stage_error_with_the_path_scrubbed(tmp_path: Path) -> None:
    def boom(ctx: Context) -> Outcome:
        raise RuntimeError(f"cannot write {ctx.work}/x")

    stage = Stage("ledger", "neptune-ledger", "catalog-api", False, None, boom)
    entry = run_stage(stage, _context(tmp_path), services_up=True, upstream_ok=True)
    assert entry["status"] == "error"
    assert entry["problems"] == ["RuntimeError: cannot write <work>/x"]


def test_a_stage_after_a_failure_is_skipped(tmp_path: Path) -> None:
    entry = run_stage(LEDGER, _context(tmp_path), services_up=True, upstream_ok=False)
    assert entry["status"] == "skipped" and entry["output"] == {}


def test_a_missing_source_folder_fails_the_compiler_stage_with_a_code(tmp_path: Path) -> None:
    ctx = Context(
        registry=contracts.registry(),
        work=tmp_path,
        cases=[corpus.Case("ghost", tmp_path / "nope")],
    )
    entry = run_stage(COMPILER, ctx, services_up=True, upstream_ok=True)
    assert entry["status"] == "failed" and entry["mode"] == "real"
    assert entry["problems"][0].startswith("ghost: ")
    assert entry["output"]["cases"] == [{"case": "ghost", "state": "error"}]


def test_the_context_stub_serves_a_published_query_packet_golden(tmp_path: Path) -> None:
    copy = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", copy)
    for published in (copy / "query-packet").glob("v*"):  # only the draft below is published
        shutil.rmtree(published)
    version = copy / "query-packet" / "v0.0.1"
    (version / "golden").mkdir(parents=True)
    (version / "schema.json").write_text("{}\n", encoding="utf-8")
    (version / "golden" / "packet.json").write_text('{"packet": "golden"}\n', encoding="utf-8")
    (version / "version.json").write_text(
        json.dumps(
            {
                "contract": "query-packet",
                "goldens": {"packet.json": "#"},
                "owner_version": None,
                "schema_sha256": "sha256:0",
                "status": "draft",
                "version": "0.0.1",
            }
        ),
        encoding="utf-8",
    )
    ctx = _context(tmp_path / "work", contracts.registry(copy))
    outcome = BY_ID["context"].stub(ctx)
    smoke = outcome.output["smoke"]
    assert smoke["packet"] == {"packet": "golden"}
    assert smoke["packet_source"] == "golden query-packet packet.json"
    assert outcome.output["contract_version"] == "0.0.1"


def _compiled(tmp_path: Path, registry: object | None = None) -> Context:
    """A context whose compiler stage has run for real over the worked examples."""
    ctx = _context(tmp_path, registry)
    entry = run_stage(COMPILER, ctx, services_up=False, upstream_ok=True)
    assert entry["status"] == "ok"
    return ctx


def test_a_tampered_package_is_refused_by_the_real_ledger(tmp_path: Path) -> None:
    ctx = _compiled(tmp_path)
    manifest = json.loads((tmp_path / "packages" / "drone" / "manifest.json").read_text())
    victim = tmp_path / "packages" / "drone" / manifest["files"][0]["path"]
    victim.write_bytes(victim.read_bytes() + b" ")  # the manifest's hash no longer matches
    entry = run_stage(LEDGER, ctx, services_up=False, upstream_ok=True)
    assert entry["mode"] == "real" and entry["status"] == "failed"
    rows = {row["case"]: row for row in entry["output"]["cases"]}
    assert rows["drone"]["registration"] == "refused"
    assert rows["drone"]["responses_valid"] is True  # a refusal is still a valid catalog answer
    # One problem: a refused package is not re-registered, verified or checked against the lock.
    assert len(entry["problems"]) == 1
    assert entry["problems"][0].startswith("drone: register was refused (")
    assert "verify" not in rows["drone"] and "reregistration" not in rows["drone"]
    assert {rows[c]["registration"] for c in ("manipulator", "mobile_robot", "quadruped")} == {
        "registered"
    }  # partial success: one tampered package does not stop the others


def test_a_package_newer_than_the_ledger_lock_fails_the_real_ledger(tmp_path: Path) -> None:
    copy = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", copy)
    registry = contracts.registry(copy)
    lock = registry.lock()
    lock["neptune-ledger"]["package-schema"] = "1.0.0"
    registry.write_lock(lock)
    ctx = _compiled(tmp_path / "work", registry)
    assert LEDGER.real is not None
    outcome = LEDGER.real(ctx)  # resolve() would make it a stub: a lock a major behind
    # The manipulator package needs package-schema 2 (compiler ADR 0037); the quadruped's
    # robot.urdf gives robot-description records, which need package-schema 8 (compiler ADR 0039).
    assert outcome.problems == (
        "manipulator: the package needs package-schema 2, neptune-ledger locks 1.0.0",
        "quadruped: the package needs package-schema 8, neptune-ledger locks 1.0.0",
    )


def test_a_ledger_a_major_behind_falls_back_to_the_catalog_api_goldens(tmp_path: Path) -> None:
    copy = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", copy)
    registry = contracts.registry(copy)
    lock = registry.lock()
    lock["neptune-ledger"]["package-schema"] = "1.0.0"
    registry.write_lock(lock)
    entry = run_stage(
        LEDGER, _context(tmp_path / "work", registry), services_up=False, upstream_ok=True
    )
    assert entry["mode"] == "stub" and entry["status"] == "ok"
    assert entry["reason"] == "neptune-ledger locks package-schema 1.0.0, a major behind"
    served = registry.latest("catalog-api")
    assert entry["output"]["served"] == "goldens"
    assert len(entry["output"]["goldens"]) == len(served.goldens)


def test_a_real_ledger_with_nothing_compiled_upstream_fails(tmp_path: Path) -> None:
    # A stub compiler serves goldens, not cases: registering nothing must not read as green.
    assert LEDGER.real is not None
    outcome = LEDGER.real(_context(tmp_path))
    assert outcome.problems == ("the compiler stage compiled no case to register",)
    assert outcome.output["cases"] == []


def test_a_ledger_without_a_package_schema_lock_fails_the_real_ledger(tmp_path: Path) -> None:
    copy = tmp_path / "contracts"
    shutil.copytree(REPO / "contracts", copy)
    registry = contracts.registry(copy)
    lock = registry.lock()
    del lock["neptune-ledger"]["package-schema"]
    registry.write_lock(lock)
    ctx = _compiled(tmp_path / "work", registry)
    assert LEDGER.real is not None
    outcome = LEDGER.real(ctx)  # the run-time lock check has nothing to honour: never silent
    assert outcome.problems == (
        "neptune-ledger has no package-schema entry in contracts/lock.toml",
    )
    assert outcome.output["locked_package_schema"] is None
    assert {row["registration"] for row in outcome.output["cases"]} == {"registered"}


def test_a_stage_whose_entry_module_is_missing_is_a_stub() -> None:
    stage = Stage("deploy", "neptune-deploy", "package-schema", False, _ok, _ok, entry="nope.x")
    resolution = resolve(stage, contracts.registry())
    assert resolution.mode == "stub" and resolution.reason == "nope.x is not importable"


def test_the_deploy_stub_maps_nothing_and_the_ledger_registers_only_compiled_packages(
    tmp_path: Path,
) -> None:
    ctx = _compiled(tmp_path)
    entry = run_stage(
        Stage("deploy", "neptune-deploy", "package-schema", False, None, DEPLOY.stub),
        ctx,
        services_up=False,
        upstream_ok=True,
    )
    assert entry["mode"] == "stub" and entry["status"] == "ok"
    assert ctx.flowing_ids() == ctx.package_ids()


def test_a_case_that_declares_no_mapping_is_passed_over(tmp_path: Path) -> None:
    ctx = _compiled(tmp_path)
    entry = run_stage(DEPLOY, ctx, services_up=False, upstream_ok=True)
    assert entry["mode"] == "real" and entry["status"] == "ok"
    assert entry["output"]["cases"] == [
        {"case": name, "declared": False} for name in corpus.EXAMPLE_NAMES
    ]


def _declaring(tmp_path: Path, declaration: dict[str, object]) -> Context:
    """The manipulator worked example, compiled, with ``declaration`` as its deploy.json."""
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps(declaration), encoding="utf-8")
    sources = REPO / "tests" / "fixtures" / "model" / "manipulator" / "sources"
    ctx = Context(
        registry=contracts.registry(),
        work=tmp_path / "work",
        cases=[corpus.Case("manipulator", sources, None, path)],
    )
    assert run_stage(COMPILER, ctx, services_up=False, upstream_ok=True)["status"] == "ok"
    return ctx


def test_a_real_deploy_map_that_writes_no_record_is_red(tmp_path: Path) -> None:
    # The manipulator example has no zone register: a declared preset that maps nothing is owed.
    ctx = _declaring(tmp_path, {"deploy_format": 1, "presets": ["register_zone"]})
    entry = run_stage(DEPLOY, ctx, services_up=False, upstream_ok=True)
    assert entry["mode"] == "real" and entry["status"] == "failed"
    (row,) = entry["output"]["cases"]
    assert row["state"] == "committed" and row["records"] == {}
    assert row["by_declaration"] == {"preset:register_zone": 0}
    assert entry["problems"] == [
        "manipulator: the Deploy map wrote no lifecycle record",
        "manipulator: preset:register_zone mapped no record",
    ]
    # The mapped package is kept in the report (evidence of what did not map); the red stage stops
    # the run before the ledger, so it is not registered.
    assert ctx.package_ids("deploy") == [row["package"]]


def test_a_preset_deploy_does_not_ship_and_an_unmet_count_are_problems(tmp_path: Path) -> None:
    declaration = {
        "deploy_format": 1,
        "presets": ["cmms_generic", "no_such_preset"],
        "at_least": {"maintenance_event": 1},
    }
    ctx = _declaring(tmp_path, declaration)
    entry = run_stage(DEPLOY, ctx, services_up=False, upstream_ok=True)
    assert entry["status"] == "failed"
    assert "manipulator: Deploy ships no preset 'no_such_preset'" in entry["problems"]
    assert (
        "manipulator: maintenance_event records are 0, the declaration needs at least 1"
        in entry["problems"]
    )


def test_a_malformed_declaration_fails_the_case_without_running_deploy(tmp_path: Path) -> None:
    ctx = _declaring(tmp_path, {"deploy_format": 1, "templates": ["../outside.json"]})
    entry = run_stage(DEPLOY, ctx, services_up=False, upstream_ok=True)
    assert entry["status"] == "failed"
    assert entry["problems"] == [
        "manipulator: template ../outside.json is not a path inside the repository"
    ]
    assert entry["output"]["cases"] == [{"case": "manipulator", "declared": True}]
    assert not ctx.deploy_root("manipulator").exists()


def test_a_deploy_built_against_another_package_schema_is_a_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import neptune_deploy

    monkeypatch.setattr(neptune_deploy, "PACKAGE_SCHEMA_VERSION", 1)
    resolution = resolve(DEPLOY, contracts.registry())
    assert resolution.mode == "stub"
    assert resolution.reason.startswith("neptune_deploy:PACKAGE_SCHEMA_VERSION is 1, ")


def test_a_map_that_raises_is_that_cases_problem_not_a_stage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness import stages

    def boom(plan: object) -> object:
        raise ValueError(f"cannot read {tmp_path}/secret")

    ctx = _declaring(tmp_path, {"deploy_format": 1, "presets": ["cmms_generic"]})
    monkeypatch.setattr(stages, "_declarations", boom)
    entry = run_stage(DEPLOY, ctx, services_up=False, upstream_ok=True)
    assert entry["status"] == "failed"
    assert entry["problems"] == ["manipulator: the Deploy map raised ValueError"]
    assert entry["output"]["cases"][0]["state"] == "error"
