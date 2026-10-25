"""Compile a site directory into its fact log (spec §5).

``build`` writes the whole log at once; ``replay`` writes the same entries live, sleeping between
feed messages. Both start a fresh log: the corpus is the source, ``.neptune/`` is output.

Static files go first: ``static/`` is ingested by the compiler (in process), the package is read
back through ``neptune.sdk.read_package`` (memory never imports ``neptune.store``), and each
record goes through ``mappers.map_record``; then ``static/layout.yaml`` through
``mappers.map_layout``. Then the feed: for each ``replay.messages`` message, a ``seen`` entry and
the facts ``mappers.map_message`` returns.

The mappers (#184) and the replay (#182) are reached through ``Seam``, so tests and early callers
can hand in their own.
"""

from __future__ import annotations

import functools
import importlib
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from neptune.sdk import Isolation, JobOptions, ingest, read_package
from neptune_memory.site.log import FactLog, log_dir

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

    @staticmethod
    def default() -> Seam:
        """``neptune_memory.site.mappers`` and ``neptune_memory.site.replay``.

        ``map_message`` carries one ``last`` state through the build, so feed facts are logged
        only when a value changes for (subject, predicate, source) (spec §4)."""
        mappers: Any = importlib.import_module("neptune_memory.site.mappers")
        replayer: Any = importlib.import_module("neptune_memory.site.replay")
        return Seam(
            map_layout=mappers.map_layout,
            map_record=mappers.map_record,
            map_message=functools.partial(mappers.map_message, last={}),
            messages=replayer.messages,
        )


def build(site_dir: Path, *, until: str | None = None, seam: Seam | None = None) -> Path:
    """Write the site's log: static first, then the feed up to ``until`` (site time, inclusive).
    Returns the log directory."""
    site_dir = Path(site_dir)
    seam = seam or Seam.default()
    with _fresh(site_dir) as log:
        _static(site_dir, log, seam)
        for message in _feed(site_dir, seam, until):
            _append_message(log, seam, message)
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
    seam = seam or Seam.default()
    with _fresh(site_dir) as log:
        _static(site_dir, log, seam)
        log.flush()
        previous: datetime | None = None
        for message in _feed(site_dir, seam, until):
            now = datetime.fromisoformat(message.t)
            if previous is not None and now > previous:
                sleep((now - previous).total_seconds() / speed)
            previous = now
            _append_message(log, seam, message)
            log.flush()
    return log_dir(site_dir)


def _fresh(site_dir: Path) -> FactLog:
    for output in (log_dir(site_dir), _package_dir(site_dir)):
        if output.exists():
            shutil.rmtree(output)
    return FactLog(site_dir)


def _package_dir(site_dir: Path) -> Path:
    return site_dir / ".neptune" / "package"


def _static(site_dir: Path, log: FactLog, seam: Seam) -> None:
    package_dir = _package_dir(site_dir)
    ingest(
        site_dir / "static",
        package_dir,
        workspace=site_dir / ".neptune" / "workspace",
        options=JobOptions(isolation=Isolation.IN_PROCESS),
    )
    package = read_package(package_dir)
    for record in package.records:
        for fact in seam.map_record(record, package):
            log.fact(fact)
    for fact in seam.map_layout(site_dir / "static" / "layout.yaml"):
        log.fact(fact)


def _feed(site_dir: Path, seam: Seam, until: str | None) -> Iterable[Message]:
    for message in seam.messages(site_dir):
        if until is not None and message.t > until:
            return
        yield message


def _append_message(log: FactLog, seam: Seam, message: Message) -> None:
    log.seen(message.source, message.t)
    robot = _fleet_robot(message)
    if robot is not None:  # one fleet source reports every AMR; freshness is per robot
        log.seen(f"{message.source}/{robot}", message.t)
    for fact in seam.map_message(message):
        log.fact(fact)


def _fleet_robot(message: Message) -> str | None:
    """The robot a fleet state message is about: ``uagv/v2/<site>/<robot>/state``."""
    parts = str(getattr(message, "topic", "")).split("/")
    if message.source == "fleet" and len(parts) == 5 and parts[0] == "uagv" and parts[4] == "state":
        return parts[3]
    return None
