"""The adapter contract as one reusable check, for adapters that live outside the compiler.

``check_conformance(adapter, samples)`` raises ``ContractError`` at the first broken law (ADR 0008
§4, ADR 0024 §6). It composes what the compiler's own adapter tests already run, and adds nothing
to the contract:

- the registry admits the adapter: a descriptor, the four methods, this ``ABI_VERSION``;
- ``probe`` is given exactly the bounded head, returns a ``ProbeResult`` whose reasons carry the
  adapter's id, and answers the same way twice;
- ``inspect`` returns an ``InspectResult``, the same twice, whose findings pass the finding checks;
- ``ingest_source`` (every check in ``neptune.adapters.check``) passes, and a second run gives
  byte-identical records, findings and series.

Each runs over the given samples and over hostile inputs derived from them: an empty source and
each sample cut in half. An exception from the adapter on any of them is a broken law ("findings,
not exceptions"), reported as a ``ContractError`` naming the input.

Workspace members that register adapters through the compiler's entry points call it from their
tests. Nothing in the runtime calls it.
"""

from collections.abc import Iterable, Mapping
from typing import Final

from neptune.adapters.check import check_finding
from neptune.adapters.contract import (
    PROBE_HEAD_SIZE,
    Adapter,
    ContractError,
    InspectResult,
    ProbeHints,
    configure,
)
from neptune.adapters.harness import SourceOutput, ingest_source
from neptune.adapters.registry import AdapterRegistry
from neptune.discovery.reader import BytesReader
from neptune.identity import canonical_json
from neptune.model.jsonvalue import JsonValue

# The name ``probe`` is hinted with: names are advisory, so conformance never relies on one.
_NO_NAME: Final = ""


def conformance_inputs(samples: Iterable[bytes]) -> tuple[bytes, ...]:
    """The empty source, each sample, then each sample cut in half; first occurrence kept."""
    given = tuple(samples)
    for sample in given:
        if not isinstance(sample, bytes):
            raise TypeError(f"a sample is bytes, got {type(sample).__name__}")
    ordered = (b"", *given, *(sample[: len(sample) // 2] for sample in given))
    return tuple(dict.fromkeys(ordered))


def _output_bytes(output: SourceOutput) -> bytes:
    """Everything a run produced, as bytes: records and findings as canonical JSON, then series."""
    records: list[JsonValue] = [record.to_json() for record in output.package_records()]
    chunks: list[JsonValue] = [chunk.to_json() for chunk in output.plan.chunks]
    document = canonical_json.dumps({"chunks": chunks, "records": records})
    return document + repr(output.series()).encode("utf-8")


def _check_one(adapter: Adapter, data: bytes, values: Mapping[str, JsonValue] | None) -> None:
    descriptor = adapter.descriptor
    registry = AdapterRegistry([adapter])
    hints = ProbeHints(_NO_NAME, len(data))
    head = data[:PROBE_HEAD_SIZE]
    first, second = registry.probe(head, hints), registry.probe(head, hints)
    if first != second:
        raise ContractError(f"adapter {descriptor.id}: probe answered two ways for one head")
    for reason in first[0].result.reasons:
        if reason.code.partition(".")[0] != descriptor.id:
            raise ContractError(f"adapter {descriptor.id}: probe reason {reason.code!r}")

    source = BytesReader(data)
    config = configure(descriptor, values)
    inspected = adapter.inspect(source, config)
    if not isinstance(inspected, InspectResult):
        raise ContractError(f"adapter {descriptor.id}: inspect returned {inspected!r}")
    if adapter.inspect(source, config) != inspected:
        raise ContractError(f"adapter {descriptor.id}: inspect answered two ways for one source")
    for finding in inspected.findings:
        check_finding(descriptor, source, config, finding)

    once = _output_bytes(ingest_source(adapter, source, values))
    if _output_bytes(ingest_source(adapter, source, values)) != once:
        raise ContractError(f"adapter {descriptor.id}: two runs over one source differ")


def check_conformance(
    adapter: Adapter,
    samples: Iterable[bytes] = (),
    values: Mapping[str, JsonValue] | None = None,
) -> None:
    """Check ``adapter`` against the contract over ``samples`` and hostile inputs from them.

    ``values`` is the config every run uses (defaults when omitted). Raises ``ContractError``.
    """
    AdapterRegistry([adapter])  # refuses a non-adapter or another ABI before anything runs
    for data in conformance_inputs(samples):
        try:
            _check_one(adapter, data, values)
        except ContractError as exc:
            raise ContractError(f"{exc} (input of {len(data)} bytes)") from exc
        except Exception as exc:
            raise ContractError(
                f"adapter {adapter.descriptor.id} raised {exc!r} on an input of {len(data)}"
                " bytes; problems are findings, not exceptions"
            ) from exc
