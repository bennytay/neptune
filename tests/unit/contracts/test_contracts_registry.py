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
    assert latest.owner_version == 1
    assert latest.schema_text == (REPO / "docs/schema/canonical.schema.json").read_text("utf-8")


def test_package_schema_goldens_cover_four_robots_and_every_document() -> None:
    latest = _registry().latest("package-schema")
    robots = {name.split(".", 1)[0] for name in latest.goldens}
    assert robots == {"drone", "manipulator", "mobile_robot", "quadruped"}
    for robot in robots:
        assert latest.goldens[f"{robot}.manifest.json"] == "#/$defs/PackageManifest"
        assert latest.goldens[f"{robot}.receipt.json"] == "#/$defs/IngestReceipt"


def test_catalog_api_is_a_draft_pending_the_ledger() -> None:
    registry = _registry()
    contract = registry.contract("catalog-api")
    assert contract.owner.package == "neptune-ledger"
    assert contract.owner.module == "neptune_ledger.api"
    (version,) = registry.versions("catalog-api")
    assert (version.status, version.version, version.owner_version) == ("draft", (0, 0, 0), None)
    assert "superseded" in version.note
    report = tool.check_owner(registry, "neptune-ledger")
    assert report.problems == [] and "SKIPPED" in report.notes[0]


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


def test_a_lock_behind_the_registry_fails(registry: Any) -> None:
    assert _check(registry).ok
    _publish(registry, "package-schema", "1.1.0")
    report = _check(registry)
    assert any("is behind" in p and "1.1.0" in p for p in report.problems)


def test_a_newer_draft_does_not_make_a_lock_behind(registry: Any) -> None:
    _publish(registry, "package-schema", "2.0.0", status="draft")
    assert _check(registry).ok


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
        shutil.copytree(version, registry.root / "graph-schema" / "v1.0.0")
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
    (root / "lock.toml").write_text('[toy-consumer]\ntoy = "1.0.0"\n')
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
        {"one.json": ("#", {"n": 1})},
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
    assert any(
        "version constant is 2" in p for p in tool.check_owner(registry, "toy-owner").problems
    )
    assert tool.main(["--root", str(registry.root), "check-owner", "--package", "toy-owner"]) == 1


def test_bump_writes_a_version_and_announces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = _toy(tmp_path, monkeypatch)
    _owner(tmp_path, {"type": "object", "required": ["n"]}, 1)
    assert tool.main(["--root", str(registry.root), "bump", "toy", "1.1.0"]) == 0
    out = capsys.readouterr().out
    assert "comment for MVL-1 (toy-consumer)" in out and "declares 1.0.0" in out
    assert tool.check_owner(registry, "toy-owner").ok
    assert tool.check_registry(registry).ok
    consumer = tool.check_package(registry, "toy-consumer", runner=lambda targets, cwd: 0)
    assert any("is behind" in p for p in consumer.problems)
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


def test_bump_refuses_planned_contracts(registry: Any) -> None:
    with pytest.raises(tool.ContractError, match="planned"):
        tool.bump(registry, "graph-schema", "0.1.0")


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
    shutil.rmtree(registry.root / "toy" / "v1.1.0")
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
