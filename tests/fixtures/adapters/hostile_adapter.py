"""A test-only adapter whose format tells it how to attack its host: the sandbox's fixture.

The sandbox (MVL-10, ADR 0030) promises that an adapter call that segfaults, hangs, spins,
allocates without bound, dies, or reaches for the network, a process or the filesystem ends as a
finding about one source, and leaves nothing behind. Those attacks need a real adapter over real
bytes; this one reads a format whose lines ask for them, so a test writes the attack it wants
into a file. Never run it in process: ``kill-parent`` would end the test run.

The format, ``hostile`` (ASCII only)::

    HOSTILE1\\n             the signature
    <line>\\n               one block per line

Chunk 0 emits the ``DocumentRecord``; every other chunk holds one line and emits its
``DocumentBlock`` citing the line's span, unless the line is one of these attacks on ``ingest``:

- ``segfault``: reads address 0; ``abort``: ``abort()``; ``die``: SIGKILLs itself;
  ``exit``: ``_exit(7)`` without a reply; ``quit``: raises ``SystemExit``;
- ``hang``: sleeps for an hour; ``spin``: burns CPU forever; ``hog``: allocates 8 MiB at a time
  forever;
- ``socket``: opens a socket; ``fork``: forks; ``exec``: runs ``/bin/true``;
  ``kill-parent``: signals the job's process; ``write <path>``: writes ``<path>``;
- ``setown``: aims a pipe's SIGIO at the job (``fcntl`` F_SETOWN); ``fioasync``: turns a pipe's
  async signal on (``ioctl`` FIOASYNC) — the signal path only Landlock ABI 6 scopes;
  ``ttyasync <path>``: opens the terminal ``<path>`` read-only and turns ``O_ASYNC`` on with
  ``fcntl`` F_SETFL, which aims SIGIO at the terminal's foreground process group.

A first line ``plan-hang`` or ``plan-segfault`` attacks ``plan`` instead, and a line
``probe-segfault`` anywhere in the head makes ``probe`` segfault.
"""

import ctypes
import fcntl
import os
import signal
import socket
import struct
import termios
import time
from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    SIGNATURE,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    Documented,
    FormatSpec,
    InspectResult,
    Magic,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
)
from neptune.identity.provenance import evidence_record_id
from neptune.model.jsonvalue import JsonObject
from neptune.model.knowledge import AssertionKind, Knowledge, Known, NotApplicable, NotCovered
from neptune.model.provenance import ByteRange, EvidenceRef, Provenance, Span
from neptune.model.world import DocumentBlock, DocumentRecord

MAGIC: Final = b"HOSTILE1\n"

DESCRIPTOR: Final = AdapterDescriptor(
    id="hostile",
    version="1.0.0",
    abi=ABI_VERSION,
    summary="A toy text format for sandbox tests whose lines ask the adapter to attack its host.",
    formats=(FormatSpec("Hostile", extensions=(".hostile",), magic=(Magic(0, MAGIC),)),),
    record_kinds=("document_block", "document_record"),
    config=(),
    libraries=(),
    finding_codes=(),
    locator_steps=(),
    conventions=(
        Documented("attacks", "lines name an attack on the host instead of a block"),
        Documented("blocks", "one block per line after the signature, citing the line's span"),
    ),
    resources=Resources(max_memory=1024 * 1024, streaming=False),
    security=("Test-only. Attacks its host on purpose; run it only in the sandbox.",),
)


def hostile(*lines: str) -> bytes:
    """A hostile file holding ``lines``."""
    return MAGIC + b"".join(line.encode("ascii") + b"\n" for line in lines)


def _lines(source: SourceReader) -> list[tuple[int, bytes]]:
    data = source.read(len(MAGIC), source.size)
    found, offset = [], len(MAGIC)
    for line in data.split(b"\n")[:-1]:
        found.append((offset, line))
        offset += len(line) + 1
    return found


def _int(context: JsonObject, key: str) -> int:
    value = context[key]
    assert isinstance(value, int)
    return value


