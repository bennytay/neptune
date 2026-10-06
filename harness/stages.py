"""The five stages, compiler -> deploy -> ledger -> memory -> context, and how each one runs.

The deploy stage maps each compiled package with the Deploy presets and templates its case
declares (``Case.deploy``; Platform ADR 0008), and its packages flow into the ledger stage beside
the compiler's.

A stage runs *real* (the package's own code) only when all of these hold, and as a *stub*
otherwise; the report says which, and why:

1. the package's contract entry point (``contract.toml`` ``[owner].module``) is importable, and so
   is the stage's own ``entry`` module when it names one (Deploy owns no contract: its stage writes
   package-schema packages through ``neptune_deploy``);
2. its version constant, when it declares one, matches the registry's latest version of the contract
   it owns (an integer constant is the registry major, a string constant must equal the version),
   and so does the stage's ``built_against`` constant (Deploy's ``PACKAGE_SCHEMA_VERSION``);
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
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from harness.contracts import load_tool
from harness.corpus import REPO

if TYPE_CHECKING:
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

    def deploy_root(self, case_id: str) -> Path:
        """Where the deploy stage writes, and the ledger stage registers, a case's mapped one."""
        return self.work / "packages" / f"{case_id}.deploy"

    def package_ids(self, stage: str = "compiler") -> list[str]:
        """The package id of every case ``stage`` wrote (default the compiler; sorted by case)."""
        written = self.upstream.get(stage, {})
        return [str(item["package"]) for item in written.get("cases", []) if item.get("package")]

    def flowing_ids(self) -> list[str]:
        """Every package that flows downstream: the compiled ones, then the mapped ones."""
        return self.package_ids() + self.package_ids("deploy")


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
    entry: str | None = None  # a module of the stage's package that a real run also needs
    # the stage package's own constant naming the contract version it was built against, for a
    # package that writes a contract it does not own; checked as the owner's constant is
    built_against: str | None = None


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
    if stage.entry is not None and not _importable(stage.entry):
        return stub(f"{stage.entry} is not importable")
    if latest is None:
        return stub(f"{stage.contract} has no published version")
    for constant in (owner.version_constant, stage.built_against):
        if constant is None:
            continue
        try:
            value = _constant(constant)
        except (ImportError, AttributeError) as error:
            return stub(f"{constant} cannot be read ({type(error).__name__})")
        expected: object = (
            latest.version[0] if isinstance(value, int) else tool.show(latest.version)
        )
        if value != expected:
            return stub(f"{constant} is {value!r}, the registry's latest is {version}")
    for contract_id, locked in sorted(registry.lock().get(stage.package, {}).items()):
        newest = registry.latest(contract_id, stable=True)
        if newest is not None and tool.parse_semver(locked)[0] < newest.version[0]:
            return stub(f"{stage.package} locks {contract_id} {locked}, a major behind")
    if stage.real is None:
        return stub(f"the harness has no real driver for {stage.package} yet")
    module = stage.entry or owner.module
    return Resolution("real", f"{module} is importable and matches {version}", version)


# --- Compiler (real) -------------------------------------------------------------------------


def _pointer(version: Any, suffix: str) -> str:
    for name, pointer in sorted(version.goldens.items()):
        if name.endswith(suffix):
            return str(pointer)
    raise LookupError(f"{version.contract} {version.version} has no golden ending {suffix}")


@dataclass(frozen=True)
class _PackageSchema:
    """The registry's stable package-schema, and the goldens a manifest and a receipt match."""

    tool: Any
    schema: Json
    manifest_at: str
    receipt_at: str

    @staticmethod
    def latest(registry: Any) -> _PackageSchema | None:
        version = registry.latest("package-schema", stable=True)
        if version is None:
            return None
        return _PackageSchema(
            load_tool(),
            json.loads(version.schema_text),
            _pointer(version, ".manifest.json"),
            _pointer(version, ".receipt.json"),
        )

    def check(self, label: str, root: Path, row: Json, problems: list[str]) -> None:
        """``manifest_valid`` and ``receipt_valid`` on ``row``; schema breaks, as problems."""
        for field_name, pointer, document in (
            ("manifest_valid", self.manifest_at, "manifest.json"),
            ("receipt_valid", self.receipt_at, "receipt.json"),
        ):
            errors = self.tool.validate_golden(
                self.schema, pointer, json.loads((root / document).read_text(encoding="utf-8"))
            )
            row[field_name] = not errors
            problems.extend(f"{label}: {document} breaks package-schema: {e}" for e in errors[:3])


