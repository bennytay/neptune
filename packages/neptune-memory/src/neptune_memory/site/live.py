"""The live site (#202): feeds over MQTT, a watched ``static/``, a log that grows as they arrive.

- ``publish`` replays the corpus feed (``replay.messages``) to a broker, each message on its real
  topic (``spBv1.0/...`` Sparkplug-style, ``uagv/v2/...`` VDA 5050-style, ``c3/controller/log``),
  the payload being the feed envelope ``{"t", "source", "topic", "payload"}``.
- ``live`` subscribes to every topic and, per message, **records first**: the envelope goes to
  ``feed/live/segment-NNNNNN.jsonl`` as one line, and that line is what the facts cite. Then it
  appends ``seen`` and the facts through ``build.Appender``, the same mapper path as ``build``,
  without a rebuild. It also polls ``static/``: when a file appears or changes, ``static/`` is
  ingested again and only the facts the log lacks are appended.
- Actions (``actions.py``): ``live`` steps a ``Watcher`` that logs each dispatched action's
  outcome and prints every action event; ``publish`` answers commands to robots like a fleet
  manager (``Simulator``). Commands (``.../instantActions``, ``.../order``) are never recorded.
- ``captured`` reads the recording back in arrival order, so ``build`` over it gives the same log
  (``Seam(messages=captured)``).
- ``broker`` runs a pure-pip MQTT broker (amqtt), so a laptop needs no Docker.

No auth, no TLS, no reconnect handling: this is a demo.
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from neptune_memory.site.actions import Simulator, Watcher, is_command
from neptune_memory.site.build import Appender, Seam
from neptune_memory.site.log import SEGMENT_SIZE
from neptune_memory.site.replay import FeedMessage, messages, pace

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

DEFAULT_BROKER = "mqtt://127.0.0.1:1883"
LIVE_DIR = "feed/live"
SUBSCRIBE = "#"
QOS = 1


def address(broker: str) -> tuple[str, int]:
    """``mqtt://host:port`` -> ``(host, port)``; port 1883 when none is given."""
    parts = urlsplit(broker if "://" in broker else f"mqtt://{broker}")
    return parts.hostname or "127.0.0.1", parts.port or 1883


def ready_path(site_dir: Path) -> Path:
    """Exists while ``live`` is subscribed: what ``make site-live`` waits for before publishing."""
    return Path(site_dir) / ".neptune" / "live.ready"


def _client(name: str) -> Any:
    from paho.mqtt.client import Client
    from paho.mqtt.enums import CallbackAPIVersion

    return Client(CallbackAPIVersion.VERSION2, client_id=name)


def _connect(client: Any, broker: str, wait: float = 10.0) -> None:
    """Connect, waiting up to ``wait`` seconds for a broker that is still starting."""
    deadline = time.monotonic() + wait
    while True:
        try:
            client.connect(*address(broker))
            return
        except ConnectionRefusedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


