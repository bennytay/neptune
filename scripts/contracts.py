#!/usr/bin/env python3
"""The cross-layer contracts registry tool (packages/neptune-platform ADR 0002).

``contracts/`` holds one directory per contract: ``contract.toml`` (owner, consumers, where the
owner exports the schema and keeps its version constant, the owner's contract tests) and one
``v<semver>/`` directory per published version with ``schema.json``, ``version.json`` and
``golden/*.json``. ``contracts/lock.toml`` records which version each consuming package is built
against; ``contracts/packages.toml`` routes announcements to each package's Linear gate issue.

Subcommands:

- ``check [--package P ...] [--all]``: the registry is well formed, every golden validates against
  its version's schema, every stable version's goldens validate against every later minor/patch
  schema of its major (a draft's goldens bind only the draft), and each named package's lock
  entries (``--all``: every package in lock.toml) are not a major version behind (a minor or patch
  lag is a warning); then the upstream owners' contract tests run once each (skipped, with a
  message, while an owner module or its parent package does not exist yet).
- ``check-owner --package P``: every schema P exports equals the registry's latest version, and
  P's version constant matches it. A changed export needs ``bump``.
- ``register PACKAGE``: add a new member's ``packages.toml`` entry and empty lock section
  (stdlib only; ``scripts/new-package.sh`` runs it).
- ``matrix [--check]``: write ``contracts/compatibility.md`` from the registry and lock.toml
  (``--check``: fail if the committed file differs).
- ``bump CONTRACT VERSION [--post]``: write ``v<VERSION>/`` from the owner's export and golden
  generator, refusing a minor/patch that rejects an earlier stable golden of its major; the first
  stable version, and every later major, sets the contract's lock entry of every in-repo consumer
  (a package with a lock.toml section). Then print the announcement comments for the
  consumers' gate issues; ``--post`` sends them through the Linear GraphQL API, and needs
  ``LINEAR_API_KEY`` before anything is written.

Stdlib plus ``jsonschema`` (already a dev dependency). Output files are canonical JSON: sorted
keys, two-space indent, UTF-8, one trailing newline, so the same inputs give the same bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

REPO: Final = Path(__file__).resolve().parents[1]
DEFAULT_ROOT: Final = REPO / "contracts"
LINEAR_API: Final = "https://api.linear.app/graphql"
CONTRACT_STATUSES: Final = frozenset({"active", "planned"})
VERSION_STATUSES: Final = frozenset({"draft", "stable"})
_SEMVER: Final = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PACKAGE: Final = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
LOCK_HEADER: Final = """\
# The contract versions each package in this repository is built against (platform ADR 0002).
# `scripts/contracts.py check --package <name>` warns while an entry lags the registry's latest
# stable version by a minor or patch release and fails when it lags by a major release. A major
# `bump` raises every entry here in the same PR. Draft versions need not be declared.
"""

SemVer = tuple[int, int, int]


class ContractError(Exception):
    """A registry operation that cannot proceed; the message says what to do."""


# --- Canonical JSON and versions ---------------------------------------------------------------


def canonical(value: Any) -> str:
    """The registry's file text for a JSON value: sorted keys, indent 2, UTF-8, final newline."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_semver(text: str) -> SemVer:
    match = _SEMVER.match(text)
    if match is None:
        raise ContractError(f"not a semantic version (MAJOR.MINOR.PATCH): {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def show(version: SemVer) -> str:
    return ".".join(str(part) for part in version)


# --- The registry model ------------------------------------------------------------------------


@dataclass(frozen=True)
class Owner:
    package: str
    module: str
    schema_export: str | None
    version_constant: str | None
    golden_generator: str | None
    contract_tests: tuple[str, ...]


@dataclass(frozen=True)
class Contract:
    id: str
    title: str
    status: str
    owner: Owner
    consumers: tuple[str, ...]
    part_of: str | None
    path: Path


@dataclass(frozen=True)
class Version:
    contract: str
    version: SemVer
    status: str
    owner_version: int | str | None
    schema_sha256: str
    goldens: Mapping[str, str]
    note: str | None
    path: Path

    @property
    def schema_text(self) -> str:
        return (self.path / "schema.json").read_text(encoding="utf-8")


@dataclass
class Report:
    """What a check found: problems fail it, notes are informational (skips, passes)."""

    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _str(table: Mapping[str, Any], key: str, where: str, *, optional: bool = False) -> Any:
    value = table.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ContractError(f"{where}: {key} must be a non-empty string")
    return value


def _strings(table: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ContractError(f"{where}: {key} must be a list of non-empty strings")
    return tuple(value)


def _toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ContractError(f"{path}: {error}") from error


class Registry:
    """The ``contracts/`` directory, read on demand."""

    def __init__(self, root: Path, repo: Path | None = None) -> None:
        self.root = root
        self.repo = root.parent if repo is None else repo  # where owner paths are relative to

    def contract_ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "contract.toml").is_file())

    def contract(self, contract_id: str) -> Contract:
        path = self.root / contract_id
        if not (path / "contract.toml").is_file():
            raise ContractError(f"unknown contract {contract_id!r}")
        where = f"contracts/{contract_id}/contract.toml"
        data = _toml(path / "contract.toml")
        owner = data.get("owner")
        if not isinstance(owner, dict):
            raise ContractError(f"{where}: needs an [owner] table")
        status = _str(data, "status", where)
        if status not in CONTRACT_STATUSES:
            raise ContractError(f"{where}: status must be one of {sorted(CONTRACT_STATUSES)}")
        return Contract(
            id=contract_id,
            title=_str(data, "title", where),
            status=status,
            owner=Owner(
                package=_str(owner, "package", where),
                module=_str(owner, "module", where),
                schema_export=_str(owner, "schema_export", where, optional=True),
                version_constant=_str(owner, "version_constant", where, optional=True),
                golden_generator=_str(owner, "golden_generator", where, optional=True),
                contract_tests=_strings(owner, "contract_tests", where),
            ),
            consumers=_strings(data, "consumers", where),
            part_of=_str(data, "part_of", where, optional=True),
            path=path,
        )

    def versions(self, contract_id: str) -> list[Version]:
        """Every published version, oldest first."""
        found: list[Version] = []
        for path in sorted((self.root / contract_id).glob("v*")):
            if path.is_dir():
                found.append(self._version(contract_id, path))
        return sorted(found, key=lambda v: v.version)

    def _version(self, contract_id: str, path: Path) -> Version:
        where = f"contracts/{contract_id}/{path.name}"
        version = parse_semver(path.name[1:])
        meta_path = path / "version.json"
        if not meta_path.is_file():
            raise ContractError(f"{where}: missing version.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            raise ContractError(f"{where}/version.json must be an object")
        goldens = meta.get("goldens", {})
        if not isinstance(goldens, dict) or not all(isinstance(t, str) for t in goldens.values()):
            raise ContractError(f"{where}/version.json: goldens must map file name to pointer")
        owner_version = meta.get("owner_version")
        if owner_version is not None and not isinstance(owner_version, int | str):
            raise ContractError(f"{where}/version.json: owner_version must be int, str or null")
        return Version(
            contract=contract_id,
            version=version,
            status=_str(meta, "status", where),
            owner_version=owner_version,
            schema_sha256=_str(meta, "schema_sha256", where),
            goldens=goldens,
            note=_str(meta, "note", where, optional=True),
            path=path,
        )

    def latest(self, contract_id: str, *, stable: bool = False) -> Version | None:
        versions = [v for v in self.versions(contract_id) if not stable or v.status == "stable"]
        return versions[-1] if versions else None

    def lock(self) -> dict[str, dict[str, str]]:
        data = _toml(self.root / "lock.toml")
        lock: dict[str, dict[str, str]] = {}
        for package, entries in sorted(data.items()):
            if not isinstance(entries, dict) or not all(
                isinstance(v, str) for v in entries.values()
            ):
                raise ContractError(f"lock.toml: [{package}] must map contract id to version")
            lock[package] = dict(sorted(entries.items()))
        return lock

    def write_lock(self, lock: Mapping[str, Mapping[str, str]]) -> None:
        (self.root / "lock.toml").write_text(render_lock(lock), encoding="utf-8")

    def packages(self) -> dict[str, dict[str, str]]:
        data = _toml(self.root / "packages.toml")
        for name, table in data.items():
            if not isinstance(table, dict):
                raise ContractError(f"packages.toml: [{name}] must be a table")
        return data


def render_lock(lock: Mapping[str, Mapping[str, str]]) -> str:
    """The canonical text of lock.toml: header, then packages and contracts in sorted order."""
    sections = [
        f"[{package}]\n" + "".join(f'{c} = "{v}"\n' for c, v in sorted(entries.items()))
        for package, entries in sorted(lock.items())
    ]
    return LOCK_HEADER + "".join(f"\n{section}" for section in sections)


# --- Validation --------------------------------------------------------------------------------


def _subschema(schema: Mapping[str, Any], pointer: str) -> dict[str, Any]:
    """A schema that validates against ``pointer`` within ``schema`` (``#`` is the root)."""
    if pointer == "#":
        return dict(schema)
    if not pointer.startswith("#/$defs/"):
        raise ContractError(f"golden target must be '#' or '#/$defs/<name>', got {pointer!r}")
    name = pointer.removeprefix("#/$defs/")
    if name not in schema.get("$defs", {}):
        raise ContractError(f"golden target {pointer} is not in the schema")
    wrapper = {k: v for k, v in schema.items() if k in ("$defs", "$id", "$schema")}
    return {**wrapper, "$ref": pointer}


def _validator() -> type[Draft202012Validator]:
    """jsonschema, imported on first use so ``register`` runs on the stdlib alone."""
    from jsonschema import Draft202012Validator

    return Draft202012Validator


def validate_golden(schema: Mapping[str, Any], pointer: str, value: Any) -> list[str]:
    validator = _validator()(_subschema(schema, pointer))
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors]


