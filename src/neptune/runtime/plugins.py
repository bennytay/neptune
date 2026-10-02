"""Plugin adapters and Sources, read from installed distributions' entry points (ADR 0058).

A workspace member or a third-party distribution reaches the compiler through two entry-point
groups (Deploy ADR 0001):

- ``neptune.adapters``: the name is the adapter id; the value is a zero-argument callable that
  returns an object with the four-method ABI (ADR 0024).
- ``neptune.sources``: the name is the connector id; the value is a callable returning a
  ``Source``. It is imported and admitted here, never called: what a connector is given is the
  connector issue's to decide (MVL-153).

``load_plugins`` reads both, the same way on every host:

- Distributions are found on the import path (``sys.path`` by default); one name found twice is
  the first, as ``import`` would load it. Entry points are then ordered by (normalised
  distribution name, entry-point name), so the order distributions were installed in, or appear
  on the path in, changes nothing.
- ``PluginPolicy`` decides which distributions are read: all of them (the default), only those an
  allowlist names, or none. A distribution the policy leaves out leaves no trace, so a package made
  without plugins does not depend on what happens to be installed.
- A plugin that cannot be imported or built, or is not an adapter of this ABI, or is named by its
  entry point as another id, is refused with a finding (``load_failed``, ``refused``). Two plugins
  with one id, or a plugin with a built-in's id, are refused with a ``duplicate_id`` finding each:
  none of them is used, so no order can decide which wins.
- An admitted adapter is wrapped so its descriptor lists its distribution and version among its
  ``libraries``: they enter its transform record, so its records' provenance and every cache key
  (plan, chunk) name the plugin that made them, and upgrading the plugin is a new lineage.

Findings are about no bytes, so their subject is an ``ExternalObjectRef``: connector
``neptune.plugins``, the object ``<group>/<distribution>/<entry point>``, the distribution's
version as the revision. Messages and details hold names, versions and exception class names,
never an exception's text, which may hold a path. The findings name this loader's transform, whose
config is the policy, and enter a package only with them (as the runtime's do).

Importing a plugin runs its code in this process, as importing any installed library does;
everything it is then asked to do (probe, plan, ingest) runs in the sandbox under the same laws as a
built-in adapter's (ADR 0030, ``neptune.adapters.check``), because the job cannot tell them apart.
"""

import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import Distribution, EntryPoint, distributions
from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    Adapter,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    Documented,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    SourceReader,
)
from neptune.identity.findings import ingest_finding
from neptune.identity.provenance import transform_record
from neptune.model.finding import FindingCategory, IngestFinding, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import TransformRecord

ADAPTERS_GROUP: Final = "neptune.adapters"
SOURCES_GROUP: Final = "neptune.sources"
GROUPS: Final = (ADAPTERS_GROUP, SOURCES_GROUP)

PLUGINS_ID: Final = "neptune.plugins"
PLUGINS_VERSION: Final = "0.1.0"

LOAD_FAILED: Final = f"{PLUGINS_ID}.load_failed"
REFUSED: Final = f"{PLUGINS_ID}.refused"
DUPLICATE_ID: Final = f"{PLUGINS_ID}.duplicate_id"

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        DUPLICATE_ID,
        "two plugins, or a plugin and a built-in adapter, claim one id; every plugin claiming it"
        " is refused and none is used (ambiguous, warning)",
    ),
    Documented(
        LOAD_FAILED,
        "importing a plugin's entry point, or calling it to build the adapter, raised; the plugin"
        " is not used (failed, warning)",
    ),
    Documented(
        REFUSED,
        "a plugin loaded but is not admissible: not callable, no descriptor, a missing method,"
        " another ABI, an id other than its entry point's name, or a library pin that contradicts"
        " its distribution; the plugin is not used (failed, warning)",
    ),
)

# Why a plugin was refused: a ``refused`` finding's ``reason``.
NOT_CALLABLE: Final = "not_callable"
NO_DESCRIPTOR: Final = "no_descriptor"
MISSING_METHOD: Final = "missing_method"
OTHER_ABI: Final = "other_abi"
NAME_MISMATCH: Final = "name_mismatch"
LIBRARY_CONFLICT: Final = "library_conflict"
INVALID_NAME: Final = "invalid_name"

