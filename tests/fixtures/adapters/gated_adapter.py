"""The text reference adapter, whose ``plan`` waits at a gate: a job that holds still on cue.

For tests of what happens while a job runs (a timeout, a cancellation, a destination that
appears under it). ``reached`` is set when a ``plan`` call arrives at the gate; the call goes on
once ``gate`` is set, or after ``timeout`` seconds so a broken test cannot hang. The gate is a
``threading.Event`` of the job's own process, so the job must run adapters in process
(``Isolation.IN_PROCESS``). Output is the text adapter's, unchanged.
"""

import threading

from neptune.adapters.contract import (
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeResult,
    SourceReader,
)
from neptune.adapters.text import TextAdapter


class GatedAdapter:
    """``TextAdapter`` with a gate before every ``plan``."""

    def __init__(self, gate: threading.Event, *, timeout: float = 30.0) -> None:
        self._text = TextAdapter()
        self.descriptor: AdapterDescriptor = self._text.descriptor
        self.gate = gate
        self.reached = threading.Event()
        self._timeout = timeout

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        return self._text.probe(head, hints)

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return self._text.inspect(source, config)

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        self.reached.set()
        self.gate.wait(self._timeout)
        return self._text.plan(source, config)

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        return self._text.ingest(source, chunk, config)
