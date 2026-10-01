"""One adapter over one source, in process: configure, plan, ingest every chunk, check.

This is the contract end to end with nothing else in the way: no store, no resume, no sandbox.
Adapter tests use it, so every test runs the contract's checks; the runtime (MVL-6) adds
persistence, isolation and resume around the same four calls. An adapter that raises is a bug,
and here the exception propagates. So does a ``ShortReadError``: it is the source's fault, not
the adapter's, and the runtime records it as ``neptune.discovery.short_read`` and goes on to the
next source (``neptune.discovery.verify.short_read_finding``).
"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from neptune.adapters.check import check_plan, check_source_output
from neptune.adapters.contract import (
    Adapter,
    AdapterConfig,
    ChunkOutput,
    Plan,
    SourceReader,
    configure,
)
from neptune.model.finding import IngestFinding
from neptune.model.ids import RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.series import SeriesBatch


@dataclass(frozen=True)
class SourceOutput:
    """Everything one adapter produced from one source, checked against the contract."""

    config: AdapterConfig
    plan: Plan
    outputs: tuple[ChunkOutput, ...]

    def records(self) -> tuple[Any, ...]:
        """The evidence records, sorted by id."""
        return tuple(
            sorted((r for o in self.outputs for r in o.records), key=lambda record: record.id)
        )

    def findings(self) -> tuple[IngestFinding, ...]:
        """The plan's and every chunk's findings, sorted by id."""
        found = [*self.plan.findings, *(f for o in self.outputs for f in o.findings)]
        return tuple(sorted(found, key=lambda finding: finding.id))

    def series(self) -> dict[RecordId, tuple[SeriesBatch, ...]]:
        """Each stream's batches, in chunk order."""
        batches: dict[RecordId, list[SeriesBatch]] = defaultdict(list)
        for output in self.outputs:
            for batch in output.series:
                batches[batch.stream].append(batch)
        return {stream: tuple(found) for stream, found in sorted(batches.items())}

    def package_records(self) -> tuple[Any, ...]:
        """What this output adds to a package: the transform, the records and the findings."""
        return (self.config.transform, *self.records(), *self.findings())


def ingest_source(
    adapter: Adapter, source: SourceReader, values: Mapping[str, JsonValue] | None = None
) -> SourceOutput:
    """Run ``adapter`` over ``source`` with config ``values``, checking every law on the way."""
    descriptor = adapter.descriptor
    config = configure(descriptor, values)
    plan = adapter.plan(source, config)
    check_plan(descriptor, source, config, plan)
    outputs = tuple(adapter.ingest(source, chunk, config) for chunk in plan.chunks)
    check_source_output(descriptor, source, config, plan, outputs)
    return SourceOutput(config, plan, outputs)