_METHODS: Final = ("probe", "inspect", "plan", "ingest")
_ID: Final = re.compile(r"[a-z][a-z0-9_.\-]*")
_DIST_SEPARATORS: Final = re.compile(r"[-_.]+")
_DIST_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._\-]*[A-Za-z0-9])?")
UNNAMED: Final = "unnamed"  # a distribution whose metadata has no usable name


def normalise(name: str) -> str:
    """A distribution name as the packaging specifications compare it (PEP 503)."""
    return _DIST_SEPARATORS.sub("-", name).lower()


@dataclass(frozen=True)
class PluginPolicy:
    """Which installed distributions' plugins a client reads.

    ``enabled`` False reads none (``--no-plugins``). ``allow`` names the only distributions to
    read (``--plugin NAME``, repeatable), compared as normalised names; ``None`` reads every one.
    """

    enabled: bool = True
    allow: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {self.enabled!r}")
        if self.allow is None:
            return
        allow: object = self.allow
        if isinstance(allow, str) or not isinstance(allow, Iterable):
            raise ValueError(f"allow names distributions, got {allow!r}")
        names = tuple(allow)
        for name in names:
            if not isinstance(name, str) or not _DIST_NAME.fullmatch(name):
                raise ValueError(f"not a distribution name: {name!r}")
        object.__setattr__(self, "allow", tuple(sorted({normalise(name) for name in names})))

    def admits(self, distribution: str) -> bool:
        """Whether the plugins of ``distribution`` (a normalised name) are read."""
        return self.enabled and (self.allow is None or distribution in self.allow)

    def config(self) -> dict[str, JsonValue]:
        """The loader's transform config: what decided which plugins a job saw."""
        return {} if self.allow is None else {"allow": list(self.allow)}


ALL_PLUGINS: Final = PluginPolicy()
NO_PLUGINS: Final = PluginPolicy(enabled=False)


def plugins_transform(policy: PluginPolicy = ALL_PLUGINS) -> TransformRecord:
    """The loader as a producer under ``policy``: the transform its findings name."""
    return transform_record(
        adapter_id=PLUGINS_ID, adapter_version=PLUGINS_VERSION, config=policy.config()
    )


@dataclass(frozen=True)
class Origin:
    """Where a plugin comes from: its group, distribution (normalised), version and entry point."""

    group: str
    distribution: str
    version: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.distribution}:{self.name}"

    def subject(self) -> ExternalObjectRef:
        return ExternalObjectRef(
            PLUGINS_ID, f"{self.group}/{self.distribution}/{self.name}", self.version
        )

    def details(self) -> dict[str, JsonValue]:
        return {
            "distribution": self.distribution,
            "entry_point": self.name,
            "group": self.group,
            "version": self.version,
        }


class PluginAdapter:
    """A plugin's adapter as the registry sees it: the plugin's own four methods, and its
    descriptor with the distribution and its version added to ``libraries``, so the transform
    (and with it every record's provenance and every cache key) names the plugin."""

    def __init__(self, adapter: Adapter, origin: Origin, descriptor: AdapterDescriptor) -> None:
        self.adapter = adapter
        self.origin = origin
        self.descriptor = descriptor

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        return self.adapter.probe(head, hints)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return self.adapter.inspect(source, config)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return self.adapter.plan(source, config)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        return self.adapter.ingest(source, chunk, config)

    def __repr__(self) -> str:
        return f"PluginAdapter({self.descriptor.id!r} from {self.origin.label})"


@dataclass(frozen=True)
class PluginSource:
    """A connector a plugin registers: its id (the entry-point name), where it comes from, and
    the callable that builds the ``Source``, not yet called."""

    id: str
    origin: Origin
    factory: Callable[..., object] = field(compare=False)


@dataclass(frozen=True)
class Plugins:
    """What ``load_plugins`` read: admitted adapters and Sources, each in id order, the findings
    about the plugins it refused (sorted by id), and the transform those findings name."""

    adapters: tuple[PluginAdapter, ...] = ()
    sources: tuple[PluginSource, ...] = ()
    findings: tuple[IngestFinding, ...] = ()
    transform: TransformRecord = field(default_factory=plugins_transform)