def _check_version(contract: Contract, version: Version) -> list[str]:
    where = f"contracts/{contract.id}/{version.path.name}"
    problems: list[str] = []
    meta = json.loads((version.path / "version.json").read_text(encoding="utf-8"))
    if meta.get("contract") != contract.id or meta.get("version") != show(version.version):
        problems.append(f"{where}/version.json: contract/version disagree with the directory")
    if (version.path / "version.json").read_text(encoding="utf-8") != canonical(meta):
        problems.append(f"{where}/version.json is not canonical JSON")
    if version.status not in VERSION_STATUSES:
        problems.append(f"{where}: status must be one of {sorted(VERSION_STATUSES)}")
    if isinstance(version.owner_version, int) and version.owner_version != version.version[0]:
        problems.append(
            f"{where}: an integer owner version ({version.owner_version}) must equal the major"
        )
    if isinstance(version.owner_version, str) and version.owner_version != show(version.version):
        problems.append(f"{where}: a string owner version must equal the registry version")
    if not (version.path / "schema.json").is_file():
        return [*problems, f"{where}: missing schema.json"]
    text = version.schema_text
    schema = json.loads(text)
    if text != canonical(schema):
        problems.append(f"{where}/schema.json is not canonical JSON")
    if sha256(text) != version.schema_sha256:
        problems.append(f"{where}: schema.json does not match version.json's schema_sha256")
    try:
        _validator().check_schema(schema)
    except _schema_error() as error:
        return [*problems, f"{where}/schema.json is not a valid JSON Schema: {error.message}"]
    golden_dir = version.path / "golden"
    present = sorted(p.name for p in golden_dir.glob("*.json")) if golden_dir.is_dir() else []
    if present != sorted(version.goldens):
        problems.append(f"{where}: golden/ files differ from version.json's goldens list")
    if not version.goldens:
        problems.append(f"{where}: a version needs at least one golden example")
    for name in sorted(set(present) & set(version.goldens)):
        golden_text = (golden_dir / name).read_text(encoding="utf-8")
        value = json.loads(golden_text)
        if golden_text != canonical(value):
            problems.append(f"{where}/golden/{name} is not canonical JSON")
        try:
            errors = validate_golden(schema, version.goldens[name], value)
        except ContractError as error:
            errors = [str(error)]
        problems += [f"{where}/golden/{name}: {message}" for message in errors]
    return problems


