"""Replay the corpus feeds as one ordered stream of messages."""

import json
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FeedMessage:
    """One feed line: the envelope, plus where it came from for citations."""

    t: str
    source: str
    topic: str
    payload: dict[str, Any]
    file: str  # corpus-relative, e.g. "feed/plc_cv2.jsonl"
    line: int  # 1-based


def messages(site_dir: Path) -> Iterator[FeedMessage]:
    """Every message under ``<site_dir>/feed/*.jsonl`` in arrival order: ``(t, source, line)``."""
    found: list[FeedMessage] = []
    for path in sorted((site_dir / "feed").glob("*.jsonl")):
        rel = path.relative_to(site_dir).as_posix()
        with path.open(encoding="utf-8") as handle:
            for number, text in enumerate(handle, start=1):
                if not text.strip():
                    continue
                env = json.loads(text)
                found.append(
                    FeedMessage(env["t"], env["source"], env["topic"], env["payload"], rel, number)
                )
    found.sort(key=lambda m: (m.t, m.source, m.line))
    yield from found


def pace(
    stream: Iterable[FeedMessage],
    speed: float = 60.0,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[FeedMessage]:
    """Yield each message after sleeping ``Δt / speed`` seconds since the previous one."""
    previous: datetime | None = None
    for message in stream:
        now = datetime.fromisoformat(message.t)
        if previous is not None and now > previous:
            sleep((now - previous).total_seconds() / speed)
        previous = now
        yield message