def _distributions(path: Sequence[str] | None) -> list[tuple[str, Distribution]]:
    """Each distribution on ``path`` once, by normalised name, the first found as ``import``
    would load it; sorted by that name."""
    found: dict[str, Distribution] = {}
    for dist in distributions(path=list(sys.path if path is None else path)):
        try:
            raw = dist.metadata["Name"]
        except Exception:  # unreadable metadata: a distribution with no usable name
            raw = None
        name = normalise(raw) if isinstance(raw, str) and _DIST_NAME.fullmatch(raw) else UNNAMED
        found.setdefault(name, dist)
    return sorted(found.items())


def _entry_points(
    policy: PluginPolicy, path: Sequence[str] | None, groups: Sequence[str]
) -> list[tuple[Origin, EntryPoint]]:
    """Every entry point of ``groups`` the policy admits, by (group, distribution, name)."""
    if not policy.enabled:
        return []
    points: list[tuple[Origin, EntryPoint]] = []
    for name, dist in _distributions(path):
        if not policy.admits(name):
            continue
        try:
            version = dist.version or "0"
            listed = list(dist.entry_points)
        except Exception:
            version, listed = "0", []
        for point in listed:
            if point.group in groups:
                points.append((Origin(point.group, name, str(version), point.name), point))
    # One name twice in one distribution's group is kept twice, so the duplicate rule refuses
    # both rather than the order of its entry_points.txt choosing one.
    return sorted(
        points,
        key=lambda item: (item[0].group, item[0].distribution, item[0].name, item[1].value),
    )


class _Loader:
    """One ``load_plugins`` call: what it admitted and what it refused, in entry-point order."""

    def __init__(self, policy: PluginPolicy) -> None:
        self.transform = plugins_transform(policy)
        self.findings: dict[str, IngestFinding] = {}

    def _finding(
        self,
        code: str,
        category: FindingCategory,
        origin: Origin,
        message: str,
        details: dict[str, JsonValue],
    ) -> None:
        finding = ingest_finding(
            code=code,
            category=category,
            severity=Severity.WARNING,
            subject=origin.subject(),
            transform=self.transform,
            message=f"plugin {origin.name} of {origin.distribution} {origin.version}: {message}",
            details={**origin.details(), **details},
        )
        self.findings[finding.id] = finding

    def failed(self, origin: Origin, step: str, exc: BaseException) -> None:
        self._finding(
            LOAD_FAILED,
            FindingCategory.FAILED,
            origin,
            f"{type(exc).__name__} while it was {step}; it is not used",
            {"error": type(exc).__name__, "step": step},
        )

    def refused(self, origin: Origin, reason: str, message: str, **facts: JsonValue) -> None:
        self._finding(
            REFUSED,
            FindingCategory.FAILED,
            origin,
            f"{message}; it is not used",
            {"reason": reason, **facts},
        )

    def duplicate(self, origin: Origin, claimants: list[str]) -> None:
        self._finding(
            DUPLICATE_ID,
            FindingCategory.AMBIGUOUS,
            origin,
            f"id {origin.name} is claimed by {', '.join(claimants)}; no plugin with it is used",
            {"claimants": list(claimants)},
        )

    def load(self, origin: Origin, point: EntryPoint) -> object | None:
        """The entry point's object, or ``None`` with a finding."""
        if not _ID.fullmatch(origin.name):
            self.refused(origin, INVALID_NAME, "its entry-point name is not an id")
            return None
        try:
            loaded: object = point.load()
        except (Exception, SystemExit) as exc:  # a broken plugin is a finding, never the job's
            self.failed(origin, "imported", exc)
            return None
        return loaded

    def adapter(self, origin: Origin, point: EntryPoint) -> PluginAdapter | None:
        """The plugin's adapter, admitted and wrapped, or ``None`` with a finding."""
        factory = self.load(origin, point)
        if factory is None:
            return None
        if not callable(factory):
            self.refused(origin, NOT_CALLABLE, "its entry point is not a callable")
            return None
        try:
            adapter = factory()
            descriptor = getattr(adapter, "descriptor", None)
            methods = [
                method for method in _METHODS if not callable(getattr(adapter, method, None))
            ]
        except (Exception, SystemExit) as exc:
            self.failed(origin, "built", exc)
            return None
        if not isinstance(descriptor, AdapterDescriptor):
            self.refused(origin, NO_DESCRIPTOR, "what it builds has no AdapterDescriptor")
            return None
        if methods:
            self.refused(
                origin,
                MISSING_METHOD,
                f"adapter {descriptor.id} has no {', '.join(methods)}",
                methods=list(methods),
            )
            return None
        if descriptor.abi != ABI_VERSION:
            self.refused(
                origin,
                OTHER_ABI,
                f"adapter {descriptor.id} implements ABI {descriptor.abi}, not {ABI_VERSION}",
                abi=descriptor.abi,
            )
            return None
        if descriptor.id != origin.name:
            self.refused(
                origin,
                NAME_MISMATCH,
                f"its entry point names id {origin.name}, its descriptor {descriptor.id}",
                adapter=descriptor.id,
            )
            return None
        libraries = dict(descriptor.libraries)
        pinned = libraries.get(origin.distribution)
        if pinned is not None and pinned != origin.version:
            self.refused(
                origin,
                LIBRARY_CONFLICT,
                f"adapter {descriptor.id} lists {origin.distribution} {pinned} among its libraries",
                pinned=pinned,
            )
            return None
        libraries[origin.distribution] = origin.version
        wrapped = replace(descriptor, libraries=tuple(sorted(libraries.items())))
        return PluginAdapter(adapter, origin, wrapped)

    def source(self, origin: Origin, point: EntryPoint) -> PluginSource | None:
        factory = self.load(origin, point)
        if factory is None:
            return None
        if not callable(factory):
            self.refused(origin, NOT_CALLABLE, "its entry point is not a callable")
            return None
        return PluginSource(origin.name, origin, factory)


