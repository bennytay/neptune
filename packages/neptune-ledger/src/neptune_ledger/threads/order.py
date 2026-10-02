"""World order, transaction order and the current-view resolver over thread members (ADR 0003).

Pure functions of a thread's members at one catalog point: no database, no clock, no randomness.
Every sort key is total and compares ids as UTF-8 bytes, so the result does not depend on the
order rows arrive in (ADR 0003 §7, P1-P3). ``merge.py`` maps entries onto a reference clock.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from neptune.model.knowledge import (
    Ambiguous,
    Candidate,
    Knowledge,
    Known,
    NotApplicable,
    NotCovered,
)
from neptune.model.versions import SemanticVersion
from neptune_ledger.api.types import (
    AsRegisteredBy,
    LatestTransform,
    LineageSet,
    Partition,
    Pinned,
    Preference,
    Role,
    ThreadEntry,
    TransactionKey,
    WorldTime,
)

SetKey = tuple[str, str]  # (record kind, source content id): one lineage set of one thread
Chain = tuple[tuple[str, str], ...]  # (adapter id, adapter version) from the root to a transform
_OPEN: Final = (1, 0)


@dataclass(frozen=True)
class Member:
    """One thread entry as stored: a record in one registering package (``thread_member``)."""

    package_id: str
    record_id: str
    kind: str
    roles: tuple[str, ...]
    registration: TransactionKey
    transform_id: str
    source: str
    world: Knowledge[WorldTime]

    @property
    def lineage_set(self) -> SetKey:
        return self.kind, self.source


def timed(entry: ThreadEntry) -> WorldTime | None:
    return entry.world.value if isinstance(entry.world, Known) else None


def native_key(entry: ThreadEntry) -> tuple[object, ...]:
    """ADR 0003 §3: inside a clock partition, ``(s, e with open last, registration key, record
    id, package id)``; untimed entries by ``(registration key, record id, package id)``."""
    tail = (
        entry.registration_key.tx_seq,
        entry.record_id.encode("utf-8"),
        entry.packages[0].encode("utf-8"),
    )
    world = timed(entry)
    if world is None:
        return tail
    closed = world.closed_end
    return (world.start.ticks, (0, closed) if closed is not None else _OPEN, *tail)


def transaction_key(entry: ThreadEntry) -> tuple[object, ...]:
    return (
        entry.registration_key.tx_seq,
        entry.record_id.encode("utf-8"),
        entry.packages[0].encode("utf-8"),
    )


def history_entries(members: Iterable[Member]) -> list[ThreadEntry]:
    """Every member as its own entry: one package each, nothing collapsed (ADR 0003 §4.2)."""
    return [_entry(m.record_id, m.kind, set(m.roles), [m]) for m in members]


def _entry(record_id: str, kind: str, roles: set[str], members: Sequence[Member]) -> ThreadEntry:
    first = members[0]
    return ThreadEntry(
        record_id=record_id,
        kind=kind,
        roles=cast("tuple[Role, ...]", tuple(sorted(roles))),
        packages=tuple(m.package_id for m in members),
        registration_key=first.registration,
        transform_id=first.transform_id,
        lineage_source=first.source,
        world=first.world,
    )


def collapse(members: Iterable[Member]) -> list[ThreadEntry]:
    """Selected members with one entry per record id, its packages in registration order and
    the roles it holds in any of them (ADR 0003 §4.3). One record id has one body (ADR 0005 §2),
    so its transform, source and world time agree across packages."""
    by_record: dict[str, list[Member]] = {}
    for member in members:
        by_record.setdefault(member.record_id, []).append(member)
    out = []
    for record_id, group in by_record.items():
        group.sort(key=lambda m: (m.registration.tx_seq, m.package_id.encode("utf-8")))
        roles = {role for m in group for role in m.roles}
        out.append(_entry(record_id, group[0].kind, roles, group))
    return out


def world_partitions(entries: Iterable[ThreadEntry]) -> tuple[Partition, ...]:
    """ADR 0003 §3 world order: one partition per ordering clock, sorted by its smallest
    registration key then clock key bytes, then the untimed partition. Never empty partitions."""
    clocks: dict[str, list[ThreadEntry]] = {}
    untimed: list[ThreadEntry] = []
    for entry in entries:
        world = timed(entry)
        if world is None:
            untimed.append(entry)
        else:
            clocks.setdefault(world.clock, []).append(entry)
    partitions = [
        Partition("clock", tuple(sorted(group, key=native_key)), clock)
        for clock, group in clocks.items()
    ]
    partitions.sort(key=partition_key)
    if untimed:
        partitions.append(Partition("untimed", tuple(sorted(untimed, key=native_key))))
    return tuple(partitions)


def partition_key(partition: Partition) -> tuple[int, bytes]:
    """Partitions by ``(smallest registration key among their entries, clock key bytes)``."""
    smallest = min(entry.registration_key.tx_seq for entry in partition.entries)
    return smallest, (partition.clock_key or "").encode("utf-8")


def transaction_partitions(entries: Iterable[ThreadEntry]) -> tuple[Partition, ...]:
    """Transaction order: one partition by ``(registration key, record id, package id)``."""
    ordered = tuple(sorted(entries, key=transaction_key))
    return (Partition("transaction", ordered),) if ordered else ()


def ordered(
    entries: Iterable[ThreadEntry], order: Literal["transaction", "world"]
) -> tuple[Partition, ...]:
    return world_partitions(entries) if order == "world" else transaction_partitions(entries)


# --- Lineage sets and the current-view resolver (ADR 0003 §4) ----------------------------------


def lineage_sets(members: Iterable[Member]) -> dict[SetKey, list[Member]]:
    sets: dict[SetKey, list[Member]] = {}
    for member in members:
        sets.setdefault(member.lineage_set, []).append(member)
    return sets


def history_sets(sets: Mapping[SetKey, Sequence[Member]]) -> tuple[LineageSet, ...]:
    """Every lineage set with its transforms, resolution ``NotApplicable`` (history)."""
    return tuple(
        LineageSet(kind, source, _transforms(sets[(kind, source)]), NotApplicable())
        for kind, source in _set_order(sets)
    )


def _set_order(sets: Mapping[SetKey, object]) -> list[SetKey]:
    return sorted(sets, key=lambda k: (k[0].encode("utf-8"), k[1].encode("utf-8")))


def _transforms(members: Iterable[Member]) -> tuple[str, ...]:
    return tuple(sorted({m.transform_id for m in members}, key=str.encode))


def resolve(
    sets: Mapping[SetKey, Sequence[Member]],
    preference: Preference,
    chains: Mapping[str, Chain | None],
) -> tuple[tuple[LineageSet, ...], list[Member]]:
    """Each lineage set resolved under ``preference``, and the members of ``Known`` sets whose
    transform is the resolved one. ``chains`` gives each transform's chain, ``None`` when some
    transform on it is not registered at this point (then it compares with nothing)."""
    out: list[LineageSet] = []
    selected: list[Member] = []
    for key in _set_order(sets):
        members = sets[key]
        resolution = _resolve_set(members, preference, chains)
        out.append(LineageSet(key[0], key[1], _transforms(members), resolution))
        if isinstance(resolution, Known):
            selected += [m for m in members if m.transform_id == resolution.value]
    return tuple(out), selected


def _resolve_set(
    members: Sequence[Member], preference: Preference, chains: Mapping[str, Chain | None]
) -> Knowledge[str]:
    transforms = _transforms(members)
    match preference:
        case Pinned(transform_id=pinned):
            return Known(pinned) if pinned in transforms else NotCovered()
        case AsRegisteredBy(package_id=package):
            used = _transforms(m for m in members if m.package_id == package)
            return _one_of(used)
        case LatestTransform():
            return _one_of(undominated(transforms, chains))
    raise TypeError(f"not a current-view preference: {preference!r}")


def _one_of(candidates: Sequence[str]) -> Knowledge[str]:
    if not candidates:
        return NotCovered()
    if len(candidates) == 1:
        return Known(candidates[0])
    return Ambiguous(tuple(Candidate(c) for c in sorted(candidates, key=str.encode)))


def _precedence(chain: Chain | None) -> tuple[tuple[str, ...], tuple[object, ...]] | None:
    """A chain's adapter-id sequence and its versions' SemVer §11 keys; None when any version
    is not valid SemVer or the chain is not known (incomparable with every chain)."""
    if chain is None:
        return None
    keys = []
    for _, version in chain:
        try:
            keys.append(SemanticVersion(version).precedence_key())
        except ValueError:
            return None
    return tuple(adapter for adapter, _ in chain), tuple(keys)


def undominated(transforms: Sequence[str], chains: Mapping[str, Chain | None]) -> list[str]:
    """ADR 0003 §4.4: the candidates no comparable candidate strictly exceeds.

    Chains are comparable iff their adapter-id sequences are equal; they compare element-wise
    from the root by SemVer precedence. Registration order never decides anything here.
    """
    keyed = {t: _precedence(chains.get(t)) for t in transforms}
    out = []
    for t in transforms:
        mine = keyed[t]
        dominated = mine is not None and any(
            other is not None and other[0] == mine[0] and other[1] > mine[1]
            for other in keyed.values()
        )
        if not dominated:
            out.append(t)
    return sorted(out, key=str.encode)


def chain_of(
    transform_id: str,
    adapters: Mapping[str, tuple[str, str]],
    upstream: Mapping[str, Sequence[str]],
) -> Chain | None:
    """The transform's chain: upstream flattened depth first in consumed order, then itself
    (root ADR 0016 §4). ``None`` when a transform on it is not registered, or on a cycle."""

    def walk(tid: str, seen: frozenset[str]) -> Chain | None:
        if tid in seen or tid not in adapters:
            return None
        out: list[tuple[str, str]] = []
        for parent in upstream.get(tid, ()):
            above = walk(parent, seen | {tid})
            if above is None:
                return None
            out += above
        out.append(adapters[tid])
        return tuple(out)

    return walk(transform_id, frozenset())