def compiler_real(ctx: Context) -> Outcome:
    """Ingest every case with the compiler's SDK, read each package back and verify it, and
    validate its manifest and receipt against the registry's package-schema."""
    from neptune.sdk import Neptune, NeptuneError

    schema = _PackageSchema.latest(ctx.registry)
    if schema is None:
        return Outcome({"cases": []}, ("package-schema has no stable version to validate against",))
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
        schema.check(case.id, destination, row, problems)
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
    (ADR 0007): counts, the evidence that resolved to nothing, and the gold file's own problems."""
    from harness.acceptance import resolve

    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    return {
        **resolve.summary(resolve.resolve(package, gold)),
        "corpus_version": gold.get("corpus_version"),
        "problems": resolve.check_gold(gold),
        "questions": len(gold.get("questions", [])),
    }


# --- Deploy (real) ---------------------------------------------------------------------------

DEPLOY_FORMAT: Final = 1
DEPLOY_TIMEOUT_S: Final = 900  # the mapper streams; the acceptance corpus maps in about a second


SOURCE_KEYS: Final = frozenset({"preset", "source", "civil_time_zone"})


@dataclass(frozen=True)
class SourceZone:
    """One ``sources`` entry (Platform ADR 0009): the civil time zone a declared preset reads one
    source's zone-less times in. ``source`` is the corpus path of the source; the zone is an IANA
    name, checked by spelling only (root ADR 0061 §1)."""

    preset: str
    source: str
    civil_time_zone: str


@dataclass(frozen=True)
class DeployPlan:
    """A case's declaration (``deploy.json``, Platform ADR 0008): Deploy presets by name, document
    templates by repository-relative path (a file or a directory of them), the lifecycle
    record counts the mapped package must reach, and the civil zones of sources whose exports
    state none (ADR 0009)."""

    presets: tuple[str, ...]
    templates: tuple[str, ...]
    at_least: dict[str, int]
    sources: tuple[SourceZone, ...] = ()
    # Every scope entry of every stated same-event assertion must be an identifier the mapped
    # package declares (``require_assertion_scopes``; ADR 0009).
    require_assertion_scopes: bool = False


def _plain_path(path: str) -> bool:
    """A relative POSIX path with no empty, ``.`` or ``..`` part: a corpus path, never a way out."""
    parts = path.split("/")
    return "\\" not in path and "\0" not in path and all(p not in ("", ".", "..") for p in parts)


def _read_sources(
    value: Any, presets: list[str] | None
) -> tuple[tuple[SourceZone, ...], list[str]]:
    """The ``sources`` entries, and why any cannot be used; ``presets`` is None when the
    declaration's own presets did not read, so membership is not judged against them."""
    from neptune.model.reference import check_iana_zone

    if not isinstance(value, list):
        return (), ["sources is not a list of entries"]
    out: list[SourceZone] = []
    problems: list[str] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) != SOURCE_KEYS:
            problems.append(f"sources[{i}] is not {{{', '.join(sorted(SOURCE_KEYS))}}}")
            continue
        if not all(isinstance(entry[key], str) and entry[key] for key in SOURCE_KEYS):
            problems.append(f"sources[{i}] has a value that is not a name")
            continue
        zone = SourceZone(entry["preset"], entry["source"], entry["civil_time_zone"])
        if presets is not None and zone.preset not in presets:
            problems.append(f"sources[{i}] names preset {zone.preset}, which is not declared")
        if not _plain_path(zone.source):
            problems.append(f"sources[{i}] source {zone.source} is not a plain corpus path")
        try:
            check_iana_zone(f"sources[{i}] civil_time_zone", zone.civil_time_zone)
        except ValueError as error:
            problems.append(str(error))
        out.append(zone)
    if len({(z.preset, z.source) for z in out}) != len(out):
        problems.append("sources declares one preset's source twice")
    return tuple(sorted(out, key=lambda z: (z.preset, z.source))), problems


