"""The cache: what a job reuses, why it recomputes, and what it reports (ADR 0031).

Everything a job computes is kept in the workspace under a key that covers all it depends on, and
a job reuses whatever is kept under the key it needs. Nothing else decides: no clock, no file
times, no flag.

- A **plan** is kept by (source content id, transform id) (ADR 0026 §1).
- A **chunk's output** is kept by the chunk's id, which is its cache key: the source's content id,
  the transform (adapter id, version, resolved config, libraries) and the chunk's context, its
  identity within the source (ADR 0024 §4). Equal ids mean equal output.
- A **derivative** (a stream's series file, a source's verdict on the cross-chunk laws) is kept
  by a ``DerivativeKey``: its recipe and version, the chunk ids and settings it reads. It is
  built the first time something reads it, and copied after that.

Each miss is explained by the first invalidation rule that holds (``Rule``, ``explain_plan``),
and every job leaves a ``CacheReport``: each plan, chunk and derivative it needed, hit or miss and
why, and how many times it called each adapter method. Two jobs over the same sources, adapters
and workspace state report the same thing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from neptune.adapters.contract import Documented
from neptune.model.ids import (
    ConfigHash,
    ContentId,
    RecordId,
    check_token,
    parse_config_hash,
    parse_content_id,
    parse_record_id,
)
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.model.provenance import TransformRecord
from neptune.store.workspace import DerivativeKey, Held, Owner

REPORT_KIND: Final = "ingest_cache_report"
REPORT_FORMAT: Final = 1
# A source's verdict on the cross-chunk laws (ADR 0028 §5), as a derivative. Its inputs carry the
# runtime's version, which changes whenever the laws do.
ADMISSION_RECIPE: Final = "neptune.runtime.admission/1"
VERDICT_FILE: Final = "verdict.json"


class Cache(StrEnum):
    HIT = "hit"
    MISS = "miss"


class Rule(StrEnum):
    """Why a plan, chunk or derivative was reused or computed (ADR 0031 §3)."""

    PLANNED = "planned"
    COMMITTED = "committed"
    HELD = "held"
    TRANSFORM_CHANGED = "transform_changed"
    ADAPTER_CHANGED = "adapter_changed"
    SOURCE_CHANGED = "source_changed"
    SOURCE_NEW = "source_new"
    NOT_COMMITTED = "not_committed"
    ABSENT = "absent"
    CORRUPT = "corrupt"


RULES: Final[tuple[Documented, ...]] = (
    Documented("planned", "hit: the workspace keeps this source's plan under this transform"),
    Documented("committed", "hit: the workspace keeps this chunk's output, by its id"),
    Documented("held", "hit: the workspace keeps this derivative, whole, by its key"),
    Documented(
        "transform_changed",
        "miss: the source was planned by this adapter under another transform; `changed` names"
        " what differs (adapter_version, config, libraries, upstream)",
    ),
    Documented(
        "adapter_changed",
        "miss: the source was planned only by other adapters; selection chose another one",
    ),
    Documented(
        "source_changed",
        "miss: a location now holds these bytes in place of others; `previous` names them",
    ),
    Documented("source_new", "miss: the workspace has never planned these bytes"),
    Documented(
        "not_committed",
        "miss: the plan is kept, but this chunk's output is not: an earlier job was interrupted,"
        " cancelled or failed on it",
    ),
    Documented("absent", "miss: the derivative was never built, or was collected"),
    Documented("corrupt", "miss: the derivative was kept but damaged; it was built again"),
)

_HITS: Final = {Rule.PLANNED, Rule.COMMITTED, Rule.HELD}
# The parts of a transform, as ``changed`` names them, and how each is read off a record.
TRANSFORM_PARTS: Final = ("adapter", "adapter_version", "config", "libraries", "upstream")


def changed_parts(old: TransformRecord, new: TransformRecord) -> tuple[str, ...]:
    """Which parts of a transform differ from ``old`` to ``new``, in ``TRANSFORM_PARTS`` order."""
    pairs = {
        "adapter": (old.adapter_id, new.adapter_id),
        "adapter_version": (old.adapter_version, new.adapter_version),
        "config": (old.config_hash, new.config_hash),
        "libraries": (old.libraries, new.libraries),
        "upstream": (old.upstream, new.upstream),
    }
    return tuple(part for part in TRANSFORM_PARTS if pairs[part][0] != pairs[part][1])


# --- Explaining a plan -------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCache:
    """Whether a source's plan was reused, and the rule that says why.

    ``changed`` names the transform's parts that differ from the closest one kept (only for
    ``transform_changed`` and ``adapter_changed``); ``previous`` names what was compared against:
    that transform's id, or for ``source_changed`` the bytes the location held before.
    """

    rule: Rule
    changed: tuple[str, ...] = ()
    previous: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.rule, Rule) or self.rule in (Rule.COMMITTED, Rule.HELD):
            raise ValueError(f"not a plan's rule: {self.rule!r}")
        if self.rule in (Rule.ABSENT, Rule.CORRUPT, Rule.NOT_COMMITTED):
            raise ValueError(f"not a plan's rule: {self.rule!r}")
        if any(part not in TRANSFORM_PARTS for part in self.changed):
            raise ValueError(f"changed names parts of a transform: {self.changed!r}")
        compared = self.rule in (Rule.TRANSFORM_CHANGED, Rule.ADAPTER_CHANGED)
        if compared != bool(self.changed):
            raise ValueError(f"{self.rule} {'names' if compared else 'has no'} changed parts")
        if self.rule is Rule.SOURCE_CHANGED or compared:
            if self.previous is None:
                raise ValueError(f"{self.rule} names what it was compared against")
            (parse_content_id if self.rule is Rule.SOURCE_CHANGED else parse_record_id)(
                self.previous
            )
        elif self.previous is not None:
            raise ValueError(f"{self.rule} was compared against nothing")

    @property
    def cache(self) -> Cache:
        return Cache.HIT if self.rule is Rule.PLANNED else Cache.MISS

    @property
    def chunk_miss(self) -> Rule:
        """The rule for a chunk of this plan that is not committed."""
        return Rule.NOT_COMMITTED if self.rule is Rule.PLANNED else self.rule

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {"cache": str(self.cache), "rule": str(self.rule)}
        if self.changed:
            out["changed"] = list(self.changed)
        if self.previous is not None:
            out["previous"] = self.previous
        return out


def explain_plan(
    transform: TransformRecord, kept: Sequence[TransformRecord], replaced: ContentId | None
) -> PlanCache:
    """The first rule that holds for a source about to be planned under ``transform``.

    ``kept`` are the transforms the workspace holds a plan of the source under; ``replaced`` is
    the content id a location of the source held before it, if one did. In order: the plan is
    kept (``planned``); the same adapter planned it under another transform
    (``transform_changed``, against the kept transform with the fewest differing parts, then the
    least id); only other adapters did (``adapter_changed``); a location held other bytes
    (``source_changed``); nothing is known of it (``source_new``).
    """
    if any(other.id == transform.id for other in kept):
        return PlanCache(Rule.PLANNED)
    same = [other for other in kept if other.adapter_id == transform.adapter_id]
    if same:
        closest = min(same, key=lambda other: (len(changed_parts(other, transform)), other.id))
        return PlanCache(Rule.TRANSFORM_CHANGED, changed_parts(closest, transform), closest.id)
    if kept:
        closest = min(kept, key=lambda other: (other.adapter_id, other.id))
        return PlanCache(Rule.ADAPTER_CHANGED, changed_parts(closest, transform), closest.id)
    if replaced is not None:
        return PlanCache(Rule.SOURCE_CHANGED, previous=replaced)
    return PlanCache(Rule.SOURCE_NEW)


def admission_key(
    source: ContentId, transform: RecordId, chunks: Sequence[str], runtime: str
) -> DerivativeKey:
    """The key of a source's verdict on the cross-chunk laws: its chunks and the runtime version."""
    inputs: JsonObject = {
        "chunks": list(chunks),
        "runtime": runtime,
        "source": source,
        "transform": transform,
    }
    return DerivativeKey(ADMISSION_RECIPE, inputs, ((source, transform),))


