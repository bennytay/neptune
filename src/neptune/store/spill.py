"""External sorting with bounded memory, spilling sorted runs to a scratch directory (ADR 0065).

A ``Sorter`` takes ``(key, payload)`` entries in any order and gives them back sorted by key,
stably: entries with equal keys come back in the order they were added. Its entries wait in memory
until the ``SpillBudget`` it shares with other sorters is spent; then the sorter holding the most
is sorted and written out as a run, a file of its own under the scratch directory. Reading merges
the runs (``heapq.merge``, which is stable across runs taken in the order they were written) with
whatever is still held, so the memory a sorter needs is its share of the budget plus one read
buffer per run merged, never its entry count.

Without a directory nothing is spilled: the sorter holds everything and sorts it in memory, which
is what a package built wholly in memory (``package_contents``) wants.

A run is a sequence of entries, each ``>II`` (key length, payload length), the key as canonical
JSON (a list of strings and integers) and the payload as given. Nothing in a run depends on the
clock, the process or chance, so the same entries spill to the same bytes; runs are named by the
sorter and a counter, and removed by ``close``.
"""

import heapq
import json
import struct
from collections.abc import Iterator
from operator import itemgetter
from pathlib import Path
from typing import BinaryIO, Final, TypeAlias

from neptune.identity import canonical_json

Key: TypeAlias = tuple[str | int, ...]
Entry: TypeAlias = tuple[Key, bytes]

# Memory held by unspilled entries across every sorter of one write, as charged by ``_charge``.
SPILL_BUDGET: Final = 32 * 1024 * 1024
# The most runs merged at once: more are first merged into fewer, in order, so stability holds
# and a merge never holds more than this many read buffers or file descriptors.
FAN_IN: Final = 64
_READ_BUFFER: Final = 64 * 1024
_HEADER: Final = struct.Struct(">II")
# What one held entry costs beyond its key and payload bytes: the tuples and object headers.
_OVERHEAD: Final = 160


def _charge(key: Key, payload: bytes) -> int:
    return _OVERHEAD + len(payload) + sum(len(part) if isinstance(part, str) else 8 for part in key)


class SpillBudget:
    """The memory every sorter of one write may hold before the largest spills a run."""

    def __init__(self, limit: int = SPILL_BUDGET) -> None:
        if limit < 1:
            raise ValueError("a spill budget must be positive")
        self.limit = limit
        self.held = 0
        self._sorters: list[Sorter] = []

    def _join(self, sorter: "Sorter") -> None:
        self._sorters.append(sorter)

    def _spend(self, amount: int) -> None:
        self.held += amount
        while self.held > self.limit:
            candidates = [s for s in self._sorters if s.spillable and s.held]
            if not candidates:
                return
            max(candidates, key=lambda sorter: sorter.held).spill()


class Sorter:
    """Entries sorted by key, stably, in bounded memory when given a scratch directory."""

    def __init__(self, name: str, directory: Path | None, budget: SpillBudget) -> None:
        self.name = name
        self._directory = directory
        self._budget = budget
        self._buffer: list[Entry] = []
        self._runs: list[Path] = []
        self._made = 0
        self._count = 0
        self._sorted = True  # whether ``_buffer`` is in key order
        self._reading = False
        self.held = 0  # what the buffer is charged
        budget._join(self)

    @property
    def spillable(self) -> bool:
        return self._directory is not None and not self._reading

    def __len__(self) -> int:
        return self._count

    def add(self, key: Key, payload: bytes) -> None:
        if self._reading:
            raise RuntimeError(f"sorter {self.name} is being read; it takes no more entries")
        if self._sorted and self._buffer and key < self._buffer[-1][0]:
            self._sorted = False
        self._buffer.append((key, payload))
        self._count += 1
        cost = _charge(key, payload)
        self.held += cost
        self._budget._spend(cost)

    def _sort_buffer(self) -> None:
        if not self._sorted:
            self._buffer.sort(key=itemgetter(0))  # stable: equal keys keep their order
            self._sorted = True

    def _new_run(self) -> Path:
        assert self._directory is not None
        self._made += 1
        return self._directory / f"{self.name}.{self._made:06d}.run"

    def spill(self) -> None:
        """Write what the sorter holds, sorted, as a new run, and let it go."""
        if self._directory is None or not self._buffer:
            return
        self._sort_buffer()
        path = self._new_run()
        with path.open("xb") as run:
            _write(run, self._buffer)
        self._runs.append(path)
        self._buffer = []
        self._sorted = True
        self._budget.held -= self.held
        self.held = 0

    def __iter__(self) -> Iterator[Entry]:
        """Every entry, sorted by key; equal keys in the order added. Reading may repeat.

        Once read, a sorter takes no more entries. With a directory, what it still holds is spilled
        first, so a reader holds only read buffers while it merges.
        """
        self._reading = True
        if self._directory is None:
            self._sort_buffer()
            return iter(self._buffer)
        self.spill()
        while len(self._runs) > FAN_IN:
            self._reduce()
        if len(self._runs) == 1:
            return _read(self._runs[0])
        return heapq.merge(*(_read(run) for run in self._runs), key=itemgetter(0))

    def _reduce(self) -> None:
        """Merge the first ``FAN_IN`` runs into one, in place: they were written in order, so
        equal keys stay in the order they were added."""
        group, rest = self._runs[:FAN_IN], self._runs[FAN_IN:]
        path = self._new_run()
        with path.open("xb") as run:
            _write(run, heapq.merge(*(_read(r) for r in group), key=itemgetter(0)))
        for done in group:
            done.unlink()
        self._runs = [path, *rest]

    def close(self) -> None:
        """Remove every run and let go of what is held."""
        for run in self._runs:
            run.unlink(missing_ok=True)
        self._runs = []
        self._budget.held -= self.held
        self.held = 0
        self._buffer = []


def _write(run: BinaryIO, entries: "Iterator[Entry] | list[Entry]") -> None:
    for key, payload in entries:
        encoded = canonical_json.dumps(list(key))
        run.write(_HEADER.pack(len(encoded), len(payload)))
        run.write(encoded)
        run.write(payload)


def _read(path: Path) -> Iterator[Entry]:
    with path.open("rb", buffering=_READ_BUFFER) as run:
        while header := run.read(_HEADER.size):
            if len(header) != _HEADER.size:
                raise OSError(f"{path} is truncated")
            key_size, payload_size = _HEADER.unpack(header)
            key = run.read(key_size)
            payload = run.read(payload_size)
            if len(key) != key_size or len(payload) != payload_size:
                raise OSError(f"{path} is truncated")
            yield tuple(json.loads(key)), payload