def read_deploy(path: Path) -> tuple[DeployPlan | None, list[str]]:
    """The declaration at ``path``, or ``None`` and why it cannot be used."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, [f"the deploy declaration cannot be read ({type(error).__name__})"]
    if not isinstance(document, dict):
        return None, ["the deploy declaration is not a JSON object"]
    problems: list[str] = []
    if document.get("deploy_format") != DEPLOY_FORMAT:
        problems.append(f"deploy_format is {document.get('deploy_format')!r}, not {DEPLOY_FORMAT}")
    names: dict[str, list[str]] = {}
    for key in ("presets", "templates"):
        value = document.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            problems.append(f"{key} is not a list of names")
            value = []
        if len(set(value)) != len(value):
            problems.append(f"{key} repeats an entry")
        names[key] = sorted(set(value))
    for template in names["templates"]:
        where = (REPO / template).resolve()
        if Path(template).is_absolute() or not where.is_relative_to(REPO):
            problems.append(f"template {template} is not a path inside the repository")
        elif not where.exists():
            problems.append(f"template {template} does not exist")
    at_least = document.get("at_least", {})
    if not isinstance(at_least, dict) or not all(
        isinstance(k, str) and type(v) is int and v >= 1 for k, v in at_least.items()
    ):
        problems.append("at_least is not an object of record kinds to counts of at least 1")
        at_least = {}
    if not names["presets"] and not names["templates"]:
        problems.append("the deploy declaration names no preset and no template")
    scopes = document.get("require_assertion_scopes", False)
    if type(scopes) is not bool:
        problems.append("require_assertion_scopes is not true or false")
    presets_read = not any(problem.startswith("presets ") for problem in problems)
    sources, refused = _read_sources(
        document.get("sources", []), names["presets"] if presets_read else None
    )
    problems += refused
    if problems:
        return None, problems
    plan = DeployPlan(
        tuple(names["presets"]), tuple(names["templates"]), dict(at_least), sources, scopes
    )
    return plan, []


# Deploy's preset families, by module: each publishes ``PRESETS`` (names) and ``preset(name)``
# (a mapping with ``sha256``), and ``neptune_deploy map -p`` takes a name from any of them. A
# family this Deploy does not have yet is passed over (Deploy ADR 0017 adds ``eventlogs``).
PRESET_FAMILIES: Final = ("neptune_deploy.lifecycle", "neptune_deploy.eventlogs")


def _shipped_presets() -> tuple[dict[str, str], list[str]]:
    """Every preset Deploy ships, by name, mapped to its file's sha256 (what its transform
    record's ``mapping_sha256`` names); and a name two families both ship, which ``-p`` could not
    tell apart. Only each family's public ``PRESETS`` and ``preset`` are used."""
    shipped: dict[str, str] = {}
    problems: list[str] = []
    for family in PRESET_FAMILIES:
        try:
            module = importlib.import_module(family)
        except ModuleNotFoundError as error:
            if error.name == family:
                continue  # a family this Deploy does not ship
            raise
        if not hasattr(module, "PRESETS") or not callable(getattr(module, "preset", None)):
            continue  # not a family (a namespace package left by a cache, a renamed module)
        for name in module.PRESETS:
            if name in shipped:
                problems.append(f"Deploy ships preset {name!r} in two families")
                continue
            shipped[name] = str(module.preset(name).sha256)
    return shipped, problems


def _declarations(plan: DeployPlan) -> tuple[dict[str, str], list[str]]:
    """Each declared preset and template file, by the sha256 its transform record names, mapped to
    the declaration that brought it in (``preset:<name>``, ``template:<path>``); and why the
    declaration cannot run: a preset Deploy does not ship in any family, a file declared twice.
    Only Deploy's public names are used (each family's ``PRESETS`` and ``preset``,
    ``TemplateRegistry``)."""
    from neptune_deploy.lifecycle import TemplateRegistry

    shipped, problems = _shipped_presets()
    labels: dict[str, str] = {}
    problems += [f"Deploy ships no preset {name!r}" for name in plan.presets if name not in shipped]
    declared = [(shipped[name], f"preset:{name}") for name in plan.presets if name in shipped]
    for path in plan.templates:
        declared += [
            (str(template.sha256), f"template:{path}")
            for template in TemplateRegistry.from_paths([REPO / path]).templates()
        ]
    for sha, label in declared:
        if sha in labels:
            problems.append(f"{label} declares the same file as {labels[sha]}")
        labels.setdefault(sha, label)
    return labels, problems