def _segfault() -> None:
    ctypes.string_at(0)


def _spin() -> None:
    while True:
        pass


def _hog() -> None:
    held = []
    while True:
        held.append(bytearray(8 * 1024 * 1024))


def attack(text: str) -> str:
    """Carry out the attack ``text`` names; return the text a block holds if it is none."""
    if text == "segfault":
        _segfault()
    elif text == "abort":
        os.abort()
    elif text == "die":
        os.kill(os.getpid(), signal.SIGKILL)
    elif text == "exit":
        os._exit(7)
    elif text == "quit":
        raise SystemExit(0)
    elif text == "hang":
        time.sleep(3600)
    elif text == "spin":
        _spin()
    elif text == "hog":
        _hog()
    elif text == "socket":
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
    elif text == "fork":
        if os.fork() == 0:
            os._exit(0)
    elif text == "exec":
        os.execv("/bin/true", ["true"])
    elif text == "kill-parent":
        os.kill(os.getppid(), signal.SIGTERM)
    elif text == "setown":
        read_fd, write_fd = os.pipe()  # a pipe, not a socket: socket() is already denied
        try:
            fcntl.fcntl(write_fd, fcntl.F_SETOWN, os.getppid())
        finally:
            os.close(read_fd)
            os.close(write_fd)
    elif text == "fioasync":
        read_fd, write_fd = os.pipe()
        try:
            fcntl.ioctl(write_fd, termios.FIOASYNC, struct.pack("i", 1))
        finally:
            os.close(read_fd)
            os.close(write_fd)
    elif text.startswith("ttyasync "):
        terminal = os.open(text.removeprefix("ttyasync "), os.O_RDONLY)  # reads stay open
        try:
            flags = fcntl.fcntl(terminal, fcntl.F_GETFL)
            fcntl.fcntl(terminal, fcntl.F_SETFL, flags | os.O_ASYNC)
        finally:
            os.close(terminal)
    elif text.startswith("write "):
        with open(text.removeprefix("write "), "wb") as target:  # noqa: PTH123 - the attack
            target.write(b"escaped")
    return text


class HostileAdapter:
    descriptor = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        if not head.startswith(MAGIC):
            return ProbeResult(0.0, ())
        if b"\nprobe-segfault\n" in head:
            _segfault()
        return ProbeResult(SIGNATURE, (ProbeReason("hostile.magic", "starts HOSTILE1"),), "1")

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return InspectResult({"size": source.size})

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        lines = _lines(source)
        if lines and lines[0][1] in (b"plan-hang", b"plan-segfault"):
            attack(lines[0][1].decode("ascii").removeprefix("plan-"))
        chunks = [make_chunk(source, config, {"part": "document"}, source.size)]
        for order, (offset, line) in enumerate(lines):
            context: JsonObject = {"end": offset + len(line), "order": order, "start": offset}
            chunks.append(make_chunk(source, config, context, len(line)))
        return Plan(tuple(chunks))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        evidence = EvidenceRef(source.content_id, (ByteRange(0, source.size),))
        document = evidence_record_id(DocumentRecord.kind, evidence, config.transform)
        if chunk.context.get("part") == "document":
            title: Knowledge[str] = NotCovered()
            provenance = Provenance(evidence, config.transform.id, AssertionKind.OBSERVED)
            return ChunkOutput(records=(DocumentRecord(document, provenance, "text", title, ()),))
        start, end, order = (_int(chunk.context, key) for key in ("start", "end", "order"))
        text = attack(source.read(start, end - start).decode("ascii"))
        span = EvidenceRef(source.content_id, (Span(start, end),))
        block = DocumentBlock(
            id=evidence_record_id(DocumentBlock.kind, span, config.transform),
            provenance=Provenance(span, config.transform.id, AssertionKind.OBSERVED),
            document=document,
            order=order,
            role=NotCovered(),
            level=NotCovered(),
            text=Known(text),
            region=NotApplicable(),
        )
        return ChunkOutput(records=(block,))
