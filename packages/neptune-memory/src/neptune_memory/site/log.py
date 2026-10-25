"""The append-only fact log (spec §5): JSON Lines segments under ``<site_dir>/.neptune/log/``.

Each line is one ``LogEntry`` in canonical JSON. ``seq`` starts at 1 and counts entries: it is when
Neptune knew something, never a wall clock. A segment holds ``SEGMENT_SIZE`` entries, then the next
one starts; nothing is rewritten.
"""

from __future__ import annotations

import fcntl
import json
import os
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

    Use it as a context manager. Several processes may append to one log at once (``live``, the
    MCP server proposing an action, a person approving it on the CLI): each append holds
    ``.neptune/log.lock``, picks up what the others wrote, and is on disk when it returns.
    """

    def __init__(self, site_dir: Path) -> None:
        self.site_dir = Path(site_dir)
        self.root = log_dir(self.site_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = os.open(self.root.parent / "log.lock", os.O_RDWR | os.O_CREAT, 0o644)
        self._handle: IO[str] | None = None
        self._segment: Path | None = None
        self._sync()

    def __enter__(self) -> FactLog:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
        os.close(self._lock)

    def append(
        self, entry_type: Literal["fact", "seen", "confirm"], payload: Fact | Seen | Confirm
    ) -> LogEntry:
        """Number ``payload`` with the next ``seq`` and write it as one line."""
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            if self._size(self.seq) != self._end:  # another writer appended since
                self._sync()
            seq = self.seq + 1
            entry = LogEntry(
                seq=seq,
                type=entry_type,
                fact=payload if isinstance(payload, Fact) else None,
                seen=payload if isinstance(payload, Seen) else None,
                confirm=payload if isinstance(payload, Confirm) else None,
            )
            handle = self._writer(seq)
            handle.write(canonical(entry.to_json()) + "\n")
            handle.flush()
            self.seq = seq
            self._end = os.fstat(handle.fileno()).st_size
        finally:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
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

    def _size(self, seq: int) -> int:
        """The size of the segment holding ``seq`` (0 when there is none yet)."""
        try:
            return segment_path(self.root, max(seq, 1)).stat().st_size
        except FileNotFoundError:
            return 0

    def _sync(self) -> None:
        self.close()
        self.seq = last_seq(self.site_dir)
        self._end = self._size(self.seq)

    def _writer(self, seq: int) -> IO[str]:
        segment = segment_path(self.root, seq)
        if segment != self._segment:
            self.close()
            self._handle = segment.open("a", encoding="utf-8", newline="\n")
            self._segment = segment
        assert self._handle is not None
        return self._handle


class Tail:
    """Reads a log as it grows: each ``new()`` returns the complete entries appended since the
    last call. ``None`` means the log was started again (a fresh ``build``): read it from scratch.
    """

    def __init__(self, site_dir: Path) -> None:
        self.root = log_dir(Path(site_dir))
        self.seq = 0
        self._segment: Path | None = None
        self._inode = 0
        self._offset = 0

    def new(self) -> list[LogEntry] | None:
        segments = sorted(self.root.glob("segment-*.jsonl"))
        if self._segment is not None:
            if self._segment not in segments or self._segment.stat().st_ino != self._inode:
                return None
            segments = [s for s in segments if s >= self._segment]
        out: list[LogEntry] = []
        for segment in segments:
            if segment != self._segment:
                self._segment, self._inode, self._offset = segment, segment.stat().st_ino, 0
            with segment.open("rb") as handle:
                handle.seek(self._offset)
                data = handle.read()
            if len(data) == 0 and segment.stat().st_size < self._offset:
                return None
            complete = data[: data.rfind(b"\n") + 1]
            for line in complete.splitlines():
                try:
                    entry = LogEntry.from_json(json.loads(line))
                except (ValueError, KeyError):  # read from mid-line: the log was replaced
                    return None
                if entry.seq != self.seq + 1:
                    return None
                self.seq = entry.seq
                out.append(entry)
            self._offset += len(complete)
        return out
