"""Identifier types for the three identity tiers (ADR 0003, rendering in ADR 0009).

Tier-1 and tier-2 ids are plain ``str`` at runtime so they drop straight into canonical JSON. The
``NewType`` wrappers keep the tiers apart for the type checker. Obtain them from the ``parse_*``
functions below or from the derivation functions in ``neptune.identity``, never by casting.
"""

import re
from dataclasses import dataclass
from typing import NewType

from neptune.model.jsonvalue import JsonObject

# Tier 1: sha256 of a source's complete bytes, e.g. "sha256:9f86d0…". Also used for chunk hashes.
ContentId = NewType("ContentId", str)
# sha256 of the canonical JSON of a resolved adapter config. Same rendering as a content id; a
# separate type so it cannot be passed where a source id is expected.
ConfigHash = NewType("ConfigHash", str)
# Tier 2: one canonical record in one lineage, e.g. "rec:sha256:3a7b…".
RecordId = NewType("RecordId", str)

_SHA256 = "sha256:[0-9a-f]{64}"
_CONTENT_ID = re.compile(_SHA256)
_RECORD_ID = re.compile(f"rec:{_SHA256}")
# Namespaces, connector ids, record kinds and adapter ids: short lowercase machine tokens.
_TOKEN = re.compile(r"[a-z][a-z0-9_.\-]*")


def parse_content_id(text: str) -> ContentId:
    if not _CONTENT_ID.fullmatch(text):
        raise ValueError(f"not a content id (want 'sha256:<64 lowercase hex>'): {text!r}")
    return ContentId(text)


def parse_config_hash(text: str) -> ConfigHash:
    return ConfigHash(parse_content_id(text))


def parse_record_id(text: str) -> RecordId:
    if not _RECORD_ID.fullmatch(text):
        raise ValueError(f"not a record id (want 'rec:sha256:<64 lowercase hex>'): {text!r}")
    return RecordId(text)


def check_token(field: str, value: str) -> str:
    """Validate a machine token: lowercase ASCII letter, then ``[a-z0-9_.-]``."""
    if not _TOKEN.fullmatch(value):
        raise ValueError(f"{field} must match {_TOKEN.pattern}: {value!r}")
    return value


def check_text(field: str, value: str) -> str:
    """Validate free text that must be representable in canonical JSON: non-empty, valid Unicode."""
    if not value:
        raise ValueError(f"{field} must be non-empty")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} is not valid Unicode (lone surrogate): {value!r}") from exc
    return value


@dataclass(frozen=True)
class LogicalId:
    """Tier 3: a real-world thing across sources, e.g. ``LogicalId("serial", "SPOT-1234")``.

    Declared, never inferred (ADR 0003): build one only from a value a source *states*, a manifest
    entry, or an explicit alias. Equal content never produces or merges logical ids.
    """

    namespace: str
    value: str

    def __post_init__(self) -> None:
        check_token("namespace", self.namespace)
        check_text("value", self.value)

    def to_json(self) -> JsonObject:
        return {"namespace": self.namespace, "value": self.value}


@dataclass(frozen=True)
class ExternalObjectRef:
    """An object in an external store: ``(connector id, external object id, revision token)``.

    Identifies the object before its bytes are fetched. The revision token is the store's etag,
    version id or generation. Once bytes are fetched the content id is the identity (ADR 0003).
    """

    connector_id: str
    object_id: str
    revision_token: str

    def __post_init__(self) -> None:
        check_token("connector_id", self.connector_id)
        check_text("object_id", self.object_id)
        check_text("revision_token", self.revision_token)

    @property
    def key(self) -> tuple[str, ...]:
        """The location, without the revision: every revision of one object shares this key."""
        return ("external", self.connector_id, self.object_id)

    def to_json(self) -> JsonObject:
        return {
            "connector_id": self.connector_id,
            "kind": "external",
            "object_id": self.object_id,
            "revision_token": self.revision_token,
        }