def compatibility_breaks(
    registry: Registry, contract_id: str, version: SemVer, schema: Mapping[str, Any]
) -> list[str]:
    """Goldens of earlier stable versions with the same major that ``schema`` rejects.

    Reader compatibility defines a breaking change: a minor or patch version must accept every
    golden its major has published as stable before it. Anything else needs a new major. A draft
    binds nobody, so its goldens do not constrain the versions after it.
    """
    breaks: list[str] = []
    for prior in registry.versions(contract_id):
        if prior.version[0] != version[0] or prior.version >= version or prior.status == "draft":
            continue
        for name, target in sorted(prior.goldens.items()):
            path = prior.path / "golden" / name
            if not path.is_file():
                continue  # reported against the prior version itself
            try:
                errors = validate_golden(schema, target, json.loads(path.read_text("utf-8")))
            except ContractError as error:
                errors = [str(error)]
            if errors:
                breaks.append(f"v{show(prior.version)}/golden/{name}: {errors[0]}")
    return breaks


def _schema_error() -> type[SchemaError]:
    from jsonschema.exceptions import SchemaError

    return SchemaError


def check_registry(registry: Registry) -> Report:
    """Structure, schemas and goldens of every contract, plus the lock's consistency."""
    report = Report()
    try:
        packages = registry.packages()
        lock = registry.lock()
    except ContractError as error:
        report.problems.append(str(error))
        return report
    contracts: dict[str, Contract] = {}
    for contract_id in registry.contract_ids():
        try:
            contract = registry.contract(contract_id)
            versions = registry.versions(contract_id)
        except (ContractError, json.JSONDecodeError) as error:
            report.problems.append(f"{contract_id}: {error}")
            continue
        contracts[contract_id] = contract
        for package in (contract.owner.package, *contract.consumers):
            if package not in packages:
                report.problems.append(f"{contract_id}: package {package!r} not in packages.toml")
        if contract.part_of is not None and contract.part_of not in registry.contract_ids():
            report.problems.append(f"{contract_id}: part_of names no contract")
        if contract.status == "planned" and versions:
            report.problems.append(f"{contract_id}: a planned contract has no versions")
        if contract.status == "active" and not versions:
            report.problems.append(f"{contract_id}: an active contract needs a version")
        for version in versions:
            report.problems += _check_version(contract, version)
            try:
                schema = json.loads(version.schema_text)
            except (OSError, json.JSONDecodeError):
                continue  # reported by _check_version
            breaks = compatibility_breaks(registry, contract_id, version.version, schema)
            if breaks:
                report.problems.append(
                    f"{contract_id} {show(version.version)} rejects an earlier golden of its "
                    f"major, so it must be a major version: {breaks[0]}"
                )
            report.notes.append(
                f"{contract_id} {show(version.version)} ({version.status}): schema and "
                f"{len(version.goldens)} goldens checked"
            )
    if (registry.root / "lock.toml").read_text(encoding="utf-8") != render_lock(lock):
        report.problems.append("lock.toml is not in canonical form (scripts/contracts.py)")
    for package, entries in lock.items():
        if package not in packages:
            report.problems.append(f"lock.toml: package {package!r} not in packages.toml")
        for contract_id in entries:
            if contract_id not in contracts:
                report.problems.append(f"lock.toml: {package} declares unknown {contract_id}")
                continue
            if package not in contracts[contract_id].consumers:
                report.problems.append(
                    f"lock.toml: {package} is not a consumer in {contract_id}/contract.toml"
                )
    return report


