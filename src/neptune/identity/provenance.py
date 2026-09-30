"""Building and verifying transform records, and tier-2 ids from provenance (ADR 0003, ADR 0016)."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Final

from neptune.identity.ids import adapter_record_id, config_hash, record_id
from neptune.model.ids import ContentId, RecordId
from neptune.model.jsonvalue import JsonValue
from neptune.model.provenance import EvidenceRef, TransformRecord

TRANSFORM_RECORD_KIND: Final = "transform_record"


def transform_record(
    *,
    adapter_id: str,
    adapter_version: str,
    config: Mapping[str, JsonValue],
    libraries: Mapping[str, str] | None = None,
    upstream: Sequence[RecordId] = (),
) -> TransformRecord:
    """The transform record for a producer at one version and resolved config.

    ``config`` must already be resolved (defaults filled in). ``upstream`` is empty for an adapter
    reading source bytes, and otherwise names the transforms whose output this one consumes.
    """
    # The id covers every field but itself, so derive it from a draft holding a placeholder.
    draft = TransformRecord(
        id=RecordId("rec:sha256:" + "0" * 64),
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        config_hash=config_hash(config),
        config=config,
        libraries=tuple(sorted((libraries or {}).items())),
        upstream=tuple(upstream),
    )
    return replace(draft, id=transform_record_id(draft))


def transform_record_id(record: TransformRecord) -> RecordId:
    """The id is the record id over its canonical JSON, less the id itself (ADR 0006 §4)."""
    return record_id(TRANSFORM_RECORD_KIND, record.content_json())


def check_transform_record(record: TransformRecord) -> TransformRecord:
    """Verify a record read from a store: its config hash and its id must both recompute."""
    if config_hash(record.config) != record.config_hash:
        raise ValueError(f"transform {record.id}: config does not hash to {record.config_hash}")
    if transform_record_id(record) != record.id:
        raise ValueError(f"transform {record.id}: id does not match its content")
    return record


def evidence_record_id(kind: str, evidence: EvidenceRef, transform: TransformRecord) -> RecordId:
    """The tier-2 id of a record of ``kind`` that ``transform`` produced from ``evidence``.

    For an adapter this is exactly ADR 0003's formula. For a transform with upstream transforms
    (a normaliser), the upstream ids are added, so the same normaliser over another adapter
    version's output gets new ids too: lineage stays scoped through the whole chain.
    """
    if not isinstance(evidence.source, str):
        raise ValueError("tier-2 ids need fetched bytes: the evidence source is not a content id")
    source = ContentId(evidence.source)
    if not transform.upstream:
        return adapter_record_id(
            kind=kind,
            source=source,
            locator=evidence.locator_json(),
            adapter_id=transform.adapter_id,
            adapter_version=transform.adapter_version,
            config=transform.config_hash,
        )
    return record_id(
        kind,
        {
            "adapter_id": transform.adapter_id,
            "adapter_version": transform.adapter_version,
            "config_hash": transform.config_hash,
            "locator": evidence.locator_json(),
            "source": source,
            "upstream": list(transform.upstream),
        },
    )
