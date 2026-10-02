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
COMPILER: Final = STAGES[0]


def _ok(_: Context) -> Outcome:
    return Outcome({"ran": True})


def _context(tmp_path: Path, registry: object | None = None) -> Context:
    _, cases = corpus.select()
    return Context(registry=registry or contracts.registry(), work=tmp_path, cases=cases)


def test_the_stage_order_and_service_flags() -> None:
    assert [(s.id, s.needs_services) for s in STAGES] == [
        ("compiler", False),
        ("ledger", True),
        ("memory", True),
        ("context", True),
    ]
    assert STAGES[0].real is not None and all(s.real is None for s in STAGES[1:])


def test_today_only_the_compiler_resolves_to_real() -> None:
    registry = contracts.registry()
    resolved = {stage.id: resolve(stage, registry) for stage in STAGES}
    assert resolved["compiler"].mode == "real"
    assert resolved["compiler"].contract_version == "3.0.0"
    assert resolved["ledger"].mode == "stub"
    # neptune_ledger.api is importable (MVL-88) but only as a contract and stub: no real driver.
    assert "no real driver for neptune-ledger" in resolved["ledger"].reason
    assert resolved["ledger"].contract_version == "1.3.0"
    assert resolved["context"].mode == "stub"
    assert resolved["memory"].mode == "stub"  # graph-schema 1.0.0 is published; no driver yet
    assert resolved["memory"].contract_version == "1.0.0"


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
    entry = run_stage(STAGES[1], _context(tmp_path), services_up=True, upstream_ok=False)
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
    outcome = STAGES[3].stub(ctx)
    smoke = outcome.output["smoke"]
    assert smoke["packet"] == {"packet": "golden"}
    assert smoke["packet_source"] == "golden query-packet packet.json"
    assert outcome.output["contract_version"] == "0.0.1"
