"""Plugin adapters and Sources, read from installed distributions' entry points (ADR 0058).

A workspace member or a third-party distribution reaches the compiler through two entry-point
groups (Deploy ADR 0001):

- ``neptune.adapters``: the name is the adapter id; the value is a zero-argument callable that
  returns an object with the four-method ABI (ADR 0024).
- ``neptune.sources``: the name is the connector id; the value is a callable returning a
  ``Source``. It is imported and admitted here, never called. It may declare the URI schemes it
  reads (``schemes``, ADR 0067); a client calls it when a source URI names one of them
  (``neptune.discovery.external``).

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
config is the policy and whose ``libraries`` are the distributions it admitted a plugin from; a job
records that transform in its package whenever any was admitted, so a package says which plugins
could have changed it. What a plugin prints while it is imported or built is captured, never
passed to the client's stdout or stderr, and kept (bounded) in a finding.

Importing a plugin runs its code in this process, as importing any installed library does;
everything it is then asked to do (probe, plan, ingest) runs in the sandbox under the same laws as a
built-in adapter's (ADR 0030, ``neptune.adapters.check``), because the job cannot tell them apart.
"""

import contextlib
import io
import re
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import Distribution, EntryPoint, distributions
from typing import Final, cast

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
OUTPUT: Final = f"{PLUGINS_ID}.output"

# The most a finding keeps of what a plugin printed while it was imported or built.
MAX_OUTPUT: Final = 1000