# --- The report --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkCache:
    chunk: str
    rule: Rule

    def __post_init__(self) -> None:
        if not isinstance(self.chunk, str) or not self.chunk.startswith("chunk:sha256:"):
            raise ValueError(f"not a chunk id: {self.chunk!r}")
        if self.rule in (Rule.PLANNED, Rule.HELD, Rule.ABSENT, Rule.CORRUPT):
            raise ValueError(f"not a chunk's rule: {self.rule!r}")

    @property
    def cache(self) -> Cache:
        return Cache.HIT if self.rule in _HITS else Cache.MISS

    def to_json(self) -> JsonObject:
        return {"cache": str(self.cache), "chunk": self.chunk, "rule": str(self.rule)}


@dataclass(frozen=True)
class SourceCache:
    """One source's plan and chunks under its transform, in plan order."""

    source: ContentId
    transform: RecordId
    adapter: str
    adapter_version: str
    config_hash: ConfigHash
    plan: PlanCache
    chunks: tuple[ChunkCache, ...]

    def __post_init__(self) -> None:
        parse_content_id(self.source)
        parse_record_id(self.transform)
        check_token("adapter", self.adapter)
        parse_config_hash(self.config_hash)
        if len({chunk.chunk for chunk in self.chunks}) != len(self.chunks):
            raise ValueError(f"source {self.source} lists a chunk twice")

    def to_json(self) -> JsonObject:
        return {
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "chunks": [chunk.to_json() for chunk in self.chunks],
            "config_hash": self.config_hash,
            "plan": self.plan.to_json(),
            "source": self.source,
            "transform": self.transform,
        }


