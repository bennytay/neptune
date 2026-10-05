"""The four stages, compiler -> ledger -> memory -> context, and how each one runs.

A stage runs *real* (the package's own code) only when all of these hold, and as a *stub*
otherwise; the report says which, and why:

1. the package's contract entry point (``contract.toml`` ``[owner].module``) is importable;
2. its version constant, when it declares one, matches the registry's latest version of the contract
   it owns (an integer constant is the registry major, a string constant must equal the version);
3. every contract the package locks in ``contracts/lock.toml`` is within its latest major;
4. the harness has a real driver for it (``Stage.real``).

A stub never runs package code: it serves the registry's golden examples for the contract
(``contracts/<id>/v*/golden``), or a minimal canned document when the contract has no published
version yet. ``needs_services`` says that a *real* run of the stage needs the compose stack
(``harness/compose.yaml``); a stub never does.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from harness.contracts import load_tool

if TYPE_CHECKING:
    from pathlib import Path

    from harness.corpus import Case

Json = dict[str, Any]
STATUSES: Final = ("ok", "failed", "error", "skipped")


@dataclass
class Context:
    """What a driver may use: the registry, a scratch directory, the corpus, earlier outputs."""

    registry: Any
    work: Path
    cases: Sequence[Case]
    upstream: dict[str, Json] = field(default_factory=dict)

    def package_ids(self) -> list[str]:
        """The package id of every case the compiler stage ingested (sorted by case)."""
        compiled = self.upstream.get("compiler", {})
        return [str(item["package"]) for item in compiled.get("cases", []) if item.get("package")]


@dataclass(frozen=True)
class Outcome:
    """A driver's result: its output for the report, and the problems that make it fail."""

    output: Json
    problems: tuple[str, ...] = ()


Driver = Callable[[Context], Outcome]


@dataclass(frozen=True)
class Stage:
    id: str
    package: str  # the workspace package that owns the stage
    contract: str  # the contract it owns; its owner entry point decides real vs stub
    needs_services: bool  # a real run needs harness/compose.yaml
    real: Driver | None
    stub: Driver


@dataclass(frozen=True)
class Resolution:
    mode: str  # "real" | "stub"
    reason: str
    contract_version: str | None


# --- Real or stub ----------------------------------------------------------------------------


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _constant(reference: str) -> Any:
    module, _, attr = reference.partition(":")
    return getattr(importlib.import_module(module), attr)


def resolve(stage: Stage, registry: Any) -> Resolution:
    """Decide real or stub for ``stage``; the reason always says what decided it."""
    tool = load_tool()
    contract = registry.contract(stage.contract)
    latest = registry.latest(stage.contract, stable=True) or registry.latest(stage.contract)
    version = tool.show(latest.version) if latest is not None else None

    def stub(reason: str) -> Resolution:
        return Resolution("stub", reason, version)

    owner = contract.owner
    if not _importable(owner.module):
        return stub(f"{owner.module} is not importable")
    if latest is None:
        return stub(f"{stage.contract} has no published version")
    if owner.version_constant is not None:
        try:
            value = _constant(owner.version_constant)
        except (ImportError, AttributeError) as error:
            return stub(f"{owner.version_constant} cannot be read ({type(error).__name__})")
        expected: object = (
            latest.version[0] if isinstance(value, int) else tool.show(latest.version)
        )
        if value != expected:
            return stub(
                f"{owner.version_constant} is {value!r}, the registry's latest is {version}"
            )
    for contract_id, locked in sorted(registry.lock().get(stage.package, {}).items()):
        newest = registry.latest(contract_id, stable=True)
        if newest is not None and tool.parse_semver(locked)[0] < newest.version[0]:
            return stub(f"{stage.package} locks {contract_id} {locked}, a major behind")
    if stage.real is None:
        return stub(f"the harness has no real driver for {stage.package} yet")
    return Resolution("real", f"{owner.module} is importable and matches {version}", version)


# --- Compiler (real) -------------------------------------------------------------------------


def _pointer(version: Any, suffix: str) -> str:
    for name, pointer in sorted(version.goldens.items()):
        if name.endswith(suffix):
            return str(pointer)
    raise LookupError(f"{version.contract} {version.version} has no golden ending {suffix}")