def _lifecycle_records(root: Path) -> tuple[dict[str, int], Counter[str]]:
    """The mapped package's lifecycle record counts by kind, and by the transform that made each."""
    from neptune.model.lifecycle import LIFECYCLE_KINDS

    kinds: dict[str, int] = {}
    transforms: Counter[str] = Counter()
    for kind in sorted(k.kind for k in LIFECYCLE_KINDS):
        path = root / "records" / f"{kind}.jsonl"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as lines:  # one record at a time (ADR 0070)
            for line in lines:
                if line.strip():
                    record = json.loads(line)
                    kinds[kind] = kinds.get(kind, 0) + 1
                    transforms[str(record.get("provenance", {}).get("transform"))] += 1
    return kinds, transforms


def _zone_problems(
    plan: DeployPlan,
    compiled: Path,
    mapped: Path,
    labels: dict[str, str],
    applied: dict[str, str],
) -> list[str]:
    """Why a declared source zone was not applied: the mapped package must hold, for each
    ``sources`` entry, a ``civil_time_zone`` record citing that source and made by that preset's
    transform, and every such record must state the declared zone (ADR 0009)."""
    from harness.acceptance.resolve import Package, _known, _source

    base, out = Package(compiled), Package(mapped)
    problems: list[str] = []
    for zone in plan.sources:
        content = base.content(zone.source)
        if content is None:
            problems.append(
                f"sources names {zone.source}, which the compiled package does not hold"
            )
            continue
        states = [
            record.get("zone")
            for record in out.kind("civil_time_zone")
            if _source(record) == content
            and labels.get(applied.get(str(record.get("provenance", {}).get("transform")), ""))
            == f"preset:{zone.preset}"
        ]
        if not states:
            problems.append(
                f"preset:{zone.preset} wrote no civil time zone for {zone.source}"
                f" (declared {zone.civil_time_zone})"
            )
        elif any(_known(state) != zone.civil_time_zone for state in states):
            problems.append(
                f"preset:{zone.preset} did not apply the declared zone {zone.civil_time_zone}"
                f" to every clock of {zone.source}"
            )
    return problems


Pair = tuple[str, str]


def _pair(value: Any) -> Pair | None:
    """An identifier's ``(namespace, value)``, kept as two fields and never joined."""
    if not isinstance(value, dict):
        return None
    namespace, text = value.get("namespace"), value.get("value")
    return (namespace, text) if isinstance(namespace, str) and isinstance(text, str) else None


def _declared_identifiers(root: Path) -> set[Pair]:
    """Every ``(namespace, value)`` the mapped package declares: each lifecycle record's own
    ``identifiers`` (a bare list or a ``Known`` list; ADR 0061 §5), and each row's cell of a typed
    table's ``@id:<namespace>`` column (Deploy ADR 0017 §1), as the cell's text."""
    from harness.acceptance.resolve import Package, _known
    from neptune.model.lifecycle import LIFECYCLE_KINDS

    package = Package(root)
    out: set[Pair] = set()
    for kind in sorted(k.kind for k in LIFECYCLE_KINDS):
        for record in package.kind(kind):
            listed = record.get("identifiers")
            items = _known(listed) if isinstance(listed, dict) else listed
            for item in items if isinstance(items, list) else []:
                pair = _pair(
                    _known(item) if isinstance(item, dict) and "knowledge" in item else item
                )
                if pair is not None:
                    out.add(pair)
    columns: dict[str, dict[int, str]] = {}
    for table in package.kind("structured_table"):
        header = _known(table.get("header"))
        ids = {
            i: name.removeprefix("@id:")
            for i, name in enumerate(header if isinstance(header, list) else [])
            if isinstance(name, str) and name.startswith("@id:") and len(name) > 4
        }
        if ids:
            columns[str(table["id"])] = ids
    for row in package.kind("structured_record"):
        cells = row.get("cells", [])
        for i, namespace in columns.get(str(row.get("table")), {}).items():
            text = _known(cells[i]) if i < len(cells) else None
            if isinstance(text, str):
                out.add((namespace, text))
    return out