@dataclass(frozen=True)
class DerivativeCache:
    """A derivative the job read: reused as kept, or built."""

    derivative: str
    recipe: str
    owners: tuple[Owner, ...]
    rule: Rule

    def __post_init__(self) -> None:
        if self.rule not in (Rule.HELD, Rule.ABSENT, Rule.CORRUPT):
            raise ValueError(f"not a derivative's rule: {self.rule!r}")

    @classmethod
    def of(cls, key: DerivativeKey, held: Held) -> "DerivativeCache":
        rule = {Held.HELD: Rule.HELD, Held.BUILT: Rule.ABSENT, Held.REBUILT: Rule.CORRUPT}[held]
        return cls(key.id, key.recipe, key.owners, rule)

    @property
    def cache(self) -> Cache:
        return Cache.HIT if self.rule is Rule.HELD else Cache.MISS

    def to_json(self) -> JsonObject:
        return {
            "cache": str(self.cache),
            "derivative": self.derivative,
            "owners": [list(owner) for owner in self.owners],
            "recipe": self.recipe,
            "rule": str(self.rule),
        }


@dataclass(frozen=True)
class Calls:
    """How many times the job called each adapter method; a retry is another call."""

    probe: int = 0
    plan: int = 0
    ingest: int = 0

    def __post_init__(self) -> None:
        for name in ("probe", "plan", "ingest"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} calls are a count, got {value!r}")

    def to_json(self) -> JsonObject:
        return {"ingest": self.ingest, "plan": self.plan, "probe": self.probe}


def _count(items: Sequence[ChunkCache | DerivativeCache | PlanCache]) -> JsonObject:
    hits = sum(1 for item in items if item.cache is Cache.HIT)
    return {"hit": hits, "miss": len(items) - hits}


@dataclass(frozen=True)
class CacheReport:
    """What a job reused and recomputed, and why (ADR 0031 §5).

    Deterministic: sources sorted by (source, transform) with chunks in plan order, derivatives
    by id, and no clock. ``receipt`` names the receipt core it accompanies once the job has a
    package; it is written beside the envelope, outside the manifest, because a rerun of the
    same job hits where the first one missed and the package must not differ.
    """

    sources: tuple[SourceCache, ...] = ()
    derivatives: tuple[DerivativeCache, ...] = ()
    calls: Calls = Calls()
    receipt: RecordId | None = None

    def __post_init__(self) -> None:
        keys = [(s.source, s.transform) for s in self.sources]
        if keys != sorted(set(keys)):
            raise ValueError("a report's sources are sorted by (source, transform), each once")
        ids = [d.derivative for d in self.derivatives]
        if ids != sorted(set(ids)):
            raise ValueError("a report's derivatives are sorted by id, each once")
        if self.receipt is not None:
            parse_record_id(self.receipt)

    def for_receipt(self, receipt: RecordId) -> "CacheReport":
        return replace(self, receipt=receipt)

    def totals(self) -> JsonObject:
        return {
            "chunks": _count([chunk for s in self.sources for chunk in s.chunks]),
            "derivatives": _count(self.derivatives),
            "plans": _count([s.plan for s in self.sources]),
        }

    def to_json(self) -> JsonObject:
        out: dict[str, JsonValue] = {
            "calls": self.calls.to_json(),
            "derivatives": [d.to_json() for d in self.derivatives],
            "format": REPORT_FORMAT,
            "kind": REPORT_KIND,
            "sources": [s.to_json() for s in self.sources],
            "totals": self.totals(),
        }
        if self.receipt is not None:
            out["receipt"] = self.receipt
        return out