# --- Consumer side -----------------------------------------------------------------------------


def _installed(module: str) -> bool:
    """Whether the owner module imports; False only while it or a parent package does not exist.

    Any other import failure (a parent ``__init__`` importing something missing, or raising) is a
    bug in the owner, never "not installed yet", so it raises ``ContractError``.
    """
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as error:
        name = error.name or ""
        if name and (module == name or module.startswith(f"{name}.")):
            return False
        raise ContractError(f"{module} fails to import: {error!r}") from error
    except Exception as error:  # an import-time bug in the owner, reported rather than skipped
        raise ContractError(f"{module} fails to import: {error!r}") from error
    return True


Runner = Callable[[Sequence[str], Path], int]


def run_pytest(targets: Sequence[str], cwd: Path) -> int:
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
    return subprocess.run(command, cwd=cwd, check=False).returncode


def check_package(
    registry: Registry, package: str, *, runner: Runner | None = run_pytest
) -> Report:
    """A consumer's CI step: registry valid, lock current, upstream contract tests green."""
    return check_packages(registry, [package], runner=runner)


def check_packages(
    registry: Registry, packages: Iterable[str], *, runner: Runner | None = run_pytest
) -> Report:
    """``check_package`` for several packages: the registry and each owner's tests run once."""
    report = check_registry(registry)
    tested: set[str] = set()
    for package in packages:
        try:
            _check_lock(registry, package, report, runner, tested)
        except (ContractError, json.JSONDecodeError) as error:
            report.problems.append(str(error))
    return report


def _check_lock(
    registry: Registry, package: str, report: Report, runner: Runner | None, tested: set[str]
) -> None:
    lock = registry.lock()
    if package not in lock:
        report.problems.append(f"lock.toml has no [{package}] entry; declare its contracts")
        return
    declared = lock[package]
    for contract_id in registry.contract_ids():
        contract = registry.contract(contract_id)
        if (
            package in contract.consumers
            and contract_id not in declared
            and registry.latest(contract_id, stable=True) is not None
        ):
            report.problems.append(
                f"{package} consumes {contract_id} but lock.toml does not declare a version"
            )
    for contract_id, text in declared.items():
        if contract_id not in registry.contract_ids():
            continue  # reported by check_registry
        try:
            version = parse_semver(text)
        except ContractError as error:
            report.problems.append(f"lock.toml [{package}] {contract_id}: {error}")
            continue
        published = {v.version for v in registry.versions(contract_id)}
        if version not in published:
            report.problems.append(f"{package} declares {contract_id} {text}, never published")
            continue
        latest = registry.latest(contract_id, stable=True)
        if latest is not None and latest.version[0] > version[0]:
            report.problems.append(
                f"{package} is a major version behind: declares {contract_id} {text}, registry "
                f"has {show(latest.version)}; a major bump raises in-repo locks in its own PR"
            )
        elif latest is not None and latest.version > version:
            report.notes.append(
                f"WARNING: {package} is behind: declares {contract_id} {text}, registry has "
                f"{show(latest.version)}; its coordinator picks the bump up as an issue"
            )
        else:
            report.notes.append(f"{package}: {contract_id} {text} is current")
        if contract_id not in tested:
            tested.add(contract_id)
            _run_owner_tests(registry, registry.contract(contract_id), report, runner)


