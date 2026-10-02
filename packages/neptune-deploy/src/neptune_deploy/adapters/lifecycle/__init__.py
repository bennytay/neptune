"""Deployment lifecycle records: the registered adapter, reading no format yet (ADR 0001).

The adapter that commissioning sheets, authorisation envelopes, intervention logs, CMMS work
orders, incident tickets, change records and risk registers will be read through, whether they
come from an arm cell or an AMR fleet. It declares the eight lifecycle kinds of the compiler's
model (root ADR 0051) as what it may emit, and today reads no format:

- ``probe`` never claims a source (confidence 0), so installing Deploy changes no selection;
- ``inspect`` reports the source's size and nothing else;
- ``plan`` is one chunk; ``ingest`` emits no record and one ``deploy_lifecycle.not_read`` finding
  citing the whole source, so a job told to use it (a manifest naming it) says why nothing came out.

It is the plugin boundary under test: entry-point registration and the compiler's conformance
check run against it in CI. Formats land as their issues do, each emitting ``stated`` records only.
"""

from typing import Final

from neptune.adapters.contract import (
    ABI_VERSION,
    AdapterConfig,
    AdapterDescriptor,
    Chunk,
    ChunkOutput,
    Documented,
    FormatSpec,
    InspectResult,
    Plan,
    ProbeHints,
    ProbeReason,
    ProbeResult,
    Resources,
    SourceReader,
    make_chunk,
)
from neptune.identity.findings import ingest_finding
from neptune.model.finding import FindingCategory, Severity
from neptune.model.lifecycle import LIFECYCLE_KINDS
from neptune.model.provenance import ByteRange, EvidenceRef

ADAPTER_ID: Final = "deploy_lifecycle"
NOT_READ: Final = f"{ADAPTER_ID}.not_read"
NO_READER: Final = f"{ADAPTER_ID}.no_reader"

DESCRIPTOR: Final = AdapterDescriptor(
    id=ADAPTER_ID,
    version="0.1.0",
    abi=ABI_VERSION,
    summary="Deployment lifecycle records as stated; registered, and reading no format yet.",
    formats=(FormatSpec("Deployment lifecycle records"),),
    record_kinds=tuple(sorted(kind.kind for kind in LIFECYCLE_KINDS)),
    config=(),
    libraries=(),
    finding_codes=(
        Documented(
            NOT_READ,
            "no lifecycle format is read yet; the source has no record (unsupported, error)",
        ),
    ),
    locator_steps=(),
    conventions=(
        Documented("chunks", "one chunk per source, with an empty context"),
        Documented("probe", "never claims a source: confidence 0 for every head"),
    ),
    resources=Resources(max_memory=1024 * 1024, streaming=True),
    security=(
        "reads no source bytes: probe, inspect, plan and ingest decode nothing",
        "no network, filesystem, subprocess or environment access",
    ),
)


class LifecycleAdapter:
    """The ``deploy_lifecycle`` adapter: registered, claiming nothing, reading nothing."""

    descriptor: Final = DESCRIPTOR

    def probe(self, head: bytes, hints: ProbeHints) -> ProbeResult:
        return ProbeResult(0.0, (ProbeReason(NO_READER, "no lifecycle format is read yet"),))

    def inspect(self, source: SourceReader, config: AdapterConfig) -> InspectResult:
        return InspectResult({"size": source.size})

    def plan(self, source: SourceReader, config: AdapterConfig) -> Plan:
        return Plan((make_chunk(source, config, {}, 0),))

    def ingest(self, source: SourceReader, chunk: Chunk, config: AdapterConfig) -> ChunkOutput:
        finding = ingest_finding(
            code=NOT_READ,
            category=FindingCategory.UNSUPPORTED,
            severity=Severity.ERROR,
            subject=EvidenceRef(source.content_id, (ByteRange(0, source.size),)),
            transform=config.transform,
            message="no deployment lifecycle format is read yet; the source has no record",
        )
        return ChunkOutput(findings=(finding,))
