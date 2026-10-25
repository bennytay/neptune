"""The append-only fact log (spec §5): JSON Lines segments under ``<site_dir>/.neptune/log/``.

Each line is one ``LogEntry`` in canonical JSON. ``seq`` starts at 1 and counts entries: it is when
Neptune knew something, never a wall clock. A segment holds ``SEGMENT_SIZE`` entries, then the next
one starts; nothing is rewritten.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import IO, TYPE_CHECKING, Literal

from neptune_memory.site.facts import Confirm, Fact, LogEntry, Seen, canonical

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

SEGMENT_SIZE = 10_000


def log_dir(site_dir: Path) -> Path:
    """Where a site's log lives."""
    return Path(site_dir) / ".neptune" / "log"


def segment_path(root: Path, seq: int) -> Path:
    """The segment file that holds entry ``seq``."""
    return root / f"segment-{(seq - 1) // SEGMENT_SIZE + 1:06d}.jsonl"


def read(site_dir: Path, *, as_of_seq: int | None = None) -> Iterator[LogEntry]:
    """Every entry in ``seq`` order, up to and including ``as_of_seq``."""
    for segment in sorted(log_dir(site_dir).glob("segment-*.jsonl")):
        with segment.open(encoding="utf-8") as lines:
            for line in lines:
                entry = LogEntry.from_json(json.loads(line))
                if as_of_seq is not None and entry.seq > as_of_seq:
                    return
                yield entry


def last_seq(site_dir: Path) -> int:
    """The highest ``seq`` written so far; 0 for an empty log."""
    segments = sorted(log_dir(site_dir).glob("segment-*.jsonl"))
    if not segments:
        return 0
    lines = segments[-1].read_text(encoding="utf-8").splitlines()
    if not lines:
        return 0
    seq: int = json.loads(lines[-1])["seq"]
    return seq


class FactLog:
    """A writer that appends entries to a site's log, numbering them as it goes.

    Use it as a context manager; ``flush`` makes what was appended visible to readers at once
    (a live replay calls it after each message).
    """

    def __init__(self, site_dir: Path) -> None:
        self.site_dir = Path(site_dir)
        self.root = log_dir(self.site_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.seq = last_seq(self.site_dir)
        self._handle: IO[str] | None = None
        self._segment: Path | None = None

    def __enter__(self) -> FactLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def append(
        self, entry_type: Literal["fact", "seen", "confirm"], payload: Fact | Seen | Confirm
    ) -> LogEntry:
        """Number ``payload`` with the next ``seq`` and write it as one line."""
        seq = self.seq + 1
        entry = LogEntry(
            seq=seq,
            type=entry_type,
            fact=payload if isinstance(payload, Fact) else None,
            seen=payload if isinstance(payload, Seen) else None,
            confirm=payload if isinstance(payload, Confirm) else None,
        )
        self._writer(seq).write(canonical(entry.to_json()) + "\n")
        self.seq = seq
        return entry

    def fact(self, fact: Fact) -> LogEntry:
        return self.append("fact", fact)

    def seen(self, source: str, t: str) -> LogEntry:
        return self.append("seen", Seen(source=source, t=t))

    def confirm(self, proposal: str, answer: Literal["yes", "no"], by: str) -> LogEntry:
        return self.append("confirm", Confirm(proposal=proposal, answer=answer, by=by))

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
            self._segment = None

    def _writer(self, seq: int) -> IO[str]:
        segment = segment_path(self.root, seq)
        if segment != self._segment:
            self.close()
            self._handle = segment.open("a", encoding="utf-8", newline="\n")
            self._segment = segment
        assert self._handle is not None
        return self._handle