def _run_owner_tests(
    registry: Registry, contract: Contract, report: Report, runner: Runner | None
) -> None:
    owner = contract.owner
    if not owner.contract_tests:
        report.notes.append(f"{contract.id}: owner {owner.package} declares no contract tests")
        return
    try:
        installed = _installed(owner.module)
    except ContractError as error:
        report.problems.append(f"{contract.id}: owner {owner.package}: {error}")
        return
    if not installed:
        report.notes.append(
            f"{contract.id}: SKIPPED owner contract tests, {owner.package} is not installed "
            f"({owner.module} is not importable)"
        )
        return
    missing = [t for t in owner.contract_tests if not (registry.repo / t).exists()]
    if missing:
        report.problems.append(f"{contract.id}: owner contract tests missing: {missing}")
        return
    if runner is None:
        report.notes.append(f"{contract.id}: owner contract tests not run (--no-tests)")
        return
    if runner(owner.contract_tests, registry.repo) != 0:
        report.problems.append(f"{contract.id}: owner {owner.package}'s contract tests failed")
    else:
        report.notes.append(f"{contract.id}: owner {owner.package}'s contract tests passed")


# --- Owner side --------------------------------------------------------------------------------


def _resolve(reference: str) -> Any:
    module, _, name = reference.partition(":")
    if not name:
        raise ContractError(f"expected 'module:attribute', got {reference!r}")
    value = getattr(importlib.import_module(module), name)
    return value() if callable(value) else value


def owner_export(contract: Contract) -> tuple[Any, int | str | None]:
    """The owner's exported schema and its version constant, imported from the owner package."""
    owner = contract.owner
    if owner.schema_export is None:
        raise ContractError(f"{contract.id}: the owner declares no schema_export")
    schema = _resolve(owner.schema_export)
    constant = _resolve(owner.version_constant) if owner.version_constant else None
    if constant is not None and not isinstance(constant, int | str):
        raise ContractError(f"{contract.id}: version constant must be an int or a str")
    return schema, constant


def check_owner(registry: Registry, package: str) -> Report:
    """The owner-side rule: an exported schema equals the registry's latest version."""
    report = Report()
    owned = [
        c
        for c in (registry.contract(i) for i in registry.contract_ids())
        if c.owner.package == package and c.owner.schema_export is not None
    ]
    if not owned:
        report.notes.append(f"{package} exports no registered schema")
    for contract in owned:
        try:
            installed = _installed(contract.owner.module)
        except ContractError as error:
            report.problems.append(f"{contract.id}: {error}")
            continue
        if not installed:
            report.notes.append(
                f"{contract.id}: SKIPPED, {contract.owner.module} is not importable yet"
            )
            continue
        schema, constant = owner_export(contract)
        latest = registry.latest(contract.id)
        if latest is None:
            report.problems.append(
                f"{contract.id}: {package} exports a schema the registry has no version of; "
                f"run scripts/contracts.py bump {contract.id} <version>"
            )
            continue
        if canonical(schema) != latest.schema_text:
            report.problems.append(
                f"{contract.id}: {package}'s exported schema differs from registry "
                f"{show(latest.version)}; bump the version constant if the change is breaking, "
                f"then run scripts/contracts.py bump {contract.id} <next version>"
            )
        elif constant != latest.owner_version:
            target = f"{constant}.0.0" if isinstance(constant, int) else str(constant)
            report.problems.append(
                f"{contract.id}: the schema is unchanged but the version constant is "
                f"{constant!r} while registry {show(latest.version)} records "
                f"{latest.owner_version!r}; restore the constant, or publish it with "
                f"scripts/contracts.py bump {contract.id} {target}"
            )
        else:
            report.notes.append(f"{contract.id}: {package} matches {show(latest.version)}")
    return report


# --- Compatibility matrix ----------------------------------------------------------------------

MATRIX_HEADER: Final = """\
# Contract compatibility matrix

Which contract versions exist and which version each consuming package is built against. Generated
by `scripts/contracts.py matrix` from `contracts/<id>/contract.toml`,
`contracts/<id>/v*/version.json`, `contracts/lock.toml` and `contracts/packages.toml`; do not edit
it by hand. `make contracts-check` fails while it is stale. The policy is ADR 0002 in
`packages/neptune-platform/docs/adr/`. The file at a release tag is that release's compatibility
statement.
"""

