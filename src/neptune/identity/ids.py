"""Tier-2 record ids and config hashes (ADR 0003, rendering in ADR 0009)."""

import hashlib
from collections.abc import Mapping
from typing import Final

from neptune.identity import canonical_json
from neptune.model.ids import (
    ConfigHash,
    ContentId,
    RecordId,
    check_text,
    check_token,
    parse_config_hash,
    parse_content_id,
)
from neptune.model.jsonvalue import JsonValue

# Hashed into every record id so the derivation can be versioned without ambiguity. Changing it
# re-lineages every record; that needs a new ADR.
RECORD_ID_SCHEME: Final = "neptune.record-id/1"


def config_hash(resolved_config: Mapping[str, JsonValue]) -> ConfigHash:
    """Hash of the *resolved* config: the caller fills in defaults first, so explicit == omitted."""
    return ConfigHash("sha256:" + hashlib.sha256(canonical_json.dumps(resolved_config)).hexdigest())


def record_id(kind: str, inputs: Mapping[str, JsonValue]) -> RecordId:
    """Derive a record id from a record kind and the complete, deterministic set of its inputs.

    Use ``adapter_record_id`` for records an adapter produces from one source. Other record kinds
    (a ``SourceRevision``, a grouped ``Run``) call this directly with their documented inputs.
    """
    check_token("kind", kind)
    payload: dict[str, JsonValue] = {"inputs": inputs, "kind": kind, "scheme": RECORD_ID_SCHEME}
    return RecordId("rec:sha256:" + hashlib.sha256(canonical_json.dumps(payload)).hexdigest())


def adapter_record_id(
    *,
    kind: str,
    source: ContentId,
    locator: JsonValue,
    adapter_id: str,
    adapter_version: str,
    config: ConfigHash,
) -> RecordId:
    """The ADR 0003 tier-2 formula. Lineage-scoped: a new adapter version gives new ids."""
    parse_content_id(source)
    parse_config_hash(config)
    check_token("adapter_id", adapter_id)
    check_text("adapter_version", adapter_version)
    return record_id(
        kind,
        {
            "adapter_id": adapter_id,
            "adapter_version": adapter_version,
            "config_hash": config,
            "locator": locator,
            "source": source,
        },
    )
