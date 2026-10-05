"""Query language (ADR 0002): a typed ``Query``, its canonical JSON and id, and its refusals.

A ``Query`` says what to retrieve, on which snapshot (``as_of``), world-time interval
(``during``), frame and site; never how. ``decode.loads`` reads untrusted JSON into a ``Query`` or
a ``Refused`` with structured findings; ``codec.canonical_bytes`` and ``codec.query_id`` give the
bytes and the ``query:sha256:<hex>`` id a context packet keys on. Planning onto channels is a later
issue; no channel is privileged, and model-assisted parsing, if it ever exists, is derived.
"""

from neptune_context.query.codec import (
    QUERY_ID_PATTERN,
    QUERY_ID_PREFIX,
    canonical_bytes,
    query_id,
    to_json,
)
from neptune_context.query.decode import accept, from_json, loads
from neptune_context.query.findings import FindingCode, QueryFinding, Refused
from neptune_context.query.model import (
    HEAD,
    QUERY_VERSION,
    AsOf,
    Box,
    Budget,
    Caller,
    CivilTime,
    Clock,
    ClockBridge,
    Diff,
    Direction,
    DomainClock,
    During,
    Explain,
    FrameBridge,
    FrameRef,
    FrameRegion,
    GraphClause,
    Instant,
    Query,
    Shape,
    SiteScope,
    Sphere,
    Subject,
    TextChannel,
    TextClause,
    TextField,
    Why,
    default_include_inferred,
)
from neptune_context.query.validate import validate

__all__ = [
    "HEAD",
    "QUERY_ID_PATTERN",
    "QUERY_ID_PREFIX",
    "QUERY_VERSION",
    "AsOf",
    "Box",
    "Budget",
    "Caller",
    "CivilTime",
    "Clock",
    "ClockBridge",
    "Diff",
    "Direction",
    "DomainClock",
    "During",
    "Explain",
    "FindingCode",
    "FrameBridge",
    "FrameRef",
    "FrameRegion",
    "GraphClause",
    "Instant",
    "Query",
    "QueryFinding",
    "Refused",
    "Shape",
    "SiteScope",
    "Sphere",
    "Subject",
    "TextChannel",
    "TextClause",
    "TextField",
    "Why",
    "accept",
    "canonical_bytes",
    "default_include_inferred",
    "from_json",
    "loads",
    "query_id",
    "to_json",
    "validate",
]