MATRIX_LOCKS_NOTE: Final = """\
Cells: the version `lock.toml` declares, then `current` (the latest stable), `behind` (an older
stable) or `draft`; `no stable` when the contract has no stable version yet (nothing to declare);
`not declared` when a stable version exists but the lock has no entry, `(no package yet)` when the
package has no lock section; blank when the package does not consume the contract. A contract that
is part of another rides on its version and has no column.
"""


def _matrix_cell(registry: Registry, contract: Contract, package: str, lock: Any) -> str:
    if package not in contract.consumers:
        return ""
    stable = registry.latest(contract.id, stable=True)
    declared = lock.get(package, {}).get(contract.id)
    if declared is not None:
        version = parse_semver(declared)
        if stable is not None and version == stable.version:
            return f"{declared} current"
        if stable is not None and version < stable.version:
            return f"{declared} behind"
        return f"{declared} draft"
    if stable is None:
        return "no stable"
    return "not declared" if package in lock else "not declared (no package yet)"


def render_matrix(registry: Registry) -> str:
    """The text of ``contracts/compatibility.md``: a pure function of the registry's files.

    Contracts are ordered by their owner's position in packages.toml, then by id; consumer rows
    follow packages.toml order.
    """
    packages = list(registry.packages())
    lock = registry.lock()
    rank = {package: index for index, package in enumerate(packages)}
    contracts = sorted(
        (registry.contract(i) for i in registry.contract_ids()),
        key=lambda c: (rank.get(c.owner.package, len(rank)), c.id),
    )
    lines = [
        MATRIX_HEADER,
        "## Contracts\n",
        "| Contract | Owner package | Status | Latest stable | Latest draft | Consumers |",
        "|---|---|---|---|---|---|",
    ]
    for contract in contracts:
        stable = registry.latest(contract.id, stable=True)
        drafts = [v for v in registry.versions(contract.id) if v.status == "draft"]
        draft = (
            drafts[-1]
            if drafts and (stable is None or drafts[-1].version > stable.version)
            else None
        )
        owner = f"`{contract.owner.package}`"
        if contract.part_of is not None:
            owner += f" (part of `{contract.part_of}`)"
        consumers = (
            ", ".join(f"`{c}`" for c in sorted(contract.consumers)) or "none in this repository"
        )
        cells = [
            f"`{contract.id}`",
            owner,
            contract.status,
            show(stable.version) if stable else "—",
            show(draft.version) if draft else "—",
            consumers,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    columns = [c for c in contracts if c.part_of is None and c.consumers]
    consumers = {p for c in columns for p in c.consumers}
    rows = [p for p in packages if p in consumers] + sorted(consumers - set(packages))
    lines += [
        "",
        "## Consumer locks\n",
        MATRIX_LOCKS_NOTE,
        "| Consumer | " + " | ".join(f"`{c.id}`" for c in columns) + " |",
        "|---|" + "---|" * len(columns),
    ]
    for package in rows:
        cells = [_matrix_cell(registry, c, package, lock) for c in columns]
        lines.append(f"| `{package}` | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def matrix(registry: Registry, *, check: bool = False) -> Report:
    """Write ``compatibility.md`` (or, with ``check``, report that the committed file is stale)."""
    report = Report()
    path = registry.root / "compatibility.md"
    text = render_matrix(registry)
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current == text:
        report.notes.append("contracts/compatibility.md is current")
    elif check:
        report.problems.append(
            "contracts/compatibility.md is stale; run scripts/contracts.py matrix and commit it"
        )
    else:
        path.write_text(text, encoding="utf-8")
        report.notes.append("wrote contracts/compatibility.md")
    return report


# --- Registering a new package -----------------------------------------------------------------


def register(registry: Registry, package: str) -> list[str]:
    """Give a new workspace member its packages.toml entry and an empty lock section.

    Idempotent; returns the files it changed. ``scripts/new-package.sh`` calls it so a fresh
    scaffold's ``make contracts-check PKG=<name>`` is green without hand edits.
    """
    if not _PACKAGE.match(package):
        raise ContractError(f"not a package name: {package!r}")
    changed: list[str] = []
    if package not in registry.packages():
        path = registry.root / "packages.toml"
        text = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(f'{text}\n\n[{package}]\npath = "packages/{package}"\n', encoding="utf-8")
        changed.append("contracts/packages.toml")
    lock = registry.lock()
    if package not in lock:
        registry.write_lock({**lock, package: {}})
        changed.append("contracts/lock.toml")
    return changed


# --- Bump and announce -------------------------------------------------------------------------


@dataclass(frozen=True)
class Announcement:
    package: str
    issue: str | None
    body: str


def generate_goldens(registry: Registry, contract: Contract) -> dict[str, tuple[str, Any]]:
    """Run the owner's golden generator: it prints ``{name: {"target", "value"}}`` as JSON."""
    generator = contract.owner.golden_generator
    if generator is None:
        raise ContractError(f"{contract.id}: the owner declares no golden_generator")
    result = subprocess.run(
        [sys.executable, str(registry.repo / generator)],
        cwd=registry.repo,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise ContractError(f"{generator} failed:\n{result.stderr}")
    produced = json.loads(result.stdout)
    goldens: dict[str, tuple[str, Any]] = {}
    for name, entry in sorted(produced.items()):
        if not name.endswith(".json") or "/" in name:
            raise ContractError(f"{generator}: golden name {name!r} must be a plain *.json name")
        goldens[name] = (entry["target"], entry["value"])
    return goldens


def write_version(
    path: Path,
    contract: str,
    version: SemVer,
    status: str,
    owner_version: int | str | None,
    schema: Any,
    goldens: Mapping[str, tuple[str, Any]],
    note: str | None = None,
) -> None:
    """Write ``v<version>/``: schema, goldens and version.json, all canonical."""
    schema_text = canonical(schema)
    (path / "golden").mkdir(parents=True)
    (path / "schema.json").write_text(schema_text, encoding="utf-8")
    for name, (_, value) in sorted(goldens.items()):
        (path / "golden" / name).write_text(canonical(value), encoding="utf-8")
    meta: dict[str, Any] = {
        "contract": contract,
        "goldens": {name: target for name, (target, _) in sorted(goldens.items())},
        "owner_version": owner_version,
        "schema_sha256": sha256(schema_text),
        "status": status,
        "version": show(version),
    }
    if note is not None:
        meta["note"] = note
    (path / "version.json").write_text(canonical(meta), encoding="utf-8")


def announcements(
    registry: Registry,
    contract: Contract,
    version: SemVer,
    before: Mapping[str, Mapping[str, str]],
) -> list[Announcement]:
    """One comment per consumer; ``before`` is the lock as it was before the bump."""
    packages = registry.packages()
    lock = registry.lock()
    new = show(version)
    found: list[Announcement] = []
    for package in sorted(contract.consumers):
        declared = before.get(package, {}).get(contract.id)
        body = (
            f"Contract `{contract.id}` {new} is published by {contract.owner.package} "
            f"(`contracts/{contract.id}/v{new}/`). "
        )
        if declared == new:
            body += f"`{package}` already declares it in `contracts/lock.toml`; nothing to do."
        elif declared is None and lock.get(package, {}).get(contract.id) == new:
            body += (
                f"This is the contract's first stable version: the same PR adds `{package}` at "
                f"{new} to `contracts/lock.toml`, and `{package}`'s contract tests must pass at "
                f"{new} before it merges. Review the change for this project."
            )
        elif lock.get(package, {}).get(contract.id) == new:
            body += (
                f"This is a major version: the same PR raises `{package}` from {declared} to "
                f"{new} in `contracts/lock.toml`, and `{package}`'s contract tests must pass at "
                f"{new} before it merges. Review the change for this project."
            )
        else:
            body += (
                f"`{package}` declares {declared or 'no version'} in `contracts/lock.toml`; "
                f"`scripts/contracts.py check --package {package}` warns until the lock is "
                "raised. Pick the bump up as an issue in this project."
            )
        found.append(Announcement(package, packages.get(package, {}).get("gate_issue"), body))
    return found


def bump(
    registry: Registry,
    contract_id: str,
    text: str,
    *,
    status: str = "stable",
) -> list[Announcement]:
    """Publish a new version from the owner's export and golden generator."""
    contract = registry.contract(contract_id)
    version = parse_semver(text)
    if status not in VERSION_STATUSES:
        raise ContractError(f"status must be one of {sorted(VERSION_STATUSES)}")
    if contract.status != "active":
        raise ContractError(f"{contract_id} is {contract.status}; set status = 'active' first")
    latest = registry.latest(contract_id)
    if latest is not None and version <= latest.version:
        raise ContractError(f"{text} is not newer than {show(latest.version)}")
    if not _installed(contract.owner.module):
        raise ContractError(f"{contract.owner.package} is not installed; cannot export")
    schema, constant = owner_export(contract)
    if (
        latest is not None
        and canonical(schema) == latest.schema_text
        and constant == latest.owner_version
    ):
        raise ContractError(
            f"the exported schema and constant equal {show(latest.version)}; nothing to bump"
        )
    if isinstance(constant, int) and constant != version[0]:
        raise ContractError(
            f"the version constant is {constant}; the new version's major must equal it "
            "(raise the constant for a breaking change)"
        )
    if isinstance(constant, str) and constant != text:
        raise ContractError(f"the version constant is {constant!r}; bump it to {text!r} first")
    goldens = generate_goldens(registry, contract)
    for name, (target, value) in goldens.items():
        errors = validate_golden(schema, target, value)
        if errors:
            raise ContractError(f"golden {name} does not validate: {errors[0]}")
    breaks = compatibility_breaks(registry, contract_id, version, schema)
    if breaks:
        raise ContractError(
            f"{text} is not reader-compatible with its major, so it cannot be a minor or patch "
            f"version: {breaks[0]}. Publish a major version (raise the constant first)."
        )
    before = registry.lock()
    stable = registry.latest(contract_id, stable=True)
    write_version(
        contract.path / f"v{text}", contract_id, version, status, constant, schema, goldens
    )
    if status == "stable" and (stable is None or version[0] > stable.version[0]):
        # The first stable version adds, and a major version raises, the lock entry of every
        # in-repo consumer in the same PR (ADR 0002 §4). A package is in the repository once it
        # has a lock.toml section (``register``); one without a section has nothing to raise.
        raised = {
            package: {**entries, contract_id: text}
            if contract_id in entries or package in contract.consumers
            else dict(entries)
            for package, entries in before.items()
        }
        registry.write_lock(raised)
    return announcements(registry, contract, version, before)


def post_comment(
    issue: str,
    body: str,
    api_key: str,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Create one Linear comment through the GraphQL API."""
    payload = {
        "query": "mutation($input: CommentCreateInput!) { commentCreate(input: $input) "
        "{ success } }",
        "variables": {"input": {"issueId": issue, "body": body}},
    }
    request = urllib.request.Request(
        LINEAR_API,
        data=json.dumps(payload, sort_keys=True).encode("utf-8"),
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        result = json.loads(response.read())
    if result.get("errors") or not result.get("data", {}).get("commentCreate", {}).get("success"):
        raise ContractError(f"Linear refused the comment on {issue}: {result}")


# --- CLI ---------------------------------------------------------------------------------------


def _print(lines: Iterable[str], stream: Any = None) -> None:
    for line in lines:
        print(line, file=stream or sys.stdout)


def _finish(report: Report) -> int:
    _print(report.notes)
    _print((f"FAIL: {problem}" for problem in report.problems), sys.stderr)
    return 0 if report.ok else 1


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contracts.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="the contracts/ dir")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate the registry and a consumer's lock")
    check.add_argument(
        "--package", action="append", default=[], help="consumer to check (repeatable)"
    )
    check.add_argument("--all", action="store_true", help="every package in lock.toml as well")
    check.add_argument("--no-tests", action="store_true", help="skip owner contract tests")
    owner = commands.add_parser("check-owner", help="owner's export equals the registry")
    owner.add_argument("--package", required=True)
    registrar = commands.add_parser("register", help="add a new package to the registry")
    registrar.add_argument("package")
    matrixer = commands.add_parser("matrix", help="write contracts/compatibility.md")
    matrixer.add_argument("--check", action="store_true", help="fail if the file is stale")
    bumper = commands.add_parser("bump", help="publish a new version of a contract")
    bumper.add_argument("contract")
    bumper.add_argument("version")
    bumper.add_argument("--status", default="stable", choices=sorted(VERSION_STATUSES))
    bumper.add_argument("--post", action="store_true", help="post comments (LINEAR_API_KEY)")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    registry = Registry(args.root.resolve())
    try:
        if args.command == "check":
            runner = None if args.no_tests else run_pytest
            named = [*args.package, *(registry.lock() if args.all else [])]
            if not named:
                return _finish(check_registry(registry))
            return _finish(check_packages(registry, dict.fromkeys(named), runner=runner))
        if args.command == "check-owner":
            return _finish(check_owner(registry, args.package))
        if args.command == "matrix":
            return _finish(matrix(registry, check=args.check))
        if args.command == "register":
            changed = register(registry, args.package)
            _print([f"registered {args.package}: {', '.join(changed) or 'already registered'}"])
            return 0
        key = environ.get("LINEAR_API_KEY")
        if args.post and not key:
            raise ContractError("--post needs LINEAR_API_KEY; nothing was written")
        notes = bump(registry, args.contract, args.version, status=args.status)
        _print([f"wrote contracts/{args.contract}/v{args.version}/"])
        for note in notes:
            target = note.issue or f"<no gate issue for {note.package} in packages.toml>"
            _print([f"--- comment for {target} ({note.package})", note.body])
        if args.post and key:
            for note in notes:
                if note.issue is not None:
                    post_comment(note.issue, note.body, key)
                    _print([f"posted to {note.issue}"])
    except ContractError as error:
        _print([f"FAIL: {error}"], sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