def _same_event_scopes(root: Path) -> list[tuple[Pair | None, list[Any]]]:
    """Each stated same-event assertion of the compiled package (root ADR 0062: ``same_identity``
    whose payload's ``relation`` is ``same_event``): its own identifier and its scope entries."""
    from harness.acceptance.resolve import Package, _known

    out = []
    for record in Package(root).kind("assertion"):
        if record.get("provenance", {}).get("assertion_kind") != "stated":
            continue
        if _known(record.get("assertion_type")) != "same_identity":
            continue
        payload = _known(record.get("payload"))
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if not isinstance(payload, dict) or payload.get("relation") != "same_event":
            continue
        scope = _known(record.get("scope"))
        out.append(
            (_pair(_known(record.get("identifier"))), scope if isinstance(scope, list) else [])
        )
    return out


def _dangling_scopes(compiled: Path, mapped: Path) -> list[str]:
    """Why a same-event link would dangle: a scope entry whose ``(namespace, value)`` no record of
    the mapped package declares, so Memory has nothing to join it to (ADR 0009). Never green on
    nothing: a case that requires the check and holds no such assertion is a problem too."""
    assertions = _same_event_scopes(compiled)
    if not assertions:
        return ["require_assertion_scopes, but the package holds no stated same-event assertion"]
    declared = _declared_identifiers(mapped)
    problems = []
    for ident, scope in assertions:
        name = "/".join(ident) if ident else "an assertion without an identifier"
        for entry in scope:
            pair = _pair(entry)
            if pair is None:
                problems.append(f"assertion {name}: a scope entry is not a namespace and value")
            elif pair not in declared:
                problems.append(
                    f"assertion {name}: scope ({pair[0]}, {pair[1]}) is no identifier the mapped"
                    " package declares (a dangling link)"
                )
    return problems


def _event_rows(root: Path) -> tuple[dict[str, int], Counter[str]]:
    """The mapped package's typed event-table rows (Deploy ADR 0017 §1: ``structured_record`` rows
    a mapping writes, such as ``syslog_csv``'s ``syslog events``), counted by their table's name and
    by the transform that made each. The base package's tables are not copied into a mapped
    package, so every row here is Deploy's."""
    from harness.acceptance.resolve import _known

    names: dict[str, str] = {}
    path = root / "records" / "structured_table.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        if line.strip():
            table = json.loads(line)
            names[str(table["id"])] = str(_known(table.get("name")) or table["id"])
    by_table: dict[str, int] = {}
    transforms: Counter[str] = Counter()
    path = root / "records" / "structured_record.jsonl"
    if path.is_file():
        with path.open(encoding="utf-8") as lines:  # one record at a time (ADR 0070)
            for line in lines:
                if line.strip():
                    row = json.loads(line)
                    name = names.get(str(row.get("table")), str(row.get("table")))
                    by_table[name] = by_table.get(name, 0) + 1
                    transforms[str(row.get("provenance", {}).get("transform"))] += 1
    return dict(sorted(by_table.items())), transforms


def _transform_sources(root: Path) -> dict[str, str]:
    """Each Deploy transform record's id, mapped to the sha256 of the mapping or template file it
    applied (its config's ``mapping_sha256`` or ``template_sha256``)."""
    path = root / "records" / "transform_record.jsonl"
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        record = json.loads(line)
        config = record.get("config") or {}
        applied = config.get("mapping_sha256") or config.get("template_sha256")
        if applied:
            out[str(record["id"])] = str(applied)
    return out