def compiler_real(ctx: Context) -> Outcome:
    """Ingest every case with the compiler's SDK, read each package back and verify it, and
    validate its manifest and receipt against the registry's package-schema."""
    from neptune.sdk import Neptune, NeptuneError

    tool = load_tool()
    version = ctx.registry.latest("package-schema", stable=True)
    if version is None:
        return Outcome({"cases": []}, ("package-schema has no stable version to validate against",))
    schema = json.loads(version.schema_text)
    manifest_at, receipt_at = (
        _pointer(version, ".manifest.json"),
        _pointer(version, ".receipt.json"),
    )
    cases: list[Json] = []
    problems: list[str] = []
    for case in ctx.cases:
        row: Json = {"case": case.id}
        cases.append(row)
        destination = ctx.work / "packages" / case.id
        try:
            result = Neptune(ctx.work / "workspaces" / case.id).ingest(case.sources, destination)
            if not result.committed:
                row["state"] = str(result.state)
                problems.append(f"{case.id}: the job ended {result.state}, not committed")
                continue
            result.read_package()
            receipt = result.read_receipt()
        except NeptuneError as error:
            row["state"] = "error"
            problems.append(f"{case.id}: {error.code}: {error}")
            continue
        row.update(
            state="committed",
            package=str(result.package),
            receipt=str(result.receipt),
            sources=len(result.ingested),
            findings=dict(sorted(Counter(f.code for f in receipt.findings).items())),
            package_verified=True,
        )
        for field_name, pointer, document in (
            ("manifest_valid", manifest_at, "manifest.json"),
            ("receipt_valid", receipt_at, "receipt.json"),
        ):
            errors = tool.validate_golden(
                schema, pointer, json.loads((destination / document).read_text(encoding="utf-8"))
            )
            row[field_name] = not errors
            problems.extend(f"{case.id}: {document} breaks package-schema: {e}" for e in errors[:3])
        if case.gold is not None:
            gold = resolve_gold(destination, case.gold)
            row["gold"] = gold
            problems.extend(
                f"{case.id}: gold evidence {key} resolves to nothing"
                + (f" ({gold['reasons'][key]})" if key in gold["reasons"] else "")
                for key in gold["missing"]
            )
            problems.extend(f"{case.id}: {problem}" for problem in gold["problems"])
    return Outcome({"cases": cases}, tuple(problems))


def resolve_gold(package: Path, gold_path: Path) -> Json:
    """The case's gold answers checked, and their evidence resolved against its package
    (ADR 0006): counts, the evidence that resolved to nothing, and the gold file's own problems."""
    from harness.acceptance import resolve

    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    return {
        **resolve.summary(resolve.resolve(package, gold)),
        "corpus_version": gold.get("corpus_version"),
        "problems": resolve.check_gold(gold),
        "questions": len(gold.get("questions", [])),
    }


# --- Contract stubs --------------------------------------------------------------------------


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _golden_stub(contract_id: str, consumes: str | None) -> Driver:
    """A stub for the stage owning ``contract_id``: the registry's goldens for its latest
    version, or a canned marker when none is published. ``consumes`` is the upstream stage."""

    def run(ctx: Context) -> Outcome:
        tool = load_tool()
        version = ctx.registry.latest(contract_id)
        goldens: list[Json] = []
        if version is not None:
            goldens = [
                {"file": path.name, "sha256": _digest(path.read_bytes())}
                for path in sorted((version.path / "golden").glob("*.json"))
            ]
        output: Json = {
            "contract": contract_id,
            "contract_version": tool.show(version.version) if version else None,
            "served": "goldens" if goldens else "canned",
            "goldens": goldens,
            "consumed_packages": len(ctx.package_ids()) if consumes else 0,
        }
        if consumes:
            output["consumed_from"] = consumes
        return Outcome(output)

    return run


def ledger_stub(ctx: Context) -> Outcome:
    """Serves the catalog-api goldens (v0.0.0, draft: the registry's only catalog document)."""
    return _golden_stub("catalog-api", "compiler")(ctx)


def memory_stub(ctx: Context) -> Outcome:
    """graph-schema has no published version, so this serves a canned empty result."""
    return _golden_stub("graph-schema", "ledger")(ctx)


def _gold_query(ctx: Context) -> str | None:
    """The first gold question of the first case that has gold answers (the acceptance corpus's
    "why did the incident happen"), so the smoke query is the demo's question."""
    for case in ctx.cases:
        if case.gold is not None and case.gold.is_file():
            questions = json.loads(case.gold.read_text(encoding="utf-8")).get("questions", [])
            if questions:
                return str(questions[0]["question"])
    return None


def context_stub(ctx: Context) -> Outcome:
    """The context stage's stub also answers the smoke query: a golden packet when query-packet
    has one, else a minimal canned packet naming the packages that flowed in."""
    base = _golden_stub("query-packet", "memory")(ctx).output
    cases = [case.id for case in ctx.cases]
    query = _gold_query(ctx) or (
        f"what hardware and calibration does {cases[0] if cases else 'the robot'} carry?"
    )
    packet: Json
    if base["served"] == "goldens":
        version = ctx.registry.latest("query-packet")
        first = sorted((version.path / "golden").glob("*.json"))[0]
        packet = json.loads(first.read_text(encoding="utf-8"))
        source = f"golden {version.contract} {first.name}"
    else:
        packet = {
            "packet_format": "harness-canned-0",
            "query": query,
            "evidence": [
                {"case": case.id, "package": package}
                for case, package in zip(ctx.cases, ctx.package_ids(), strict=False)
            ],
        }
        source = "canned: query-packet has no published version"
    return Outcome({**base, "smoke": {"query": query, "packet_source": source, "packet": packet}})


STAGES: Final[tuple[Stage, ...]] = (
    Stage(
        "compiler",
        "neptune",
        "package-schema",
        False,
        compiler_real,
        _golden_stub("package-schema", None),
    ),
    Stage("ledger", "neptune-ledger", "catalog-api", True, None, ledger_stub),
    Stage("memory", "neptune-memory", "graph-schema", True, None, memory_stub),
    Stage("context", "neptune-context", "query-packet", True, None, context_stub),
)
