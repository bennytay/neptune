"""The ``query-packet`` contract's owner module (``contracts/query-packet``, ADR 0006 §1).

What other packages may rely on from Context, and nothing else: the query language (ADR 0002)
and the context packet (ADR 0003) as one registry contract. ``contract_schema`` is the registry
export: one JSON Schema that embeds the query schema and the packet schema verbatim, each as its
own resource (``#/$defs/Query``, ``#/$defs/ContextPacket``). ``QUERY_PACKET_VERSION`` is the
registry major; it rises whenever ``QUERY_VERSION`` or ``PACKET_VERSION`` does.

Readers and checks a consumer needs: the query's canonical bytes, id and strict reader; the
packet's strict reader, canonical bytes and conformance checks; ``answer_problems`` for a caller
that holds both a query and the packet answering it. The SDK (``neptune_context.sdk``) is a
library over this contract, versioned with the package, and is deliberately not re-exported here
(ADR 0006 §6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from neptune_context.answer import answer_problems
from neptune_context.packets.codec import canonical_bytes as packet_canonical_bytes
from neptune_context.packets.codec import decode as decode_packet
from neptune_context.packets.conformance import check as check_packet
from neptune_context.packets.model import PACKET_VERSION, ContextPacket
from neptune_context.packets.schema import packet_schema
from neptune_context.query import QUERY_ID_PATTERN, QUERY_VERSION, Query, canonical_bytes, query_id
from neptune_context.query.decode import loads
from neptune_context.query.schema import query_schema

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonObject

# The registry major of ``query-packet``. Raise it, and the halves' own version, for any change an
# older reader would misread (ADR 0002 §9, ADR 0003 §9).
QUERY_PACKET_VERSION: Final = 1
SCHEMA_ID: Final = f"urn:neptune:schema:query-packet:{QUERY_PACKET_VERSION}"
DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"


def contract_schema() -> JsonObject:
    """The registry export: both halves' schemas, each embedded whole under its own ``$id``."""
    return {
        "$defs": {"ContextPacket": packet_schema(), "Query": query_schema()},
        "$id": SCHEMA_ID,
        "$schema": DIALECT,
        "anyOf": [{"$ref": "#/$defs/Query"}, {"$ref": "#/$defs/ContextPacket"}],
        "description": (
            "Neptune Context's query-packet contract: a query document (#/$defs/Query, "
            "neptune-context ADR 0002) or a context packet (#/$defs/ContextPacket, ADR 0003). "
            "Each half is its own schema resource, exported verbatim; the Python readers "
            "(neptune_context.contract.loads and decode_packet) are stricter."
        ),
        "title": (
            f"Neptune query-packet {QUERY_PACKET_VERSION} "
            f"(query_version {QUERY_VERSION}, packet_version {PACKET_VERSION})"
        ),
    }


__all__ = [
    "PACKET_VERSION",
    "QUERY_ID_PATTERN",
    "QUERY_PACKET_VERSION",
    "QUERY_VERSION",
    "ContextPacket",
    "Query",
    "answer_problems",
    "canonical_bytes",
    "check_packet",
    "contract_schema",
    "decode_packet",
    "loads",
    "packet_canonical_bytes",
    "packet_schema",
    "query_id",
    "query_schema",
]