def _envelope(message: FeedMessage) -> bytes:
    env: dict[str, Any] = {"t": message.t, "source": message.source, "topic": message.topic}
    env["payload"] = message.payload
    return json.dumps(env, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- publish ----------------------------------------------------------------------------------


def publish(
    site_dir: Path,
    broker: str = DEFAULT_BROKER,
    *,
    speed: float = 60.0,
    start: str | None = None,
    until: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Publish the corpus feed in arrival order: messages before ``start`` at once, then paced
    at ``speed`` x site time, up to ``until``. Returns how many were published.

    It also answers commands to robots (``actions.Simulator``): a paused AMR reports ``PAUSED``,
    one sent to a zone drives there."""
    simulator = Simulator()
    client = _client("neptune-publish")
    client.on_connect = lambda c, *_: [c.subscribe(t, qos=QOS) for t in simulator.TOPICS]
    client.on_message = lambda _c, _u, m: simulator.receive(m.topic, m.payload)
    _connect(client, broker)
    client.loop_start()
    count = 0
    stream = (m for m in messages(Path(site_dir)) if until is None or m.t <= until)
    info = None
    try:
        for message in _paced(stream, speed, start, sleep):
            message = simulator.apply(message)
            info = client.publish(message.topic, _envelope(message), qos=QOS)
            count += 1
        if info is not None:
            info.wait_for_publish()
    finally:
        client.loop_stop()
        client.disconnect()
    return count


def _paced(
    stream: Iterator[FeedMessage], speed: float, start: str | None, sleep: Callable[[float], None]
) -> Iterator[FeedMessage]:
    later: list[FeedMessage] = []
    for message in stream:
        if start is not None and message.t < start:
            yield message
        else:
            later.append(message)
            break
    yield from pace((m for part in (later, stream) for m in part), speed, sleep=sleep)


# --- live -------------------------------------------------------------------------------------


class Recorder:
    """Appends received envelopes to ``feed/live/segment-NNNNNN.jsonl``, continuing an earlier
    recording; each becomes the ``FeedMessage`` that cites its own line."""

    def __init__(self, site_dir: Path) -> None:
        self.site_dir = Path(site_dir)
        self.root = self.site_dir / LIVE_DIR
        self.root.mkdir(parents=True, exist_ok=True)
        self.count = sum(1 for _ in captured(self.site_dir))

    def record(self, topic: str, payload: bytes) -> FeedMessage | None:
        """Record one MQTT message; ``None`` (and nothing recorded) if it is not an envelope."""
        try:
            env = json.loads(payload)
            t, source, body = env["t"], env["source"], env["payload"]
        except (ValueError, KeyError, TypeError):
            sys.stderr.write(f"live: skipped a message on {topic}: not a feed envelope\n")
            return None
        env = {"t": t, "source": source, "topic": topic, "payload": body}
        line = json.dumps(env, ensure_ascii=False, separators=(",", ":"))
        self.count += 1
        segment = _segment(self.root, self.count)
        with segment.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        rel = segment.relative_to(self.site_dir).as_posix()
        number = (self.count - 1) % SEGMENT_SIZE + 1
        return FeedMessage(t, source, topic, body, rel, number)


def _segment(root: Path, n: int) -> Path:
    return root / f"segment-{(n - 1) // SEGMENT_SIZE + 1:06d}.jsonl"


def captured(site_dir: Path) -> Iterator[FeedMessage]:
    """Every recorded message in arrival order (file, then line), citing where it was recorded."""
    site_dir = Path(site_dir)
    for path in sorted((site_dir / LIVE_DIR).glob("segment-*.jsonl")):
        rel = path.relative_to(site_dir).as_posix()
        with path.open(encoding="utf-8") as handle:
            for number, text in enumerate(handle, start=1):
                env = json.loads(text)
                yield FeedMessage(
                    env["t"], env["source"], env["topic"], env["payload"], rel, number
                )


def _static_files(site_dir: Path) -> dict[str, tuple[int, int]]:
    root = Path(site_dir) / "static"
    return {
        p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def live(
    site_dir: Path,
    broker: str = DEFAULT_BROKER,
    *,
    poll: float = 1.0,
    stop: threading.Event | None = None,
    seam: Seam | None = None,
    say: Callable[[str], object] = lambda line: print(line, flush=True),  # noqa: T201
) -> int:
    """Subscribe, then keep the site's log current until ``stop`` is set (or Ctrl-C).

    The log starts fresh: ``static/``, then any earlier recording, then each message as it
    arrives. Returns how many messages arrived."""
    site_dir = Path(site_dir)
    stop = stop or threading.Event()
    inbox: queue.Queue[tuple[str, bytes]] = queue.Queue()
    subscribed = threading.Event()
    ready = ready_path(site_dir)
    ready.unlink(missing_ok=True)

    client = _client(f"neptune-live-{site_dir.resolve().name}")
    client.on_connect = lambda c, *_: c.subscribe(SUBSCRIBE, qos=QOS)
    client.on_subscribe = lambda *_: subscribed.set()
    client.on_message = lambda _c, _u, m: inbox.put((m.topic, m.payload))
    _connect(client, broker)
    client.loop_start()
    arrived = 0
    try:
        if not subscribed.wait(10):
            raise TimeoutError(f"no subscription on {broker}")
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.touch()
        say(f"live: subscribed to {SUBSCRIBE} on {broker}; recording to {site_dir / LIVE_DIR}")
        with Appender(site_dir, seam) as log:
            log.static()
            for earlier in captured(site_dir):  # an earlier run's recording comes first
                log.message(earlier)
            recorder = Recorder(site_dir)
            watcher = Watcher(site_dir)
            files = _static_files(site_dir)
            say(f"live: static ingested; log at seq {log.log.seq}")
            next_poll = time.monotonic() + poll
            while not stop.is_set():
                try:
                    topic, payload = inbox.get(timeout=poll)
                except queue.Empty:
                    pass
                else:
                    message = None if is_command(topic) else recorder.record(topic, payload)
                    if message is not None:
                        log.message(message)
                        arrived += 1
                for event in watcher.step(log.log):  # actions: what was proposed, approved, done
                    say(f"live: action {event}")
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + poll
                    now = _static_files(site_dir)
                    if now != files:
                        changed = sorted(
                            k for k in now.keys() | files.keys() if now.get(k) != files.get(k)
                        )
                        files = now
                        added = log.restatic()
                        say(f"live: static changed ({', '.join(changed)}): {added} new entries")
    except KeyboardInterrupt:
        pass
    finally:
        ready.unlink(missing_ok=True)
        client.loop_stop()
        client.disconnect()
    return arrived


# --- broker -----------------------------------------------------------------------------------


def broker(
    url: str = DEFAULT_BROKER,
    *,
    stop: threading.Event | None = None,
    started: threading.Event | None = None,
) -> None:
    """Run an amqtt broker on ``url`` until ``stop`` is set (or Ctrl-C). Anonymous, no TLS."""
    asyncio.run(_serve(url, stop or threading.Event(), started))


async def _serve(url: str, stop: threading.Event, started: threading.Event | None) -> None:
    from amqtt.broker import Broker

    host, port = address(url)
    server = Broker(
        {
            "listeners": {"default": {"type": "tcp", "bind": f"{host}:{port}"}},
            "plugins": {
                "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}
            },
        }
    )
    await server.start()
    if started is not None:
        started.set()
    try:
        while not stop.is_set():
            await asyncio.sleep(0.1)
    finally:
        await server.shutdown()
