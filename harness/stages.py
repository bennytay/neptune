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

    def package_root(self, case_id: str) -> Path:
        """Where the compiler stage writes, and the ledger stage registers, a case's package."""
        return self.work / "packages" / case_id

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
        destination = ctx.package_root(case.id)
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
    return Outcome({"cases": cases}, tuple(problems))


# --- Ledger (real) ---------------------------------------------------------------------------

LEDGER_TENANT: Final = "harness"


def _embedded_postgres(ctx: Context) -> Any:
    """A PostgreSQL 16 server from the ``pgserver`` wheel (the server the Ledger's own catalog
    tests use; platform ADR 0006), with its data directory in the run's scratch."""
    from pgserver.postgres_server import get_server

    return get_server(ctx.work / "ledger-pgdata", cleanup_mode="delete")


def _catalog_database(server: Any) -> str:
    """A new C-collated database (Ledger ADR 0005 §4) on the server, migrated for the tenant."""
    import psycopg
    from neptune_ledger.catalog.migrate import apply_migrations
    from psycopg import sql

    name = "harness_catalog"
    with psycopg.connect(str(server.get_uri()), autocommit=True) as admin:
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
            ).format(sql.Identifier(name))
        )
    uri = str(server.get_uri(database=name))
    with psycopg.connect(uri, autocommit=True) as conn:
        apply_migrations(conn, LEDGER_TENANT)
    return uri


def _known(slot: Any) -> Any:
    """The value of a ``Known`` slot, else ``None`` (the slot's state is the catalog's answer)."""
    from neptune.model.knowledge import Known

    return slot.value if isinstance(slot, Known) else None


def ledger_real(ctx: Context) -> Outcome:
    """Register every package the compiler stage committed into the Ledger's real catalog
    (``PostgresCatalog`` on an embedded PostgreSQL), register it again, verify it, and validate
    every response against the registry's catalog-api schema. The package-schema version each
    package needs (its manifest's ``schema_version``, the lowest version whose readers read it;
    compiler ADR 0037) must be within the major ``neptune-ledger`` locks in ``contracts/lock.toml``.
    One case's refusal or error is that case's problem; the other cases still run. Platform
    ADR 0006."""
    from neptune_ledger.catalog.registry import PostgresCatalog

    tool = load_tool()
    version = ctx.registry.latest("catalog-api", stable=True)
    if version is None:
        return Outcome({"cases": []}, ("catalog-api has no stable version to validate against",))
    locked = ctx.registry.lock().get("neptune-ledger", {}).get("package-schema")
    check = _LedgerCheck(
        tool=tool,
        schema=json.loads(version.schema_text),
        pointers={
            "register": _pointer(version, ".registration.json"),
            "verify": _pointer(version, ".verify_report.json"),
        },
        locked=locked,
        locked_major=tool.parse_semver(locked)[0] if locked else None,
    )
    compiled = ctx.upstream.get("compiler", {}).get("cases") or []
    if not compiled:  # e.g. a stub compiler: a real ledger that registered nothing is not green
        return Outcome(
            {"cases": [], "locked_package_schema": locked, "tenant": LEDGER_TENANT},
            ("the compiler stage compiled no case to register",),
        )
    cases: list[Json] = []
    server = _embedded_postgres(ctx)
    try:
        uri = _catalog_database(server)
        roots = (ctx.work / "packages",)
        with PostgresCatalog(uri, LEDGER_TENANT, package_roots=roots) as catalog:
            for upstream in compiled:
                case, package = str(upstream["case"]), upstream.get("package")
                row: Json = {"case": case}
                cases.append(row)
                if not package:
                    row["registration"] = "not_attempted"
                    check.problems.append(f"{case}: the compiler stage committed no package")
                    continue
                try:
                    check.case(catalog, row, ctx.package_root(case), str(package))
                except Exception as error:  # one case's failure is a finding, not the stage's
                    # The type only: a message can carry the server's socket path.
                    row["error"] = type(error).__name__
                    check.problems.append(f"{case}: the catalog raised {type(error).__name__}")
    finally:
        server.cleanup()
    output: Json = {"cases": cases, "locked_package_schema": locked, "tenant": LEDGER_TENANT}
    return Outcome(output, tuple(check.problems))


@dataclass
class _LedgerCheck:
    """What the ledger stage checks per case, and the problems it found."""

    tool: Any
    schema: Json
    pointers: dict[str, str]
    locked: str | None
    locked_major: int | None
    problems: list[str] = field(default_factory=list)

    def _valid(self, case: str, call: str, response: Any) -> bool:
        from neptune_ledger.api import codec

        errors = self.tool.validate_golden(
            self.schema, self.pointers[call], codec.to_json(response)
        )
        self.problems.extend(f"{case}: {call} breaks catalog-api: {e}" for e in errors[:3])
        return not errors

    def case(self, catalog: Any, row: Json, root: Path, package: str) -> None:
        case = str(row["case"])
        first = catalog.register(root)
        row.update(
            registration=first.outcome,
            registration_findings=dict(sorted(Counter(f.code for f in first.findings).items())),
            responses_valid=self._valid(case, "register", first),
        )
        if first.outcome != "registered":
            codes = ", ".join(sorted({f.code for f in first.findings})) or "no finding"
            self.problems.append(f"{case}: register was {first.outcome} ({codes})")
            return  # nothing was registered: re-registering, verifying or the lock say nothing
        if _known(first.package_id) != package:
            self.problems.append(
                f"{case}: the catalog registered {_known(first.package_id)}, not {package}"
            )
        again = catalog.register(root)
        report = catalog.verify(package)
        needs = _known(first.schema_version)
        key = _known(first.registration_key)
        again_valid = self._valid(case, "register", again)
        report_valid = self._valid(case, "verify", report)
        row.update(
            tx_seq=key.tx_seq if key is not None else None,  # never tx_time: a clock
            schema_version=needs,
            records=sum(count.count for count in first.record_counts),
            reregistration=again.outcome,
            verify=report.verdict,
            files_checked=report.files_checked,
            responses_valid=row["responses_valid"] and again_valid and report_valid,
        )
        if again.outcome != "already_registered":
            self.problems.append(f"{case}: a second register was {again.outcome}")
        if report.verdict != "intact":
            self.problems.append(f"{case}: verify was {report.verdict}")
        if self.locked_major is not None and (needs is None or needs > self.locked_major):
            self.problems.append(
                f"{case}: the package needs package-schema {needs}, "
                f"neptune-ledger locks {self.locked}"
            )


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
    """Serves the goldens of catalog-api's latest version (used only when the stage is a stub)."""
    return _golden_stub("catalog-api", "compiler")(ctx)


def memory_stub(ctx: Context) -> Outcome:
    """Serves the goldens of graph-schema's latest version (a canned marker when none exists)."""
    return _golden_stub("graph-schema", "ledger")(ctx)


def context_stub(ctx: Context) -> Outcome:
    """The context stage's stub also answers the smoke query: a golden packet when query-packet
    has one, else a minimal canned packet naming the packages that flowed in."""
    base = _golden_stub("query-packet", "memory")(ctx).output
    cases = [case.id for case in ctx.cases]
    query = f"what hardware and calibration does {cases[0] if cases else 'the robot'} carry?"
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
    Stage("ledger", "neptune-ledger", "catalog-api", False, ledger_real, ledger_stub),
    Stage("memory", "neptune-memory", "graph-schema", True, None, memory_stub),
    Stage("context", "neptune-context", "query-packet", True, None, context_stub),
)
