"""The adapters a job may use, and the rule that picks one for a source (ADR 0024).

A registry holds adapters by id. It refuses two adapters with one id and an adapter built for
another ABI version, so a conflict between adapters is an error when the registry is built, never
a silent choice at ingest time.

Choosing an adapter for a source is a pure rule over every adapter's probe result:

- an adapter with confidence 0 is not a candidate;
- candidates rank by confidence, highest first, then by adapter id, so the order is total;
- no candidate: the source is ``unsupported``;
- two or more candidates share the top confidence: the source is ``ambiguous``. Nothing picks one
  of them silently; the tie is reported (MVL-8) and a manifest can name the adapter (MVL-14);
- otherwise the top candidate is ``selected``.

Running probes over real sources (bounded heads, isolation from crashing probes, reporting) is the
probe engine's (MVL-8).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from neptune.adapters.contract import (
    ABI_VERSION,
    PROBE_HEAD_SIZE,
    Adapter,
    AdapterDescriptor,
    ContractError,
    ProbeHints,
    ProbeResult,
)


class RegistryError(ContractError):
    """Adapters conflict, or one is not registered."""


class SelectionStatus(StrEnum):
    SELECTED = "selected"  # one candidate is more confident than every other
    AMBIGUOUS = "ambiguous"  # candidates tie at the top confidence
    UNSUPPORTED = "unsupported"  # no adapter claims the source


@dataclass(frozen=True)
class Candidate:
    """One adapter's probe result for one source."""

    adapter: str
    version: str
    result: ProbeResult

    @property
    def confidence(self) -> float:
        return self.result.confidence


@dataclass(frozen=True)
class Selection:
    """The outcome of the rule for one source, with every candidate it weighed, best first."""

    status: SelectionStatus
    candidates: tuple[Candidate, ...]

    @property
    def adapter(self) -> str | None:
        """The selected adapter's id; ``None`` unless the status is ``selected``."""
        return self.candidates[0].adapter if self.status is SelectionStatus.SELECTED else None

    @property
    def tied(self) -> tuple[Candidate, ...]:
        """The candidates sharing the top confidence: one if selected, several if ambiguous."""
        if not self.candidates:
            return ()
        top = self.candidates[0].confidence
        return tuple(c for c in self.candidates if c.confidence == top)


def rank(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """Candidates with confidence above 0, highest first, ties by adapter id."""
    return tuple(
        sorted(
            (c for c in candidates if c.confidence > 0.0),
            key=lambda c: (-c.confidence, c.adapter),
        )
    )


def select(candidates: Iterable[Candidate]) -> Selection:
    """Apply the selection rule to every adapter's probe result for one source."""
    ranked = rank(candidates)
    if len({c.adapter for c in ranked}) != len(ranked):
        raise RegistryError("one adapter probed a source twice")
    if not ranked:
        return Selection(SelectionStatus.UNSUPPORTED, ())
    if len(ranked) > 1 and ranked[1].confidence == ranked[0].confidence:
        return Selection(SelectionStatus.AMBIGUOUS, ranked)
    return Selection(SelectionStatus.SELECTED, ranked)


class AdapterRegistry:
    """Adapters by id, in id order. Built once per job and then only read."""

    def __init__(self, adapters: Iterable[Adapter] = ()) -> None:
        self._adapters: dict[str, Adapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: Adapter) -> None:
        """Add ``adapter``. Another adapter with its id, or another ABI version, is refused."""
        descriptor = getattr(adapter, "descriptor", None)
        if not isinstance(descriptor, AdapterDescriptor):
            raise RegistryError(f"{adapter!r} has no AdapterDescriptor")
        for method in ("probe", "inspect", "plan", "ingest"):
            if not callable(getattr(adapter, method, None)):
                raise RegistryError(f"adapter {descriptor.id} has no {method} method")
        if descriptor.abi != ABI_VERSION:
            raise RegistryError(
                f"adapter {descriptor.id} implements ABI {descriptor.abi},"
                f" and this registry ABI {ABI_VERSION}"
            )
        existing = self._adapters.get(descriptor.id)
        if existing is not None:
            raise RegistryError(
                f"two adapters have id {descriptor.id!r}"
                f" (versions {existing.descriptor.version} and {descriptor.version})"
            )
        self._adapters[descriptor.id] = adapter

    def get(self, adapter_id: str) -> Adapter:
        try:
            return self._adapters[adapter_id]
        except KeyError:
            raise RegistryError(f"no adapter {adapter_id!r} is registered") from None

    def adapters(self) -> tuple[Adapter, ...]:
        return tuple(self._adapters[key] for key in sorted(self._adapters))

    def descriptors(self) -> Mapping[str, AdapterDescriptor]:
        return {key: self._adapters[key].descriptor for key in sorted(self._adapters)}

    def probe(self, head: bytes, hints: ProbeHints) -> tuple[Candidate, ...]:
        """Every adapter's probe of one source, in adapter id order.

        ``head`` is the source's first ``min(size, PROBE_HEAD_SIZE)`` bytes. A probe that raises
        is a bug in that adapter and propagates; the probe engine (MVL-8) isolates them.
        """
        if len(head) != min(hints.size, PROBE_HEAD_SIZE):
            raise ValueError(
                f"head holds {len(head)} bytes; a {hints.size}-byte source's head holds"
                f" {min(hints.size, PROBE_HEAD_SIZE)}"
            )
        candidates = []
        for adapter in self.adapters():
            result = adapter.probe(head, hints)
            if not isinstance(result, ProbeResult):
                raise ContractError(f"adapter {adapter.descriptor.id}: probe returned {result!r}")
            candidates.append(Candidate(adapter.descriptor.id, adapter.descriptor.version, result))
        return tuple(candidates)

    def select(self, head: bytes, hints: ProbeHints) -> Selection:
        """Probe with every adapter and apply the selection rule."""
        return select(self.probe(head, hints))