def _unique(
    loader: _Loader, admitted: list[tuple[str, Origin]], reserved: frozenset[str]
) -> frozenset[Origin]:
    """The origins whose id no other plugin, and no built-in, claims; the rest get a finding."""
    claims: defaultdict[str, list[Origin]] = defaultdict(list)
    for key, origin in admitted:
        claims[key].append(origin)
    keep: set[Origin] = set()
    for key, origins in claims.items():
        if len(origins) == 1 and key not in reserved:
            keep.add(origins[0])
            continue
        claimants = sorted(origin.label for origin in origins)
        if key in reserved:
            claimants = ["built-in", *claimants]
        for origin in origins:
            loader.duplicate(origin, claimants)
    return frozenset(keep)


def load_plugins(
    policy: PluginPolicy = ALL_PLUGINS,
    *,
    reserved: Iterable[str] = (),
    groups: Sequence[str] = GROUPS,
    path: Sequence[str] | None = None,
) -> Plugins:
    """Read the ``groups`` entry points the policy admits from the distributions on ``path``
    (``sys.path`` by default). ``reserved`` are the built-in adapter ids no plugin may take.

    Never raises for a plugin's sake: what cannot be used is a finding (module docstring).
    """
    if not isinstance(policy, PluginPolicy):
        raise TypeError(f"policy must be a PluginPolicy, got {policy!r}")
    unknown = sorted(set(groups) - set(GROUPS))
    if unknown:
        raise ValueError(f"no plugin groups {unknown}; the groups are {list(GROUPS)}")
    loader = _Loader(policy)
    adapters: list[tuple[str, Origin]] = []
    built: dict[Origin, PluginAdapter] = {}
    sources: list[tuple[str, Origin]] = []
    made: dict[Origin, PluginSource] = {}
    for origin, point in _entry_points(policy, path, groups):
        if origin.group == ADAPTERS_GROUP:
            if (adapter := loader.adapter(origin, point)) is not None:
                adapters.append((adapter.descriptor.id, origin))
                built[origin] = adapter
        elif (source := loader.source(origin, point)) is not None:
            sources.append((source.id, origin))
            made[origin] = source
    kept_adapters = _unique(loader, adapters, frozenset(reserved))
    kept_sources = _unique(loader, sources, frozenset())
    return Plugins(
        adapters=tuple(
            sorted(
                (built[origin] for origin in kept_adapters),
                key=lambda adapter: adapter.descriptor.id,
            )
        ),
        sources=tuple(sorted((made[origin] for origin in kept_sources), key=lambda s: s.id)),
        findings=tuple(sorted(loader.findings.values(), key=lambda finding: finding.id)),
        transform=loader.transform,
    )