def _map(
    ctx: Context,
    case: Case,
    plan: DeployPlan,
    schema: _PackageSchema,
    row: Json,
    problems: list[str],
) -> None:
    """``python -m neptune_deploy map`` over one compiled package, then the mapped package read
    back, verified, validated and counted."""
    from neptune.store.package import PackageError, read_package

    shipped, _ = _shipped_presets()
    labels, refused = _declarations(plan)
    problems.extend(f"{case.id}: {problem}" for problem in refused)
    out = ctx.deploy_root(case.id)
    argv = [sys.executable, "-m", "neptune_deploy", "map", str(ctx.package_root(case.id))]
    argv += [arg for name in plan.presets if name in shipped for arg in ("-p", name)]
    argv += [arg for path in plan.templates for arg in ("-t", str(REPO / path))]
    argv += [  # only for presets the run maps: Deploy refuses a zone for any other
        arg
        for zone in plan.sources
        if zone.preset in shipped
        for arg in ("--source-zone", zone.preset, zone.source, zone.civil_time_zone)
    ]
    argv += ["-o", str(out)]
    done = subprocess.run(
        argv, cwd=REPO, capture_output=True, text=True, timeout=DEPLOY_TIMEOUT_S, check=False
    )
    if done.returncode != 0:
        row["state"] = "error"
        last = (done.stderr.strip().splitlines() or ["no message"])[-1]
        problems.append(f"{case.id}: neptune_deploy map exited {done.returncode}: {last}")
        return
    try:
        package = read_package(out)
    except PackageError as error:
        row["state"] = "error"
        problems.append(f"{case.id}: the mapped package does not verify: {error}")
        return
    receipt = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    kinds, by_transform = _lifecycle_records(out)
    tables, row_transforms = _event_rows(out)
    applied = _transform_sources(out)
    made: Counter[str] = Counter({label: 0 for label in labels.values()})
    for transform, count in (by_transform + row_transforms).items():
        label = labels.get(applied.get(transform, ""))
        if label is not None:
            made[label] += count
    # What at_least counts: lifecycle records by kind, and event-table rows as structured_record.
    counted = {**kinds, **({"structured_record": sum(tables.values())} if tables else {})}
    row.update(
        state="committed",
        package=str(package.id),
        package_verified=True,
        records=kinds,
        event_rows=tables,
        by_declaration=dict(sorted(made.items())),
        findings=dict(sorted(Counter(f["code"] for f in receipt.get("findings", [])).items())),
    )
    schema.check(f"{case.id} (deploy)", out, row, problems)
    if not kinds and not tables:  # never green on nothing: the case declares mappings
        problems.append(f"{case.id}: the Deploy map wrote no lifecycle record or event-table row")
    problems.extend(
        f"{case.id}: {label} mapped no record" for label, count in sorted(made.items()) if not count
    )
    problems.extend(
        f"{case.id}: {kind} records are {counted.get(kind, 0)}, the declaration needs at least {n}"
        for kind, n in sorted(plan.at_least.items())
        if counted.get(kind, 0) < n
    )
    if plan.sources:
        row["zones"] = [
            {"preset": z.preset, "source": z.source, "civil_time_zone": z.civil_time_zone}
            for z in plan.sources
        ]
        zones = _zone_problems(plan, ctx.package_root(case.id), out, labels, applied)
        problems.extend(f"{case.id}: {problem}" for problem in zones)
    if plan.require_assertion_scopes:
        dangling = _dangling_scopes(ctx.package_root(case.id), out)
        row["assertion_scopes"] = "declared" if not dangling else "dangling"
        problems.extend(f"{case.id}: {problem}" for problem in dangling)


