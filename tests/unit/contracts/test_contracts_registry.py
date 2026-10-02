"""The contracts registry (scripts/contracts.py, platform ADR 0002): the committed registry is
valid and current, a consumer's check catches a stale lock, the owner-side rule catches an
unbumped schema, and bump writes deterministic versions and announcements without the network.
"""

import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from neptune.model.record import SCHEMA_VERSION

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "contracts"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("contracts_tool", REPO / "scripts/contracts.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["contracts_tool"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


tool = _load()


def _registry(root: Path = CONTRACTS) -> Any:
    return tool.Registry(root)


# --- The committed registry ---------------------------------------------------------------------


def test_committed_registry_is_valid() -> None:
    report = tool.check_registry(_registry())
    assert report.problems == []
    assert set(tool.Registry(CONTRACTS).contract_ids()) == {
        "alignment-records",
        "catalog-api",
        "dataset-manifest",
        "graph-schema",
        "lifecycle-records",
        "package-schema",
        "query-packet",
    }


def test_ledger_consumer_check_runs_green() -> None:
    """The Ledger's CI step: lock current, goldens valid, the compiler's contract tests pass."""
    assert tool.main(["check", "--package", "neptune-ledger"]) == 0


def test_compiler_owner_rule_holds() -> None:
    """The compiler's exported schema is exactly the registry's latest package-schema."""
    report = tool.check_owner(_registry(), "neptune")
    assert report.problems == []
    latest = _registry().latest("package-schema")
    assert latest.owner_version == SCHEMA_VERSION
    assert latest.version[0] == SCHEMA_VERSION
    assert latest.schema_text == (REPO / "docs/schema/canonical.schema.json").read_text("utf-8")


def test_package_schema_goldens_cover_four_robots_and_every_document() -> None:
    latest = _registry().latest("package-schema")
    robots = {name.split(".", 1)[0] for name in latest.goldens}
    assert robots == {
        "drone",
        "manipulator",
        "mobile_robot",
        "quadruped",
        "warehouse_amr",  # deployment lifecycle records (ADR 0051)
        "manipulator_cell",
    }
    for robot in robots:
        assert latest.goldens[f"{robot}.manifest.json"] == "#/$defs/PackageManifest"
        assert latest.goldens[f"{robot}.receipt.json"] == "#/$defs/IngestReceipt"


def test_catalog_api_is_owned_by_the_ledger_export() -> None:
    """The Platform draft 0.0.0 stays; the Ledger's export (MVL-88) superseded it as 1.0.0."""
    registry = _registry()
    contract = registry.contract("catalog-api")
    assert contract.owner.package == "neptune-ledger"
    assert contract.owner.module == "neptune_ledger.api"
    draft, export = registry.versions("catalog-api")[:2]
    assert (draft.status, draft.version, draft.owner_version) == ("draft", (0, 0, 0), None)
    assert "superseded" in draft.note
    assert (export.status, export.version, export.owner_version) == ("draft", (1, 0, 0), "1.0.0")
    report = tool.check_owner(registry, "neptune-ledger")
    assert report.problems == []


def test_committed_compatibility_matrix_is_current() -> None:
    """contracts/compatibility.md is generated; a PR that changes the registry regenerates it."""
    registry = _registry()
    text = tool.render_matrix(registry)
    assert text == (CONTRACTS / "compatibility.md").read_text("utf-8")
    assert text == tool.render_matrix(registry)
    pinned = registry.lock()["neptune-ledger"]["package-schema"]
    assert f"| `neptune-ledger` | {pinned} current |" in text
    catalog = ".".join(map(str, registry.versions("catalog-api")[-1].version))
    assert f"| `catalog-api` | `neptune-ledger` | active | {catalog} | — |" in text


def test_golden_generator_is_deterministic() -> None:
    contract = _registry().contract("package-schema")
    first = tool.generate_goldens(_registry(), contract)
    assert first == tool.generate_goldens(_registry(), contract)
    assert set(first) == set(_registry().latest("package-schema").goldens)


# --- Consumer side over a copy of the registry --------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> Any:
    shutil.copytree(CONTRACTS, tmp_path / "contracts")
    return tool.Registry(tmp_path / "contracts", repo=REPO)


def _publish(registry: Any, contract: str, version: str, status: str = "stable") -> None:
    latest = registry.latest(contract)
    schema = json.loads(latest.schema_text)
    goldens = {
        name: (target, json.loads((latest.path / "golden" / name).read_text("utf-8")))
        for name, target in latest.goldens.items()
    }
    owner_version = int(version.split(".")[0]) if isinstance(latest.owner_version, int) else None
    tool.write_version(
        registry.root / contract / f"v{version}",
        contract,
        tool.parse_semver(version),
        status,
        owner_version,
        schema,
        goldens,
    )


def _check(registry: Any, package: str = "neptune-ledger") -> Any:
    return tool.check_package(registry, package, runner=lambda targets, cwd: 0)


def _next(registry: Any, contract: str, *, major: bool = False) -> str:
    """The next minor (or major) version after the registry's latest, whatever that is."""
    current = registry.latest(contract).version
    following = (current[0] + 1, 0, 0) if major else (current[0], current[1] + 1, 0)
    return ".".join(str(part) for part in following)


def test_a_minor_lag_warns_and_passes(registry: Any) -> None:
    assert _check(registry).ok
    newer = _next(registry, "package-schema")
    _publish(registry, "package-schema", newer)
    report = _check(registry)
    assert report.ok
    assert any(n.startswith("WARNING") and "is behind" in n and newer in n for n in report.notes)


def test_a_major_lag_fails(registry: Any) -> None:
    newer = _next(registry, "package-schema", major=True)
    _publish(registry, "package-schema", newer)
    report = _check(registry)
    assert any("a major version behind" in p and newer in p for p in report.problems)


def test_a_newer_draft_does_not_make_a_lock_behind(registry: Any) -> None:
    _publish(registry, "package-schema", _next(registry, "package-schema", major=True), "draft")
    assert _check(registry).ok


def test_check_all_validates_once_and_runs_each_owner_once(registry: Any) -> None:
    current = tool.show(registry.latest("package-schema", stable=True).version)
    graph = tool.show(registry.latest("graph-schema", stable=True).version)
    catalog = tool.show(registry.latest("catalog-api", stable=True).version)
    registry.write_lock(
        {
            "neptune-deploy": {
                "catalog-api": catalog,
                "graph-schema": graph,
                "package-schema": current,
            },
            "neptune-ledger": {"package-schema": current},
        }
    )
    calls: list[Any] = []

    def runner(targets: Any, cwd: Path) -> int:
        calls.append(targets)
        return 0

    report = tool.check_packages(registry, registry.lock(), runner=runner)
    assert report.ok and len(calls) == 3  # one per owner: compiler, neptune-ledger, neptune-memory
    versions = len(registry.versions("package-schema"))  # each published version, once
    assert sum(
        n.startswith("package-schema ") and "goldens checked" in n for n in report.notes
    ) == (versions)


def test_a_minor_version_must_accept_its_majors_goldens(registry: Any) -> None:
    newer = _next(registry, "package-schema")
    _publish(registry, "package-schema", newer)
    path = registry.root / "package-schema" / f"v{newer}"
    meta = json.loads(_text(path / "version.json"))
    schema = json.loads(_text(path / "schema.json"))
    schema["$defs"]["PackageManifest"]["required"] = ["no_such_field"]
    (path / "schema.json").write_text(tool.canonical(schema))
    meta["schema_sha256"] = tool.sha256(tool.canonical(schema))
    meta["goldens"] = {k: v for k, v in meta["goldens"].items() if "manifest" not in k}
    for golden in (path / "golden").glob("*.manifest.json"):
        golden.unlink()
    (path / "version.json").write_text(tool.canonical(meta))
    problems = tool.check_registry(registry).problems
    assert any("must be a major version" in p and "manifest.json" in p for p in problems)


def test_matrix_follows_the_registry(registry: Any, capsys: pytest.CaptureFixture[str]) -> None:
    root = ["--root", str(registry.root)]
    assert tool.main([*root, "matrix", "--check"]) == 0
    pinned = registry.lock()["neptune-ledger"]["package-schema"]
    newer = _next(registry, "package-schema")
    _publish(registry, "package-schema", newer)
    assert tool.main([*root, "matrix", "--check"]) == 1
    assert "compatibility.md is stale" in capsys.readouterr().err
    assert tool.main([*root, "matrix"]) == 0
    text = _text(registry.root / "compatibility.md")
    assert f"| `package-schema` | `neptune` | active | {newer} | — |" in text
    assert f"| `neptune-ledger` | {pinned} behind |" in text
    assert tool.main([*root, "matrix", "--check"]) == 0
    lock = registry.lock()
    registry.write_lock({**lock, "neptune-deploy": {}})
    assert "| `neptune-deploy` | not declared |" in tool.render_matrix(registry)


def test_check_cli_unions_packages_and_runs_each_owner_once(
    registry: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check --all --package P` (make contracts-check without PKG) runs owner tests once."""
    calls: list[Any] = []

    def runner(targets: Any, cwd: Path) -> int:
        calls.append(targets)
        return 0

    monkeypatch.setattr(tool, "run_pytest", runner)
    pinned = sorted({contract for pins in registry.lock().values() for contract in pins})
    for contract in pinned:  # every contract the lock pins
        for target in registry.contract(contract).owner.contract_tests:  # the CLI's repo
            (registry.root.parent / target).parent.mkdir(parents=True, exist_ok=True)
            (registry.root.parent / target).touch()
    argv = ["--root", str(registry.root), "check", "--all", "--package", "neptune-ledger"]
    assert tool.main([*argv, "--package", "neptune-ledger"]) == 0
    assert len(calls) == len(pinned)  # each pinned contract's owner tests, once each
    assert tool.main(["--root", str(registry.root), "check", "--package", "demo"]) == 1


def test_lock_must_be_canonical(registry: Any) -> None:
    lock = registry.root / "lock.toml"
    lock.write_text(lock.read_text() + "\n")
    assert any("not in canonical form" in p for p in tool.check_registry(registry).problems)


def test_lock_problems(registry: Any) -> None:
    lock = registry.root / "lock.toml"
    lock.write_text('[neptune-ledger]\npackage-schema = "1.0.1"\n')
    assert any("never published" in p for p in _check(registry).problems)
    lock.write_text('[neptune-ledger]\npackage-schema = "one"\n')
    assert any("not a semantic version" in p for p in _check(registry).problems)
    lock.write_text('[neptune-deploy]\n[neptune-ledger]\npackage-schema = "1.0.0"\n')
    deploy = _check(registry, "neptune-deploy").problems
    assert any("consumes package-schema but lock.toml does not declare" in p for p in deploy)
    assert any(
        "no [neptune-memory] entry" in p for p in _check(registry, "neptune-memory").problems
    )
    lock.write_text('[neptune-memory]\npackage-schema = "1.0.0"\n')
    assert any("not a consumer" in p for p in tool.check_registry(registry).problems)


def test_owner_contract_tests_gate_the_consumer(registry: Any) -> None:
    failing = tool.check_package(registry, "neptune-ledger", runner=lambda targets, cwd: 1)
    assert any("contract tests failed" in p for p in failing.problems)
    skipped = tool.check_package(registry, "neptune-ledger", runner=None)
    assert skipped.ok and any("not run" in n for n in skipped.notes)


def test_an_uninstalled_owner_is_skipped_with_a_message(registry: Any) -> None:
    path = registry.root / "package-schema" / "contract.toml"
    text = path.read_text().replace('module = "neptune.model"', 'module = "neptune_absent.x"')
    path.write_text(text)
    report = _check(registry)
    assert report.ok
    assert any("SKIPPED" in n and "neptune_absent.x is not importable" in n for n in report.notes)


@pytest.mark.parametrize(
    ("init", "problem"),
    [
        ("import neptune_nowhere_dependency\n", "neptune_nowhere_dependency"),
        ("raise ImportError('boom in init')\n", "boom in init"),
        ("raise RuntimeError('boom in init')\n", "boom in init"),
        ("", None),  # the parent imports; only the owner module is missing: not installed yet
    ],
)
def test_a_broken_owner_import_is_a_problem_not_a_skip(
    registry: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, init: str, problem: str | None
) -> None:
    (tmp_path / "mvl195_owner").mkdir()
    (tmp_path / "mvl195_owner" / "__init__.py").write_text(init)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    path = registry.root / "package-schema" / "contract.toml"
    path.write_text(_text(path).replace('module = "neptune.model"', 'module = "mvl195_owner.api"'))
    consumer, owner = _check(registry), tool.check_owner(registry, "neptune")
    sys.modules.pop("mvl195_owner", None)
    if problem is None:
        assert consumer.ok and owner.ok
        assert any("SKIPPED" in n for n in consumer.notes + owner.notes)
        return
    for report in (consumer, owner):
        assert any("fails to import" in p and problem in p for p in report.problems), report
        assert not any("SKIPPED" in n for n in report.notes)


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        ("golden", "is not valid under any of the given schemas"),
        ("schema_bytes", "is not canonical JSON"),
        ("sha", "does not match version.json's schema_sha256"),
        ("extra_golden", "golden/ files differ"),
        ("planned_with_version", "a planned contract has no versions"),
        ("unknown_package", "not in packages.toml"),
        ("toml", "contract.toml"),
    ],
)
def test_malformed_registry_is_reported(registry: Any, damage: str, expected: str) -> None:
    version = registry.root / "package-schema" / "v1.0.0"
    if damage == "golden":
        (version / "golden" / "drone.record.run.json").write_text('{\n  "kind": "run"\n}\n')
    elif damage == "schema_bytes":
        (version / "schema.json").write_text(json.dumps(json.loads(_text(version / "schema.json"))))
    elif damage == "sha":
        meta = json.loads(_text(version / "version.json"))
        meta["schema_sha256"] = "sha256:" + "0" * 64
        (version / "version.json").write_text(tool.canonical(meta))
    elif damage == "extra_golden":
        (version / "golden" / "stray.json").write_text("{}\n")
    elif damage == "planned_with_version":
        shutil.copytree(version, registry.root / "query-packet" / "v1.0.0")
    elif damage == "unknown_package":
        path = registry.root / "query-packet" / "contract.toml"
        path.write_text(_text(path).replace('"neptune-learn"]', '"neptune-nowhere"]'))
    else:
        (registry.root / "graph-schema" / "contract.toml").write_text("title = \n")
    problems = tool.check_registry(registry).problems
    assert any(expected in p for p in problems), problems
    assert tool.main(["--root", str(registry.root), "check"]) == 1


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- Owner side and bump over a toy registry ----------------------------------------------------


def _toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int = 1) -> Any:
    """A registry with one contract whose owner is a module in tmp_path."""
    root = tmp_path / "contracts"
    (root / "toy").mkdir(parents=True)
    (root / "packages.toml").write_text(
        '[toy-owner]\npath = "."\n[toy-consumer]\npath = "c"\ngate_issue = "MVL-1"\n'
    )
    tool.Registry(root).write_lock({"toy-consumer": {"toy": "1.0.0"}})
    (root / "toy" / "contract.toml").write_text(
        'title = "Toy"\nstatus = "active"\nconsumers = ["toy-consumer"]\n[owner]\n'
        'package = "toy-owner"\nmodule = "toy_owner"\nschema_export = "toy_owner:schema"\n'
        'version_constant = "toy_owner:VERSION"\ngolden_generator = "gen.py"\n'
        'contract_tests = ["test_toy.py"]\n'
    )
    (tmp_path / "gen.py").write_text(
        "import json\nprint(json.dumps({'one.json': {'target': '#', 'value': {'n': 1}}}))\n"
    )
    (tmp_path / "test_toy.py").write_text("def test_ok() -> None:\n    pass\n")
    _owner(tmp_path, {"type": "object"}, version)
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = tool.Registry(root)
    tool.write_version(
        root / "toy" / "v1.0.0",
        "toy",
        (1, 0, 0),
        "stable",
        1,
        {"type": "object"},
        {"one.json": ("#", {"n": 1, "old": True})},
    )
    return registry


def _owner(tmp_path: Path, schema: Any, version: int | str) -> None:
    (tmp_path / "toy_owner.py").write_text(
        f"VERSION = {version!r}\n\ndef schema():\n    return {schema!r}\n"
    )
    sys.modules.pop("toy_owner", None)
    importlib.invalidate_caches()


def test_owner_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _toy(tmp_path, monkeypatch)
    assert tool.check_owner(registry, "toy-owner").ok
    _owner(tmp_path, {"type": "object", "required": ["n"]}, 1)
    changed = tool.check_owner(registry, "toy-owner").problems
    assert any("differs from registry 1.0.0" in p and "bump toy" in p for p in changed)
    _owner(tmp_path, {"type": "object"}, 2)
    (only_constant,) = tool.check_owner(registry, "toy-owner").problems
    assert "schema is unchanged" in only_constant and "bump toy 2.0.0" in only_constant
    assert tool.main(["--root", str(registry.root), "check-owner", "--package", "toy-owner"]) == 1
    tool.bump(registry, "toy", "2.0.0")  # the suggested command works
    assert tool.check_owner(registry, "toy-owner").ok


def test_bump_writes_a_version_and_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _toy(tmp_path, monkeypatch)
    _owner(tmp_path, {"type": "object", "required": ["n"]}, 1)
    assert tool.main(["--root", str(registry.root), "bump", "toy", "1.1.0"]) == 0
    assert tool.main(["--root", str(registry.root), "matrix", "--check"]) == 0  # bump wrote it
    out = capsys.readouterr().out
    assert "comment for MVL-1 (toy-consumer)" in out and "declares 1.0.0" in out
    assert tool.check_owner(registry, "toy-owner").ok
    assert tool.check_registry(registry).ok
    consumer = tool.check_package(registry, "toy-consumer", runner=lambda targets, cwd: 0)
    assert consumer.ok and any("WARNING" in n for n in consumer.notes)
    assert registry.lock() == {"toy-consumer": {"toy": "1.0.0"}}  # a minor leaves locks alone
    written = _files(registry.root / "toy" / "v1.1.0")
    shutil.rmtree(registry.root / "toy" / "v1.1.0")
    tool.bump(registry, "toy", "1.1.0")
    again = _files(registry.root / "toy" / "v1.1.0")
    assert written == again


@pytest.mark.parametrize(
    ("schema", "constant", "version", "expected"),
    [
        ({"type": "object", "required": ["n"]}, 1, "1.0.0", "not newer"),
        ({"type": "object"}, 1, "1.1.0", "nothing to bump"),
        (
            {"type": "object", "properties": {"n": {}}, "additionalProperties": False},
            1,
            "1.1.0",
            "not reader-compatible.*v1.0.0/golden/one.json",
        ),
        ({"type": "object", "required": ["n"]}, 1, "2.0.0", "major must equal"),
        ({"type": "object", "required": ["n"]}, "1.2.0", "1.1.0", "bump it to '1.1.0'"),
        ({"type": "array"}, 1, "1.1.0", "does not validate"),
        ({"type": "object", "required": ["n"]}, 1, "v1.1", "not a semantic version"),
    ],
)
def test_bump_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema: Any,
    constant: int | str,
    version: str,
    expected: str,
) -> None:
    registry = _toy(tmp_path, monkeypatch)
    _owner(tmp_path, schema, constant)
    with pytest.raises(tool.ContractError, match=expected):
        tool.bump(registry, "toy", version)
    assert [v.version for v in registry.versions("toy")] == [(1, 0, 0)]


def test_a_major_bump_raises_in_repo_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _toy(tmp_path, monkeypatch)
    _owner(tmp_path, {"type": "object", "properties": {"n": {}}, "additionalProperties": False}, 2)
    assert tool.main(["--root", str(registry.root), "bump", "toy", "2.0.0"]) == 0
    assert registry.lock() == {"toy-consumer": {"toy": "2.0.0"}}
    assert "raises `toy-consumer` from 1.0.0 to 2.0.0" in capsys.readouterr().out
    assert tool.check_package(registry, "toy-consumer", runner=lambda targets, cwd: 0).ok


def _make_draft(registry: Any, lock: dict[str, dict[str, str]]) -> None:
    """Turn the toy's v1.0.0 into a draft that nobody locks."""
    meta_path = registry.root / "toy" / "v1.0.0" / "version.json"
    meta = json.loads(_text(meta_path))
    meta["status"] = "draft"
    meta_path.write_text(tool.canonical(meta))
    registry.write_lock(lock)


def test_a_drafts_goldens_do_not_bind_the_next_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The draft's golden carries `old`, which the closed schema rejects; a draft binds nobody."""
    registry = _toy(tmp_path, monkeypatch)
    _make_draft(registry, {"toy-consumer": {}})
    closed = {"type": "object", "properties": {"n": {}}, "additionalProperties": False}
    _owner(tmp_path, closed, 1)
    assert tool.compatibility_breaks(registry, "toy", (1, 1, 0), closed) == []
    tool.bump(registry, "toy", "1.1.0", status="draft")
    assert tool.check_registry(registry).ok
    assert registry.lock() == {"toy-consumer": {}}  # a draft sets no lock entry
    _owner(tmp_path, {"type": "object", "properties": {"n": {}}}, 1)
    tool.bump(registry, "toy", "1.2.0")
    assert tool.check_registry(registry).ok
    strict = {"type": "object", "properties": {"n": {"type": "string"}}}
    breaks = tool.compatibility_breaks(registry, "toy", (1, 3, 0), strict)
    assert [b.split(":", 1)[0] for b in breaks] == ["v1.2.0/golden/one.json"]  # stable binds


def test_the_first_stable_version_adds_every_in_repo_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _toy(tmp_path, monkeypatch)
    _make_draft(registry, {"toy-consumer": {}, "toy-owner": {}})
    _owner(tmp_path, {"type": "object", "required": ["n"]}, 1)
    assert tool.main(["--root", str(registry.root), "bump", "toy", "1.1.0"]) == 0
    assert registry.lock() == {"toy-consumer": {"toy": "1.1.0"}, "toy-owner": {}}
    assert "first stable version: the same PR adds `toy-consumer` at 1.1.0" in (
        capsys.readouterr().out
    )
    assert tool.check_package(registry, "toy-consumer", runner=lambda targets, cwd: 0).ok


def test_bump_refuses_planned_contracts(registry: Any) -> None:
    with pytest.raises(tool.ContractError, match="planned"):
        tool.bump(registry, "query-packet", "0.1.0")


def test_post_needs_a_key_and_never_guesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _toy(tmp_path, monkeypatch)
    _owner(tmp_path, {"type": "object", "required": ["n"]}, 1)
    posted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(tool, "post_comment", lambda *args: posted.append(args))
    argv = ["--root", str(registry.root), "bump", "toy", "1.1.0", "--post"]
    assert tool.main(argv, environ={}) == 1
    assert "needs LINEAR_API_KEY" in capsys.readouterr().err and posted == []
    assert not (registry.root / "toy" / "v1.1.0").exists()  # checked before writing
    assert tool.main(argv, environ={"LINEAR_API_KEY": "k"}) == 0
    assert [(issue, key) for issue, _, key in posted] == [("MVL-1", "k")]


class _Response:
    def __init__(self, body: Any) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_post_comment_sends_one_graphql_mutation() -> None:
    sent: list[Any] = []

    def urlopen(request: Any, timeout: float) -> _Response:
        sent.append(request)
        return _Response({"data": {"commentCreate": {"success": True}}})

    tool.post_comment("MVL-89", "hello", "key", urlopen=urlopen)
    (request,) = sent
    assert request.full_url == tool.LINEAR_API and request.get_header("Authorization") == "key"
    payload = json.loads(request.data)
    assert payload["variables"] == {"input": {"body": "hello", "issueId": "MVL-89"}}

    def refused(request: Any, timeout: float) -> _Response:
        return _Response({"errors": [{"message": "no"}]})

    with pytest.raises(tool.ContractError, match="refused"):
        tool.post_comment("MVL-89", "hello", "key", urlopen=refused)


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_register_makes_a_new_package_green(registry: Any) -> None:
    """What scripts/new-package.sh runs: an empty lock section in canonical form, idempotently."""
    assert _check(registry, "demo").problems  # unregistered: no lock entry
    assert tool.register(registry, "demo") == ["contracts/packages.toml", "contracts/lock.toml"]
    assert tool.register(registry, "demo") == []
    assert registry.lock()["demo"] == {}
    assert registry.packages()["demo"] == {"path": "packages/demo"}
    assert _check(registry, "demo").ok
    with pytest.raises(tool.ContractError, match="not a package name"):
        tool.register(registry, "Demo_Bad")
    assert tool.main(["--root", str(registry.root), "matrix", "--check"]) == 0


def test_register_regenerates_the_matrix(registry: Any) -> None:
    """A planned consumer's scaffold turns `not declared (no package yet)` into `not declared`,
    and `new-package.sh` stays green because register writes the matrix."""
    root = ["--root", str(registry.root)]
    assert tool.register(registry, "neptune-learn") == [
        "contracts/lock.toml",
        "contracts/compatibility.md",
    ]
    assert tool.main([*root, "matrix", "--check"]) == 0
    assert "| `neptune-learn` | not declared |" in _text(registry.root / "compatibility.md")
    assert tool.register(registry, "neptune-learn") == []