FINDING_CODES: Final[tuple[Documented, ...]] = (
    Documented(
        DUPLICATE_ID,
        "two plugins, or a plugin and a built-in adapter, claim one id; every plugin claiming it"
        " is refused and none is used (ambiguous, warning)",
    ),
    Documented(
        LOAD_FAILED,
        "importing a plugin's entry point, or calling it to build the adapter, raised (step"
        " imported or built), or a distribution's entry_points.txt naming a plugin group cannot be"
        " read (step listed); the plugin is not used (failed, warning)",
    ),
    Documented(
        OUTPUT,
        "a plugin printed to stdout or stderr while it was imported or built; the first"
        f" {MAX_OUTPUT} characters are kept in the finding, none reach Neptune's own output"
        " (skipped, info)",
    ),
    Documented(
        REFUSED,
        "a plugin loaded but is not admissible: not callable, no descriptor, a missing method,"
        " another ABI, an id other than its entry point's name, a library pin that contradicts"
        " its distribution, a distribution with no usable name, or a Source factory whose"
        " schemes are not distinct lowercase URI schemes other than file; the plugin is not used"
        " (failed, warning)",
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
UNNAMED_DISTRIBUTION: Final = "unnamed_distribution"
INVALID_SCHEMES: Final = "invalid_schemes"

# A URI scheme a connector may claim (RFC 3986, lowercase). ``file`` is the local root's own.
SCHEME: Final = re.compile(r"[a-z][a-z0-9+.\-]{0,31}")
RESERVED_SCHEMES: Final = frozenset({"file"})

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


def plugins_transform(
    policy: PluginPolicy = ALL_PLUGINS, loaded: Mapping[str, str] | None = None
) -> TransformRecord:
    """The loader as a producer under ``policy``, having admitted plugins from ``loaded``
    (normalised distribution name to version) as its ``libraries``: the transform its findings
    name, and the record of which plugins a package's job could use."""
    return transform_record(
        adapter_id=PLUGINS_ID,
        adapter_version=PLUGINS_VERSION,
        config=policy.config(),
        libraries=dict(loaded or {}),
    )


@dataclass(frozen=True)
class Origin:
    """Where a plugin comes from: its group, distribution (normalised), version and entry point.

    A finding about a whole distribution (its entry points cannot be listed) has no group and no
    entry point: both are ``""``."""

    group: str
    distribution: str
    version: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.distribution}:{self.name}"

    def subject(self) -> ExternalObjectRef:
        parts = (self.group, self.distribution, self.name)
        return ExternalObjectRef(PLUGINS_ID, "/".join(p for p in parts if p), self.version)

    def describe(self) -> str:
        where = f"{self.distribution} {self.version}"
        return f"plugin {self.name} of {where}" if self.name else f"distribution {where}"

    def details(self) -> dict[str, JsonValue]:
        details: dict[str, JsonValue] = {
            "distribution": self.distribution,
            "version": self.version,
        }
        if self.group:
            details["group"] = self.group
        if self.name:
            details["entry_point"] = self.name
        return details


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
    """A connector a plugin registers: its id (the entry-point name), where it comes from, the
    callable that builds the ``Source`` (not yet called), and the URI schemes the callable
    declares it reads (its ``schemes`` attribute; none if it declares none, when the connector is
    used only when named, ADR 0067)."""

    id: str
    origin: Origin
    factory: Callable[..., object] = field(compare=False)
    schemes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plugins:
    """What ``load_plugins`` read.

    ``adapters`` and ``sources`` are the admitted plugins, each in id order; ``findings`` the
    findings about what it refused or what a plugin printed, sorted by id; ``transform`` the
    loader's transform, whose ``libraries`` are the distributions it admitted a plugin from (its
    findings name it). ``unmatched`` are the names an allowlist gives that no installed
    distribution registering a plugin answers to: a client refuses them (ADR 0058 §8).
    """

    adapters: tuple[PluginAdapter, ...] = ()
    sources: tuple[PluginSource, ...] = ()
    findings: tuple[IngestFinding, ...] = ()
    transform: TransformRecord = field(default_factory=plugins_transform)
    unmatched: tuple[str, ...] = ()

    @property
    def loaded(self) -> tuple[tuple[str, str], ...]:
        """The distributions a plugin was admitted from, with their versions, sorted by name."""
        return self.transform.libraries

    def distribution_of(self, adapter_id: str) -> str | None:
        """``"<distribution> <version>"`` of the admitted plugin adapter ``adapter_id``."""
        for adapter in self.adapters:
            if adapter.descriptor.id == adapter_id:
                return f"{adapter.origin.distribution} {adapter.origin.version}"
        return None


def _schemes(declared: object) -> tuple[str, ...] | None:
    """The schemes a Source factory declares, sorted, or ``None`` if they are not usable."""
    if not isinstance(declared, tuple | list | frozenset | set):
        return None
    schemes = list(declared)
    if not all(isinstance(s, str) and SCHEME.fullmatch(s) for s in schemes):
        return None
    if len(set(schemes)) != len(schemes) or RESERVED_SCHEMES.intersection(schemes):
        return None
    return tuple(sorted(schemes))


def _text(value: object) -> str | None:
    """Metadata text as a valid, non-empty one-line string, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


def _mentions_groups(dist: Distribution) -> bool:
    """Whether ``dist``'s raw ``entry_points.txt`` names one of the plugin groups."""
    try:
        text = dist.read_text("entry_points.txt") or ""
    except Exception:
        return False
    return any(f"[{group}]" in text for group in GROUPS)


@dataclass(frozen=True)
class _Listed:
    """One distribution's plugin entry points, or why they could not be listed."""

    origin: Origin  # group and name empty: the distribution itself
    points: tuple[EntryPoint, ...]
    named: bool  # its metadata has a usable name
    failure: BaseException | None = None


def _listed(path: Sequence[str] | None) -> list[_Listed]:
    """Every distribution on ``path`` that registers (or tries to register) a plugin.

    One name found twice is the first, as ``import`` would load it. A distribution with no usable
    name is listed once per occurrence: it cannot be merged with another by name.
    """
    seen: set[str] = set()
    found: list[_Listed] = []
    for dist in distributions(path=list(sys.path if path is None else path)):
        try:
            metadata = dist.metadata
            raw = _text(metadata["Name"]) if "Name" in metadata else None
            version = _text(dist.version) or "unknown"
        except Exception:  # unreadable metadata
            raw, version = None, "unknown"
        named = raw is not None and _DIST_NAME.fullmatch(raw) is not None
        name = normalise(raw) if named and raw is not None else UNNAMED
        if named and name in seen:
            continue
        seen.add(name)
        origin = Origin("", name, version, "")
        try:
            points = tuple(point for point in dist.entry_points if point.group in GROUPS)
        except Exception as exc:  # a malformed entry_points.txt
            if _mentions_groups(dist):
                found.append(_Listed(origin, (), named, exc))
            continue
        if points:
            found.append(_Listed(origin, points, named))
    return sorted(found, key=lambda listed: (listed.origin.distribution, listed.origin.version))


@contextlib.contextmanager
def _captured() -> Iterator[io.StringIO]:
    """Whatever a plugin prints while it is imported or built, kept off the client's own
    stdout (which ``--json`` owns) and stderr."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


def _output(buffer: io.StringIO) -> dict[str, JsonValue]:
    """A plugin's printed output as finding details, bounded: at most ``MAX_OUTPUT`` characters."""
    text = buffer.getvalue()
    if not text:
        return {}
    kept = text[:MAX_OUTPUT].encode("utf-8", "backslashreplace").decode("utf-8")
    return {"output": kept, "output_chars": len(text)}


@dataclass(frozen=True)
class _Draft:
    """A finding whose transform is not known until every plugin has been admitted or not."""

    code: str
    category: FindingCategory
    severity: Severity
    origin: Origin
    message: str
    details: dict[str, JsonValue]


class _Loader:
    """One ``load_plugins`` call: what it admitted and what it refused, in entry-point order."""

    def __init__(self) -> None:
        self.drafts: list[_Draft] = []

    def _draft(
        self,
        code: str,
        category: FindingCategory,
        origin: Origin,
        message: str,
        details: dict[str, JsonValue],
        severity: Severity = Severity.WARNING,
    ) -> None:
        self.drafts.append(
            _Draft(
                code,
                category,
                severity,
                origin,
                f"{origin.describe()}: {message}",
                {**origin.details(), **details},
            )
        )

    def failed(
        self, origin: Origin, step: str, exc: BaseException, output: dict[str, JsonValue]
    ) -> None:
        self._draft(
            LOAD_FAILED,
            FindingCategory.FAILED,
            origin,
            f"{type(exc).__name__} while it was {step}; it is not used",
            {"error": type(exc).__name__, "step": step, **output},
        )

    def refused(self, origin: Origin, reason: str, message: str, **facts: JsonValue) -> None:
        self._draft(
            REFUSED,
            FindingCategory.FAILED,
            origin,
            f"{message}; it is not used",
            {"reason": reason, **facts},
        )

    def printed(self, origin: Origin, step: str, output: dict[str, JsonValue]) -> None:
        if output:
            self._draft(
                OUTPUT,
                FindingCategory.SKIPPED,
                origin,
                f"it printed {output['output_chars']} characters while it was {step}; they are"
                " kept here, not passed to Neptune's output",
                {"step": step, **output},
                Severity.INFO,
            )

    def duplicate(self, origin: Origin, claimants: list[str]) -> None:
        self._draft(
            DUPLICATE_ID,
            FindingCategory.AMBIGUOUS,
            origin,
            f"id {origin.name} is claimed by {', '.join(claimants)}; no plugin with it is used",
            {"claimants": list(claimants)},
        )

    def _call(self, origin: Origin, step: str, call: Callable[[], object]) -> tuple[bool, object]:
        """Run ``call`` with its output captured; ``(False, None)`` with a finding if it raised.

        Anything it raises but ``KeyboardInterrupt`` is the plugin's failure, ``SystemExit`` and
        other ``BaseException`` subclasses included: a plugin never ends the client.
        """
        with _captured() as buffer:
            try:
                value = call()
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                failure: BaseException | None = exc
            else:
                failure = None
        output = _output(buffer)
        if failure is not None:
            self.failed(origin, step, failure, output)
            return False, None
        self.printed(origin, step, output)
        return True, value

    def load(self, origin: Origin, point: EntryPoint) -> object | None:
        """The entry point's object, or ``None`` with a finding."""
        if not _ID.fullmatch(origin.name):
            self.refused(origin, INVALID_NAME, "its entry-point name is not an id")
            return None
        ok, loaded = self._call(origin, "imported", point.load)
        if not ok:
            return None
        if not callable(loaded):
            self.refused(origin, NOT_CALLABLE, "its entry point is not a callable")
            return None
        return loaded

    def adapter(self, origin: Origin, point: EntryPoint) -> PluginAdapter | None:
        """The plugin's adapter, admitted and wrapped, or ``None`` with a finding."""
        factory = self.load(origin, point)
        if factory is None:
            return None
        assert callable(factory)

        def build() -> tuple[object, object, list[str]]:
            built = factory()
            descriptor = getattr(built, "descriptor", None)
            missing = [name for name in _METHODS if not callable(getattr(built, name, None))]
            return built, descriptor, missing

        ok, value = self._call(origin, "built", build)
        if not ok:
            return None
        assert isinstance(value, tuple)
        adapter, descriptor, methods = value
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
        # Its own distribution, however it spells it, is listed once: as installed.
        own = {name for name, _ in descriptor.libraries if normalise(name) == origin.distribution}
        libraries = {k: v for k, v in descriptor.libraries if k not in own}
        if others := sorted({v for k, v in descriptor.libraries if k in own} - {origin.version}):
            pinned = others[0]
            self.refused(
                origin,
                LIBRARY_CONFLICT,
                f"adapter {descriptor.id} lists {origin.distribution} {pinned} among its libraries",
                pinned=pinned,
            )
            return None
        libraries[origin.distribution] = origin.version
        wrapped = replace(descriptor, libraries=tuple(sorted(libraries.items())))
        return PluginAdapter(cast("Adapter", adapter), origin, wrapped)

    def source(self, origin: Origin, point: EntryPoint) -> PluginSource | None:
        factory = self.load(origin, point)
        if factory is None:
            return None
        assert callable(factory)
        ok, declared = self._call(origin, "imported", lambda: getattr(factory, "schemes", ()))
        if not ok:
            return None
        schemes = _schemes(declared)
        if schemes is None:
            self.refused(
                origin,
                INVALID_SCHEMES,
                "its schemes are not a list of distinct lowercase URI schemes other than file",
            )
            return None
        return PluginSource(origin.name, origin, factory, schemes)

    def findings(self, transform: TransformRecord) -> tuple[IngestFinding, ...]:
        made = {
            finding.id: finding
            for finding in (
                ingest_finding(
                    code=draft.code,
                    category=draft.category,
                    severity=draft.severity,
                    subject=draft.origin.subject(),
                    transform=transform,
                    message=draft.message,
                    details=draft.details,
                )
                for draft in self.drafts
            )
        }
        return tuple(made[key] for key in sorted(made))


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
    if not policy.enabled:
        return Plugins(transform=plugins_transform(policy))
    loader = _Loader()
    listed = [item for item in _listed(path) if policy.admits(item.origin.distribution)]
    points: list[tuple[Origin, EntryPoint]] = []
    for item in listed:
        dist = item.origin
        if item.failure is not None:
            loader.failed(dist, "listed", item.failure, {})
            continue
        for point in item.points:
            origin = Origin(point.group, dist.distribution, dist.version, point.name)
            if not item.named:
                loader.refused(
                    origin,
                    UNNAMED_DISTRIBUTION,
                    "its distribution's metadata has no usable name",
                    value=point.value,
                )
            elif point.group in groups:
                points.append((origin, point))
    # One name twice in one distribution's group is kept twice, so the duplicate rule refuses
    # both rather than the order of its entry_points.txt choosing one.
    points.sort(key=lambda item: (item[0].group, item[0].distribution, item[0].name, item[1].value))
    adapters: list[tuple[str, Origin]] = []
    built: dict[Origin, PluginAdapter] = {}
    sources: list[tuple[str, Origin]] = []
    made: dict[Origin, PluginSource] = {}
    for origin, point in points:
        if origin.group == ADAPTERS_GROUP:
            if (adapter := loader.adapter(origin, point)) is not None:
                adapters.append((adapter.descriptor.id, origin))
                built[origin] = adapter
        elif (source := loader.source(origin, point)) is not None:
            sources.append((source.id, origin))
            made[origin] = source
    kept = _unique(loader, adapters, frozenset(reserved)) | _unique(loader, sources, frozenset())
    loaded = {origin.distribution: origin.version for origin in kept}
    transform = plugins_transform(policy, loaded)
    registering = {item.origin.distribution for item in listed if item.named}
    return Plugins(
        adapters=tuple(
            sorted((built[o] for o in kept if o in built), key=lambda one: one.descriptor.id)
        ),
        sources=tuple(sorted((made[o] for o in kept if o in made), key=lambda one: one.id)),
        findings=loader.findings(transform),
        transform=transform,
        unmatched=tuple(name for name in policy.allow or () if name not in registering),
    )