def deploy_real(ctx: Context) -> Outcome:
    """Map every compiled case that declares Deploy mappings with ``python -m neptune_deploy map``
    into a new package (``Context.deploy_root``), which the ledger stage registers beside the
    compiled one. A case that declares none is passed over; one whose mapping fails is that case's
    problem, and an unmapped table is a finding in the mapped package's receipt, never a problem.
    Platform ADR 0008."""
    compiled = {str(row["case"]): row for row in ctx.upstream.get("compiler", {}).get("cases", [])}
    cases: list[Json] = []
    problems: list[str] = []
    schema = _PackageSchema.latest(ctx.registry)
    if schema is None:
        return Outcome({"cases": []}, ("package-schema has no stable version to validate against",))
    for case in ctx.cases:
        row: Json = {"case": case.id, "declared": case.deploy is not None}
        cases.append(row)
        if case.deploy is None:
            continue
        plan, unusable = read_deploy(case.deploy)
        problems.extend(f"{case.id}: {problem}" for problem in unusable)
        if plan is None:
            continue
        row.update(presets=list(plan.presets), templates=list(plan.templates))
        if not compiled.get(case.id, {}).get("package"):
            row["state"] = "not_attempted"
            problems.append(f"{case.id}: the compiler stage committed no package to map")
            continue
        try:
            _map(ctx, case, plan, schema, row, problems)
        except Exception as error:  # one case's failure is a problem, not the stage's (partial)
            # The type only: a timeout's or a parser's message carries paths and arguments.
            row["state"] = "error"
            problems.append(f"{case.id}: the Deploy map raised {type(error).__name__}")
    return Outcome({"cases": cases}, tuple(problems))


def deploy_stub(ctx: Context) -> Outcome:
    """Maps nothing (the stage is a stub only when Deploy cannot run): the packages flow on as
    the compiler wrote them, and the report says so."""
    return Outcome({"cases": [], "mapped": "nothing: the deploy stage is a stub"})


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


def _to_register(ctx: Context) -> list[tuple[str, str, Path, str | None]]:
    """(label, stage, root, package id) of every package to register: each case the compiler
    stage compiled, then each package the deploy stage mapped (labelled ``<case>.deploy``)."""
    out: list[tuple[str, str, Path, str | None]] = []
    for upstream in ctx.upstream.get("compiler", {}).get("cases") or []:
        case = str(upstream["case"])
        out.append((case, "compiler", ctx.package_root(case), upstream.get("package")))
    for upstream in ctx.upstream.get("deploy", {}).get("cases") or []:
        case = str(upstream["case"])
        if upstream.get("package"):
            out.append((f"{case}.deploy", "deploy", ctx.deploy_root(case), upstream["package"]))
    return out


def ledger_real(ctx: Context) -> Outcome:
    """Register every package the compiler and deploy stages committed into the Ledger's real
    catalog (``PostgresCatalog`` on an embedded PostgreSQL), register it again, verify it, and
    validate every response against the registry's catalog-api schema. The package-schema version
    each package needs (its manifest's ``schema_version``, the lowest version whose readers read
    it; compiler ADR 0037) must be within the major ``neptune-ledger`` locks in
    ``contracts/lock.toml``. One package's refusal or error is that package's problem; the others
    still run. Platform ADR 0006."""
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
    if locked is None:  # the lock cannot be honoured, so the stage cannot be green
        check.problems.append("neptune-ledger has no package-schema entry in contracts/lock.toml")
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
            for case, stage, root, package in _to_register(ctx):
                row: Json = {"case": case, "stage": stage}
                cases.append(row)
                if not package:
                    row["registration"] = "not_attempted"
                    check.problems.append(f"{case}: the {stage} stage committed no package")
                    continue
                try:
                    check.case(catalog, row, root, str(package))
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
            "consumed_packages": len(ctx.flowing_ids()) if consumes else 0,
        }
        if consumes:
            output["consumed_from"] = consumes
        return Outcome(output)

    return run


def ledger_stub(ctx: Context) -> Outcome:
    """Serves the goldens of catalog-api's latest version (used only when the stage is a stub)."""
    return _golden_stub("catalog-api", "deploy")(ctx)


def memory_stub(ctx: Context) -> Outcome:
    """Serves the goldens of graph-schema's latest version (a canned marker when none exists)."""
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
    Stage(
        "deploy",
        "neptune-deploy",
        "package-schema",
        False,
        deploy_real,
        deploy_stub,
        entry="neptune_deploy.lifecycle",
        built_against="neptune_deploy:PACKAGE_SCHEMA_VERSION",
    ),
    Stage("ledger", "neptune-ledger", "catalog-api", False, ledger_real, ledger_stub),
    Stage("memory", "neptune-memory", "graph-schema", True, None, memory_stub),
    Stage("context", "neptune-context", "query-packet", True, None, context_stub),
)