# --- Reading a report back ---------------------------------------------------------------------


def _object(
    data: JsonValue, keys: set[str], what: str, optional: set[str] | None = None
) -> Mapping[str, JsonValue]:
    if not isinstance(data, Mapping) or not keys <= data.keys() <= keys | (optional or set()):
        raise ValueError(f"{what} is {{{', '.join(sorted(keys))}}}: {data!r}")
    return data


def _str(value: JsonValue, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text, got {value!r}")
    return value


def _list(value: JsonValue, what: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{what} must be an array, got {value!r}")
    return value


def _plan_from_json(data: JsonValue) -> PlanCache:
    obj = _object(data, {"cache", "rule"}, "a plan's cache", {"changed", "previous"})
    changed = tuple(_str(p, "a changed part") for p in _list(obj.get("changed", []), "changed"))
    previous = obj.get("previous")
    plan = PlanCache(
        Rule(_str(obj["rule"], "rule")),
        changed,
        _str(previous, "previous") if previous is not None else None,
    )
    if obj["cache"] != str(plan.cache):
        raise ValueError(f"a plan whose rule is {plan.rule} is a {plan.cache}")
    return plan


def _chunk_from_json(data: JsonValue) -> ChunkCache:
    obj = _object(data, {"cache", "chunk", "rule"}, "a chunk's cache")
    chunk = ChunkCache(_str(obj["chunk"], "chunk"), Rule(_str(obj["rule"], "rule")))
    if obj["cache"] != str(chunk.cache):
        raise ValueError(f"a chunk whose rule is {chunk.rule} is a {chunk.cache}")
    return chunk


def _owner(data: JsonValue) -> Owner:
    pair = _list(data, "an owner")
    if len(pair) != 2:
        raise ValueError(f"an owner is [source, transform]: {pair!r}")
    return (
        parse_content_id(_str(pair[0], "an owner's source")),
        parse_record_id(_str(pair[1], "an owner's transform")),
    )


def cache_report_from_json(data: JsonValue) -> CacheReport:
    """Parse strictly; the totals must count what the report lists."""
    keys = {"calls", "derivatives", "format", "kind", "sources", "totals"}
    obj = _object(data, keys, "a cache report", {"receipt"})
    if obj["kind"] != REPORT_KIND or obj["format"] != REPORT_FORMAT:
        raise ValueError(f"not a format-{REPORT_FORMAT} {REPORT_KIND}")
    calls = _object(obj["calls"], {"ingest", "plan", "probe"}, "calls")

    def count(name: str) -> int:
        value = calls[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} calls are a count, got {value!r}")
        return value

    sources = []
    for item in _list(obj["sources"], "sources"):
        source = _object(
            item,
            {"adapter", "adapter_version", "chunks", "config_hash", "plan", "source", "transform"},
            "a source's cache",
        )
        sources.append(
            SourceCache(
                source=parse_content_id(_str(source["source"], "source")),
                transform=parse_record_id(_str(source["transform"], "transform")),
                adapter=_str(source["adapter"], "adapter"),
                adapter_version=_str(source["adapter_version"], "adapter_version"),
                config_hash=parse_config_hash(_str(source["config_hash"], "config_hash")),
                plan=_plan_from_json(source["plan"]),
                chunks=tuple(_chunk_from_json(c) for c in _list(source["chunks"], "chunks")),
            )
        )
    derivatives = []
    for item in _list(obj["derivatives"], "derivatives"):
        entry = _object(item, {"cache", "derivative", "owners", "recipe", "rule"}, "a derivative")
        derivative = DerivativeCache(
            _str(entry["derivative"], "derivative"),
            _str(entry["recipe"], "recipe"),
            tuple(_owner(o) for o in _list(entry["owners"], "owners")),
            Rule(_str(entry["rule"], "rule")),
        )
        if entry["cache"] != str(derivative.cache):
            raise ValueError(
                f"a derivative whose rule is {derivative.rule} is a {derivative.cache}"
            )
        derivatives.append(derivative)
    receipt = obj.get("receipt")
    report = CacheReport(
        tuple(sources),
        tuple(derivatives),
        Calls(probe=count("probe"), plan=count("plan"), ingest=count("ingest")),
        parse_record_id(_str(receipt, "receipt")) if receipt is not None else None,
    )
    if obj["totals"] != report.totals():
        raise ValueError("a cache report's totals do not count what it lists")
    return report
