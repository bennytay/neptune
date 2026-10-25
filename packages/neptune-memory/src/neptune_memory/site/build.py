"""Compile a site directory into its fact log (spec §5).

``build`` writes the whole log at once; ``replay`` writes the same entries live, sleeping between
feed messages. Both start a fresh log: the corpus is the source, ``.neptune/`` is output.

Static files go first: ``static/`` is ingested by the compiler (in process), the package is read
back through ``neptune.sdk.read_package`` (memory never imports ``neptune.store``), and each
record goes through ``mappers.map_record`` with the site's mapping file
(``static/neptune.mappings.yaml``, spec §12); then ``static/layout.yaml`` through
``mappers.map_layout``. Then the feed: for each ``replay.messages`` message, a ``seen`` entry and
the facts ``mappers.map_message`` returns. Whenever a new ``serial`` lands, ``identity`` proposes
any ``same_as`` it now implies (inferred, pending until someone confirms it), so a live replay
shows the proposal the moment the second serial arrives.

The mappers (#184) and the replay (#182) are reached through ``Seam``, so tests and early callers
can hand in their own. ``Appender`` is the same path held open, one input at a time: the live site
(``live.py``) appends each message as it arrives and each static file as it changes.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from neptune.sdk import Isolation, JobOptions, ingest, read_package
from neptune_memory.site.identity import propose_same_as
from neptune_memory.site.log import FactLog, log_dir, read

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from neptune_memory.site.facts import Fact


class Message(Protocol):
    """What the log needs of a ``FeedMessage``: its site time and source."""

    @property
    def t(self) -> str: ...

    @property
    def source(self) -> str: ...


@dataclass(frozen=True)
class Seam:
    """The functions the log is fed by: the mappers' three and the replay's ``messages``."""

    map_layout: Callable[[Path], Iterable[Fact]]
    map_record: Callable[[Any, Any], Iterable[Fact]]  # (record, IngestPackage)
    map_message: Callable[[Any], Iterable[Fact]]
    messages: Callable[[Path], Iterable[Any]]
    device: Callable[[Any], str | None] = lambda message: None
    """The device a message reports for, if its feed names one per message (spec §12)."""

    @staticmethod
    def default(site_dir: Path) -> Seam:
        """``neptune_memory.site.mappers`` with the site's mapping file, and
        ``neptune_memory.site.replay``.

        ``map_message`` carries one ``last`` state through the build, so feed facts are logged
        only when a value changes for (subject, predicate, source) (spec §4)."""
        mappers: Any = importlib.import_module("neptune_memory.site.mappers")
        replayer: Any = importlib.import_module("neptune_memory.site.replay")
        mapping = mappers.load_mapping(Path(site_dir))
        return Seam(
            map_layout=mappers.map_layout,
            map_record=functools.partial(mappers.map_record, mapping=mapping),
            map_message=functools.partial(mappers.map_message, mapping=mapping, last={}),
            messages=replayer.messages,
            device=functools.partial(mappers.device, mapping=mapping),
        )


def build(site_dir: Path, *, until: str | None = None, seam: Seam | None = None) -> Path:
    """Write the site's log: static first, then the feed up to ``until`` (site time, inclusive).
    Returns the log directory."""
    site_dir = Path(site_dir)
    seam = seam or Seam.default(site_dir)
    with _fresh(site_dir) as log:
        out = _Writer(log)
        _static(site_dir, out, seam)
        for message in _feed(site_dir, seam, until):
            _append_message(out, seam, message)
    return log_dir(site_dir)


def replay(
    site_dir: Path,
    *,
    speed: float = 60.0,
    until: str | None = None,
    seam: Seam | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """``build``, live: sleep ``Δt / speed`` between feed messages, flushing each as it lands."""
    site_dir = Path(site_dir)
    seam = seam or Seam.default(site_dir)
    with _fresh(site_dir) as log:
        out = _Writer(log)
        _static(site_dir, out, seam)
        log.flush()
        previous: datetime | None = None
        for message in _feed(site_dir, seam, until):
            now = datetime.fromisoformat(message.t)
            if previous is not None and now > previous:
                sleep((now - previous).total_seconds() / speed)
            previous = now
            _append_message(out, seam, message)
            log.flush()
    return log_dir(site_dir)


class Appender:
    """A fresh log held open: ``static()`` once, then ``message()`` / ``restatic()`` as inputs
    arrive. Each call flushes, so readers (the MCP server) see it at once. ``static()`` then
    ``message()`` for each message writes the bytes ``build`` writes for the same messages."""

    def __init__(self, site_dir: Path, seam: Seam | None = None) -> None:
        self.site_dir = Path(site_dir)
        self._default = seam is None
        self.seam = seam or Seam.default(self.site_dir)
        self.log = _fresh(self.site_dir)
        self._out = _Writer(self.log)

    def __enter__(self) -> Appender:
        return self

    def __exit__(self, *exc: object) -> None:
        self.log.__exit__(None, None, None)

    def static(self) -> None:
        """Ingest ``static/`` and append its facts."""
        _static(self.site_dir, self._out, self.seam)
        self.log.flush()

    def message(self, message: Message) -> None:
        """Append one feed message: ``seen``, then its facts."""
        _append_message(self._out, self.seam, message)
        self.log.flush()

    def restatic(self) -> int:
        """``static/`` changed: ingest it again and append only the facts not yet in the log (a
        record's facts may lean on other files, e.g. the asset register). Returns how many."""
        self.log.flush()
        if self._default:  # the mapping file may be what changed: tables and documents reread it
            fresh_seam = Seam.default(self.site_dir)
            self.seam = dataclasses.replace(self.seam, map_record=fresh_seam.map_record)
        logged = {e.fact.id for e in read(self.site_dir) if e.fact is not None}
        before = self.log.seq
        fresh = _Fresh(self._out, logged)
        _static(self.site_dir, fresh, self.seam)
        self.log.flush()
        return self.log.seq - before


@dataclass
class _Fresh:
    """A ``_Writer`` front that drops facts the log already holds."""

    writer: _Writer
    logged: set[str]

    def emit(self, facts: Iterable[Fact]) -> None:
        new = [f for f in facts if f.id not in self.logged]
        self.logged.update(f.id for f in new)
        self.writer.emit(new)


def _fresh(site_dir: Path) -> FactLog:
    for output in (log_dir(site_dir), _package_dir(site_dir)):
        if output.exists():
            shutil.rmtree(output)
    return FactLog(site_dir)


def _package_dir(site_dir: Path) -> Path:
    return site_dir / ".neptune" / "package"


def _static(site_dir: Path, out: _Writer | _Fresh, seam: Seam) -> None:
    package_dir = _package_dir(site_dir)
    if package_dir.exists():  # a live re-ingest; build and replay start without one
        shutil.rmtree(package_dir)
    ingest(
        site_dir / "static",
        package_dir,
        workspace=site_dir / ".neptune" / "workspace",
        options=JobOptions(isolation=Isolation.IN_PROCESS),
    )
    package = read_package(package_dir)
    for record in package.records:
        out.emit(seam.map_record(record, package))
    out.emit(seam.map_layout(site_dir / "static" / "layout.yaml"))


def _feed(site_dir: Path, seam: Seam, until: str | None) -> Iterable[Message]:
    for message in seam.messages(site_dir):
        if until is not None and message.t > until:
            return
        yield message


def _append_message(out: _Writer, seam: Seam, message: Message) -> None:
    log = out.log
    log.seen(message.source, message.t)
    device = seam.device(message)
    if device is not None:  # one source reports for many devices; freshness is per device
        log.seen(f"{message.source}/{device}", message.t)
    out.emit(seam.map_message(message))


@dataclass
class _Writer:
    """The log plus the serials seen so far, so identity proposals follow the serials live."""

    log: FactLog
    serials: list[Fact] = field(default_factory=list)
    proposed: set[str] = field(default_factory=set)

    def emit(self, facts: Iterable[Fact]) -> None:
        """Append ``facts``; after a new ``serial``, append the proposals it implies."""
        new_serial = False
        for fact in facts:
            self.log.fact(fact)
            if fact.predicate == "serial":
                self.serials.append(fact)
                new_serial = True
        if new_serial:
            for proposal in propose_same_as(self.serials):
                if proposal.id not in self.proposed:
                    self.proposed.add(proposal.id)
                    self.log.fact(proposal)
